from __future__ import annotations
import json
import time
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_CREDS_FILE = _CONFIG_DIR / "apns_credentials.json"
_TOKENS_FILE = _CONFIG_DIR / "push_tokens.json"

APNS_PROD_HOST = "https://api.push.apple.com"
APNS_DEV_HOST  = "https://api.sandbox.push.apple.com"


def load_credentials() -> dict:
    if _CREDS_FILE.exists():
        try:
            with open(_CREDS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_credentials(creds: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CREDS_FILE, "w") as f:
        json.dump(creds, f, indent=2)


def load_tokens() -> list[str]:
    if _TOKENS_FILE.exists():
        try:
            with open(_TOKENS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_tokens(tokens: list[str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def register_token(token: str) -> None:
    """Add a device token if not already registered."""
    tokens = load_tokens()
    if token not in tokens:
        tokens.append(token)
        save_tokens(tokens)


def _make_jwt(key_file: str, key_id: str, team_id: str) -> str:
    try:
        import jwt
    except ImportError:
        raise RuntimeError("PyJWT not installed. Run: pip install 'PyJWT[crypto]'")
    with open(key_file) as f:
        private_key = f.read()
    payload = {"iss": team_id, "iat": int(time.time())}
    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})


def send_notification(title: str, body: str) -> None:
    """Send an APNs push notification to all registered devices."""
    creds = load_credentials()
    tokens = load_tokens()
    if not creds or not tokens:
        print(f"[push] skipped '{title}': no creds={bool(creds)} tokens={len(tokens)}")
        return
    required = {"key_file", "key_id", "team_id", "bundle_id"}
    if not required.issubset(creds):
        print(f"[push] skipped '{title}': missing keys {required - creds.keys()}")
        return

    try:
        import httpx
    except ImportError:
        print("[push] httpx not installed. Run: pip install 'httpx[http2]'")
        return

    try:
        bearer = _make_jwt(creds["key_file"], creds["key_id"], creds["team_id"])
    except Exception as e:
        print(f"[push] JWT error: {e}")
        return

    host = APNS_DEV_HOST if creds.get("sandbox") else APNS_PROD_HOST
    print(f"[push] sending '{title}' via {'sandbox' if creds.get('sandbox') else 'production'} to {len(tokens)} device(s)")
    dead: list[str] = []

    with httpx.Client(http2=True) as client:
        for token in tokens:
            try:
                resp = client.post(
                    f"{host}/3/device/{token}",
                    headers={
                        "authorization": f"bearer {bearer}",
                        "apns-topic": creds["bundle_id"],
                        "apns-push-type": "alert",
                        "apns-priority": "10",
                    },
                    json={"aps": {"alert": {"title": title, "body": body}, "sound": "default"}},
                    timeout=10,
                )
                if resp.status_code == 200:
                    print(f"[push] OK: '{title}'")
                elif resp.status_code == 410:    # token expired / unregistered
                    print(f"[push] token expired, removing")
                    dead.append(token)
                else:
                    print(f"[push] APNs {resp.status_code}: {resp.text}")
            except Exception as e:
                print(f"[push] send error: {e}")

    if dead:
        save_tokens([t for t in tokens if t not in dead])
