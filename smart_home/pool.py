from __future__ import annotations
import json
import dataclasses
from pathlib import Path

CONFIG_PATH = Path("~/.config/smart-home/pool_monitors.json").expanduser()

# Battery voltage thresholds (raw decoded value from GATT characteristic)
_BATT_100 = 3190
_BATT_0   = 1950

# GATT characteristic UUID to read all sensor data
READ_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"


@dataclasses.dataclass
class PoolReading:
    address: str
    label: str | None
    temp_c: float    # °C
    ph: float        # pH units
    ec: int          # µS/cm
    tds: int         # ppm
    orp: int         # mV
    chlorine: float  # mg/L (free chlorine)
    battery: int     # percent
    rssi: int | None = None

    @property
    def temp_f(self) -> float:
        return self.temp_c * 9 / 5 + 32

    def __str__(self) -> str:
        display = self.label or self.address
        return (
            f"{display}"
            f"  temp={self.temp_f:.1f}°F"
            f"  pH={self.ph:.2f}"
            f"  ORP={self.orp}mV"
            f"  Cl={self.chlorine:.1f}mg/L"
            f"  EC={self.ec}µS/cm"
            f"  TDS={self.tds}ppm"
            f"  battery={self.battery}%"
        )


def load_config() -> list[dict]:
    if not CONFIG_PATH.exists():
        return []
    return json.loads(CONFIG_PATH.read_text())


def save_config(monitors: list[dict]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(monitors, indent=2))


def _decode_bytes(raw: bytes) -> list[int]:
    """Reverse the BLE_YC01 byte encoding (paired bit-swap XOR transform)."""
    frame = list(raw)
    for i in range(len(frame) - 1, 0, -1):
        tmp = frame[i]
        hibit1 = (tmp & 0x55) << 1
        lobit1 = (tmp & 0xAA) >> 1
        tmp = frame[i - 1]
        hibit  = (tmp & 0x55) << 1
        lobit  = (tmp & 0xAA) >> 1
        frame[i]     = 0xFF - (hibit1 | lobit)
        frame[i - 1] = 0xFF - (hibit  | lobit1)
    return frame


def _i16(data: list[int], idx: int) -> int:
    return int.from_bytes(bytes(data[idx:idx + 2]), byteorder="big", signed=True)


def parse_gatt_data(raw: bytes) -> PoolReading | None:
    """Parse raw GATT bytes from the BLE_YC01 into a PoolReading.

    Byte layout after decoding (indices into decoded list):
      [3-4]  pH × 100
      [5-6]  EC (µS/cm)
      [7-8]  TDS (ppm)
      [9-10] ORP (mV)
      [11-12] chlorine × 10 (mg/L)
      [13-14] temperature × 10 (°C)
      [15-16] battery raw voltage (scaled to 0-100% via _BATT_0/_BATT_100)
    """
    if len(raw) < 18:
        return None
    d = _decode_bytes(raw)

    batt_raw = _i16(d, 15)
    battery = round(100 * (batt_raw - _BATT_0) / (_BATT_100 - _BATT_0))
    battery = max(0, min(100, battery))

    cloro_raw = _i16(d, 11)
    chlorine = max(0.0, cloro_raw / 10.0)

    return PoolReading(
        address="",
        label=None,
        temp_c=_i16(d, 13) / 10.0,
        ph=_i16(d, 3) / 100.0,
        ec=_i16(d, 5),
        tds=_i16(d, 7),
        orp=_i16(d, 9),
        chlorine=chlorine,
        battery=battery,
    )
