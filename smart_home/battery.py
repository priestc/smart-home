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


async def dump_gatt(address: str, timeout: float = 15.0) -> None:
    """Scan for device then connect and print all GATT services and characteristics."""
    from bleak import BleakScanner
    print(f"  Scanning to locate device (10s)...")
    device = await BleakScanner.find_device_by_address(address, timeout=10.0)
    if device is None:
        print(f"  Device not found during scan. Make sure it's nearby and not connected to another device.")
        return
    print(f"  Found: {device.name} — attempting connection...")
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
                            print(f"        read error: {e}")
    except Exception as e:
        print(f"  Connection failed: {e}")
        print(f"  The device may use non-connectable advertising (common on Govee sensors).")
