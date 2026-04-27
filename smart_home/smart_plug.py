from __future__ import annotations
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_PLUGS_FILE = _CONFIG_DIR / "smart_plugs.json"


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
