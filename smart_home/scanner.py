from __future__ import annotations
import asyncio
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from smart_home.decoder import decode_advertisement, decode_xiaomi_mibeacon, Reading

# LYWSD03MMC GATT characteristic: temp (int16 LE, 0.01°C) + humidity (uint8, %) + voltage (uint16 LE, mV)
_LYWSD03MMC_CHAR = "EBE0CCC1-7A0A-4B0C-8A1A-6FF2997DA3A6"


async def read_lywsd03mmc(address: str, name: str) -> Reading | None:
    """Actively connect to a LYWSD03MMC and read temperature/humidity via GATT."""
    try:
        async with BleakClient(address, timeout=10.0) as client:
            data = await client.read_gatt_char(_LYWSD03MMC_CHAR)
    except Exception:
        return None
    if len(data) < 3:
        return None
    temp_c   = int.from_bytes(data[0:2], "little", signed=True) / 100.0
    humidity = float(data[2])
    battery  = None
    if len(data) >= 5:
        mv = int.from_bytes(data[3:5], "little")
        battery = max(0, min(100, int((mv - 2100) / 10)))
    return Reading(
        address=address,
        name=name,
        temp_c=temp_c,
        humidity=humidity,
        battery=battery,
        rssi=None,
        raw_reading=data.hex(),
    )


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
):
    """Scan all BLE devices. For every device seen, on_device(device, adv) is called.
    For supported temperature sensors, callback(Reading) is also called.
    If duration is None, scan indefinitely.
    extra_tasks is a list of coroutines to run concurrently with the scanner.
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
        async with BleakScanner(detection_callback=detection_callback):
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

    await _run()
