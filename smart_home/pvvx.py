from __future__ import annotations
import asyncio
import datetime
import json
import struct
import time
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_PVVX_FILE  = _CONFIG_DIR / "pvvx_devices.json"

_PVVX_CHAR      = "00001f1f-0000-1000-8000-00805f9b34fb"  # PVVX control/history characteristic
_CMD_SYNC_TIME  = 0x23
_CMD_GET_MEMO   = 0x33  # request last N history records; second byte = count (max 255)


def load_addresses() -> set[str]:
    """Return the set of MAC addresses known to be running PVVX firmware."""
    if _PVVX_FILE.exists():
        try:
            with open(_PVVX_FILE) as f:
                return set(json.load(f))
        except (json.JSONDecodeError, ValueError):
            pass
    return set()


def mark_address(address: str) -> None:
    """Record a MAC address as having PVVX firmware installed."""
    addresses = load_addresses()
    addresses.add(address.upper())
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_PVVX_FILE, "w") as f:
        json.dump(sorted(addresses), f, indent=2)


async def read_pvvx_history(
    address: str,
    count: int = 255,
    timeout: float = 30.0,
    idle_timeout: float = 5.0,
    verbose: bool = False,
) -> list[dict]:
    """Connect to a PVVX sensor, sync its clock, and read stored history records.

    count: max records to fetch (1-255). Sensor returns newest-first.
    Returns a list of dicts: ts, temp_c, humidity, vbat_mv. Returns [] on error.
    """
    from bleak import BleakClient, BleakError, BleakScanner

    def _log(msg):
        if verbose:
            print(f"  [pvvx] {msg}")

    address = address.upper()
    # Raw records collect (vbat_mv, temp_c, humidity, boot_minutes).
    # Timestamps are reconstructed after all records arrive using relative offsets.
    raw_records: list[tuple[int, float, float, int]] = []  # (vbat_mv, temp_c, humidity, boot_min)
    last_activity: list[float] = [0.0]
    raw_bytes_received: list[int] = [0]

    def handle_notification(sender, data: bytearray):
        last_activity[0] = time.monotonic()
        raw_bytes_received[0] += len(data)
        _log(f"notification {len(data)} bytes: {data.hex()}")
        # The first response is a 2-byte echo of the command — skip it.
        # Records are 14 bytes with the following layout (all little-endian):
        #   0:    uint8  command echo (0x33)
        #   1-2:  uint16 battery voltage in mV
        #   3-4:  int16  temperature * 100 (°C)
        #   5-6:  uint16 humidity * 100 (%)
        #   7-8:  uint16 minutes since device boot (increments by recording interval)
        #   9-13: (other fields, unused)
        if len(data) < 14 or data[0] != _CMD_GET_MEMO:
            return
        vbat_mv  = struct.unpack_from("<H", data, 1)[0]
        raw_temp = struct.unpack_from("<h", data, 3)[0]
        raw_humi = struct.unpack_from("<H", data, 5)[0]
        boot_min = struct.unpack_from("<H", data, 7)[0]
        raw_records.append((vbat_mv, raw_temp / 100.0, raw_humi / 100.0, boot_min))

    # Scan first so BlueZ caches the device
    device = None
    _log(f"Scanning for {address} (up to 15s)...")
    try:
        async with BleakScanner() as scanner:
            deadline = asyncio.get_running_loop().time() + 15.0
            while asyncio.get_running_loop().time() < deadline:
                for dev, _ in scanner.discovered_devices_and_advertisement_data.values():
                    if dev.address.upper() == address:
                        device = dev
                        break
                if device:
                    break
                await asyncio.sleep(0.5)
    except (BleakError, Exception) as e:
        _log(f"Scan error: {type(e).__name__}: {e}")
        return []

    if device is None:
        _log("Device not found during scan.")
        return []

    _log(f"Found: {device.name} — connecting...")
    try:
        async with BleakClient(device, timeout=timeout) as client:
            if verbose:
                _log("Connected. Services available:")
                for svc in client.services:
                    for ch in svc.characteristics:
                        print(f"    {ch.uuid}  [{','.join(ch.properties)}]")
            # Sync RTC on the sensor
            now_epoch = int(time.time())
            sync_cmd = bytes([_CMD_SYNC_TIME]) + struct.pack("<I", now_epoch)
            _log(f"Sending clock sync: {sync_cmd.hex()}")
            await client.write_gatt_char(_PVVX_CHAR, sync_cmd, response=False)
            # Subscribe to notifications
            _log(f"Subscribing to notifications on {_PVVX_CHAR}")
            await client.start_notify(_PVVX_CHAR, handle_notification)
            # Request history: [0x33, count] — sensor streams records, ends when idle
            memo_cmd = bytes([_CMD_GET_MEMO, min(count, 255)])
            _log(f"Sending GetMemo: {memo_cmd.hex()} (requesting {min(count, 255)} records)")
            await client.write_gatt_char(_PVVX_CHAR, memo_cmd, response=False)
            # Wait until no new notifications for idle_timeout seconds
            last_activity[0] = time.monotonic()
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_activity[0] >= idle_timeout:
                    break
            _log(f"Total bytes received: {raw_bytes_received[0]}, raw records: {len(raw_records)}")
            await client.stop_notify(_PVVX_CHAR)
    except (BleakError, asyncio.TimeoutError, Exception) as e:
        _log(f"Connection/GATT error: {type(e).__name__}: {e}")
        return []

    if not raw_records:
        return []

    # Reconstruct absolute timestamps from "minutes since device boot".
    # Records are sent oldest-first; the last record is the most recent (~now).
    # boot_min increments by 1 per recording interval (1 minute here).
    newest_boot_min = raw_records[-1][3]
    query_epoch = time.time()
    records = []
    for vbat_mv, temp_c, humidity, boot_min in raw_records:
        offset_secs = (newest_boot_min - boot_min) * 60
        ts_str = datetime.datetime.fromtimestamp(query_epoch - offset_secs).strftime("%Y-%m-%d %H:%M:%S")
        records.append({"ts": ts_str, "temp_c": temp_c, "humidity": humidity, "vbat_mv": vbat_mv})

    _log(f"Reconstructed {len(records)} records, oldest: {records[0]['ts']}, newest: {records[-1]['ts']}")
    return records
