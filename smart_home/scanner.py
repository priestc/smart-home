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
    on_device=None,
    extra_tasks: list | None = None,
):
    """Scan for Govee H5074 devices and call callback(Reading) for each update.
    If duration is None, scan indefinitely.
    on_device(device, adv) is called for every BLE device seen (not just Govee).
    extra_tasks is a list of coroutines to run concurrently with the scanner.
    """
    def detection_callback(device: BLEDevice, adv: AdvertisementData):
        if on_device:
            on_device(device, adv)

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
