from __future__ import annotations
import asyncio
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from smart_home.decoder import (
    decode_advertisement,
    decode_pvvx_advertisement,
    PVVX_SERVICE_UUID,
    Reading,
)
from smart_home.pool import READ_UUID as _YC01_READ_UUID, parse_gatt_data, PoolReading


def is_pvvx_lywsd03mmc(device: BLEDevice, adv: AdvertisementData) -> bool:
    """True for LYWSD03MMC sensors running PVVX/ATC custom firmware.
    Detects by ATC_ name prefix OR by presence of service data UUID 0x181A,
    so it works even when BlueZ has the old LYWSD03MMC name cached.
    """
    name = device.name or adv.local_name or ""
    if name.startswith("ATC_"):
        return True
    return PVVX_SERVICE_UUID in (adv.service_data or {})


def is_ble_yc01(device: BLEDevice, adv: AdvertisementData) -> bool:
    name = device.name or adv.local_name or ""
    return name.startswith("BLE_YC01") or name.startswith("BLE-YC01")


async def read_ble_yc01(device: BLEDevice, label: str) -> tuple[PoolReading | None, str | None]:
    """Actively connect to a BLE_YC01 and read all pool metrics via GATT.
    Returns (PoolReading, None) on success or (None, error_message) on failure.
    """
    try:
        async with BleakClient(device, timeout=10.0) as client:
            raw = await client.read_gatt_char(_YC01_READ_UUID)
    except Exception as e:
        return None, str(e) or type(e).__name__
    reading = parse_gatt_data(raw)
    if reading is None:
        return None, f"GATT data too short ({len(raw)} bytes)"
    reading.address = device.address
    reading.label = label
    return reading, None


def is_govee_h5074(device: BLEDevice, adv: AdvertisementData) -> bool:
    name = device.name or adv.local_name or ""
    return name.startswith("GVH5074") or name.startswith("Govee_H5074")



async def scan(
    callback,
    duration: float | None = None,
    verbose: bool = False,
    on_device=None,
    extra_tasks: list | None = None,
    scanner_ref: list | None = None,
    on_status=None,
):
    """Scan all BLE devices. For every device seen, on_device(device, adv) is called.
    For supported temperature sensors, callback(Reading) is also called.
    If duration is None, scan indefinitely.
    extra_tasks is a list of coroutines to run concurrently with the scanner; they
    start right away and keep running if the scanner can't start.
    If scanner_ref is a list, it holds the running BleakScanner (empty while it
    isn't running) so callers can call scanner.stop()/scanner.start() to pause
    scanning (e.g. for GATT).
    on_status(error) is called with None when the scanner starts, or with the
    reason each time it fails to start.
    """
    def detection_callback(device: BLEDevice, adv: AdvertisementData):
        if on_device:
            on_device(device, adv)

        name = device.name or adv.local_name or device.address

        if is_govee_h5074(device, adv):
            if verbose:
                print(f"[raw] {name} ({device.address})")
                print(f"  manufacturer_data={adv.manufacturer_data!r}")
                print(f"  service_data={adv.service_data!r}")
                print(f"  rssi={adv.rssi}")
            reading = decode_advertisement(
                address=device.address,
                name=name,
                manufacturer_data=adv.manufacturer_data,
                rssi=adv.rssi,
            )
            if reading is not None:
                callback(reading)
            elif verbose:
                print(f"  [decode failed — could not parse advertisement]")

        elif is_pvvx_lywsd03mmc(device, adv):
            if verbose:
                print(f"[raw] {name} ({device.address})")
                print(f"  service_data={adv.service_data!r}")
                print(f"  rssi={adv.rssi}")
            reading = decode_pvvx_advertisement(
                address=device.address,
                name=name,
                service_data=adv.service_data,
                rssi=adv.rssi,
            )
            if reading is not None:
                callback(reading)
            elif verbose:
                print(f"  [decode failed — could not parse PVVX advertisement]")

    async def _scan_forever():
        # BlueZ can hold a stale scan registration for several seconds after a
        # crash/restart; that clears in 15s. Any other failure (no adapter,
        # bluetoothd not running) is retried every minute, so the scanner
        # attaches by itself once the adapter is back.
        attempt = 0
        while True:
            try:
                scanner = BleakScanner(detection_callback=detection_callback)
                async with scanner:
                    # Only a started scanner is handed out: other tasks stop/start it
                    if scanner_ref is not None:
                        scanner_ref[:] = [scanner]
                    attempt = 0
                    if on_status:
                        on_status(None)
                    await asyncio.get_running_loop().create_future()  # until cancelled
            except Exception as e:
                if scanner_ref is not None:
                    scanner_ref.clear()
                if "InProgress" in str(e) or "Operation already in progress" in str(e):
                    attempt += 1
                    print(f"BLE scanner busy (BlueZ stale registration), retrying in 15s... (attempt {attempt})")
                    await asyncio.sleep(15)
                    continue
                msg = f"{type(e).__name__}: {e}"
                print(f"BLE scanner couldn't start ({msg}); retrying in 60s", flush=True)
                if on_status:
                    on_status(msg)
                await asyncio.sleep(60)

    # The other tasks run whether or not Bluetooth works
    scan_task = asyncio.ensure_future(_scan_forever())
    try:
        until = asyncio.sleep(duration) if duration is not None else asyncio.get_running_loop().create_future()
        await asyncio.gather(*(extra_tasks or []), until)
    finally:
        scan_task.cancel()
