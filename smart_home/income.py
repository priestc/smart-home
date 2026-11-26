"""Income read from the local email archive (email_archive.py).

Each rule picks out one kind of income email by sender and subject and reads
the amount from it, e.g. Coinbase's weekly "You earned $46.81 in USDC
rewards". scan() looks at every archived message the rules match and adds
any it hasn't seen to the income table (one row per email, so re-scanning
never double counts). It runs after each archive sync, and from /income.
Income can also be entered by hand (email_id NULL).
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import threading

from smart_home import email_archive

# sender_like / subject_like narrow the archive with SQL LIKE; subject_re
# then reads the amount (group "amount"). A matching email whose amount
# can't be read is listed as unreadable on /income, not skipped silently.
RULES = [
    {
        "source": "Coinbase USDC rewards",
        "sender_like": "%coinbase.com%",
        "subject_like": "%USDC%reward%",
        # "You earned $46.81 in USDC rewards", "You received $112.70 in USDC
        # rewards", "You received 516.62 USDC rewards", "... 500.94 USDC in rewards"
        "subject_re": re.compile(r"You (?:earned|received) \$?(?P<amount>[\d,]+(?:\.\d+)?) (?:in )?USDC (?:in )?rewards",
                                 re.I),
        # Only these subjects are reward payouts (not "Update to your USDC rewards rate")
        "payout_re": re.compile(r"^\s*Congrats!?\s+You (?:earned|received)", re.I),
        # "Payout date: Nov 26, 2026" in the body; otherwise the day it arrived
        "date_re": re.compile(r"Payout date:\s*(?:<[^>]+>\s*)*([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})"),
    },
]

_lock = threading.Lock()


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _payout_date(rule: dict, message_id: str, db_path: str, fallback: str) -> str:
    found = email_archive.get_message(db_path, message_id)
    if found:
        body = email_archive.body_text(email_archive.parse(found[1]))
        m = rule["date_re"].search(body)
        if m:
            text = m.group(1).replace(".", "")
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    return datetime.datetime.strptime(text, fmt).date().isoformat()
                except ValueError:
                    pass
    return fallback


def scan(db_path: str) -> dict:
    """Add income from archived emails not read yet. Returns {n_new,
    unreadable: [{message_id, received_at, subject}]}; also saved as the
    setting income_scan, with when it ran, for /income to show."""
    with _lock:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            seen = {r[0] for r in conn.execute("SELECT email_id FROM income WHERE email_id IS NOT NULL")}
            index = email_archive.open_index(db_path)
            n_new, unreadable = 0, []
            try:
                for rule in RULES:
                    rows = index.execute(
                        "SELECT message_id, received_at, subject FROM messages "
                        "WHERE sender LIKE ? AND subject LIKE ? AND deleted_at IS NULL ORDER BY received_at",
                        (rule["sender_like"], rule["subject_like"])).fetchall()
                    for r in rows:
                        if not rule["payout_re"].search(r["subject"]):
                            continue
                        m = rule["subject_re"].search(r["subject"])
                        if not m:
                            unreadable.append({"message_id": r["message_id"], "received_at": r["received_at"],
                                               "subject": r["subject"], "source": rule["source"]})
                            continue
                        if r["message_id"] in seen:
                            continue
                        day = _payout_date(rule, r["message_id"], db_path, r["received_at"][:10])
                        conn.execute(
                            "INSERT INTO income (received_on, source, amount, email_id, subject, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (day, rule["source"], float(m.group("amount").replace(",", "")),
                             r["message_id"], r["subject"], _now()))
                        n_new += 1
            finally:
                index.close()
            result = {"ok": True, "ts": _now(), "n_new": n_new, "unreadable": unreadable}
        except Exception as e:
            result = {"ok": False, "ts": _now(), "error": f"{type(e).__name__}: {e}", "n_new": 0, "unreadable": []}
        conn.execute("INSERT INTO settings (key, value) VALUES ('income_scan', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(result),))
        conn.commit()
        conn.close()
        return result


def last_scan(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key='income_scan'").fetchone()
    return json.loads(row[0]) if row else None
