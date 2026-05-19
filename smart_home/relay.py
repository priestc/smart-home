from __future__ import annotations
import json
import secrets
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_RELAY_FILE = _CONFIG_DIR / "relays.json"
_DEFAULTS_FILE = _CONFIG_DIR / "relay_defaults.json"

FIRMWARE_DIR = Path(__file__).parent / "relay_firmware"
_APP_BIN    = FIRMWARE_DIR / "esp32_relay.ino.bin"
_BOOT_BIN   = FIRMWARE_DIR / "esp32_relay.ino.bootloader.bin"
_PART_BIN   = FIRMWARE_DIR / "esp32_relay.ino.partitions.bin"
_INO_SOURCE = FIRMWARE_DIR / "esp32_relay" / "esp32_relay.ino"


def firmware_version() -> tuple[str, int]:
    """Return (version_string, rev_int) of the firmware that would be flashed.
    Reads directly from the .ino source so it's always current."""
    import re
    if not _INO_SOURCE.exists():
        return ("?", 0)
    text = _INO_SOURCE.read_text()
    ver_m = re.search(r'#define FIRMWARE_VERSION\s+"([^"]+)"', text)
    rev_m = re.search(r'#define FIRMWARE_REV\s+(\d+)', text)
    return (ver_m.group(1) if ver_m else "?", int(rev_m.group(1)) if rev_m else 0)




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


def set_relay_firmware_rev(relay_id: str, rev: int | None) -> bool:
    """Update the firmware_rev for a relay in relays.json. Returns False if not found."""
    relays = load_relays()
    for r in relays:
        if r.get("id") == relay_id:
            if rev is not None:
                r["firmware_rev"] = rev
            else:
                r.pop("firmware_rev", None)
            save_relays(relays)
            return True
    return False


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
        f"  bash \"$(smart-home firmware-dir)/setup.sh\"   # if arduino-cli is missing\n"
        f"  bash \"$(smart-home firmware-dir)/build.sh\""
    )


def _resolve_esptool() -> str:
    """Return the esptool command name, raising RuntimeError if not found."""
    import subprocess
    for candidate in ("esptool", "esptool.py"):
        try:
            subprocess.run([candidate, "version"], check=True, capture_output=True)
            return candidate
        except FileNotFoundError:
            continue
    raise RuntimeError("esptool not found. Install with: pip install esptool")


def read_chip_mac(port: str) -> str | None:
    """Read the ESP32 MAC address from a connected device without flashing.

    Returns the MAC as a lowercase colon-separated string (e.g. '30:76:f5:b9:6f:c0'),
    or None if it could not be determined.
    """
    import re
    import subprocess
    try:
        esptool_cmd = _resolve_esptool()
    except RuntimeError:
        return None
    try:
        result = subprocess.run(
            [esptool_cmd, "--chip", "esp32", "--port", port, "chip_id"],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout + result.stderr
        m = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", output)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    return None


def flash_firmware(port: str, print_fn=print) -> None:
    """Write firmware binaries to ESP32. NVS config is preserved (no reset)."""
    import subprocess
    import time

    if not _APP_BIN.exists():
        raise FileNotFoundError(firmware_missing_message())

    flash_args = ["-z"]
    if _BOOT_BIN.exists():
        flash_args += ["0x1000", str(_BOOT_BIN)]
    if _PART_BIN.exists():
        flash_args += ["0x8000", str(_PART_BIN)]
    flash_args += ["0x10000", str(_APP_BIN)]

    esptool_cmd = _resolve_esptool()

    # The chip occasionally fails to respond immediately after a reset (timing issue
    # during boot). Retry the flash up to 3 times with a short delay between attempts.
    flash_cmd = [esptool_cmd, "--chip", "esp32", "--port", port, "--baud", "460800",
                 "write-flash"] + flash_args
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            subprocess.run(flash_cmd, check=True)
            break
        except subprocess.CalledProcessError:
            if attempt < max_attempts:
                print_fn(f"Flash attempt {attempt} failed, retrying in 3 seconds...")
                time.sleep(3)
            else:
                raise


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
    import time
    import serial  # pyserial

    flash_firmware(port, print_fn)

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
        # Discard boot ROM output buffered while we were sleeping.
        # The ESP32 ROM logs at 74880 baud, which looks like garbage at 115200.
        ser.reset_input_buffer()

        ser.write(b"RESET_CONFIG\n")

        deadline = time.time() + 25
        got_prompt = False
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            # Skip lines that are mostly non-printable (garbled baud-rate output)
            printable = sum(c.isprintable() for c in line)
            if line and printable >= len(line) * 0.8:
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
