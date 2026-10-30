from __future__ import annotations
import json
import os
from pathlib import Path

import httpx

from smart_home import delegated

from smart_home.decoder import Reading

CONFIG_PATH = Path(os.path.expanduser("~/.config/smart-home/ecobee.json"))
API_BASE = "https://api.ecobee.com"


def load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def request_pin(api_key: str) -> dict:
    """Step 1 of PIN auth: request a PIN and auth code from Ecobee."""
    resp = delegated.request(
        "GET", f"{API_BASE}/authorize",
        params={"response_type": "ecobeePin", "client_id": api_key, "scope": "smartRead"},
    )
    resp.raise_for_status()
    return resp.json()


def authorize(api_key: str, code: str) -> dict:
    """Step 2 of PIN auth: exchange the auth code for access/refresh tokens."""
    resp = delegated.request(
        "POST", f"{API_BASE}/token",
        params={"grant_type": "ecobeePin", "code": code, "client_id": api_key},
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(cfg: dict) -> dict:
    """Refresh the access token. Saves updated tokens and returns the new cfg."""
    resp = delegated.request(
        "POST", f"{API_BASE}/token",
        params={
            "grant_type": "refresh_token",
            "code": cfg["refresh_token"],
            "client_id": cfg["api_key"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    cfg = {**cfg, "access_token": data["access_token"], "refresh_token": data["refresh_token"]}
    save_config(cfg)
    return cfg


def _get_thermostat_data(cfg: dict) -> dict:
    selection = json.dumps({
        "selection": {
            "selectionType": "registered",
            "selectionMatch": "",
            "includeRuntime": True,
        }
    })
    resp = delegated.request(
        "GET", f"{API_BASE}/1/thermostat",
        params={"json": selection},
        headers={"Authorization": f"Bearer {cfg['access_token']}"},
    )
    resp.raise_for_status()
    return resp.json()


def get_thermostats(cfg: dict) -> list[dict]:
    """Return a list of registered thermostats with 'identifier' and 'name'."""
    data = _get_thermostat_data(cfg)
    return [
        {"identifier": t["identifier"], "name": t["name"]}
        for t in data.get("thermostatList", [])
    ]


def fetch_reading(cfg: dict) -> tuple[Reading, dict]:
    """Fetch the current temperature/humidity from the configured Ecobee thermostat.

    Returns (Reading, cfg) — cfg may be updated if the access token was refreshed.
    """
    try:
        data = _get_thermostat_data(cfg)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            cfg = refresh_access_token(cfg)
            data = _get_thermostat_data(cfg)
        else:
            raise

    thermostat = data["thermostatList"][0]
    runtime = thermostat["runtime"]
    # Ecobee reports temperature as integer 1/10 °F (e.g. 723 = 72.3 °F)
    temp_f = runtime["actualTemperature"] / 10.0
    temp_c = (temp_f - 32) * 5 / 9
    humidity = float(runtime["actualHumidity"])

    reading = Reading(
        address=thermostat["identifier"],
        name="Ecobee",
        temp_c=temp_c,
        humidity=humidity,
        battery=None,
        rssi=None,
        label=cfg["label"],
    )
    return reading, cfg
