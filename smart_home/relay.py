from __future__ import annotations
import json
import secrets
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_RELAY_FILE = _CONFIG_DIR / "relays.json"
_DEFAULTS_FILE = _CONFIG_DIR / "relay_defaults.json"

FIRMWARE_DIR = Path(__file__).parent / "relay_firmware"
_APP_BIN = FIRMWARE_DIR / "esp32_relay.ino.bin"
_BOOT_BIN = FIRMWARE_DIR / "esp32_relay.ino.bootloader.bin"
_PART_BIN = FIRMWARE_DIR / "esp32_relay.ino.partitions.bin"


def load_relays() -> list[dict]:
    if _RELAY_FILE.exists():
        try:
            with open(_RELAY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_relays(relays: list[dict]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_RELAY_FILE, "w") as f:
        json.dump(relays, f, indent=2)


def load_defaults() -> dict:
    if _DEFAULTS_FILE.exists():
        try:
            with open(_DEFAULTS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_defaults(defaults: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_DEFAULTS_FILE, "w") as f:
        json.dump(defaults, f, indent=2)


def find_relay_by_token(token: str) -> dict | None:
    for relay in load_relays():
        if relay.get("token") == token:
            return relay
    return None


def generate_token() -> str:
    return secrets.token_hex(24)


def detect_serial_ports() -> list[str]:
    import glob
    candidates = []
    candidates += glob.glob("/dev/ttyUSB*")
    candidates += glob.glob("/dev/ttyACM*")
    candidates += glob.glob("/dev/cu.usbserial*")
    candidates += glob.glob("/dev/cu.SLAB_USBtoUART*")
    candidates += glob.glob("/dev/cu.wchusbserial*")
    return sorted(candidates)


def firmware_missing_message() -> str:
    return (
        f"Firmware binary not found at {_APP_BIN}\n"
        f"Build it first:\n"
        f"  cd {FIRMWARE_DIR}\n"
        f"  ./build.sh"
    )


def flash_and_provision(
    port: str,
    relay_id: str,
    token: str,
    wifi_ssid: str,
    wifi_pass: str,
    server_url: str,
    print_fn=print,
) -> None:
    """Flash ESP32 firmware and send config over serial."""
    import subprocess
    import time
    import serial  # pyserial

    if not _APP_BIN.exists():
        raise FileNotFoundError(firmware_missing_message())

    flash_args = ["-z"]
    if _BOOT_BIN.exists():
        flash_args += ["0x1000", str(_BOOT_BIN)]
    if _PART_BIN.exists():
        flash_args += ["0x8000", str(_PART_BIN)]
    flash_args += ["0x10000", str(_APP_BIN)]

    # Try esptool (newer) then esptool.py (older installs)
    for esptool_cmd in ("esptool", "esptool.py"):
        try:
            subprocess.run(
                [esptool_cmd, "--chip", "esp32", "--port", port, "--baud", "921600",
                 "write-flash"] + flash_args,
                check=True,
            )
            break
        except FileNotFoundError:
            continue
    else:
        raise RuntimeError(
            "esptool not found. Install with: pip install esptool"
        )

    print_fn("Waiting for device to boot...")
    time.sleep(2)

    config_json = json.dumps({
        "ssid": wifi_ssid,
        "pass": wifi_pass,
        "url": server_url.rstrip("/"),
        "token": token,
        "id": relay_id,
    }) + "\n"

    with serial.Serial(port, 115200, timeout=1) as ser:
        # Send RESET_CONFIG immediately to clear any stale config from a
        # previous flash (no-op on a fresh chip where NVS is empty).
        ser.write(b"RESET_CONFIG\n")

        deadline = time.time() + 25
        got_prompt = False
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                print_fn(f"  device: {line}")
            if "WAITING_FOR_CONFIG" in line:
                got_prompt = True
                break

        if not got_prompt:
            raise TimeoutError(
                "Device did not enter provisioning mode within 25 seconds.\n"
                "Check the USB connection and try again."
            )

        ser.write(config_json.encode())

        deadline = time.time() + 10
        confirmed = False
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                print_fn(f"  device: {line}")
            if "CONFIG_SAVED" in line:
                confirmed = True
                break

        if not confirmed:
            raise TimeoutError("Device did not confirm config was saved.")


# ── GATT task coordination (IPC via shared SQLite) ──────────────────────────


def create_gatt_task(conn, address: str, device_type: str, label: str | None, relay_id: str) -> str:
    """Create a pending GATT task for the given relay. Returns the task ID."""
    task_id = generate_token()[:16]
    conn.execute(
        "INSERT INTO gatt_tasks (id, address, device_type, label, relay_id) VALUES (?,?,?,?,?)",
        (task_id, address.upper(), device_type, label, relay_id),
    )
    conn.commit()
    return task_id


def claim_pending_tasks(conn, relay_id: str) -> list[dict]:
    """Atomically claim and return all pending tasks assigned to this relay."""
    rows = conn.execute(
        "SELECT id, address, device_type, label FROM gatt_tasks "
        "WHERE relay_id=? AND status='pending' ORDER BY ts",
        (relay_id,),
    ).fetchall()
    tasks = [dict(r) for r in rows]
    if tasks:
        ids = [t["id"] for t in tasks]
        ph = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE gatt_tasks SET status='claimed', "
            f"updated_ts=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id IN ({ph})",
            ids,
        )
        conn.commit()
    return tasks


def set_task_done(conn, task_id: str, result_hex: str) -> None:
    conn.execute(
        "UPDATE gatt_tasks SET status='done', result_hex=?, "
        "updated_ts=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
        (result_hex, task_id),
    )
    conn.commit()


def set_task_failed(conn, task_id: str, error: str) -> None:
    conn.execute(
        "UPDATE gatt_tasks SET status='failed', error=?, "
        "updated_ts=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
        (error, task_id),
    )
    conn.commit()


def get_settled_tasks(conn) -> list[dict]:
    """Return all tasks with status 'done' or 'failed'."""
    rows = conn.execute(
        "SELECT id, address, device_type, label, relay_id, status, result_hex, error "
        "FROM gatt_tasks WHERE status IN ('done','failed')",
    ).fetchall()
    return [dict(r) for r in rows]


def delete_task(conn, task_id: str) -> None:
    conn.execute("DELETE FROM gatt_tasks WHERE id=?", (task_id,))
    conn.commit()


def expire_stale_tasks(conn, timeout_seconds: int = 120) -> None:
    """Mark pending/claimed tasks that have waited too long as failed."""
    conn.execute(
        "UPDATE gatt_tasks SET status='failed', error='timeout', "
        "updated_ts=strftime('%Y-%m-%d %H:%M:%S','now') "
        "WHERE status IN ('pending','claimed') "
        "AND (julianday('now') - julianday(ts)) * 86400 > ?",
        (timeout_seconds,),
    )
    conn.commit()
