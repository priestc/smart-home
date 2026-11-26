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
# "Successful cancellation of 1 item from your Amazon.com order: #(...)",
# "1 of 5 items could not be cancelled: Automotive" (see read_cancellation)
CANCEL_SENDER_LIKE = "%order-update@amazon.com%"
CANCEL_SUBJECT_LIKE = "%cancel%"
# "Your refund for Kasa Smart Plug..." (return@), "Refund on order 112-..."
# (payments-messages@, e.g. shipping refunded for a late item); see read_refund
REFUND_SENDERS_LIKE = ("%return@amazon.com%", "%payments-messages@amazon.com%")
REFUND_SUBJECT_LIKE = "%refund%"
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


def _detailed_orders(conn: sqlite3.Connection) -> dict[str, int]:
    """{order id: receipt id} for receipts filled in with the real items, from
    Amazon's export or the Chrome extension."""
    out = {}
    for rid, raw in conn.execute("SELECT id, raw_json FROM receipts WHERE source='amazon' "
                                 "AND raw_json LIKE '%\"detailed\": true%'"):
        for oid in json.loads(raw).get("orders") or []:
            out[oid] = rid
    return out


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
        # Orders already filled in from Amazon's export (amazon_export below)
        # keep those items; the receipt is just linked to this email
        exported = _detailed_orders(conn)
        for o in orders:
            for oid in o["orders"]:
                if oid in exported:
                    conn.execute("UPDATE receipts SET email_id=? WHERE id=? AND email_id IS NULL",
                                 (message_id, exported[oid]))
        orders = [o for o in orders if not any(oid in exported for oid in o["orders"])]
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
                                          (message_id,)) if r[0] not in exported.values()]
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
    # Reading it again brought back items cancelled since: take them off again
    # (each cancellation records its own outcome)
    oids = [oid for o in orders for oid in o["orders"]]
    for (cid,) in conn.execute(
            f"SELECT message_id FROM amazon_emails WHERE kind='cancellation' AND status IN ('imported', 'needs_attention') "
            f"AND order_id IN ({','.join('?' * len(oids))}) ORDER BY received_at", oids).fetchall():
        try:
            read_cancellation(conn, db_path, cid)
        except Exception:
            pass  # recorded on the cancellation's row, shown on /receipts
    reapply_refunds(conn, db_path, oids)


# ---- Cancellation emails ----------------------------------------------------------
#
# order-update@amazon.com says when items are cancelled, but not what they cost:
#  - Older emails list the cancelled items' full names ("Canceled Items",
#    then the order, "Placed on ...", each name and "Sold by ..."). Those
#    lines are taken off the order's receipt, matched by name (the receipt
#    has the name as the order email printed it, cut short), and the tax is
#    reduced in proportion. A receipt left with nothing is deleted.
#  - Newer ones only summarize them under "Cancelled" ("4 items: 2
#    Computers, 1 Cables, and 1 other"). The amount can't be known, so the
#    receipt is flagged (cancellation_pending in its raw_json) and listed on
#    /receipts: opening the order's page with the Chrome extension replaces
#    it with what was really charged.
# A receipt filled in from the order page or the export already leaves
# cancelled items out, so it's left alone.

def parse_cancellation(msg) -> dict:
    """{order_id, names: [...], summaries: [...]} for the cancelled items."""
    lines = _html_lines(msg)
    if not lines:
        raise AmazonError("The email has no HTML part to read")
    order_id, names, summaries = None, [], []
    section = None  # 'items' after "Canceled Items", 'cancelled' after a "Cancelled" heading
    for i, ln in enumerate(lines):
        m = _ORDER_RE.search(ln)
        if m and (ln.startswith("Order #") or "order" in ln.lower()):
            order_id = order_id or m.group(1)
            continue
        if re.fullmatch(r"Cancel+ed Items", ln, re.I):
            section = "items"
        elif re.fullmatch(r"Cancel+ed", ln, re.I):
            section = "cancelled"
        elif section == "items":
            if ln.startswith(("Placed on", "Sold by")) or not ln.strip():
                continue
            if ln.startswith(("Cancel Reason", "If you need", "We hope")):
                section = None
                continue
            names.append(ln)
        elif section == "cancelled" and _SUMMARY_RE.match(ln):
            summaries.append(ln)
    if not order_id:
        raise AmazonError("No order number in the cancellation email")
    if not names and not summaries:
        raise AmazonError("Couldn't find which items were cancelled")
    return {"order_id": order_id, "names": names, "summaries": summaries}


def _same_item(printed: str, full: str) -> bool:
    """Whether a receipt line (the name as the order email printed it, cut
    short) is this full product name."""
    def key(t: str) -> str:
        # Letters and digits only: one email prints "Pool&Spa\u2122", another "Pool&SpaTM"
        t = t.lower().replace("\u2122", "tm").replace("\u2120", "sm")
        return re.sub(r"[^0-9a-z]", "", t)

    a, b = key(printed), key(full)
    return len(a) >= 10 and (b.startswith(a) or a.startswith(b))


def read_cancellation(conn: sqlite3.Connection, db_path: str, message_id: str) -> None:
    """Apply one cancellation email to its order's receipt and record the
    outcome on its amazon_emails row. Raises on failure (also recorded)."""
    em = conn.execute("SELECT * FROM amazon_emails WHERE message_id=?", (message_id,)).fetchone()
    day = em["received_at"][:10]
    status, reason = "imported", None
    try:
        found = email_archive.get_message(db_path, message_id)
        if not found:
            raise AmazonError("The email is no longer in the archive")
        c = parse_cancellation(email_archive.parse(found[1]))
        oid = c["order_id"]
        conn.execute("UPDATE amazon_emails SET order_id=? WHERE message_id=?", (oid, message_id))
        rid = raw = None
        for r in conn.execute("SELECT id, raw_json FROM receipts WHERE source='amazon'"):
            j = json.loads(r["raw_json"] or "{}")
            if oid in (j.get("orders") or []):
                rid, raw = r["id"], j
                break
        if rid is None:
            status, reason = "skipped", f"No receipt for order {oid}"
        elif raw.get("detailed"):
            status, reason = "skipped", ("The receipt has the items from the order's page or export, "
                                         "which already leave out cancelled items")
        else:
            rec = conn.execute("SELECT subtotal, tax, total, notes FROM receipts WHERE id=?", (rid,)).fetchone()
            items = conn.execute("SELECT id, description, total_price FROM receipt_items WHERE receipt_id=? "
                                 "ORDER BY position", (rid,)).fetchall()
            removed, unmatched = [], []
            for name in c["names"]:
                hit = next((it for it in items if it["id"] not in {x["id"] for x in removed}
                            and _same_item(it["description"], name)), None)
                (removed.append(hit) if hit else unmatched.append(name))
            if c["summaries"] or unmatched:
                what = "; ".join(c["summaries"] + unmatched)
                status = "needs_attention"
                reason = (f"Amazon says items were cancelled ({what}) without saying what they cost. "
                          "Open the order's page on amazon.com with the Smart Home extension to correct the total.")
                if not raw.get("cancellation_pending"):
                    raw["cancellation_pending"] = True
                    conn.execute("UPDATE receipts SET raw_json=?, notes=COALESCE(notes, '') || ? WHERE id=?",
                                 (json.dumps(raw), f" Cancellation email of {day}: {reason}", rid))
            if removed:
                for it in removed:
                    conn.execute("DELETE FROM receipt_items WHERE id=?", (it["id"],))
                old_sub = round(sum(it["total_price"] for it in items), 2)
                new_sub = round(old_sub - sum(it["total_price"] for it in removed), 2)
                if not conn.execute("SELECT 1 FROM receipt_items WHERE receipt_id=?", (rid,)).fetchone():
                    conn.execute("DELETE FROM receipts WHERE id=?", (rid,))
                    reason = f"Every item on order {oid} was cancelled; its receipt was deleted"
                else:
                    tax = rec["tax"]
                    if tax is not None and old_sub > 0:
                        tax = round(tax * new_sub / old_sub, 2)
                    note = (f" Cancelled per Amazon's email of {day}: "
                            + ", ".join(it["description"] for it in removed)
                            + (" (tax reduced in proportion)." if tax is not None else "."))
                    conn.execute("UPDATE receipts SET subtotal=?, tax=?, total=?, notes=COALESCE(notes, '') || ? "
                                 "WHERE id=?", (new_sub, tax, round(new_sub + (tax or 0), 2), note, rid))
                db.prune_products(conn)
        conn.execute("UPDATE amazon_emails SET status=?, reason=?, processed_at=? WHERE message_id=?",
                     (status, reason, _now(), message_id))
    except Exception as e:
        msg = str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}"
        conn.rollback()
        conn.execute("UPDATE amazon_emails SET status='error', reason=?, processed_at=? WHERE message_id=?",
                     (msg, _now(), message_id))
        raise
    finally:
        conn.commit()


# ---- Refund emails --------------------------------------------------------------
#
# Each refund becomes a negative line on its order's receipt ("Refund: ..."),
# in the category of the item it's for when that's clear, and the receipt's
# total goes down by the amount (tax included, as Amazon refunds it). The
# line's name_key is "refund:<message id>", so applying an email again
# replaces its line rather than adding another. Writing a receipt's items
# again (reading its email again, the export, the extension) drops the
# lines; reapply_refunds puts them back.

def parse_refund(msg) -> dict:
    """{orders, amount, names, shipping_only}."""
    lines = _html_lines(msg)
    if not lines:
        raise AmazonError("The email has no HTML part to read")
    text = "\n".join(lines)
    m = (re.search(r"Total refund\*?\^?\s*\n?\s*(\$[\d,]+\.\d{2})", text)
         or re.search(r"refund of (\$[\d,]+\.\d{2})", text)
         or re.search(r"(\$[\d,]+\.\d{2}) will be credited", text))
    if not m:
        raise AmazonError("No refund amount in the email")
    amount = _price(m.group(1))
    orders = []
    for o in _ORDER_RE.findall(text):
        if o not in orders:
            orders.append(o)
    names = []
    section = False
    for ln in lines:
        if ln.startswith("Item: "):
            names.append(ln[6:].strip())
        elif re.match(r"Items? to be returned|Item\(s\) in your return", ln):
            section = True
        elif section:
            if ln.startswith(("Quantity", "Order #")):
                continue
            if ln.startswith(("View invoice", "Products related", "*If your", "Learn more", "\u00a9")) or "\u00a9" in ln:
                section = False
                continue
            names.append(ln.rstrip(". \u2026").strip())
    shipping_only = bool(re.search(r"Shipping Refund", text)) and not re.search(r"Item Refund|Item Price", text)
    if not orders:
        raise AmazonError("No order number in the refund email")
    return {"orders": orders, "amount": amount, "names": names, "shipping_only": shipping_only}


def _receipt_for_order(conn: sqlite3.Connection, oid: str) -> int | None:
    for r in conn.execute("SELECT id, raw_json FROM receipts WHERE source='amazon'"):
        if oid in (json.loads(r["raw_json"] or "{}").get("orders") or []):
            return r["id"]
    return None


def read_refund(conn: sqlite3.Connection, db_path: str, message_id: str) -> None:
    """Put one refund email's negative line on its order's receipt and record
    the outcome on its amazon_emails row. Raises on failure (also recorded)."""
    em = conn.execute("SELECT * FROM amazon_emails WHERE message_id=?", (message_id,)).fetchone()
    key = f"refund:{message_id}"
    try:
        found = email_archive.get_message(db_path, message_id)
        if not found:
            raise AmazonError("The email is no longer in the archive")
        r = parse_refund(email_archive.parse(found[1]))
        oid = r["orders"][0]
        conn.execute("UPDATE amazon_emails SET order_id=? WHERE message_id=?", (oid, message_id))
        # Take off this email's line wherever it is, giving its amount back
        for line in conn.execute("SELECT id, receipt_id, total_price FROM receipt_items WHERE name_key=?",
                                 (key,)).fetchall():
            conn.execute("UPDATE receipts SET subtotal = ROUND(subtotal - ?, 2), total = ROUND(total - ?, 2) WHERE id=?",
                         (line["total_price"], line["total_price"], line["receipt_id"]))
            conn.execute("DELETE FROM receipt_items WHERE id=?", (line["id"],))
        rid = _receipt_for_order(conn, oid)
        if rid is None:
            status, reason = "skipped", f"No receipt for order {oid}"
        else:
            items = conn.execute("SELECT description, category FROM receipt_items WHERE receipt_id=? "
                                 "AND total_price > 0", (rid,)).fetchall()
            cats = {it["category"] for n in r["names"] for it in items if _same_item(it["description"], n)}
            category = cats.pop() if len(cats) == 1 else "Other"
            what = ", ".join(r["names"]) or f"order {oid}"
            desc = (f"Refund of shipping: {what}" if r["shipping_only"] else f"Refund: {what}")[:300]
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM receipt_items WHERE receipt_id=?",
                               (rid,)).fetchone()[0]
            conn.execute("INSERT INTO receipt_items (receipt_id, position, description, total_price, category, "
                         "name_key) VALUES (?, ?, ?, ?, ?, ?)", (rid, pos, desc, -r["amount"], category, key))
            conn.execute("UPDATE receipts SET subtotal = ROUND(COALESCE(subtotal, 0) - ?, 2), "
                         "total = ROUND(COALESCE(total, 0) - ?, 2) WHERE id=?", (r["amount"], r["amount"], rid))
            status = "imported"
            reason = (f"Refunded {r['amount']:.2f} on receipt {rid}"
                      + (f" (it covers orders {', '.join(r['orders'])}; all put on {oid}'s receipt)"
                         if len(r["orders"]) > 1 else ""))
        conn.execute("UPDATE amazon_emails SET status=?, reason=?, processed_at=? WHERE message_id=?",
                     (status, reason, _now(), message_id))
    except Exception as e:
        msg = str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}"
        conn.rollback()
        conn.execute("UPDATE amazon_emails SET status='error', reason=?, processed_at=? WHERE message_id=?",
                     (msg, _now(), message_id))
        raise
    finally:
        conn.commit()


def reapply_refunds(conn: sqlite3.Connection, db_path: str, order_ids: list[str]) -> None:
    """Put back the refund lines of these orders after their receipt's items
    were written again. Each refund records its own outcome."""
    if not order_ids:
        return
    for (mid,) in conn.execute(
            f"SELECT message_id FROM amazon_emails WHERE kind='refund' AND status='imported' "
            f"AND order_id IN ({','.join('?' * len(order_ids))}) ORDER BY received_at", order_ids).fetchall():
        try:
            read_refund(conn, db_path, mid)
        except Exception:
            pass  # recorded on the refund's row, shown on /receipts


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
                # Orders first, so a cancellation finds its order's receipt
                rows = [dict(r, kind="order") for r in index.execute(
                    "SELECT message_id, received_at, subject FROM messages WHERE sender LIKE ? AND subject LIKE ? "
                    "AND received_at >= ? AND deleted_at IS NULL ORDER BY received_at",
                    (SENDER_LIKE, SUBJECT_LIKE, START_DATE))]
                rows += [dict(r, kind="cancellation") for r in index.execute(
                    "SELECT message_id, received_at, subject FROM messages WHERE sender LIKE ? AND subject LIKE ? "
                    "AND received_at >= ? AND deleted_at IS NULL ORDER BY received_at",
                    (CANCEL_SENDER_LIKE, CANCEL_SUBJECT_LIKE, START_DATE))]
                rows += [dict(r, kind="refund") for r in index.execute(
                    "SELECT message_id, received_at, subject FROM messages WHERE (sender LIKE ? OR sender LIKE ?) "
                    "AND subject LIKE ? AND received_at >= ? AND deleted_at IS NULL ORDER BY received_at",
                    (*REFUND_SENDERS_LIKE, REFUND_SUBJECT_LIKE, START_DATE))]
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
                        "INSERT OR IGNORE INTO amazon_emails (message_id, received_at, subject, status, processed_at, kind) "
                        "VALUES (?, ?, ?, 'processing', ?, ?)",
                        (mid, r["received_at"], r["subject"], _now(), r["kind"])).rowcount
                conn.commit()
                if not claimed:
                    continue
                try:
                    if r["kind"] == "cancellation":
                        read_cancellation(conn, db_path, mid)
                    elif r["kind"] == "refund":
                        read_refund(conn, db_path, mid)
                    else:
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
    # Receipts a cancellation email couldn't be applied to (no amounts given)
    to_correct = []
    for r in conn.execute("SELECT id, purchase_date, total, raw_json FROM receipts WHERE source='amazon' "
                          "AND raw_json LIKE '%cancellation_pending%'"):
        j = json.loads(r["raw_json"])
        if j.get("cancellation_pending") and not j.get("detailed"):
            to_correct.append({"receipt_id": r["id"], "purchase_date": r["purchase_date"], "total": r["total"],
                               "orders": j.get("orders") or []})
    exp = conn.execute("SELECT value FROM settings WHERE key='amazon_export'").fetchone()
    return {"last_scan": json.loads(row[0]) if row else None, "errors": errors, "n_imported": n,
            "scanning": _lock.locked(), "last_export": json.loads(exp[0]) if exp else None,
            "importing_export": _export_lock.locked(), "extension": extension_log(conn)[:15],
            "to_correct": to_correct}


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


# ---- Amazon's data export ------------------------------------------------------
#
# Amazon > Account > "Request Your Data" > "Your Orders" emails, after a few
# days, a zip with a spreadsheet of every item ever ordered (order ID, date,
# full product name, ASIN, quantity, prices, tax). Uploading it on /receipts
# puts the real items on the receipts made from the emails, matched by order
# number: the "4 items: ..." summary lines become the products, and the tax
# is the tax Amazon charged. Orders with no receipt yet (no email in the
# archive) get one. A receipt already filled in from an export is left alone,
# so uploading a newer export keeps your edits. Orders before START_DATE and
# cancelled items are skipped.
#
# Amazon has changed the file's layout over the years, so columns are found
# by name (case and punctuation ignored) and the spreadsheet by having them.

_COLUMNS = {
    "order_id": ("orderid",),
    "date": ("orderdate",),
    "name": ("productname", "title"),
    "asin": ("asin", "asinisbn"),
    "quantity": ("quantity", "originalquantity"),
    "owed": ("totalowed", "itemtotal"),
    "unit_price": ("unitprice", "purchaseppu", "listedppu"),
    "tax": ("shipmentitemsubtotaltax", "itemsubtotaltax"),
    "unit_tax": ("unitpricetax",),
    "status": ("orderstatus",),
}
_REQUIRED = ("order_id", "date", "name", "quantity")
EXPORT_MAX_BYTES = 200 * 1024 * 1024


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _money(v: str | None) -> float | None:
    """'$12.99', "'12.99'", '1,234.50' -> number; 'Not Available' or '' -> None."""
    s = re.sub(r"[^\d.\-]", "", v or "")
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _local_date(v: str) -> str | None:
    """The order date in local time (Amazon gives UTC, e.g. 2026-07-07T18:43:09Z)."""
    v = (v or "").strip()
    try:
        d = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        return (d.astimezone() if d.tzinfo else d).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _export_rows(data: bytes, filename: str) -> list[dict]:
    """Rows of the order-history spreadsheet in an uploaded zip or CSV, with
    keys from _COLUMNS. Raises AmazonError saying what was found instead."""
    import csv
    import io
    import zipfile

    files: list[tuple[str, bytes]] = []
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                files = [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".csv")]
        except zipfile.BadZipFile as e:
            raise AmazonError(f"The zip file can't be opened: {e}")
        if not files:
            raise AmazonError("The zip has no spreadsheets (.csv files) in it")
    else:
        files = [(filename or "upload.csv", data)]
    seen = []
    for name, raw in files:
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header:
            continue
        norm = [_norm(h) for h in header]
        cols = {}
        for key, names in _COLUMNS.items():
            for n in names:
                if n in norm:
                    cols[key] = norm.index(n)
                    break
        # The digital-orders file has the same columns; only physical orders here
        if any(k not in cols for k in _REQUIRED) or "digital" in name.lower() or (
                "owed" not in cols and "unit_price" not in cols):
            seen.append(f"{name} ({', '.join(header[:8])}{'...' if len(header) > 8 else ''})")
            continue
        rows = []
        for rec in reader:
            if not any(rec):
                continue
            rows.append({k: (rec[i] if i < len(rec) else "") for k, i in cols.items()})
        return rows
    raise AmazonError("Couldn't find the order history in the upload. Expected a spreadsheet with "
                      "Order ID, Order Date, Product Name and Quantity columns; found: "
                      + ("; ".join(seen) if seen else "no spreadsheets"))


def _export_orders(rows: list[dict]) -> tuple[dict[str, dict], int]:
    """{order id: {date, items: [{name, asin, quantity, price, tax}]}} and how
    many cancelled rows were left out. An item's price plus its tax is what
    was owed for it (discounts and shipping included)."""
    orders: dict[str, dict] = {}
    cancelled = 0
    for r in rows:
        oid = (r.get("order_id") or "").strip()
        if not _ORDER_RE.fullmatch(oid):
            continue  # e.g. a totals row
        if "cancel" in (r.get("status") or "").lower():
            cancelled += 1
            continue
        try:
            qty = max(int(float(r.get("quantity") or 1)), 1)
        except ValueError:
            qty = 1
        tax = _money(r.get("tax"))
        if tax is None and _money(r.get("unit_tax")) is not None:
            tax = _money(r.get("unit_tax")) * qty
        owed = _money(r.get("owed"))
        if owed is None:
            each = _money(r.get("unit_price"))
            if each is None:
                continue
            owed = each * qty + (tax or 0)
        tax = round(tax or 0, 2)
        date = _local_date(r.get("date") or "")
        o = orders.setdefault(oid, {"date": date, "items": []})
        o["items"].append({"name": " ".join((r.get("name") or "").split()) or "Amazon item",
                           "asin": (r.get("asin") or "").strip() or None, "quantity": qty,
                           "price": round(owed - tax, 2), "tax": tax})
    return orders, cancelled


def _write_receipt(conn: sqlite3.Connection, rid: int | None, oids: list[str], items: list[dict],
                   date: str | None, tax: float, total: float, notes: str, detail_source: str, cats) -> int:
    """Put these items ({name, asin, quantity, price[, category]}) on receipt
    rid (made if None), marking it as detailed: a later email, export or
    extension page for the same order leaves it alone. Items without a
    category take the next one from cats."""
    subtotal = round(sum(it["price"] for it in items), 2)
    raw = json.dumps({"orders": oids, "detailed": True, "detail_source": detail_source, "items": items})
    old_keys = []
    if rid is None:
        rid = conn.execute(
            "INSERT INTO receipts (uploaded_at, status, processed_at, image_path, media_type, merchant, "
            "purchase_date, subtotal, tax, total, notes, raw_json, kind, source) "
            "VALUES (?, 'done', ?, '', '', ?, ?, ?, ?, ?, ?, ?, 'receipt', 'amazon')",
            (_now(), _now(), MERCHANT, date, subtotal, tax, total, notes, raw)).lastrowid
    else:
        old_keys = [r[0] for r in conn.execute(
            "SELECT name_key FROM receipt_items WHERE receipt_id=? AND name_key IS NOT NULL", (rid,))]
        conn.execute("UPDATE receipts SET status='done', error=NULL, processed_at=?, subtotal=?, tax=?, total=?, "
                     "notes=?, raw_json=?, purchase_date=COALESCE(?, purchase_date) WHERE id=?",
                     (_now(), subtotal, tax, total, notes, raw, date, rid))
        conn.execute("DELETE FROM receipt_items WHERE receipt_id=?", (rid,))
    for i, it in enumerate(items):
        pid = key = None
        if it.get("category") is None:  # a product, not a shipping or discount line
            key = f"asin:{it['asin']}" if it.get("asin") else receipts.name_key(it["name"], None)
            pid = db.match_product(conn, key, it["name"])
        qty = it["quantity"] if (it.get("quantity") or 1) > 1 else None
        conn.execute(
            "INSERT INTO receipt_items (receipt_id, position, description, quantity, unit, total_price, category, "
            "raw_text, item_code, name_key, product_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, i, it["name"], qty, "ea" if qty else None, it["price"], it.get("category") or next(cats),
             it["name"] if key else None, None, key, pid))
    # The shortened names from the email no longer describe anything
    for k in old_keys:
        conn.execute("DELETE FROM product_keys WHERE name_key=? AND product_id NOT IN "
                     "(SELECT product_id FROM receipt_items WHERE product_id IS NOT NULL)", (k,))
    return rid


def _fill_receipt(conn: sqlite3.Connection, rid: int | None, oids: list[str], orders: dict[str, dict],
                  cats) -> int:
    """Put the export's items for these orders on receipt rid (made if None)."""
    items = [it for oid in oids for it in orders[oid]["items"]]
    tax = round(sum(it["tax"] for it in items), 2)
    total = round(sum(it["price"] for it in items) + tax, 2)
    date = min((orders[oid]["date"] for oid in oids if orders[oid]["date"]), default=None)
    notes = (f"From Amazon's order history export (order {', '.join(oids)}). An item's price includes its "
             "share of any discount and shipping; tax is what Amazon charged.")
    return _write_receipt(conn, rid, oids, [{k: it[k] for k in ("name", "asin", "quantity", "price")} for it in items],
                          date, tax, total, notes, "export", cats)


def import_export(db_path: str, data: bytes, filename: str = "") -> dict:
    """Apply an uploaded export. Returns {ok, ts, n_updated, n_created,
    n_already, n_old, n_cancelled, errors: [{orders, error}]}, also saved as
    the setting amazon_export."""
    with _export_lock:
        started = _now()
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        result = {"ok": True, "ts": started, "filename": filename, "n_updated": 0, "n_created": 0,
                  "n_already": 0, "n_old": 0, "n_cancelled": 0, "errors": []}
        try:
            orders, result["n_cancelled"] = _export_orders(_export_rows(data, filename))
            if not orders:
                raise AmazonError("The order history has no orders in it")
            # Receipts made from emails (or an earlier export), by order number
            by_order: dict[str, int] = {}
            filled: set[int] = set()
            for r in conn.execute("SELECT id, raw_json FROM receipts WHERE source='amazon'"):
                try:
                    j = json.loads(r["raw_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                for oid in j.get("orders") or []:
                    by_order[oid] = r["id"]
                if j.get("detailed"):
                    filled.add(r["id"])
            # Group the export's orders the way the receipts are (one email
            # receipt can cover several orders)
            groups: dict[int | str, list[str]] = {}
            for oid, o in orders.items():
                rid = by_order.get(oid)
                if rid is None and (o["date"] or "") < START_DATE:
                    result["n_old"] += 1
                    continue
                groups.setdefault(rid if rid is not None else oid, []).append(oid)
            for key, oids in sorted(groups.items(), key=lambda kv: orders[kv[1][0]]["date"] or ""):
                rid = key if isinstance(key, int) else None
                if rid in filled:
                    result["n_already"] += 1
                    continue
                if rid is not None:
                    # An order on this receipt that isn't in the export yet: leave it for a later one
                    j = json.loads(conn.execute("SELECT raw_json FROM receipts WHERE id=?", (rid,)).fetchone()[0])
                    missing = [o for o in j.get("orders") or [] if o not in orders]
                    if missing:
                        result["errors"].append({"orders": oids, "receipt_id": rid,
                                                 "error": f"Order {', '.join(missing)} on the same receipt isn't in this export"})
                        continue
                try:
                    cats = iter(_categories([it["name"] for oid in oids for it in orders[oid]["items"]]))
                    new = _fill_receipt(conn, rid, oids, orders, cats)
                    db.prune_products(conn)
                    conn.commit()
                    reapply_refunds(conn, db_path, oids)
                    result["n_created" if rid is None else "n_updated"] += 1
                    filled.add(new)
                except Exception as e:
                    conn.rollback()
                    result["errors"].append({"orders": oids, "receipt_id": rid,
                                             "error": str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}"})
        except Exception as e:
            result.update(ok=False, error=str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}")
        result["ended"] = _now()
        conn.execute("INSERT INTO settings (key, value) VALUES ('amazon_export', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(result),))
        conn.commit()
        conn.close()
        return result


_export_lock = threading.Lock()


def import_export_in_background(db_path: str, data: bytes, filename: str) -> bool:
    """Start import_export() unless one is running. Returns whether it started."""
    if _export_lock.locked():
        return False

    def _bg():
        r = import_export(db_path, data, filename)  # result shown on /receipts
        if not r["ok"]:
            print(f"Amazon export import failed: {r['error']}", flush=True)

    threading.Thread(target=_bg, daemon=True).start()
    return True


# ---- The Chrome extension (chrome_extension/ in the repo) -----------------------
#
# It reads an order's details page on amazon.com in the browser you shop in
# (after you check out, when you open one, or for recent orders with "Sync")
# and posts it here: every item's full name, ASIN, quantity and price, and
# the order summary. The items must add up to the page's Item(s) Subtotal, or
# the extension reports an error instead. Shipping and discounts (Total before
# tax minus the subtotal) become a line of their own. The receipt made from
# the order's email gets the items; with no email yet, a receipt is made. An
# email receipt that covers several orders is split. The page shows what was
# charged after cancellations, so it wins over the email's total. Categories are picked by
# the model afterwards, in the background; until then a product keeps the
# category it had on an earlier receipt, else "Other".
#
# Every post is kept in the setting amazon_extension (the latest 50) for
# /receipts to show; for one the extension couldn't read, the page itself is
# saved under <data dir>/amazon-extension/ so the reader can be fixed.

EXTENSION_LOG = 50


def _log_extension(conn: sqlite3.Connection, entry: dict) -> None:
    row = conn.execute("SELECT value FROM settings WHERE key='amazon_extension'").fetchone()
    log = json.loads(row[0]) if row else []
    log = [dict(entry, ts=_now())] + log[:EXTENSION_LOG - 1]
    conn.execute("INSERT INTO settings (key, value) VALUES ('amazon_extension', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(log),))
    conn.commit()


def extension_log(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute("SELECT value FROM settings WHERE key='amazon_extension'").fetchone()
    return json.loads(row[0]) if row else []


def order_states(conn: sqlite3.Connection, order_ids: list[str]) -> dict[str, str]:
    """For each order: 'detailed' (has its real items), 'summary' (only the
    email's summary or shortened names) or 'missing' (no receipt)."""
    detailed = _detailed_orders(conn)
    known = set()
    for (raw,) in conn.execute("SELECT raw_json FROM receipts WHERE source='amazon'"):
        try:
            known.update(json.loads(raw or "{}").get("orders") or [])
        except json.JSONDecodeError:
            pass
    # A receipt with its items whose product pictures weren't fetched yet
    needs_images = set()
    for rid in set(detailed.values()):
        raw = json.loads(conn.execute("SELECT raw_json FROM receipts WHERE id=?", (rid,)).fetchone()[0])
        if not raw.get("images_tried") and raw.get("detail_source") == "extension":
            needs_images.update(raw.get("orders") or [])
    return {o: ("needs_images" if o in needs_images else "detailed") if o in detailed
            else "summary" if o in known else "missing" for o in order_ids}


def _check_page_order(d: dict) -> dict:
    """The posted order, checked and cleaned. Raises AmazonError."""
    oid = str(d.get("order_id") or "").strip()
    if not _ORDER_RE.fullmatch(oid):
        raise AmazonError(f"Not an Amazon order number: {oid!r}")
    items = []
    for it in d.get("items") or []:
        try:
            qty = max(int(it.get("quantity") or 1), 1)
            each = round(float(it["unit_price"]), 2)
        except (KeyError, TypeError, ValueError):
            raise AmazonError(f"An item has no usable price or quantity: {it!r}")
        name = " ".join(str(it.get("name") or "").split())[:300]
        if not name:
            raise AmazonError("An item has no name")
        asin = str(it.get("asin") or "").strip() or None
        items.append({"name": name, "asin": asin, "quantity": qty, "price": round(each * qty, 2),
                      "image_url": _image_url(it.get("image_url"))})
    if not items:
        raise AmazonError("No items")
    nums = {}
    for k in ("subtotal", "total_before_tax", "tax", "total"):
        try:
            nums[k] = round(float(d[k]), 2)
        except (KeyError, TypeError, ValueError):
            raise AmazonError(f"Missing {k}")
    got = round(sum(it["price"] for it in items), 2)
    if abs(got - nums["subtotal"]) >= 0.005:
        raise AmazonError(f"The items add up to {got:.2f}, not the page's Item(s) Subtotal {nums['subtotal']:.2f}")
    extra = round(nums["total_before_tax"] - nums["subtotal"], 2)
    if abs(extra) >= 0.01:
        items.append({"name": "Shipping" if extra > 0 else "Discounts and credits", "asin": None, "quantity": 1,
                      "price": extra, "category": "Fees & Deposits" if extra > 0 else "Other"})
    # Paid partly from the Amazon balance (a gift card, or refunds credited
    # to it): that's how it was paid, not a discount, so the receipt is the
    # full amount. (A refund credited to the balance is already a negative
    # line on its own order's receipt.)
    try:
        gift = round(abs(float(d.get("gift_card") or 0)), 2)
    except (TypeError, ValueError):
        raise AmazonError("gift_card must be a number")
    spent = round(nums["total_before_tax"] + nums["tax"], 2)
    if abs(spent - gift - nums["total"]) >= 0.015:
        raise AmazonError(f"Total before tax {nums['total_before_tax']:.2f} + tax {nums['tax']:.2f}"
                          + (f" - gift card {gift:.2f}" if gift else "")
                          + f" isn't the Grand Total {nums['total']:.2f}")
    date = _local_date(str(d.get("order_date") or "")) or None
    return {"order_id": oid, "date": date, "items": items, "tax": nums["tax"], "total": spent, "gift_card": gift,
            "grand_total": nums["total"]}


def apply_page_order(db_path: str, d: dict) -> dict:
    """Store an order the extension read. Returns {outcome: 'filled' |
    'created' | 'already', receipt_id}. Raises AmazonError (also logged)."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    oid = str(d.get("order_id") or "")
    try:
        try:
            o = _check_page_order(d)
        except AmazonError as e:
            _log_extension(conn, {"order_id": oid, "outcome": "error", "error": str(e), "url": d.get("url")})
            raise
        detailed = _detailed_orders(conn)
        if oid in detailed:
            result = {"outcome": "already", "receipt_id": detailed[oid]}
            _log_extension(conn, {"order_id": oid, **result})
            # A sync sends it again for the product pictures (order_states)
            _attach_images_in_background(db_path, detailed[oid], o["items"])
            return result
        rid, email_id, others = None, None, []
        for r in conn.execute("SELECT id, raw_json, email_id FROM receipts WHERE source='amazon'"):
            orders = (json.loads(r["raw_json"] or "{}").get("orders") or [])
            if oid in orders:
                rid, email_id, others = r["id"], r["email_id"], [x for x in orders if x != oid]
                break
        if rid is not None and others:
            # One email receipt for several orders: this order becomes its own
            # receipt and the rest stays on the email's
            old = conn.execute("SELECT total, raw_json FROM receipts WHERE id=?", (rid,)).fetchone()
            raw = json.loads(old["raw_json"])
            raw["orders"] = others
            rest = round((old["total"] or 0) - o["total"], 2)
            conn.execute("UPDATE receipts SET total=?, subtotal=?, raw_json=? WHERE id=?",
                         (rest, rest, json.dumps(raw), rid))
            lines = conn.execute("SELECT id FROM receipt_items WHERE receipt_id=?", (rid,)).fetchall()
            if len(lines) == 1:
                conn.execute("UPDATE receipt_items SET total_price=? WHERE id=?", (rest, lines[0][0]))
            rid = None
        changed = None
        if rid is not None:
            # The page shows what was charged after any cancellations, so it
            # can come to less than the order email did: the page wins, and
            # the note says by how much. (The email's total is after any gift
            # card, like the page's Grand Total.)
            old_total = conn.execute("SELECT total FROM receipts WHERE id=?", (rid,)).fetchone()[0]
            if old_total is not None and abs(old_total - o["grand_total"]) >= 0.01:
                changed = old_total
        # Until the model picks, a product keeps the category it had before
        for it in o["items"]:
            if it.get("category") is None and it["asin"]:
                prev = conn.execute(
                    "SELECT i.category FROM receipt_items i JOIN product_keys k ON k.product_id = i.product_id "
                    "WHERE k.name_key=? ORDER BY i.id DESC LIMIT 1", (f"asin:{it['asin']}",)).fetchone()
                it["guess"] = prev[0] if prev else None
        cats = iter([it.get("guess") or "Other" for it in o["items"] if it.get("category") is None])
        for it in o["items"]:
            it.pop("guess", None)
        notes = (f"From the order's page on amazon.com (order {oid}), read by the Chrome extension. "
                 "Tax is what Amazon charged.")
        if changed is not None:
            notes += (f" The order email came to {changed:.2f}; the page shows {o['grand_total']:.2f} "
                      "(items cancelled or changed since the order was placed).")
        if o["gift_card"]:
            notes += (f" {o['gift_card']:.2f} of it was paid from the Amazon balance (gift card or refunds "
                      "credited there); the total is the full amount.")
        created = rid is None
        rid = _write_receipt(conn, rid, [oid], o["items"], o["date"], o["tax"], o["total"], notes, "extension", cats)
        if created and email_id:
            conn.execute("UPDATE receipts SET email_id=? WHERE id=?", (email_id, rid))
        db.prune_products(conn)
        conn.commit()
        result = {"outcome": "created" if created else "filled", "receipt_id": rid}
        _log_extension(conn, {"order_id": oid, **result})
        reapply_refunds(conn, db_path, [oid])
    finally:
        conn.close()
    _categorize_in_background(db_path, rid)
    _attach_images_in_background(db_path, rid, o["items"])
    return result


def _categorize_in_background(db_path: str, rid: int) -> None:
    def _bg():
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            rows = conn.execute("SELECT id, description FROM receipt_items WHERE receipt_id=? "
                                "AND product_id IS NOT NULL ORDER BY position", (rid,)).fetchall()
            try:
                cats = _categories([r[1] for r in rows])
            except Exception as e:
                msg = str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}"
                conn.execute("UPDATE receipts SET notes = notes || ? WHERE id=?",
                             (f" Categories couldn't be picked automatically ({msg}); set them by hand.", rid))
                conn.commit()
                _log_extension(conn, {"receipt_id": rid, "outcome": "error",
                                      "error": f"Picking categories failed: {msg}"})
                return
            for (iid, _), cat in zip(rows, cats):
                conn.execute("UPDATE receipt_items SET category=? WHERE id=?", (cat, iid))
            conn.commit()
            # Refund lines take their item's category: put them back now that it's known
            raw = conn.execute("SELECT raw_json FROM receipts WHERE id=?", (rid,)).fetchone()
            if raw:
                conn.row_factory = sqlite3.Row
                reapply_refunds(conn, db_path, json.loads(raw[0] or "{}").get("orders") or [])
        finally:
            conn.close()

    threading.Thread(target=_bg, daemon=True).start()


# Product pictures from the order page. Only Amazon's own image servers are
# fetched; a product that already has a picture (e.g. a photo you took) keeps it.
IMAGE_HOSTS = ("m.media-amazon.com", "images-na.ssl-images-amazon.com", "images-amazon.com")
IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _image_url(u) -> str | None:
    import urllib.parse
    try:
        p = urllib.parse.urlparse(str(u or ""))
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS):
        return None
    return p.geturl()


def _attach_images_in_background(db_path: str, rid: int, items: list[dict]) -> None:
    def _bg():
        from smart_home import product_photos
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            for it in items:
                if not it.get("asin") or not it.get("image_url"):
                    continue
                row = conn.execute("SELECT product_id FROM product_keys WHERE name_key=?",
                                   (f"asin:{it['asin']}",)).fetchone()
                if row is None or conn.execute("SELECT 1 FROM product_photos WHERE product_id=?",
                                               (row[0],)).fetchone():
                    continue
                try:
                    resp = httpx.get(it["image_url"], timeout=30, follow_redirects=False)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if resp.status_code != 200:
                        raise AmazonError(f"HTTP {resp.status_code}")
                    if ctype not in ("image/jpeg", "image/png", "image/webp"):
                        raise AmazonError(f"not a picture ({ctype or 'no type'})")
                    if len(resp.content) > IMAGE_MAX_BYTES:
                        raise AmazonError("larger than 10 MB")
                    photo = product_photos.save_upload(db_path, resp.content, ctype)
                    conn.execute("UPDATE product_photos SET status='matched', product_id=?, match_source='amazon', "
                                 "receipt_id=?, processed_at=? WHERE id=?", (row[0], rid, _now(), photo))
                    conn.commit()
                except Exception as e:
                    msg = str(e) if isinstance(e, AmazonError) else f"{type(e).__name__}: {e}"
                    _log_extension(conn, {"receipt_id": rid, "outcome": "error",
                                          "error": f"Couldn't get the picture of {it['name'][:60]}: {msg}"})
            raw = conn.execute("SELECT raw_json FROM receipts WHERE id=?", (rid,)).fetchone()
            # Only a page that had pictures counts: otherwise a later sync asks again
            if raw and any(it.get("image_url") for it in items):
                j = json.loads(raw[0] or "{}")
                j["images_tried"] = True
                conn.execute("UPDATE receipts SET raw_json=? WHERE id=?", (json.dumps(j), rid))
                conn.commit()
        finally:
            conn.close()
        # Not read by the model: Amazon's pictures rarely need it, and it's slow.
        # The product's page has a button to read one.

    threading.Thread(target=_bg, daemon=True).start()


def save_unreadable_page(db_path: str, order_id: str, url: str, error: str, html: str) -> str:
    """Keep a page the extension couldn't read, for fixing the reader. Returns its path."""
    import os
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "amazon-extension")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z-]", "_", order_id or "unknown")[:40]
    path = os.path.join(d, f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{safe}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- {url}\n{error} -->\n{html}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _log_extension(conn, {"order_id": order_id, "outcome": "error", "error": error, "url": url, "saved": path})
    finally:
        conn.close()
    return path
