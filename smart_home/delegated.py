"""Outside web APIs (Google Places, Ecobee, Apple Push) are called through the
service delegator on this machine (github.com/priestc/service-delegator),
which logs every request's endpoint, status and time on its Services page.

The request itself is unchanged, API keys included. If the delegator can't be
reached, the request goes straight to the service instead (and the log says
so), so notifications and the thermostat never depend on it.
"""
from __future__ import annotations

import httpx

SERVICE_DELEGATOR_URL = "http://127.0.0.1:5055"

# Base URL -> the delegator's name for the service
_SERVICES = {
    "https://places.googleapis.com": "google-places",
    "https://api.ecobee.com": "ecobee",
    "https://api.push.apple.com": "apns",
    "https://api.sandbox.push.apple.com": "apns-sandbox",
}


def request(method: str, url: str, *, client: httpx.Client | None = None, **kwargs) -> httpx.Response:
    """Like httpx.request (or client.request), but through the delegator."""
    send = client.request if client is not None else httpx.request
    for base, service in _SERVICES.items():
        if url.startswith(base + "/"):
            proxied = f"{SERVICE_DELEGATOR_URL}/proxy/smart-home/{service}{url[len(base):]}"
            break
    else:
        return send(method, url, **kwargs)
    try:
        return send(method, proxied, **kwargs)
    except httpx.ConnectError as e:
        print(f"[delegated] service delegator unreachable ({e}); calling {base} directly", flush=True)
        return send(method, url, **kwargs)
