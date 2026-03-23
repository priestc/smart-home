"""PVVX firmware flasher for LYWSD03MMC temperature sensors.

Uses the Telink OAD (Over-the-Air Download) BLE protocol to flash the
pvvx/ATC_MiThermometer custom firmware.  After flashing, the sensor
advertises temperature/humidity passively (no GATT needed) with BLE name
ATC_XXXXXX where XXXXXX is the last 6 hex digits of the MAC address.

Protocol (from TelinkMiFlasher.html, pvvx/ATC_MiThermometer):
  OAD service:    00010203-0405-0607-0809-0a0b0c0d1912
  OAD write char: 00010203-0405-0607-0809-0a0b0c0d2b12
  Packet format:  [0x01][block_lo][block_hi][16 bytes firmware]  (19 bytes)
  Flow control:   if char supports notify, device ACKs each block with
                  [next_block_lo][next_block_hi]; otherwise write-with-
                  response provides GATT-layer flow control.
"""
from __future__ import annotations
import asyncio
import struct
import urllib.request
from pathlib import Path
from bleak import BleakClient

# Telink OAD protocol UUIDs
OAD_SERVICE = "00010203-0405-0607-0809-0a0b0c0d1912"
OAD_CHAR    = "00010203-0405-0607-0809-0a0b0c0d2b12"

BLOCK_SIZE   = 16            # firmware bytes per OTA packet payload
TELINK_MAGIC = 0x544c4e4b   # "TLNK" at offset 0x08 in firmware header

# Default firmware: PVVX custom firmware for LYWSD03MMC
# Source: https://github.com/pvvx/ATC_MiThermometer
FIRMWARE_URL = (
    "https://github.com/pvvx/ATC_MiThermometer/raw/master/bin/ATC_v57.bin"
)
_CACHE_DIR = Path("~/.cache/smart-home").expanduser()


def download_firmware(url: str = FIRMWARE_URL) -> bytes:
    """Download PVVX firmware, caching locally in ~/.cache/smart-home/."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / Path(url).name
    if cache_file.exists():
        return cache_file.read_bytes()
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
    cache_file.write_bytes(data)
    return data


def validate_firmware(data: bytes) -> int:
    """Validate Telink OTA firmware file.

    Returns the total number of 16-byte blocks.
    Raises ValueError if the file is not a valid Telink OTA image.
    """
    if len(data) < 0x20:
        raise ValueError(f"firmware too small ({len(data)} bytes)")
    magic = struct.unpack_from("<I", data, 0x08)[0]
    if magic != TELINK_MAGIC:
        raise ValueError(
            f"invalid Telink magic 0x{magic:08X} (expected 0x{TELINK_MAGIC:08X})"
        )
    return (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE


async def flash_firmware(
    address_or_device,
    firmware: bytes,
    progress=None,
) -> None:
    """Flash PVVX firmware to a LYWSD03MMC via Telink OAD BLE protocol.

    address_or_device: MAC address string or BleakClient-compatible device.
    firmware: raw .bin bytes (validated by validate_firmware before calling).
    progress: optional callable(blocks_done: int, total_blocks: int).

    The device must be in connectable mode (freshly power-cycled).
    Raises RuntimeError on failure.
    """
    total_blocks = validate_firmware(firmware)
    pad = total_blocks * BLOCK_SIZE - len(firmware)
    padded = firmware + b"\xff" * pad

    async with BleakClient(address_or_device, timeout=20.0) as client:
        # Locate OAD characteristic
        oad_char = None
        for svc in client.services:
            if svc.uuid.lower() == OAD_SERVICE.lower():
                for ch in svc.characteristics:
                    if ch.uuid.lower() == OAD_CHAR.lower():
                        oad_char = ch
                        break

        if oad_char is None:
            raise RuntimeError(
                "OAD characteristic not found — make sure the sensor is in "
                "connectable mode (power it off and back on) and that it is "
                "a LYWSD03MMC running stock or PVVX firmware."
            )

        # Enable notifications if the characteristic supports them.
        # The device ACKs each block by notifying the next expected block index.
        has_notify = "notify" in oad_char.properties or "indicate" in oad_char.properties
        _next_block: list[int] = [0]
        _ack_event = asyncio.Event()

        def _on_notify(_sender, data: bytearray) -> None:
            if len(data) >= 2:
                _next_block[0] = int.from_bytes(data[:2], "little")
                _ack_event.set()

        if has_notify:
            await client.start_notify(OAD_CHAR, _on_notify)

        block_num = 0
        while block_num < total_blocks:
            is_last = block_num == total_blocks - 1
            offset = block_num * BLOCK_SIZE
            block_data = padded[offset : offset + BLOCK_SIZE]

            # OAD packet: command(1) + block_index_LE(2) + data(16) = 19 bytes
            packet = (
                bytes([0x01, block_num & 0xFF, (block_num >> 8) & 0xFF])
                + block_data
            )

            if has_notify:
                _ack_event.clear()

            try:
                # Use write-with-response when no notify (provides GATT-layer flow ctrl).
                await client.write_gatt_char(
                    OAD_CHAR, packet, response=not has_notify
                )
            except Exception as e:
                err = str(e).lower()
                if is_last and any(
                    k in err for k in ("disconnect", "closed", "not connected", "broken pipe")
                ):
                    # Device rebooted immediately after last block — normal end
                    block_num += 1
                    break
                raise RuntimeError(f"write failed at block {block_num}: {e}") from e

            if has_notify and not is_last:
                try:
                    await asyncio.wait_for(_ack_event.wait(), timeout=10.0)
                    block_num = _next_block[0]
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"timeout waiting for ACK at block {block_num}/{total_blocks}"
                    )
            else:
                if not has_notify:
                    # ~12 ms for device to write one 16-byte block to flash
                    await asyncio.sleep(0.012)
                block_num += 1

            if progress:
                progress(block_num, total_blocks)

        if has_notify:
            try:
                await client.stop_notify(OAD_CHAR)
            except Exception:
                pass
