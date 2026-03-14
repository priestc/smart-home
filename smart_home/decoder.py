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
