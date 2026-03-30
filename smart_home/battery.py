from __future__ import annotations
import asyncio
from bleak import BleakClient, BleakError

BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


async def read_battery(address: str, timeout: float = 10.0) -> tuple[int | None, str | None]:
    """Connect to a device and read its battery level.
    Returns (level, error_message). level is None on failure."""
    try:
        async with BleakClient(address, timeout=timeout) as client:
            data = await client.read_gatt_char(BATTERY_CHAR_UUID)
            return data[0], None
    except (BleakError, asyncio.TimeoutError, Exception) as e:
        return None, str(e)


async def read_batteries(addresses: list[str]) -> dict[str, int | None]:
    """Read battery for multiple devices concurrently. Prints errors if reads fail."""
    results = await asyncio.gather(*[read_battery(a) for a in addresses])
    out = {}
    for addr, (level, err) in zip(addresses, results):
        if err:
            print(f"  battery read failed for {addr}: {err}")
        out[addr] = level
    return out


async def dump_gatt(address: str, timeout: float = 20.0) -> None:
    """Scan for ADDRESS then connect and print all GATT services and characteristics."""
    from bleak import BleakScanner
    address = address.upper()
    print(f"  Scanning for {address} (up to 15s)...")
    device = None
    # Scan until we see the device advertise (needed so BlueZ caches it)
    async with BleakScanner() as scanner:
        deadline = asyncio.get_event_loop().time() + 15.0
        while asyncio.get_event_loop().time() < deadline:
            found = scanner.discovered_devices_and_advertisement_data
            for dev, _ in found.values():
                if dev.address.upper() == address:
                    device = dev
                    break
            if device:
                break
            await asyncio.sleep(0.5)

    if device is None:
        print(f"  Device not found. Make sure it is nearby and advertising.")
        return
    print(f"  Found: {device.name} — connecting...")
    try:
        async with BleakClient(device, timeout=timeout) as client:
            print(f"  Connected. Services:")
            for service in client.services:
                print(f"    Service: {service.uuid}  ({service.description})")
                for char in service.characteristics:
                    props = ",".join(char.properties)
                    print(f"      Char: {char.uuid}  [{props}]  ({char.description})")
                    if "read" in char.properties:
                        try:
                            val = await client.read_gatt_char(char.uuid)
                            print(f"        value: {val.hex()}  {list(val)}")
                        except Exception as e:
                            print(f"        read error: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"  Connection failed: {type(e).__name__}: {e}")
