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
    verbose: bool = False,
) -> list[dict]:
    """Connect to a PVVX sensor, sync its clock, and read all stored history records.

    Returns a list of dicts with keys: ts (str "%Y-%m-%d %H:%M:%S"), temp_c (float),
    humidity (float), battery (int).  Returns [] on any error.
    """
    from bleak import BleakClient, BleakError, BleakScanner

    def _log(msg):
        if verbose:
            print(f"  [pvvx] {msg}")

    address = address.upper()
    records: list[dict] = []
    last_activity: list[float] = [0.0]  # mutable container so closure can update it
    raw_bytes_received: list[int] = [0]

    def handle_notification(sender, data: bytearray):
        last_activity[0] = time.monotonic()
        raw_bytes_received[0] += len(data)
        _log(f"notification {len(data)} bytes: {data.hex()}")
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
            _log("Connected. Services available:")
            if verbose:
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
            # Request full history dump
            _log(f"Sending history request: {bytes([_CMD_GET_HIST]).hex()}")
            await client.write_gatt_char(_PVVX_CHAR, bytes([_CMD_GET_HIST]), response=False)
            # Poll until no new notifications arrive for idle_timeout seconds
            last_activity[0] = time.monotonic()
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_activity[0] >= idle_timeout:
                    break
            _log(f"Idle timeout reached. Total bytes received: {raw_bytes_received[0]}, records parsed: {len(records)}")
            await client.stop_notify(_PVVX_CHAR)
    except (BleakError, asyncio.TimeoutError, Exception) as e:
        _log(f"Connection/GATT error: {type(e).__name__}: {e}")
        return []

    return records
