from __future__ import annotations
import asyncio
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from smart_home.decoder import decode_advertisement, Reading


def is_govee_h5074(device: BLEDevice, adv: AdvertisementData) -> bool:
    name = device.name or adv.local_name or ""
    return name.startswith("GVH5074") or name.startswith("Govee_H5074")


async def scan(
    callback,
    duration: float | None = None,
    verbose: bool = False,
):
    """Scan for Govee H5074 devices and call callback(Reading) for each update.
    If duration is None, scan indefinitely.
    """
    def detection_callback(device: BLEDevice, adv: AdvertisementData):
        name = device.name or adv.local_name or device.address
        if not is_govee_h5074(device, adv):
            return
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

    async with BleakScanner(detection_callback=detection_callback):
        if duration is not None:
            await asyncio.sleep(duration)
        else:
            while True:
                await asyncio.sleep(1)
