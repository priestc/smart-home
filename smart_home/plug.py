from __future__ import annotations
import asyncio
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_PLUGS_FILE = _CONFIG_DIR / "plugs.json"

# GATT characteristic UUIDs (same across H5080/H5082/H5086 family)
_WRITE_CHAR  = "00010203-0405-0607-0809-0a0b0c0d2b11"
_NOTIFY_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b10"

# Govee manufacturer ID
GOVEE_MFR_ID = 0x88EC


def load_config() -> list[dict]:
    if _PLUGS_FILE.exists():
        try:
            with open(_PLUGS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_config(plugs: list[dict]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_PLUGS_FILE, "w") as f:
        json.dump(plugs, f, indent=2)


def load_labels() -> dict[str, str]:
    """Return address -> label mapping for all configured plugs."""
    return {p["address"].upper(): p["label"] for p in load_config() if p.get("address")}


def is_h5086(device, adv) -> bool:
    name = device.name or adv.local_name or ""
    return name.startswith("GVH5086")


def parse_on_off(adv) -> bool | None:
    """Extract on/off state from the passive advertisement manufacturer data."""
    data = adv.manufacturer_data.get(GOVEE_MFR_ID)
    if data and len(data) >= 1:
        return data[-1] == 0x01
    return None


def _build_cmd(cmd_hi: int, cmd_lo: int) -> bytes:
    """Build a 20-byte Govee command packet with XOR checksum."""
    buf = bytearray(20)
    buf[0], buf[1] = cmd_hi, cmd_lo
    xor = 0
    for b in buf[:19]:
        xor ^= b
    buf[19] = xor
    return bytes(buf)


async def read_energy(ble_device) -> tuple[dict | None, str | None]:
    """Connect to an H5086 plug and read energy data via GATT.

    Returns (reading_dict, None) on success or (None, error_message) on failure.
    reading_dict keys: watts, volts, amps, energy_wh, power_factor, time_on_s
    """
    from bleak import BleakClient, BleakError

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _on_notify(sender, data: bytearray) -> None:
        # Response starts with EE 19
        if len(data) >= 20 and data[0] == 0xEE and data[1] == 0x19:
            payload = data[2:19]
            result = {
                "time_on_s":    int.from_bytes(payload[0:3],  "big"),
                "energy_wh":    int.from_bytes(payload[3:6],  "big") / 10.0,
                "volts":        int.from_bytes(payload[6:8],  "big") / 100.0,
                "amps":         int.from_bytes(payload[8:10], "big") / 100.0,
                "watts":        int.from_bytes(payload[10:13],"big") / 100.0,
                "power_factor": payload[13],
            }
            if not future.done():
                future.set_result(result)

    try:
        async with BleakClient(ble_device, timeout=10.0) as client:
            await client.start_notify(_NOTIFY_CHAR, _on_notify)
            await client.write_gatt_char(_WRITE_CHAR, _build_cmd(0xAA, 0x00), response=False)
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=5.0)
            except asyncio.TimeoutError:
                return None, "No energy response within 5s"
    except BleakError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)

    if future.done() and not future.cancelled():
        return future.result(), None
    return None, "No energy response received"
