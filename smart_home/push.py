from __future__ import annotations
import json
import time
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_CREDS_FILE = _CONFIG_DIR / "apns_credentials.json"
_TOKENS_FILE = _CONFIG_DIR / "push_tokens.json"
# Which APNs server each token belongs to: "sandbox" for a build run from
# Xcode, "production" for TestFlight / App Store builds. Learned on send.
_TOKEN_ENVS_FILE = _CONFIG_DIR / "push_token_envs.json"

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


def _load_token_envs() -> dict[str, str]:
    try:
        with open(_TOKEN_ENVS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[push] ignoring unreadable {_TOKEN_ENVS_FILE}: {e}")
        return {}


def _save_token_envs(envs: dict[str, str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_TOKEN_ENVS_FILE, "w") as f:
        json.dump(envs, f, indent=2)


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


def send_notification(title: str, body: str) -> bool:
    """Send an APNs push notification to all registered devices.

    Returns True if at least one device accepted it.
    """
    creds = load_credentials()
    tokens = load_tokens()
    if not creds or not tokens:
        print(f"[push] skipped '{title}': no creds={bool(creds)} tokens={len(tokens)}")
        return False
    required = {"key_file", "key_id", "team_id", "bundle_id"}
    if not required.issubset(creds):
        print(f"[push] skipped '{title}': missing keys {required - creds.keys()}")
        return False

    try:
        import httpx
        from smart_home import delegated
    except ImportError:
        print("[push] httpx not installed. Run: pip install 'httpx[http2]'")
        return False

    try:
        bearer = _make_jwt(creds["key_file"], creds["key_id"], creds["team_id"])
    except Exception as e:
        print(f"[push] JWT error: {e}")
        return False

    hosts = {"sandbox": APNS_DEV_HOST, "production": APNS_PROD_HOST}
    default_env = "sandbox" if creds.get("sandbox") else "production"
    envs = _load_token_envs()
    envs_before = dict(envs)
    print(f"[push] sending '{title}' to {len(tokens)} device(s)")
    dead: list[str] = []
    delivered = False

    with httpx.Client(http2=True) as client:
        for token in tokens:
            try:
                # A token only works on the server its build was signed for; if
                # APNs says it's the wrong one, try the other and remember it.
                env = envs.get(token, default_env)
                for attempt_env in (env, "production" if env == "sandbox" else "sandbox"):
                    resp = delegated.request(
                        "POST", f"{hosts[attempt_env]}/3/device/{token}", client=client,
                        headers={
                            "authorization": f"bearer {bearer}",
                            "apns-topic": creds["bundle_id"],
                            "apns-push-type": "alert",
                            "apns-priority": "10",
                        },
                        json={"aps": {"alert": {"title": title, "body": body}, "sound": "default"}},
                        timeout=10,
                    )
                    if not (resp.status_code == 400 and "BadDeviceToken" in resp.text):
                        break
                    print(f"[push] token rejected by {attempt_env} APNs (BadDeviceToken)")
                if resp.status_code == 200:
                    envs[token] = attempt_env
                    print(f"[push] OK: '{title}'")
                    delivered = True
                elif resp.status_code == 410:    # token expired / unregistered
                    print(f"[push] token expired, removing")
                    dead.append(token)
                else:
                    print(f"[push] APNs {resp.status_code}: {resp.text}")
            except Exception as e:
                print(f"[push] send error: {e}")

    if dead:
        save_tokens([t for t in tokens if t not in dead])
        for t in dead:
            envs.pop(t, None)
    if envs != envs_before:
        _save_token_envs(envs)
    return delivered
