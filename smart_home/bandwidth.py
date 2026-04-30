from __future__ import annotations
import json
import secrets
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_MONITORS_FILE = _CONFIG_DIR / "bandwidth_monitors.json"


def load_config() -> list[dict]:
    if _MONITORS_FILE.exists():
        try:
            with open(_MONITORS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_config(monitors: list[dict]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_MONITORS_FILE, "w") as f:
        json.dump(monitors, f, indent=2)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def find_monitor_by_token(token: str) -> dict | None:
    for monitor in load_config():
        if monitor.get("token") == token:
            return monitor
    return None
