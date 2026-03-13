from __future__ import annotations
import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_LABELS_FILE = _CONFIG_DIR / "labels.json"


def load() -> dict[str, str]:
    """Return {address: label} mapping from disk."""
    if _LABELS_FILE.exists():
        try:
            with open(_LABELS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save(labels: dict[str, str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_LABELS_FILE, "w") as f:
        json.dump(labels, f, indent=2)
