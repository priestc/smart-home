"""PVVX firmware flasher for LYWSD03MMC temperature sensors.

Uses the Telink OAD (Over-the-Air Download) BLE protocol to flash the
pvvx/ATC_MiThermometer custom firmware.  After flashing, the sensor
advertises temperature/humidity passively (no GATT needed) with BLE name
ATC_XXXXXX where XXXXXX is the last 6 hex digits of the MAC address.

Protocol (from TelinkMiFlasher.html, pvvx/ATC_MiThermometer):
  OAD service:    00010203-0405-0607-0809-0a0b0c0d1912
  OAD write char: 00010203-0405-0607-0809-0a0b0c0d2b12
  Block packet:   [block_lo][block_hi][16 bytes data][crc_lo][crc_hi]  (20 bytes)
  End packet:     [0x02][0xff][last_block_lo][last_block_hi][~last_block_lo][~last_block_hi]
  CRC:            CRC-16/CCITT-FALSE over (block_index_LE + 16 bytes data)
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


def _crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def _make_block_packet(block_num: int, block_data: bytes) -> bytes:
    """Build a 20-byte OAD block packet with CRC.

    Format: [block_lo][block_hi][16 bytes data][crc_lo][crc_hi]
    CRC covers the block index bytes + data bytes.
    """
    index_bytes = bytes([block_num & 0xFF, (block_num >> 8) & 0xFF])
    crc = _crc16(index_bytes + block_data)
    return index_bytes + block_data + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _make_end_packet(total_blocks: int) -> bytes:
    """Build the end-of-image packet that triggers the device to apply firmware.

    Format: [0x02][0xff][last_lo][last_hi][~last_lo][~last_hi]
    """
    last = total_blocks - 1
    return bytes([
        0x02, 0xFF,
        last & 0xFF, (last >> 8) & 0xFF,
        (~last) & 0xFF, (~last >> 8) & 0xFF,
    ])


_DISCONNECT_ERRORS   = ("disconnect", "closed", "not connected", "broken pipe", "service discovery")
_MAX_RETRIES         = 5    # reconnect attempts before giving up
_RECONNECT_DELAY     = 3.0  # seconds to wait before reconnecting
_POST_CONNECT_DELAY  = 1.0  # seconds to wait after reconnect for service discovery to settle
_INTER_BLOCK_DELAY   = 0.020


async def _connect_and_find_oad(address_or_device):
    """Connect to device and return (BleakClient, oad_char).

    Raises RuntimeError if the OAD characteristic is not found.
    The returned client is already connected; caller must close it.
    """
    client = BleakClient(address_or_device, timeout=20.0)
    await client.connect()

    for svc in client.services:
        if svc.uuid.lower() == OAD_SERVICE.lower():
            for ch in svc.characteristics:
                if ch.uuid.lower() == OAD_CHAR.lower():
                    return client, ch

    await client.disconnect()
    raise RuntimeError(
        "OAD characteristic not found \u2014 make sure the sensor is in "
        "connectable mode (power it off and back on) and that it is "
        "a LYWSD03MMC running stock or PVVX firmware."
    )


async def flash_firmware(
    address_or_device,
    firmware: bytes,
    progress=None,
) -> None:
    """Flash PVVX firmware to a LYWSD03MMC via Telink OAD BLE protocol.

    address_or_device: MAC address string or BleakClient-compatible device.
    firmware: raw .bin bytes (validated by validate_firmware before calling).
    progress: optional callable(blocks_done: int, total_blocks: int).

    Automatically reconnects and restarts if the device drops the connection
    mid-transfer (common on stock firmware during OAD mode switch).
    Raises RuntimeError on failure.
    """
    total_blocks = validate_firmware(firmware)
    pad = total_blocks * BLOCK_SIZE - len(firmware)
    padded = firmware + b"\xff" * pad

    # Resolve address string once so reconnects can use it directly.
    address = (
        address_or_device
        if isinstance(address_or_device, str)
        else address_or_device.address
    )

    client, oad_char = await _connect_and_find_oad(address_or_device)

    block_num = 0
    retries   = 0

    try:
        while block_num < total_blocks:
            offset     = block_num * BLOCK_SIZE
            block_data = padded[offset : offset + BLOCK_SIZE]
            packet     = _make_block_packet(block_num, block_data)

            try:
                await client.write_gatt_char(OAD_CHAR, packet, response=False)

            except Exception as e:
                err = str(e).lower()
                is_disconnect = any(k in err for k in _DISCONNECT_ERRORS)

                if is_disconnect and retries < _MAX_RETRIES:
                    retries += 1
                    if progress:
                        progress(block_num, total_blocks, reconnecting=True)
                    await asyncio.sleep(_RECONNECT_DELAY)
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    client, oad_char = await _connect_and_find_oad(address)
                    await asyncio.sleep(_POST_CONNECT_DELAY)  # let service discovery settle
                    # Telink OAD resets on disconnect \u2014 restart from block 0.
                    block_num = 0
                    continue

                raise RuntimeError(
                    f"write failed at block {block_num} "
                    f"(retried {retries}x): {e}"
                ) from e

            await asyncio.sleep(_INTER_BLOCK_DELAY)
            block_num += 1
            if progress:
                progress(block_num, total_blocks)

        # Send end-of-image packet to trigger the device to apply firmware.
        # Sent multiple times: the device reboots as soon as it accepts one,
        # so later sends will hit a disconnected device — that's expected and ignored.
        end_packet = _make_end_packet(total_blocks)
        for _ in range(3):
            try:
                await client.write_gatt_char(OAD_CHAR, end_packet, response=False)
            except Exception:
                break  # device already disconnected/rebooting, stop sending
            await asyncio.sleep(0.1)

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
