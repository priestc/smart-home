from __future__ import annotations
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_TYPES_FILE = _CONFIG_DIR / "ble_types.json"


def load() -> dict[str, str]:
    """Return {address: device_type} mapping from disk."""
    if _TYPES_FILE.exists():
        try:
            with open(_TYPES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def record(address: str, device_type: str | None) -> None:
    """Persist the device type for an address if not already stored."""
    if not device_type:
        return
    types = load()
    if types.get(address) == device_type:
        return
    types[address] = device_type
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_TYPES_FILE, "w") as f:
        json.dump(types, f, indent=2)
