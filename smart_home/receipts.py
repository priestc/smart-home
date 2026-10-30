"""Receipt scanning for the cost-of-living tracker.

The iOS app uploads a photo of a receipt; it's saved next to the database and
sent to a local vision model on Ollama, which reads it and returns the
merchant, date, line items (each assigned a spending category), tax and total
as structured JSON. Items are stored in receipt_items so spending can be
summed by category over time. A small local model will misread some lines, so
everything it extracts can be corrected by hand in the app.

Each item keeps the text and item number exactly as printed. Those form its
name_key, which links it to a human-friendly name set on the /receipts/items
web page; the next time the same item shows up it gets that name automatically.

Utility bills (water, electricity, propane, internet...) are photographed the
same way. The model tells them apart from store receipts; they're stored in
the same table with kind='bill', the utility, and the usage printed on the
bill (e.g. 1440 gallons), and shown on the /utilities page instead.
That page also sets an item's default quantity (e.g. 21.7 oz); a quantity on
the receipt line itself (a printed weight, or one entered by hand) overrides it.

Faded or torn receipts: items already seen on earlier receipts from the same
store form a catalog. Partly readable lines are matched against it (filling
in the item number and, if unreadable, the price). If the result still
doesn't check out (the printed "# ITEMS SOLD" count, missing prices, or items
not adding up to the total) the photo is read a second time with that
catalog and the specific problems given to the model, and the better of the
two readings is kept.
"""
from __future__ import annotations

import base64
import datetime
import difflib
import json
import os
import re
import sqlite3
import threading

from smart_home import delegated, gpu_monitor

# Photos are read through the service delegator on this machine (see
# delegated.py). Its "receipt-reader" task picks the model (a local Ollama
# vision model, qwen3-vl:8b-instruct) and logs each call with GPU readings.
AI_TASK = "receipt-reader"
# Longest side of the copy sent to the model. Image tokens grow with pixel
# count and dominate processing time; receipt text stays legible at this size.
MODEL_MAX_DIMENSION = 1600

CATEGORIES = [
    "Groceries",
    "Dining Out",
    "Alcohol",
    "Household",
    "Personal Care",
    "Health & Pharmacy",
    "Baby & Kids",
    "Pets",
    "Clothing",
    "Electronics",
    "Home Improvement",
    "Auto & Fuel",
    "Entertainment",
    "Gifts",
    "Fees & Deposits",
    "Utilities",
    "Other",
]
# What a utility bill (kind='bill') is for
UTILITIES = ["Electricity", "Water", "Propane", "Natural Gas", "Internet", "Phone", "Trash", "Sewer", "Other"]
UTILITY_CATEGORY = "Utilities"
# Units for an item's quantity: a count, ounces or pounds
UNITS = ["ea", "oz", "lb"]
# Not an item category: receipts.tax is reported under this name in summaries
TAX_CATEGORY = "Sales Tax"

_SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean"},
        "document_type": {"type": "string", "enum": ["receipt", "utility_bill"]},
        "utility": {"type": ["string", "null"], "enum": UTILITIES + [None]},
        "usage": {"type": ["number", "null"]},
        "usage_unit": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "merchant": {"type": ["string", "null"]},
        "purchase_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_text": {"type": "string"},
                    "item_code": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"], "enum": UNITS + [None]},
                    "total_price": {"type": ["number", "null"]},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["raw_text", "item_code", "description", "quantity", "unit", "total_price", "category"],
                "additionalProperties": False,
            },
        },
        "items_sold": {"type": ["integer", "null"]},
        "subtotal": {"type": ["number", "null"]},
        "tax": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["is_receipt", "document_type", "utility", "usage", "usage_unit", "due_date", "merchant", "purchase_date", "items", "items_sold", "subtotal", "tax", "total", "notes"],
    "additionalProperties": False,
}

_PROMPT = f"""This is a photo of a store or restaurant receipt, or of a utility bill. Extract it for a personal cost-of-living tracker.

- document_type: "utility_bill" for a bill from a utility or service provider (water, electricity, propane or gas delivery, internet, phone, trash, sewer), otherwise "receipt". A dump, landfill or transfer station scale ticket is a utility bill for "Trash".
- For a utility bill:
  - utility: one of {", ".join(UTILITIES)}. For a receipt use null, and null for usage, usage_unit and due_date too.
  - merchant: the company's name (e.g. "Moongate Water Co."). purchase_date: the billing or statement date.
  - usage and usage_unit: the amount used this period as printed, e.g. GALLONS USED 1440 gives 1440, "gal"; 812 KWH gives 812, "kWh"; propane delivered in gallons gives "gal"; a transfer station's net weight "20 lb" gives 20, "lb". Null for both if no usage is printed (e.g. internet).
  - due_date: the payment due date as YYYY-MM-DD, or null.
  - items: each charge for this period (e.g. "WATER CHRG.", "CONSERVATION FEE", "Monthly service") with category "{UTILITY_CATEGORY}". Fees and surcharges are charges wherever they are printed, including below the subtotal (e.g. "Environmental Impact Fee", "Fuel Surcharge Fee"): include each one as an item, even when it is 0.00. Do not include tax, meter readings, subtotal, balance forward, previous balance, payments or the total as items.
  - tax: the tax charged. total: this period's charges plus tax (leave out any balance forward or past-due amount). subtotal: null. items_sold: null. Skip the store-receipt rules below.

- merchant: the store or restaurant name as a person would say it (e.g. "Costco", not "COSTCO WHOLESALE #1234").
- purchase_date: the transaction date as YYYY-MM-DD, or null if it isn't printed.
- items_sold: the "# ITEMS SOLD" count printed on the receipt (Walmart and Sam's Club print one near the top or bottom), or null if there isn't one. When it is printed, return exactly that many purchased item lines (savings and coupon lines don't count), one for each line, even when part of a line is faded or torn.
- items: one entry per purchased line, top to bottom.
  - raw_text: the item's name exactly as printed, abbreviations and all (e.g. "GV 2% MLK GAL"). Leave out the price and item number. Keep markers such as "INST SV" (e.g. "INST SV LA CROIX VPF").
  - item_code: the item number, UPC or SKU printed on that line, digits only, or null if there isn't one. Walmart prints a 12-digit number after each item name; Sam's Club prints it before. Copy every digit carefully, and only from that same line. Savings and coupon lines usually have no number of their own: use null.
  - quantity and unit: only when the receipt prints an amount for that line, e.g. a weight "1.33 lb @ 5.12 /lb" gives quantity 1.33, unit "lb"; "2 @ 3.97" gives quantity 2, unit "ea". Weights are often on the line just below the item. Units are "ea", "oz" or "lb". Otherwise null for both.
  - description: Expand cryptic abbreviations into a readable description (e.g. "GV 2% MLK GAL" -> "Great Value 2% milk, 1 gal"). total_price is the line's final amount in dollars. A minus sign after the amount means it is negative: "2.00-" is -2.00. Discounts, coupons and instant savings are their own lines with a negative total_price, in the same category as the item they discount; describe them as e.g. "Instant savings: La Croix". On Sam's Club receipts "INST SV" means instant savings and is always negative, even if the minus sign is hard to see. Bottle deposits, bag fees and similar go in "Fees & Deposits". Do not include sales tax, subtotal, total, payment or change lines as items. Each price is on the same row as its item: if a line's price is faded or unreadable, use null for that line rather than taking a price from a neighbouring row.
- category: choose the single best fit from: {", ".join(CATEGORIES)}. A restaurant meal or takeout is "Dining Out"; food bought at a store is "Groceries".
- subtotal, tax, total: as printed, or null if missing. For restaurants, include any tip in total.
- If the image is not a receipt or is unreadable, set is_receipt to false, leave items empty and explain why in notes.
- notes: anything uncertain (e.g. a line you couldn't read), otherwise null."""


class ReceiptError(Exception):
    pass


_running: set[int] = set()
_running_lock = threading.Lock()


def image_dir(db_path: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "receipts")
    os.makedirs(d, exist_ok=True)
    return d


def is_running(receipt_id: int) -> bool:
    with _running_lock:
        return receipt_id in _running


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_upload(db_path: str, image: bytes, media_type: str) -> int:
    """Store the photo and create a 'processing' receipt row. Returns its id."""
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(media_type)
    if ext is None:
        raise ReceiptError(f"Unsupported image type {media_type!r}; send JPEG, PNG or WebP")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cur = conn.execute(
            "INSERT INTO receipts (uploaded_at, status, image_path, media_type) VALUES (?, 'processing', '', ?)",
            (_now(), media_type),
        )
        rid = cur.lastrowid
        path = os.path.join(image_dir(db_path), f"{rid}.{ext}")
        with open(path, "wb") as f:
            f.write(image)
        conn.execute("UPDATE receipts SET image_path=? WHERE id=?", (path, rid))
        conn.commit()
        return rid
    finally:
        conn.close()


def _shrink_for_model(image: bytes) -> bytes:
    """Downscale so the longest side is at most MODEL_MAX_DIMENSION; returns JPEG bytes."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ReceiptError("Could not decode the uploaded image")
    h, w = img.shape[:2]
    scale = MODEL_MAX_DIMENSION / max(h, w)
    if scale >= 1:
        return image
    img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ReceiptError("Could not re-encode the resized image")
    return buf.tobytes()


def _extract(image: bytes, extra: str = "") -> dict:
    """One reading of the photo. `extra` is appended to the prompt (used for
    the second, catalog-assisted reading)."""
    import httpx

    image = _shrink_for_model(image)
    try:
        resp = httpx.post(
            f"{delegated.SERVICE_DELEGATOR_URL}/api/ask",
            json={
                "task": AI_TASK,
                "project": "smart-home",
                "prompt": _PROMPT + extra,
                "images": [base64.standard_b64encode(image).decode("ascii")],
                "json_schema": _SCHEMA,
            },
            timeout=900.0,
        )
    except httpx.HTTPError as e:
        raise ReceiptError(f"Could not reach the service delegator at {delegated.SERVICE_DELEGATOR_URL}: {e}")
    try:
        body = resp.json()
    except ValueError:
        raise ReceiptError(f"Service delegator returned HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code != 200:
        notes = "; ".join(body.get("notes") or [])
        raise ReceiptError((body.get("error") or f"Service delegator returned HTTP {resp.status_code}")
                           + (f" ({notes})" if notes else ""))
    text = body.get("text") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ReceiptError(f"Unparseable response from {body.get('model')}: {text[:200]}")


def name_key(raw_text: str | None, item_code: str | None) -> str | None:
    """What identifies an item across receipts: its item number when one is
    printed (it tells apart products whose printed names are identical),
    otherwise the printed text."""
    code = re.sub(r"\D", "", item_code or "")
    if code:
        return f"code:{code}"
    text = " ".join((raw_text or "").upper().split())
    return f"text:{text}" if text else None


def _store_family(merchant: str | None) -> str:
    """Groups a chain's variants: "Walmart Neighborhood Market" and "Walmart"
    both give "walmart"; "Sam's Club" gives "sams"."""
    words = (merchant or "").lower().split()
    return re.sub(r"[^a-z0-9]", "", words[0]) if words else ""


def _norm_text(t: str | None) -> str:
    return " ".join(re.sub(r"[^A-Z0-9% ]", " ", (t or "").upper()).split())


def _catalog(conn: sqlite3.Connection, rid: int, merchant: str | None) -> list[dict]:
    """Items with an item number seen on other receipts from the same store,
    most frequently bought first."""
    family = _store_family(merchant)
    if not family:
        return []
    by_code: dict[str, dict] = {}
    for code, raw, price, m, name in conn.execute(
        "SELECT i.item_code, i.raw_text, i.total_price, r.merchant, n.name FROM receipt_items i "
        "JOIN receipts r ON r.id = i.receipt_id LEFT JOIN receipt_known_items n ON n.name_key = i.name_key "
        "WHERE i.item_code IS NOT NULL AND i.total_price > 0 AND r.status = 'done' AND r.id != ? "
        "ORDER BY i.id DESC", (rid,)
    ):
        if _store_family(m) != family:
            continue
        entry = by_code.setdefault(code, {"code": code, "raw_text": raw, "price": price, "name": name, "seen": 0})
        entry["seen"] += 1
    return sorted(by_code.values(), key=lambda e: -e["seen"])


def _match(item: dict, catalog: list[dict]) -> dict | None:
    """The catalog entry a partly readable line most likely is, or None.

    A complete item number that isn't in the catalog is a different product
    (several products can share a printed name like "PIZZA"), so it only
    matches when both its digits and text are very close, i.e. a digit or two
    misread. A partial number (the paper is torn or faded) or none at all is
    matched on the printed text. The best match must clearly beat the
    runner-up."""
    code = re.sub(r"\D", "", item.get("item_code") or "")
    text = _norm_text(item.get("raw_text"))
    if code:
        for e in catalog:
            if e["code"] == code:
                return e
    scored = []
    for e in catalog:
        t = difflib.SequenceMatcher(None, text, _norm_text(e["raw_text"])).ratio() if text else 0.0
        c = difflib.SequenceMatcher(None, code, e["code"]).ratio() if code else 0.0
        if code and len(code) >= len(e["code"]):
            ok = c >= 0.75 and t >= 0.6 and c + t >= 1.6
        elif code and len(code) >= 5 and e["code"].startswith(code):
            ok = t >= 0.6
        else:
            ok = t >= 0.85
        if ok:
            scored.append((t + c, e))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:
        return None
    return scored[0][1]


def _reconcile(data: dict, catalog: list[dict]) -> list[str]:
    """Fix lines against the catalog in place. Returns what was changed."""
    fixed_codes, filled_prices = [], []
    for it in data.get("items") or []:
        e = _match(it, catalog)
        if e is None:
            continue
        if it.get("item_code") != e["code"]:
            fixed_codes.append(e["raw_text"])
            it["item_code"] = e["code"]
            it["raw_text"] = e["raw_text"]
        if it.get("total_price") is None:
            it["total_price"] = e["price"]
            filled_prices.append(e["raw_text"])
    changes = []
    if fixed_codes:
        changes.append(f"Matched {len(fixed_codes)} partly unreadable line(s) to items from earlier receipts: "
                       + ", ".join(fixed_codes) + ".")
    if filled_prices:
        changes.append(f"Price unreadable, used the last price paid for: {', '.join(filled_prices)}.")
    return changes


def _problems(data: dict) -> list[str]:
    """Reasons to doubt a reading, in words the model can act on."""
    items = data.get("items") or []
    out = []
    sold = data.get("items_sold")
    n = sum(1 for it in items if it.get("total_price") is None or it["total_price"] > 0)
    if sold and n != sold:
        out.append(f'The receipt prints "# ITEMS SOLD {sold}", but {n} item lines were read.')
    missing = [it.get("raw_text") or it.get("description") for it in items if it.get("total_price") is None]
    if missing:
        out.append(f"No price was read for: {', '.join(missing)}.")
    target = data.get("subtotal")
    if target is None and data.get("total") is not None:
        target = data["total"] - (data.get("tax") or 0)
    if target is not None and not missing:
        got = sum(it["total_price"] for it in items)
        if abs(got - target) >= 0.01:
            out.append(f"The item prices add up to {got:.2f}, but the receipt's subtotal is {target:.2f}.")
    return out


def _retry_hint(problems: list[str], catalog: list[dict]) -> str:
    lines = ["", "", "A first reading of this receipt had these problems:"]
    lines += [f"- {p}" for p in problems]
    lines.append("Read every line again carefully. The paper may be faded or torn.")
    if catalog:
        lines.append("Items bought at this store before (item number, printed name, last price). "
                     "A partly readable line is very likely one of these; use its full item number:")
        for e in catalog[:80]:
            lines.append(f'{e["code"]}  {e["raw_text"]}  {e["price"]:.2f}')
    return "\n".join(lines)


# Lines on a bill that aren't charges for the period, which the model sometimes
# lists as items anyway
_BILL_TAX_RE = re.compile(r"^\s*(SALES\s+|STATE\s+|GROSS\s+RECEIPTS\s+)?TAX(ES)?\b", re.I)
_BILL_NOT_CHARGE_RE = re.compile(r"\b(SUB\s*TOTAL|TOTAL|BALANCE|PAYMENTS?|PREVIOUS|AMOUNT\s+DUE)\b", re.I)


def _clean_bill(data: dict) -> None:
    """Take tax, totals and balances out of a bill's charges (in place)."""
    charges = []
    for it in data.get("items") or []:
        text = it.get("raw_text") or it.get("description") or ""
        if _BILL_TAX_RE.search(text):
            if data.get("tax") is None:
                data["tax"] = it.get("total_price")
        elif not _BILL_NOT_CHARGE_RE.search(text):
            charges.append(it)
    for it in charges:
        if it.get("total_price") is None:
            it["total_price"] = 0.0
    data["items"] = charges
    data["subtotal"] = round(sum(it["total_price"] for it in charges), 2)


def _read(conn: sqlite3.Connection, rid: int, image: bytes) -> dict:
    """Read the photo, check it against earlier receipts, and read it a second
    time with that help if it still doesn't add up."""
    data = _extract(image)
    if not data.get("is_receipt"):
        return data
    if data.get("document_type") == "utility_bill":
        # No store catalog or item count to check a bill against; a mismatch
        # between its charges and total is flagged on the page instead
        _clean_bill(data)
        return data
    catalog = _catalog(conn, rid, data.get("merchant"))
    changes = _reconcile(data, catalog)
    problems = _problems(data)
    if problems:
        second = _extract(image, _retry_hint(problems, catalog))
        if second.get("is_receipt"):
            changes2 = _reconcile(second, catalog)
            problems2 = _problems(second)
            if len(problems2) < len(problems):
                data, changes, problems = second, changes2, problems2
    # A single missing price can be worked out from the subtotal
    items = data.get("items") or []
    missing = [it for it in items if it.get("total_price") is None]
    if len(missing) == 1 and data.get("subtotal") is not None:
        rest = sum(it["total_price"] for it in items if it is not missing[0])
        missing[0]["total_price"] = round(data["subtotal"] - rest, 2)
        changes.append(f"Price unreadable for {missing[0].get('raw_text')}; worked out from the subtotal.")
        problems = _problems(data)
    for it in items:
        if it.get("total_price") is None:
            it["total_price"] = 0.0
    data["notes"] = " ".join(filter(None, [data.get("notes"), *changes, *problems])) or None
    return data


# Printed markers for a line that takes money off: a savings line read as a
# positive amount is a misread (the trailing "-" is easy to miss).
_SAVINGS_RE = re.compile(r"\b(INST(ANT)?\s*SV|INSTANT SAVINGS|SAVINGS|COUPON|CPN|DISCOUNT|DISC)\b", re.I)


def _store_result(conn: sqlite3.Connection, rid: int, data: dict) -> None:
    if not data.get("is_receipt"):
        raise ReceiptError(data.get("notes") or "This doesn't look like a receipt")
    date = data.get("purchase_date")
    try:
        datetime.date.fromisoformat(date or "")
    except ValueError:
        date = None
    bill = data.get("document_type") == "utility_bill"
    names = dict(conn.execute("SELECT name_key, name FROM receipt_known_items WHERE name IS NOT NULL").fetchall())
    rows = []
    for i, it in enumerate(data.get("items") or []):
        code = re.sub(r"\D", "", it.get("item_code") or "") or None
        # A bill's charges aren't products, so they stay off the Item names page
        key = None if bill else name_key(it.get("raw_text"), code)
        price = round(float(it["total_price"]), 2)
        if price > 0 and _SAVINGS_RE.search(f'{it.get("raw_text") or ""} {it["description"]}'):
            price = -price
        qty, unit = it.get("quantity"), it.get("unit")
        if qty is None or unit not in UNITS or qty <= 0:
            qty, unit = None, None
        rows.append((rid, i, names.get(key) or it["description"], qty, unit, price,
                     UTILITY_CATEGORY if bill else it["category"] if it["category"] in CATEGORIES else "Other",
                     it.get("raw_text"), code, key))
    conn.execute("DELETE FROM receipt_items WHERE receipt_id=?", (rid,))
    conn.executemany(
        "INSERT INTO receipt_items (receipt_id, position, description, quantity, unit, total_price, category, "
        "raw_text, item_code, name_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    utility = usage = usage_unit = due = None
    if bill:
        utility = data.get("utility") if data.get("utility") in UTILITIES else "Other"
        usage = data.get("usage")
        usage_unit = (data.get("usage_unit") or "").strip() or None
        if usage is None or usage_unit is None:
            usage = usage_unit = None
        due = data.get("due_date")
        try:
            datetime.date.fromisoformat(due or "")
        except ValueError:
            due = None
    conn.execute(
        "UPDATE receipts SET status='done', error=NULL, processed_at=?, merchant=?, purchase_date=?, "
        "subtotal=?, tax=?, total=?, notes=?, raw_json=?, kind=?, utility=?, usage_amount=?, usage_unit=?, "
        "due_date=? WHERE id=?",
        (_now(), data.get("merchant"), date, data.get("subtotal"), data.get("tax"),
         data.get("total"), data.get("notes"), json.dumps(data), "bill" if bill else "receipt",
         utility, usage, usage_unit, due, rid),
    )


def process(db_path: str, rid: int) -> None:
    """Run extraction for one receipt and record the outcome on its row."""
    with _running_lock:
        if rid in _running:
            raise ReceiptError("This receipt is already being processed")
        _running.add(rid)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("SELECT image_path, media_type FROM receipts WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise ReceiptError(f"Receipt {rid} not found")
        conn.execute("UPDATE receipts SET status='processing', error=NULL WHERE id=?", (rid,))
        conn.commit()
        try:
            with open(row[0], "rb") as f:
                image = f.read()
            # Temperatures during the scan, for checking the GPU's cooling (/gpu)
            with gpu_monitor.Recorder(db_path, rid):
                data = _read(conn, rid, image)
            _store_result(conn, rid, data)
        except Exception as e:
            msg = str(e) if isinstance(e, ReceiptError) else f"{type(e).__name__}: {e}"
            conn.execute("UPDATE receipts SET status='error', error=?, processed_at=? WHERE id=?", (msg, _now(), rid))
            raise
        finally:
            conn.commit()
    finally:
        conn.close()
        with _running_lock:
            _running.discard(rid)


def process_in_background(db_path: str, rid: int) -> None:
    def _bg():
        try:
            process(db_path, rid)
        except Exception as e:
            # Also recorded on the receipt row and shown in the app
            print(f"Receipt {rid} processing failed: {e}", flush=True)

    threading.Thread(target=_bg, daemon=True).start()
