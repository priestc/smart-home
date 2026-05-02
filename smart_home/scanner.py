from __future__ import annotations
import asyncio
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from smart_home.decoder import (
    decode_advertisement,
    decode_xiaomi_mibeacon,
    decode_pvvx_advertisement,
    PVVX_SERVICE_UUID,
    Reading,
)
from smart_home.pool import READ_UUID as _YC01_READ_UUID, parse_gatt_data, PoolReading

# LYWSD03MMC GATT characteristic: temp (int16 LE, 0.01°C) + humidity (uint8, %) + voltage (uint16 LE, mV)
_LYWSD03MMC_CHAR = "EBE0CCC1-7A0A-4B0C-8A1A-6FF2997DA3A6"


async def read_lywsd03mmc(device, name: str) -> tuple[Reading | None, str | None]:
    """Actively connect to a LYWSD03MMC and read temperature/humidity via GATT.
    Returns (Reading, None) on success or (None, error_message) on failure.
    Single attempt — callers should retry by scheduling on the next advertisement.
    """
    try:
        async with BleakClient(device, timeout=10.0) as client:
            data = await client.read_gatt_char(_LYWSD03MMC_CHAR)
    except Exception as e:
        return None, str(e) or type(e).__name__
    if len(data) < 3:
        return None, f"data too short ({len(data)} bytes)"
    temp_c   = int.from_bytes(data[0:2], "little", signed=True) / 100.0
    humidity = float(data[2])
    battery  = None
    if len(data) >= 5:
        mv = int.from_bytes(data[3:5], "little")
        battery = max(0, min(100, int((mv - 2100) / 10)))
    return Reading(
        address=device.address,
        name=name,
        temp_c=temp_c,
        humidity=humidity,
        battery=battery,
        rssi=None,
        raw_reading=data.hex(),
    ), None


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


def is_xiaomi_lywsd03mmc(device: BLEDevice, adv: AdvertisementData) -> bool:
    name = device.name or adv.local_name or ""
    return name.startswith("LYWSD03MMC")


async def scan(
    callback,
    duration: float | None = None,
    verbose: bool = False,
    on_device=None,
    extra_tasks: list | None = None,
    scanner_ref: list | None = None,
):
    """Scan all BLE devices. For every device seen, on_device(device, adv) is called.
    For supported temperature sensors, callback(Reading) is also called.
    If duration is None, scan indefinitely.
    extra_tasks is a list of coroutines to run concurrently with the scanner.
    If scanner_ref is a list, the BleakScanner instance will be appended to it so
    callers can call scanner.stop()/scanner.start() to pause scanning (e.g. for GATT).
    """
    # Per-device accumulated state for sensors that split data across frames
    xiaomi_state: dict[str, dict] = {}

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

        elif is_xiaomi_lywsd03mmc(device, adv):
            if verbose:
                print(f"[raw] {name} ({device.address})")
                print(f"  service_data={adv.service_data!r}")
                print(f"  rssi={adv.rssi}")
            partial = decode_xiaomi_mibeacon(
                address=device.address,
                name=name,
                service_data=adv.service_data,
                rssi=adv.rssi,
            )
            if partial:
                state = xiaomi_state.setdefault(device.address, {})
                state.update(partial)
                state["rssi"] = adv.rssi
                if "temp_c" in state and "humidity" in state:
                    reading = Reading(
                        address=device.address,
                        name=name,
                        temp_c=state["temp_c"],
                        humidity=state["humidity"],
                        battery=state.get("battery"),
                        rssi=state.get("rssi"),
                        raw_reading=adv.service_data.get(
                            "0000fe95-0000-1000-8000-00805f9b34fb", b""
                        ).hex(),
                    )
                    callback(reading)

    async def _run():
        # BlueZ can hold a stale scan registration for several seconds after a
        # crash/restart. Retry indefinitely with a fixed wait until it clears.
        attempt = 0
        while True:
            try:
                scanner = BleakScanner(detection_callback=detection_callback)
                if scanner_ref is not None:
                    if attempt == 0:
                        scanner_ref.append(scanner)
                    else:
                        scanner_ref[0] = scanner
                async with scanner:
                    coros = list(extra_tasks or [])
                    if duration is not None:
                        coros.append(asyncio.sleep(duration))
                        if coros:
                            await asyncio.gather(*coros)
                    else:
                        async def _forever():
                            while True:
                                await asyncio.sleep(1)
                        coros.append(_forever())
                        await asyncio.gather(*coros)
                return  # clean exit
            except Exception as e:
                if "InProgress" in str(e) or "Operation already in progress" in str(e):
                    attempt += 1
                    print(f"BLE scanner busy (BlueZ stale registration), retrying in 15s... (attempt {attempt})")
                    await asyncio.sleep(15)
                else:
                    raise

    await _run()
