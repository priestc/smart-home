from __future__ import annotations
import asyncio
import json
import socket
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


def local_subnet() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


async def _probe_tasmota(client, ip: str) -> dict | None:
    try:
        r = await client.get(f"http://{ip}/cm", params={"cmnd": "Status"}, timeout=1.5)
        if r.status_code == 200:
            data = r.json()
            if "Status" in data:
                status = data["Status"]
                return {
                    "ip": ip,
                    "friendly_name": (status.get("FriendlyName") or [""])[0],
                    "topic": status.get("Topic", ""),
                }
    except Exception:
        pass
    return None


async def _scan(subnet: str) -> list[dict]:
    import httpx
    limits = httpx.Limits(max_connections=254, max_keepalive_connections=0)
    async with httpx.AsyncClient(limits=limits) as client:
        results = await asyncio.gather(*[
            _probe_tasmota(client, f"{subnet}.{i}") for i in range(1, 255)
        ])
    return [r for r in results if r is not None]


def discover(subnet: str | None = None) -> list[dict]:
    """Scan the local /24 subnet and return info dicts for every Tasmota device found."""
    if subnet is None:
        subnet = local_subnet()
    return asyncio.run(_scan(subnet))


def fetch_reading(ip: str) -> dict:
    """Fetch current power reading from a Tasmota device.

    Returns dict with keys: watts, volts, amps, energy_wh, power_factor, is_on.
    """
    import httpx
    r = httpx.get(f"http://{ip}/cm", params={"cmnd": "Status 8"}, timeout=5)
    r.raise_for_status()
    data = r.json()
    energy = data.get("StatusSNS", {}).get("ENERGY", {})
    return {
        "watts":        energy.get("Power"),
        "volts":        energy.get("Voltage"),
        "amps":         energy.get("Current"),
        "energy_wh":    energy.get("Total", 0) * 1000 if energy.get("Total") is not None else None,
        "power_factor": energy.get("Factor"),
        "is_on":        energy.get("Power", 0) > 0,
    }
