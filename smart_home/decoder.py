from __future__ import annotations
import dataclasses


@dataclasses.dataclass
class Reading:
    address: str
    name: str
    temp_c: float
    humidity: float
    battery: int | None  # percent
    rssi: int | None
    raw_reading: str | None = None  # hex-encoded full manufacturer payload
    label: str | None = None

    @property
    def temp_f(self) -> float:
        return self.temp_c * 9 / 5 + 32

    def __str__(self) -> str:
        display = self.label if self.label else self.name
        batt = f"  battery={self.battery}%" if self.battery is not None else ""
        rssi = f"  rssi={self.rssi}" if self.rssi is not None else ""
        return (
            f"{display}"
            f"  temp={self.temp_f:.1f}°F"
            f"  humidity={self.humidity:.1f}%"
            f"{batt}{rssi}"
        )


GOVEE_COMPANY_ID = 0xEC88

# Xiaomi MiBeacon (LYWSD03MMC)
XIAOMI_SERVICE_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
LYWSD03MMC_TYPE = 0x055B

# MiBeacon frame-control bit masks (low byte)
_FC_ENCRYPTED = 0x08
_FC_HAS_MAC   = 0x10
_FC_HAS_CAP   = 0x20
_FC_HAS_OBJ   = 0x40

# MiBeacon object type codes
_OBJ_TEMP     = 0x1004
_OBJ_HUMIDITY = 0x1006
_OBJ_BATTERY  = 0x100A


def decode_xiaomi_mibeacon(address: str, name: str, service_data: dict, rssi: int | None) -> dict | None:
    """Decode a Xiaomi MiBeacon v3 advertisement for the LYWSD03MMC.

    Returns a partial dict with any of {temp_c, humidity, battery} found in this
    frame, or None if the frame cannot be parsed or contains no sensor data.
    Callers should accumulate partial results across frames.
    """
    data = service_data.get(XIAOMI_SERVICE_UUID)
    if data is None or len(data) < 5:
        return None

    device_type = int.from_bytes(data[2:4], "little")
    if device_type != LYWSD03MMC_TYPE:
        return None

    fc_low = data[0]
    if fc_low & _FC_ENCRYPTED:
        return None  # encrypted — need Mi Home binding key
    if not (fc_low & _FC_HAS_OBJ):
        return None  # no sensor object in this frame

    # Locate object data: fixed header(5) + optional MAC(6) + optional capability(1)
    offset = 5
    if fc_low & _FC_HAS_MAC:
        offset += 6
    if fc_low & _FC_HAS_CAP:
        offset += 1

    result: dict = {}
    while offset + 3 <= len(data):
        obj_type = int.from_bytes(data[offset:offset + 2], "little")
        obj_len  = data[offset + 2]
        offset  += 3
        if offset + obj_len > len(data):
            break
        obj_data = data[offset:offset + obj_len]
        offset  += obj_len

        if obj_type == _OBJ_TEMP and obj_len >= 2:
            result["temp_c"] = int.from_bytes(obj_data[:2], "little", signed=True) / 10.0
        elif obj_type == _OBJ_HUMIDITY and obj_len >= 2:
            result["humidity"] = int.from_bytes(obj_data[:2], "little") / 10.0
        elif obj_type == _OBJ_BATTERY and obj_len >= 1:
            result["battery"] = obj_data[0]

    return result if result else None


# PVVX custom advertisement (non-encrypted), service UUID 0x181A
PVVX_SERVICE_UUID = "0000181a-0000-1000-8000-00805f9b34fb"


def decode_pvvx_advertisement(
    address: str, name: str, service_data: dict, rssi: int | None
) -> Reading | None:
    """Decode a PVVX/ATC_MiThermometer custom BLE advertisement.

    Payload layout (15 bytes after the service UUID):
      [MAC 6B][temp int16 LE /100 °C][humi uint16 LE /100 %]
      [batt_mv uint16 LE][batt% uint8][counter uint8][flags uint8]
    """
    data = service_data.get(PVVX_SERVICE_UUID)
    if data is None or len(data) < 15:
        return None
    temp_c   = int.from_bytes(data[6:8], "little", signed=True) / 100.0
    humidity = int.from_bytes(data[8:10], "little") / 100.0
    battery  = data[12]  # percent
    return Reading(
        address=address,
        name=name,
        temp_c=temp_c,
        humidity=humidity,
        battery=battery,
        rssi=rssi,
        raw_reading=data.hex(),
    )


def decode_advertisement(address: str, name: str, manufacturer_data: dict, rssi: int | None) -> Reading | None:
    """Decode a Govee H5074 BLE advertisement into a Reading.
    Returns None if the data cannot be decoded.
    """
    data = manufacturer_data.get(GOVEE_COMPANY_ID)
    if data is None:
        return None
    # H5074 7-byte payload (after company ID):
    #   byte 0: 0x00 prefix
    #   byte 1: packet counter (ignored)
    #   bytes 2-3: temperature as signed int16 big-endian, units = 0.01 °C
    #   byte 4: battery level (percent)
    #   byte 5: humidity (integer percent)
    #   byte 6: constant 0x02 (ignored)
    if len(data) >= 6 and data[0] == 0x00:
        temp_c = int.from_bytes(data[2:4], "big", signed=True) / 100
        humidity = int.from_bytes(data[4:6], "big") / 100
        battery = None  # not present in H5074 advertisement
    elif len(data) >= 4:
        # Fallback: original 4-byte packed format used by some other Govee models
        raw = int.from_bytes(data[0:3], "big")
        if raw & 0x800000:
            raw = 0x1000000 - raw
            temp_c = -(raw // 1000) / 10
        else:
            temp_c = (raw // 1000) / 10
        humidity = (raw % 1000) / 10
        battery = data[3]
    else:
        return None

    return Reading(
        address=address,
        name=name,
        temp_c=temp_c,
        humidity=humidity,
        battery=battery,
        rssi=rssi,
        raw_reading=data.hex(),
    )
