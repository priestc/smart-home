"""Amazon orders read from the local email archive (email_archive.py) and
added to receipts, like a photographed store receipt.

Amazon sends an "Ordered: ..." confirmation from auto-confirm@amazon.com for
each checkout. The email is read as text, without the model:
  - Until about August 2026 it lists every item: its name (cut off after ~33
    characters), "Quantity: N" and the line's price, printed with the cents
    as superscript ("$5219" is $52.19), then the Grand Total.
  - Since then it only says "4 items: 1 Small Appliances, 1 Electronics, and
    2 others" and the Grand Total. Such an order becomes one line for the
    whole amount, named after that summary; it can be split up by hand.
One email can cover several orders, each with its own Order # and usually
its own total: each total is a receipt (orders printed under one total share
it). The order numbers are in its notes. Item prices don't include tax; the
rest of the total is the receipt's tax (shipping, when charged, is in it
too). Older emails say "Total" instead of "Grand Total".

Only the categories need the model: each line's name is sent, as text, to
the same "receipt-reader" task photos use. If that fails, the email is
tried again at the next scan; the error shows on /receipts.

Every email looked at is kept in amazon_emails with what happened to it, so
a receipt deleted by hand isn't brought back. Receipts point to their email
by email_id, with source='amazon'. scan() runs after each archive
sync, and from /receipts.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import threading

import httpx

from smart_home import db, delegated, email_archive, gmail, receipts

SENDER_LIKE = "%auto-confirm@amazon.com%"
SUBJECT_LIKE = "Ordered%"
# Orders before this aren't imported (receipt tracking began in 2025)
START_DATE = "2025-01-01"
MERCHANT = "Amazon"
# An email still 'processing' after this was cut off (e.g. by a restart)
STALE_AFTER = datetime.timedelta(minutes=30)

_ORDER_RE = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")
_QTY_RE = re.compile(r"^Quantity:\s*(\d+)$", re.I)
_PRICE_RE = re.compile(r"^\$\s?([\d,]+)(?:\.(\d{2}))?$")
_TOTAL_RE = re.compile(r"^(?:(?:Grand|Order) )?Total:?\s*(\$[\d,.]+)?$", re.I)
_SUMMARY_RE = re.compile(r"^\d+ items?: .+")
# Invisible characters Amazon puts around order numbers and counts
_INVISIBLE_RE = re.compile(r"[‎‏‪-‮⁦-⁩͏­]")

_SCHEMA = {
    "type": "object",
    "properties": {
        # One per line, numbered as in the prompt: a bare list of categories
        # drifted out of step with the lines
        "lines": {"type": "array", "items": {
            "type": "object",
            "properties": {"line": {"type": "integer"}, "category": {"type": "string", "enum": receipts.CATEGORIES}},
            "required": ["line", "category"],
            "additionalProperties": False,
        }},
    },
    "required": ["lines"],
    "additionalProperties": False,
}


class AmazonError(Exception):
    pass


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _price(line: str) -> float | None:
    """'$5219' (superscript cents) or '$52.19' -> 52.19."""
    m = _PRICE_RE.match(line)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    if m.group(2) is not None:
        return float(f"{whole}.{m.group(2)}")
    return int(whole) / 100 if len(whole) >= 3 else None


def _html_lines(msg) -> list[str]:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            p = gmail._HTMLText()
            p.feed(email_archive._part_text(part))
            lines = (_INVISIBLE_RE.sub("", ln).strip() for ln in p.text().splitlines())
            return [ln for ln in lines if ln]
    return []


def parse(msg) -> list[dict]:
    """The orders in the email, one per printed total: [{orders, total,
    items: [{name, quantity, price}], summaries}]. price is the line's total
    (the email prints the price of one). Orders printed without a total of
    their own are counted in the next one. Raises AmazonError if nothing
    can be read."""
    lines = _html_lines(msg)
    if not lines:
        raise AmazonError("The email has no HTML part to read")
    # The newest emails print an order's item summary just above its Order #
    out, cur, pending = [], None, []
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ln.startswith("Order #"):
            m = _ORDER_RE.search(ln) or _ORDER_RE.search(nxt)
            if cur is None:
                cur, pending = {"orders": [], "items": [], "summaries": pending}, []
            if m and m.group(1) not in cur["orders"]:
                cur["orders"].append(m.group(1))
        elif _SUMMARY_RE.match(ln):
            (cur["summaries"] if cur else pending).append(ln)
        elif cur is None:
            continue  # before the first order, or suggested products after the last
        elif (q := _QTY_RE.match(ln)) and i > 0:
            each = _price(nxt)
            if each is None:
                raise AmazonError(f"No price after {lines[i - 1]!r} ({ln})")
            qty = int(q.group(1))
            cur["items"].append({"name": lines[i - 1].rstrip(". \u2026").strip(), "quantity": qty,
                                 "price": round(each * max(qty, 1), 2)})
        elif t := _TOTAL_RE.match(ln):
            text = (t.group(1) or nxt).replace(" ", "")
            total = _price(text)
            if total is None:
                raise AmazonError(f"Couldn't read the total from {text!r}")
            if not cur["items"] and not cur["summaries"]:
                raise AmazonError(f"No items or item summary for order {', '.join(cur['orders'])}")
            out.append(dict(cur, total=round(total, 2)))
            cur = None
    if not out:
        raise AmazonError("No order total in the email")
    return out


def _categories(names: list[str]) -> list[str]:
    """A category for each product name (or kind of product, for a summary)."""
    if not names:
        return []
    listing = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names))
    prompt = (
        "These are products from an Amazon order (names are cut off), or kinds of products. For a "
        "personal cost-of-living tracker, give each numbered line its single best spending category, "
        f"from: {', '.join(receipts.CATEGORIES)}.\n"
        "Fuel for heating the home (wood pellets) is \"Heating Fuel\"; salt for a water softener is "
        "\"Water Softener Salt\"; food and coffee are \"Groceries\"; kitchen, cleaning and storage "
        "goods are \"Household\"; computer and phone accessories are \"Electronics\"; tools, garden "
        "and pool supplies are \"Home Improvement\".\n"
        f"Return lines: exactly {len(names)} entries, one per numbered line, each with its line number "
        f"and category.\n\n{listing}")
    try:
        resp = httpx.post(
            f"{delegated.SERVICE_DELEGATOR_URL}/api/ask",
            json={"task": receipts.AI_TASK, "project": "smart-home", "prompt": prompt, "json_schema": _SCHEMA},
            timeout=900.0,
        )
    except httpx.HTTPError as e:
        raise AmazonError(f"Could not reach the service delegator at {delegated.SERVICE_DELEGATOR_URL}: {e}")
    try:
        body = resp.json()
    except ValueError:
        raise AmazonError(f"Service delegator returned HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code != 200:
        notes = "; ".join(body.get("notes") or [])
        raise AmazonError((body.get("error") or f"Service delegator returned HTTP {resp.status_code}")
                          + (f" ({notes})" if notes else ""))
    try:
        got = {int(x["line"]): x["category"] for x in json.loads(body.get("text") or "")["lines"]}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise AmazonError(f"Unparseable response from {body.get('model')}: {(body.get('text') or '')[:200]}")
    missing = [str(i + 1) for i in range(len(names)) if i + 1 not in got]
    if missing:
        raise AmazonError(f"The model gave no category for line(s) {', '.join(missing)} of {len(names)}")
    return [got[i + 1] if got[i + 1] in receipts.CATEGORIES else "Other" for i in range(len(names))]


_KINDS_RE = re.compile(r"^\d+ items?: (.+)$")


def _summary_kind(summary: str) -> str | None:
    """The one kind of product a summary line names ("1 item: Electronics"
    gives "Electronics"), or None when it lists several (that's "Other")."""
    m = _KINDS_RE.match(summary)
    kinds = m.group(1) if m else summary
    return None if ("," in kinds or " and " in kinds) else kinds.strip()


def _order_kind(order: dict) -> str | None:
    """The one kind of product an order without item names is, if its
    summaries agree on one."""
    kinds = {_summary_kind(x) for x in order["summaries"]}
    return kinds.pop() if len(kinds) == 1 and None not in kinds else None


def _lines(order: dict) -> list[dict]:
    if order["items"]:
        return order["items"]
    return [{"name": "; ".join(order["summaries"]), "quantity": None, "price": order["total"]}]


def read_email(conn: sqlite3.Connection, db_path: str, message_id: str) -> None:
    """Read one archived order email into receipts, one per order total,
    replacing any it made before, and record the outcome on its
    amazon_emails row. Raises on failure (also recorded there and on the
    receipts it made before)."""
    em = conn.execute("SELECT * FROM amazon_emails WHERE message_id=?", (message_id,)).fetchone()
    try:
        found = email_archive.get_message(db_path, message_id)
        if not found:
            raise AmazonError("The email is no longer in the archive")
        orders = parse(email_archive.parse(found[1]))
        # Product names, and the kind of product in a summary that names only
        # one, go to the model; a summary of several kinds is "Other"
        kinds = [None if o["items"] else _order_kind(o) for o in orders]
        asks = []
        for o, kind in zip(orders, kinds):
            if o["items"]:
                asks += [it["name"] for it in o["items"]]
            elif kind:
                asks.append(kind)
        answers = iter(_categories(asks))
        # Reading it again keeps the receipts' ids (pages link to them)
        old = [r[0] for r in conn.execute("SELECT id FROM receipts WHERE source='amazon' AND email_id=? ORDER BY id",
                                          (message_id,))]
        for rid in old[len(orders):]:
            conn.execute("DELETE FROM receipt_items WHERE receipt_id=?", (rid,))
            conn.execute("DELETE FROM receipts WHERE id=?", (rid,))
        for n, order in enumerate(orders):
            lines = _lines(order)
            subtotal = round(sum(ln["price"] for ln in lines), 2)
            tax = None
            notes = [f"From Amazon's order email (order {', '.join(order['orders']) or 'number not found'})."]
            if not order["items"]:
                notes.append("Amazon's email doesn't list the items, only what kinds they are; "
                             "this line is the whole order. Split it up by hand if you like.")
            elif order["total"] >= subtotal:
                tax = round(order["total"] - subtotal, 2)
                notes.append("Tax is the total minus the items (shipping, if any, is in it too).")
            else:
                notes.append("The items add up to more than the order total: a discount, coupon or credit "
                             "isn't in the email. Add it as a negative line.")
            fields = dict(uploaded_at=em["received_at"], status="done", error=None, processed_at=_now(),
                          image_path="", media_type="", merchant=MERCHANT, purchase_date=em["received_at"][:10],
                          subtotal=subtotal, tax=tax, total=order["total"], notes=" ".join(notes),
                          raw_json=json.dumps(order), kind="receipt", source="amazon", email_id=message_id)
            if n < len(old):
                rid = old[n]
                conn.execute(f"UPDATE receipts SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                             (*fields.values(), rid))
                conn.execute("DELETE FROM receipt_items WHERE receipt_id=?", (rid,))
            else:
                rid = conn.execute(f"INSERT INTO receipts ({', '.join(fields)}) "
                                   f"VALUES ({', '.join('?' * len(fields))})", tuple(fields.values())).lastrowid
            for i, ln in enumerate(lines):
                # A summary line isn't a product
                key = receipts.name_key(ln["name"], None) if order["items"] else None
                pid = db.match_product(conn, key, ln["name"]) if key else None
                qty = ln["quantity"] if ln["quantity"] and ln["quantity"] > 1 else None
                conn.execute(
                    "INSERT INTO receipt_items (receipt_id, position, description, quantity, unit, total_price, "
                    "category, raw_text, item_code, name_key, product_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, i, ln["name"], qty, "ea" if qty else None, ln["price"],
                     next(answers) if order["items"] or kinds[n] else "Other",
                     ln["name"] if key else None, None, key, pid))
        db.prune_products(conn)
        conn.execute("UPDATE amazon_emails SET status='imported', reason=NULL, processed_at=? WHERE message_id=?",
                     (_now(), message_id))
    except Exception as e:
        msg = str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}"
        conn.rollback()
        conn.execute("UPDATE amazon_emails SET status='error', reason=?, processed_at=? WHERE message_id=?",
                     (msg, _now(), message_id))
        conn.execute("UPDATE receipts SET status='error', error=?, processed_at=? WHERE source='amazon' AND email_id=?",
                     (msg, _now(), message_id))
        raise
    finally:
        conn.commit()


_lock = threading.Lock()


def scan(db_path: str) -> dict:
    """Import order emails not read yet, and retry ones that failed. Returns
    {ok, ts, n_new, n_errors[, error]}; also saved as the setting
    amazon_scan for /receipts to show."""
    with _lock:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        n_new = n_errors = 0
        try:
            index = email_archive.open_index(db_path)
            try:
                rows = index.execute(
                    "SELECT message_id, received_at, subject FROM messages WHERE sender LIKE ? AND subject LIKE ? "
                    "AND received_at >= ? AND deleted_at IS NULL ORDER BY received_at",
                    (SENDER_LIKE, SUBJECT_LIKE, START_DATE)).fetchall()
            finally:
                index.close()
            stale = (datetime.datetime.now() - STALE_AFTER).strftime("%Y-%m-%d %H:%M:%S")
            retry = {r[0] for r in conn.execute(
                "SELECT message_id FROM amazon_emails WHERE status='error' "
                "OR (status='processing' AND processed_at < ?)", (stale,))}
            for r in rows:
                mid = r["message_id"]
                if mid in retry:
                    conn.execute("UPDATE amazon_emails SET status='processing', processed_at=? WHERE message_id=?",
                                 (_now(), mid))
                    claimed = True
                else:
                    # The other process (web server or monitor) may be scanning too
                    claimed = conn.execute(
                        "INSERT OR IGNORE INTO amazon_emails (message_id, received_at, subject, status, processed_at) "
                        "VALUES (?, ?, ?, 'processing', ?)", (mid, r["received_at"], r["subject"], _now())).rowcount
                conn.commit()
                if not claimed:
                    continue
                try:
                    read_email(conn, db_path, mid)
                    n_new += 1
                except Exception:
                    n_errors += 1  # recorded on the email's row
            result = {"ok": True, "ts": _now(), "n_new": n_new, "n_errors": n_errors}
        except Exception as e:
            result = {"ok": False, "ts": _now(), "error": f"{type(e).__name__}: {e}", "n_new": n_new,
                      "n_errors": n_errors}
        conn.execute("INSERT INTO settings (key, value) VALUES ('amazon_scan', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(result),))
        conn.commit()
        conn.close()
        return result


def status(conn: sqlite3.Connection) -> dict:
    """The last scan, and the emails that couldn't be read."""
    row = conn.execute("SELECT value FROM settings WHERE key='amazon_scan'").fetchone()
    errors = [dict(r) for r in conn.execute(
        "SELECT message_id, received_at, subject, reason FROM amazon_emails WHERE status='error' "
        "ORDER BY received_at DESC")]
    n = conn.execute("SELECT COUNT(*) FROM amazon_emails WHERE status='imported'").fetchone()[0]
    return {"last_scan": json.loads(row[0]) if row else None, "errors": errors, "n_imported": n,
            "scanning": _lock.locked()}


_rereading: set[str] = set()


def is_rereading(message_id: str) -> bool:
    return message_id in _rereading


def reread_in_background(db_path: str, message_id: str) -> None:
    _rereading.add(message_id)

    def _bg():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            read_email(conn, db_path, message_id)
        except Exception as e:
            print(f"Re-reading Amazon email {message_id} failed: {e}", flush=True)  # also on its receipt
        finally:
            conn.close()
            _rereading.discard(message_id)

    threading.Thread(target=_bg, daemon=True).start()


def scan_in_background(db_path: str) -> bool:
    """Start scan() unless one is running. Returns whether it started."""
    if _lock.locked():
        return False

    def _bg():
        r = scan(db_path)  # the result, errors included, is shown on /receipts
        if not r["ok"]:
            print(f"Amazon email scan failed: {r['error']}", flush=True)

    threading.Thread(target=_bg, daemon=True).start()
    return True
