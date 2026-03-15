from __future__ import annotations
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_DEVICES_FILE = _CONFIG_DIR / "presence_devices.json"
_STATE_FILE = _CONFIG_DIR / "presence_state.json"


def load_devices() -> dict[str, str]:
    """Return {address: name} of registered presence devices."""
    if _DEVICES_FILE.exists():
        try:
            with open(_DEVICES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_devices(devices: dict[str, str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_DEVICES_FILE, "w") as f:
        json.dump(devices, f, indent=2)


def load_state() -> dict:
    """Return {address: {name, status, last_seen}} presence state."""
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_state(state: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
