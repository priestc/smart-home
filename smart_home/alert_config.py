from __future__ import annotations
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_ALERT_CONFIG_FILE = _CONFIG_DIR / "alert_config.json"


def _load() -> dict:
    if _ALERT_CONFIG_FILE.exists():
        try:
            with open(_ALERT_CONFIG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save(config: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_ALERT_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_suppressed_offline() -> set[str]:
    """Return set of labels for which offline push alerts are suppressed."""
    return set(_load().get("suppress_offline", []))


def set_suppressed_offline(labels: set[str]) -> None:
    config = _load()
    config["suppress_offline"] = sorted(labels)
    _save(config)
