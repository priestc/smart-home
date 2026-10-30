"""Utility bills that arrive by email (Starlink, El Paso Electric, AWS, the
phone bill through PayPal...) are read from Gmail and added to /utilities
the same way photographed bills are.

A Gmail filter puts these emails under a label (normally "Billz"; Gmail reserves "Bills"). Every hour
the monitor lists the messages matching QUERY, reads the text of each new one
with the same "receipt-reader" model the photos use, and records it as a
kind='bill' row in receipts with source='email'.

Not every email from a company is a bill. The model sorts each into:
  charge   - money paid or about to be taken (receipt, autopay scheduled)
  bill     - a statement that says how much is owed
  reminder - a nudge about a bill that's already been sent
  offer    - a renewal quote, promotion or anything that isn't owed yet
  other    - not about money owed
Only charges and bills are recorded. A charge or bill with the same amount as
another one for the same utility within DUPLICATE_DAYS is the same payment
seen twice (a "bill ready" email and then its receipt) and is skipped too.
Every email that's looked at is kept in email_bills with what happened to it,
so skipped ones show on /utilities with the reason.

A payment that covers several months (car insurance every 6 months, a yearly
phone plan) has period_months set; /api/utilities/summary spreads it over
those months. When the email doesn't say, it's taken from the last bill from
the same company, then from DEFAULT_PERIOD_MONTHS.

Access is read-only (gmail.readonly). Set it up with `smart-home gmail-setup`.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import sqlite3
import threading
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import httpx

from smart_home import delegated, receipts

CONFIG_PATH = Path(os.path.expanduser("~/.config/smart-home/gmail.json"))
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
# Desktop OAuth clients accept any loopback port. Nothing listens there: the
# browser shows "can't connect" and the code is copied from its address bar.
REDIRECT_URI = "http://127.0.0.1:8765"
DEFAULT_QUERY = "label:Billz newer_than:1y"
CHECK_INTERVAL = datetime.timedelta(hours=1)
# Longest email text sent to the model; bill emails are mostly footer after this
MAX_TEXT = 12000
DUPLICATE_DAYS = 20
# An email still 'processing' after this was cut off (e.g. by a restart)
STALE_AFTER = datetime.timedelta(minutes=30)
DEFAULT_PERIOD_MONTHS = {"Insurance": 6}
RECORDED_TYPES = ("charge", "bill")

_SCHEMA = {
    "type": "object",
    "properties": {
        "email_type": {"type": "string", "enum": ["charge", "bill", "reminder", "offer", "other"]},
        "utility": {"type": ["string", "null"], "enum": receipts.UTILITIES + [None]},
        "merchant": {"type": ["string", "null"]},
        "bill_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "amount": {"type": ["number", "null"]},
        "period_months": {"type": ["integer", "null"]},
        "usage": {"type": ["number", "null"]},
        "usage_unit": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["email_type", "utility", "merchant", "bill_date", "amount", "period_months",
                 "usage", "usage_unit", "notes"],
    "additionalProperties": False,
}

_PROMPT = f"""This is an email from a utility or service provider. Extract it for a personal cost-of-living tracker.

- email_type, one of:
  - "charge": money was paid or is about to be taken: a payment receipt, "payment received", "you authorized $X", "payment scheduled" or autopay notice. An "automatic payment reminder" or "scheduled payment reminder" saying an amount will be charged automatically is a charge, not a reminder.
  - "bill": a statement, invoice or "your bill is ready" that gives the amount owed for a period.
  - "reminder": a reminder to pay a bill that was already sent, a past-due notice, or "payment due soon", when nothing will be charged automatically.
  - "offer": a renewal quote, price offer, promotion or anything not yet owed.
  - "other": anything else (outage, newsletter, password reset, usage alert with no amount owed).
- utility: one of {", ".join(receipts.UTILITIES)}. Car, home or renters insurance is "Insurance". Cloud servers, web hosting and software subscriptions are "Hosting". A cell phone plan is "Phone".
- merchant: the company providing the service as a person would say it (e.g. "Starlink", "El Paso Electric"). When a payment service such as PayPal sends the email, use the merchant it was paid to (e.g. "Visible"), not PayPal.
- bill_date: the payment, statement or billing date as YYYY-MM-DD. Use the email's date if none is given.
- amount: the amount for this period in dollars. If several payment options are shown (pay in full, installments, minimum payment), use the pay-in-full amount. Not any past-due balance.
- period_months: how many months this amount covers, if the email says (e.g. a statement period of 10/12-11/11 is 1, a 6-month policy is 6, an annual plan is 12), otherwise null.
- usage and usage_unit: the amount used if printed (e.g. 812 kWh gives 812, "kWh"), otherwise null for both.
- notes: anything uncertain, otherwise null."""


class GmailError(Exception):
    pass


def load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(0o600)


# ---- OAuth -------------------------------------------------------------------

def auth_url(client_id: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent: always hand back a refresh token
        "access_type": "offline",
        "prompt": "consent",
    })


def code_from_redirect(pasted: str) -> str:
    """The code from the address the browser was sent to (or the bare code)."""
    pasted = pasted.strip()
    if "://" not in pasted:
        return pasted
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if "error" in qs:
        raise GmailError(f"Google refused access: {qs['error'][0]}")
    if "code" not in qs:
        raise GmailError("That address has no code= in it")
    return qs["code"][0]


def _token_request(data: dict) -> dict:
    try:
        resp = httpx.post(TOKEN_URL, data=data, timeout=30)
    except httpx.HTTPError as e:
        raise GmailError(f"Could not reach Google: {e}")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200:
        if body.get("error") == "invalid_grant":
            raise GmailError("Gmail access has expired or was revoked. Run `smart-home gmail-setup` "
                             "on tank2 again. (If this happens weekly, the Google Cloud app is still "
                             "in Testing mode; publish it.)")
        raise GmailError(f"Google token request failed (HTTP {resp.status_code}): "
                         f"{body.get('error_description') or body.get('error') or resp.text[:200]}")
    return body


def exchange_code(client_id: str, client_secret: str, code: str) -> str:
    """Trade the one-time code for a refresh token."""
    body = _token_request({"grant_type": "authorization_code", "code": code, "client_id": client_id,
                           "client_secret": client_secret, "redirect_uri": REDIRECT_URI})
    if not body.get("refresh_token"):
        raise GmailError("Google didn't return a refresh token")
    return body["refresh_token"]


_access = {"token": None, "expires": datetime.datetime.min}
_access_lock = threading.Lock()


def _access_token(cfg: dict) -> str:
    with _access_lock:
        if _access["token"] and datetime.datetime.now() < _access["expires"]:
            return _access["token"]
        body = _token_request({"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"],
                               "client_id": cfg["client_id"], "client_secret": cfg["client_secret"]})
        _access["token"] = body["access_token"]
        _access["expires"] = datetime.datetime.now() + datetime.timedelta(seconds=body.get("expires_in", 3600) - 120)
        return _access["token"]


def _api(cfg: dict, path: str, **params) -> dict:
    try:
        resp = httpx.get(f"{API_BASE}{path}", params=params, timeout=60,
                         headers={"Authorization": f"Bearer {_access_token(cfg)}"})
    except httpx.HTTPError as e:
        raise GmailError(f"Could not reach Gmail: {e}")
    if resp.status_code != 200:
        try:
            msg = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = resp.text[:200]
        raise GmailError(f"Gmail API {path} failed (HTTP {resp.status_code}): {msg}")
    return resp.json()


def list_message_ids(cfg: dict, query: str) -> list[str]:
    ids, page = [], None
    while True:
        params = {"q": query, "maxResults": 100}
        if page:
            params["pageToken"] = page
        body = _api(cfg, "/messages", **params)
        ids += [m["id"] for m in body.get("messages", [])]
        page = body.get("nextPageToken")
        if not page or len(ids) >= 500:
            return ids


# ---- Message text --------------------------------------------------------------

class _HTMLText(HTMLParser):
    """Visible text of an HTML email, one line per block or table cell."""
    _BLOCK = {"p", "div", "br", "tr", "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    _SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (re.sub(r"[ \t ‌​]+", " ", ln).strip() for ln in "".join(self.parts).splitlines())
        return "\n".join(ln for ln in lines if ln)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _parts(payload: dict):
    yield payload
    for p in payload.get("parts") or []:
        yield from _parts(p)


def message_text(msg: dict) -> tuple[dict, str]:
    """(headers, body text) of a message from messages.get?format=full. The
    HTML part is preferred: some senders' plain-text part leaves out the amounts."""
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    html = plain = None
    for p in _parts(payload):
        data = (p.get("body") or {}).get("data")
        if not data:
            continue
        if p.get("mimeType") == "text/html" and html is None:
            parser = _HTMLText()
            parser.feed(_decode(data))
            html = parser.text()
        elif p.get("mimeType") == "text/plain" and plain is None:
            plain = _decode(data).strip()
    return headers, html or plain or msg.get("snippet") or ""


# ---- Reading and storing ---------------------------------------------------------

def _extract(header_text: str, text: str) -> dict:
    try:
        resp = httpx.post(
            f"{delegated.SERVICE_DELEGATOR_URL}/api/ask",
            json={
                "task": receipts.AI_TASK,
                "project": "smart-home",
                "prompt": f"{_PROMPT}\n\n---\n{header_text}\n\n{text[:MAX_TEXT]}",
                "json_schema": _SCHEMA,
            },
            timeout=900.0,
        )
    except httpx.HTTPError as e:
        raise GmailError(f"Could not reach the service delegator at {delegated.SERVICE_DELEGATOR_URL}: {e}")
    try:
        body = resp.json()
    except ValueError:
        raise GmailError(f"Service delegator returned HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code != 200:
        notes = "; ".join(body.get("notes") or [])
        raise GmailError((body.get("error") or f"Service delegator returned HTTP {resp.status_code}")
                         + (f" ({notes})" if notes else ""))
    try:
        return json.loads(body.get("text") or "")
    except json.JSONDecodeError:
        raise GmailError(f"Unparseable response from {body.get('model')}: {(body.get('text') or '')[:200]}")


def _header_text(row: sqlite3.Row | dict) -> str:
    return f"From: {row['sender']}\nSubject: {row['subject']}\nDate: {row['received_at']}"


def _period_months(conn: sqlite3.Connection, data: dict, utility: str, merchant: str | None) -> int | None:
    n = data.get("period_months")
    if isinstance(n, int) and 1 <= n <= 24:
        return None if n == 1 else n
    if merchant:
        row = conn.execute(
            "SELECT period_months FROM receipts WHERE kind='bill' AND status='done' AND lower(merchant)=lower(?) "
            "ORDER BY COALESCE(purchase_date, substr(uploaded_at, 1, 10)) DESC LIMIT 1", (merchant,),
        ).fetchone()
        if row:
            return row[0]
    return DEFAULT_PERIOD_MONTHS.get(utility)


def _duplicate_of(conn: sqlite3.Connection, utility: str, amount: float, date: str, exclude: int | None) -> int | None:
    row = conn.execute(
        "SELECT id FROM receipts WHERE kind='bill' AND status='done' AND utility=? AND abs(total - ?) < 0.005 "
        "AND abs(julianday(COALESCE(purchase_date, substr(uploaded_at, 1, 10))) - julianday(?)) <= ? "
        "AND id IS NOT ? ORDER BY id LIMIT 1",
        (utility, amount, date, DUPLICATE_DAYS, exclude),
    ).fetchone()
    return row[0] if row else None


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_email(conn: sqlite3.Connection, message_id: str) -> None:
    """Read one stored email and record the outcome on its email_bills row,
    adding or updating its bill in receipts. Raises on failure (also recorded)."""
    em = conn.execute("SELECT * FROM email_bills WHERE message_id=?", (message_id,)).fetchone()
    try:
        data = _extract(_header_text(em), em["body_text"])
        kind = data.get("email_type")
        utility = data.get("utility") if data.get("utility") in receipts.UTILITIES else "Other"
        merchant = (data.get("merchant") or "").strip() or None
        amount = data.get("amount")
        date = data.get("bill_date")
        try:
            datetime.date.fromisoformat(date or "")
        except ValueError:
            date = em["received_at"][:10]
        rid = em["receipt_id"]
        if rid is not None and not conn.execute("SELECT 1 FROM receipts WHERE id=?", (rid,)).fetchone():
            rid = None  # its bill was deleted
        outcome, reason = "skipped", None
        if kind not in RECORDED_TYPES:
            reason = {"reminder": "A reminder about a bill, not a new charge",
                      "offer": "An offer or quote, not money owed"}.get(kind, "Not a bill or payment")
        elif amount is None:
            reason = "No amount found in the email"
        else:
            amount = round(float(amount), 2)
            dup = _duplicate_of(conn, utility, amount, date, rid)
            if dup:
                reason = f"Same amount as bill #{dup} within {DUPLICATE_DAYS} days; counted once"
            else:
                outcome = "imported"
        if outcome != "imported":
            # Read again and it's no longer a bill: drop the one it made before
            if rid is not None:
                conn.execute("DELETE FROM receipts WHERE id=?", (rid,))
                rid = None
        else:
            usage, unit = data.get("usage"), (data.get("usage_unit") or "").strip() or None
            if usage is None or unit is None:
                usage = unit = None
            fields = dict(status="done", error=None, processed_at=_now(), merchant=merchant,
                          purchase_date=date, total=amount, notes=data.get("notes"), raw_json=json.dumps(data),
                          kind="bill", utility=utility, usage_amount=usage, usage_unit=unit,
                          period_months=_period_months(conn, data, utility, merchant))
            if rid is None:
                fields.update(uploaded_at=_now(), image_path="", media_type="", source="email", email_id=message_id)
                cols = ", ".join(fields)
                rid = conn.execute(f"INSERT INTO receipts ({cols}) VALUES ({', '.join('?' * len(fields))})",
                                   tuple(fields.values())).lastrowid
            else:
                conn.execute(f"UPDATE receipts SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                             (*fields.values(), rid))
        conn.execute(
            "UPDATE email_bills SET status=?, reason=?, email_type=?, receipt_id=?, processed_at=? "
            "WHERE message_id=?",
            (outcome, reason, kind, rid, _now(), message_id),
        )
    except Exception as e:
        msg = str(e) if isinstance(e, GmailError) else f"{type(e).__name__}: {e}"
        conn.execute("UPDATE email_bills SET status='error', reason=?, processed_at=? WHERE message_id=?",
                     (msg, _now(), message_id))
        if em["receipt_id"] is not None:
            conn.execute("UPDATE receipts SET status='error', error=?, processed_at=? WHERE id=?",
                         (msg, _now(), em["receipt_id"]))
        raise
    finally:
        conn.commit()


_check_lock = threading.Lock()


def is_checking() -> bool:
    return _check_lock.locked()


def check(db_path: str) -> dict:
    """List the labelled emails, fetch and read the new ones. Records the run
    in email_bill_checks; raises GmailError if Gmail itself can't be read."""
    cfg = load_config()
    if not cfg:
        raise GmailError("Gmail isn't connected. Run `smart-home gmail-setup` on tank2.")
    if not _check_lock.acquire(blocking=False):
        raise GmailError("Already checking email")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    started = _now()
    result = {"n_found": 0, "n_new": 0, "n_imported": 0, "n_errors": 0}
    try:
        try:
            ids = list_message_ids(cfg, cfg.get("query") or DEFAULT_QUERY)
            result["n_found"] = len(ids)
            seen = {r[0] for r in conn.execute("SELECT message_id FROM email_bills")}
            # Reading cut off by a restart: read those again first
            stale = (datetime.datetime.now() - STALE_AFTER).strftime("%Y-%m-%d %H:%M:%S")
            for (mid,) in conn.execute("SELECT message_id FROM email_bills WHERE status='processing' "
                                       "AND processed_at < ?", (stale,)).fetchall():
                try:
                    read_email(conn, mid)
                except Exception:
                    result["n_errors"] += 1  # recorded on the email's row
            # Oldest first, so a later duplicate is the one that's skipped
            for mid in reversed([i for i in ids if i not in seen]):
                msg = _api(cfg, f"/messages/{mid}", format="full")
                headers, text = message_text(msg)
                received = datetime.datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000)
                claimed = conn.execute(
                    "INSERT OR IGNORE INTO email_bills (message_id, received_at, sender, subject, body_text, "
                    "status, processed_at) VALUES (?, ?, ?, ?, ?, 'processing', ?)",
                    (mid, received.strftime("%Y-%m-%d %H:%M:%S"), headers.get("from", ""),
                     headers.get("subject", ""), text, _now()),
                ).rowcount
                conn.commit()
                if not claimed:
                    continue  # the web server's "Check now" got to it first
                result["n_new"] += 1
                try:
                    read_email(conn, mid)
                except Exception:
                    result["n_errors"] += 1  # recorded on the email's row
                    continue
                if conn.execute("SELECT status FROM email_bills WHERE message_id=?", (mid,)).fetchone()[0] == "imported":
                    result["n_imported"] += 1
        except Exception as e:
            msg = str(e) if isinstance(e, GmailError) else f"{type(e).__name__}: {e}"
            conn.execute("INSERT INTO email_bill_checks (ts, ok, error, n_new, n_imported) VALUES (?, 0, ?, ?, ?)",
                         (started, msg, result["n_new"], result["n_imported"]))
            conn.commit()
            raise GmailError(msg) from e
        conn.execute("INSERT INTO email_bill_checks (ts, ok, error, n_new, n_imported) VALUES (?, 1, ?, ?, ?)",
                     (started, f"{result['n_errors']} email(s) couldn't be read" if result["n_errors"] else None,
                      result["n_new"], result["n_imported"]))
        conn.commit()
        return result
    finally:
        conn.close()
        _check_lock.release()


def check_due(conn: sqlite3.Connection) -> bool:
    if not CONFIG_PATH.exists():
        return False
    row = conn.execute("SELECT MAX(ts) FROM email_bill_checks").fetchone()
    if not row or not row[0]:
        return True
    return datetime.datetime.now() - datetime.datetime.fromisoformat(row[0]) >= CHECK_INTERVAL


_rereading: set[str] = set()


def is_rereading(message_id: str) -> bool:
    return message_id in _rereading


def reread_in_background(db_path: str, message_id: str) -> None:
    _rereading.add(message_id)

    def _bg():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            read_email(conn, message_id)
        except Exception as e:
            print(f"Re-reading email {message_id} failed: {e}", flush=True)  # also on its row
        finally:
            conn.close()
            _rereading.discard(message_id)

    threading.Thread(target=_bg, daemon=True).start()


def check_in_background(db_path: str) -> None:
    def _bg():
        try:
            check(db_path)
        except Exception as e:
            print(f"Email bill check failed: {e}", flush=True)  # also in email_bill_checks

    threading.Thread(target=_bg, daemon=True).start()
