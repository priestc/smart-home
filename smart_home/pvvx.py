from __future__ import annotations
import asyncio
import datetime
import json
import struct
import time
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_PVVX_FILE  = _CONFIG_DIR / "pvvx_devices.json"

_PVVX_SERVICE  = "0000181f-0000-1000-8000-00805f9b34fb"  # PVVX custom service (0x181f)
_PVVX_CHAR     = "00001f1f-0000-1000-8000-00805f9b34fb"  # PVVX control/history characteristic
_CMD_SYNC_TIME = 0x23
_CMD_GET_HIST  = 0x35


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
    timeout: float = 30.0,
    idle_timeout: float = 5.0,
) -> list[dict]:
    """Connect to a PVVX sensor, sync its clock, and read all stored history records.

    Returns a list of dicts with keys: ts (str "%Y-%m-%d %H:%M:%S"), temp_c (float),
    humidity (float), battery (int).  Returns [] on any error.
    """
    from bleak import BleakClient, BleakError, BleakScanner

    address = address.upper()
    records: list[dict] = []
    last_activity: list[float] = [0.0]  # mutable container so closure can update it

    def handle_notification(sender, data: bytearray):
        last_activity[0] = time.monotonic()
        # Each notification may carry multiple 9-byte history records
        offset = 0
        while offset + 9 <= len(data):
            chunk = data[offset:offset + 9]
            unix_ts, raw_temp, raw_humi, bat = struct.unpack_from("<IhHB", chunk)
            if unix_ts == 0:
                offset += 9
                continue
            temp_c = raw_temp / 100.0
            humidity = raw_humi / 100.0
            ts_str = datetime.datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
            records.append({"ts": ts_str, "temp_c": temp_c, "humidity": humidity, "battery": bat})
            offset += 9

    # Scan first so BlueZ caches the device
    device = None
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
    except (BleakError, Exception):
        return []

    if device is None:
        return []

    try:
        async with BleakClient(device, timeout=timeout) as client:
            # Sync RTC on the sensor
            now_epoch = int(time.time())
            await client.write_gatt_char(
                _PVVX_CHAR,
                bytes([_CMD_SYNC_TIME]) + struct.pack("<I", now_epoch),
                response=False,
            )
            # Subscribe to notifications
            await client.start_notify(_PVVX_CHAR, handle_notification)
            # Request full history dump
            await client.write_gatt_char(_PVVX_CHAR, bytes([_CMD_GET_HIST]), response=False)
            # Poll until no new notifications arrive for idle_timeout seconds
            last_activity[0] = time.monotonic()
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_activity[0] >= idle_timeout:
                    break
            await client.stop_notify(_PVVX_CHAR)
    except (BleakError, asyncio.TimeoutError, Exception):
        return []

    return records
