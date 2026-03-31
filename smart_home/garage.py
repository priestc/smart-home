from __future__ import annotations
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_GARAGES_FILE = _CONFIG_DIR / "garages.json"


def load_config() -> list[dict]:
    if _GARAGES_FILE.exists():
        try:
            with open(_GARAGES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_config(garages: list[dict]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_GARAGES_FILE, "w") as f:
        json.dump(garages, f, indent=2)


def get_status(ip: str) -> dict:
    """Return Shelly Gen3 Switch.GetStatus dict, e.g. {'output': False, 'apower': 0.0, ...}"""
    import httpx
    resp = httpx.get(f"http://{ip}/rpc/Switch.GetStatus?id=0", timeout=5)
    resp.raise_for_status()
    return resp.json()


def trigger(ip: str, pulse_seconds: float = 0.5) -> None:
    """Momentarily close the switch to press the garage door button.

    Uses Shelly's built-in toggle_after parameter so the switch turns itself
    back off automatically — no second HTTP call needed.
    """
    import httpx
    resp = httpx.get(
        f"http://{ip}/rpc/Switch.Set",
        params={"id": 0, "on": "true", "toggle_after": pulse_seconds},
        timeout=5,
    )
    resp.raise_for_status()
