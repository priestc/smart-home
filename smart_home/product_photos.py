"""Product pictures, photographed in the iOS app.

Product photos are taken together with a receipt: the products from a
shopping trip are photographed around the time its receipt is scanned. A
photo is paired with a receipt scanned (uploaded) within RECEIPT_WINDOW
before it; if there isn't one, or it's still being read, the photo waits
(status 'waiting') and is paired with the next receipt that finishes
scanning. Waiting photos are matched one at a time once it does.

Most photos are taken when the app asks for one of a receipt item right after
the receipt is scanned; those are assigned to that product directly.

Every photo is read by the local vision model (the same service delegator task
that reads receipts), which "info-scrapes" the package: the product's name,
brand, net weight or count, any price tag, the UPC. That reading is kept on
the photo (info_json) as a draft that can be corrected on the Product Photos
page and then copied to the product with "Use" (name, quantity and unit).

A photo that doesn't have a product yet (taken from the Products tab) is also
matched in the same model call: it's given the products on its receipt that
don't have a picture yet (their names, the text printed for them on receipts,
and the stores they were bought at), and picks the one the photo shows, or
says none of them match. Either way the photo can be moved to another product
by hand, which is also how an unmatched photo gets a product.

A product's picture is its most recent photo. Photos are saved next to the
database in product_photos/.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import sqlite3
import threading

from smart_home import delegated, receipts

AI_TASK = receipts.AI_TASK

# What's read off the package. These are also the fields of info_json, which
# can be edited before "Use" copies name, quantity and unit to the product.
INFO_FIELDS = ["name", "brand", "size_text", "quantity", "unit", "price", "price_text", "upc", "other_text"]

_INFO_PROPERTIES = {
    "name": {"type": "string", "maxLength": 120},
    "brand": {"type": ["string", "null"], "maxLength": 60},
    "size_text": {"type": ["string", "null"], "maxLength": 100},
    "quantity": {"type": ["number", "null"]},
    "unit": {"type": ["string", "null"], "enum": receipts.UNITS + [None]},
    "price": {"type": ["number", "null"]},
    "price_text": {"type": ["string", "null"], "maxLength": 100},
    "upc": {"type": ["string", "null"], "maxLength": 20},
    "other_text": {"type": ["string", "null"], "maxLength": 300},
    "notes": {"type": ["string", "null"], "maxLength": 300},
}

_PROMPT = f"""This is a photo of a grocery or household product, taken at home. Read what's printed on it (the package, and any price tag or sticker) for a household product list.

- name: the product as a person would name it on a shopping list: brand, product, and flavor or variety, without the size (e.g. "Red Baron Classic Crust Supreme Pizza", "Great Value 2% Milk").
- brand: the brand alone (e.g. "Red Baron"), or null if none is visible.
- size_text: the net weight, volume or count exactly as printed (e.g. "NET WT 22.3 OZ (1 LB 6.3 OZ) 632g", "12 FL OZ", "24 CT"), or null if not visible.
- quantity and unit: the amount in one package, from size_text. Units are {", ".join(receipts.UNITS)}: a weight in oz (use lb only if it's printed only in pounds; convert grams to oz), fluid ounces as oz, a count of pieces ("24 CT") as ea. Null for both if no size is visible.
- price: the price on a shelf tag, sticker or printed price in the photo, in dollars; null if there is none. price_text: that tag as printed, including any unit price (e.g. "$3.98  24.9¢/oz"), or null.
- upc: the barcode number, digits only, if its digits are readable; otherwise null.
- other_text: other details worth keeping that are printed prominently, e.g. "Family Size", "Organic", "Gluten free", servings; or null.
- notes: one short sentence on anything uncertain (e.g. the size was partly hidden), otherwise null.
"""

_MATCH_PROMPT = """
Also say which of the products below this is. They were bought on the receipt this photo was taken with and don't have a picture yet. Each has an id, a name, and the text the store printed for it on the receipt (often abbreviated, e.g. "GV 2% MLK GAL" is Great Value 2% milk, 1 gallon; "RB CL SUPR" is Red Baron Classic Supreme pizza).

- product_id: the id of the product in the list that this is. Brand, variety and size should agree; a different flavor or variety of the same brand is a different product. Use null if none of them is clearly this product; a wrong match is worse than none. Mention it in notes if two could both be it.

Products:
"""


class PhotoError(Exception):
    pass


RECEIPT_WINDOW = datetime.timedelta(hours=1)

_running: set[int] = set()
_running_lock = threading.Lock()
# Held while pairing photos with receipts, so a photo uploaded just as a
# receipt finishes is never left waiting
_pair_lock = threading.Lock()
# Photos are matched one at a time: the GPU runs one model call at a time
# anyway, and two photos can't both claim the same product
_match_lock = threading.Lock()


def image_dir(db_path: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "product_photos")
    os.makedirs(d, exist_ok=True)
    return d


def is_running(photo_id: int) -> bool:
    with _running_lock:
        return photo_id in _running


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_upload(db_path: str, image: bytes, media_type: str) -> int:
    """Store the photo and create a 'processing' row. Returns its id."""
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(media_type)
    if ext is None:
        raise PhotoError(f"Unsupported image type {media_type!r}; send JPEG, PNG or WebP")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        pid = conn.execute(
            "INSERT INTO product_photos (uploaded_at, status, image_path, media_type) VALUES (?, 'processing', '', ?)",
            (_now(), media_type),
        ).lastrowid
        path = os.path.join(image_dir(db_path), f"{pid}.{ext}")
        with open(path, "wb") as f:
            f.write(image)
        conn.execute("UPDATE product_photos SET image_path=? WHERE id=?", (path, pid))
        conn.commit()
        return pid
    finally:
        conn.close()


def candidates(conn: sqlite3.Connection, receipt_id: int | None = None) -> list[dict]:
    """Products without a picture, minus savings/coupon lines (negative price).
    With receipt_id, only the products on that receipt."""
    rows = conn.execute("""
        SELECT p.id, p.name,
               (SELECT GROUP_CONCAT(t, ' | ') FROM (SELECT DISTINCT i.raw_text AS t FROM receipt_items i
                    WHERE i.product_id = p.id AND i.raw_text IS NOT NULL)) AS printed,
               (SELECT GROUP_CONCAT(DISTINCT r.merchant) FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id
                    WHERE i.product_id = p.id) AS stores,
               (SELECT ROUND(i.total_price / CASE WHEN i.unit = 'ea' AND i.quantity > 0 THEN i.quantity ELSE 1 END, 2) FROM receipt_items i WHERE i.product_id = p.id
                    ORDER BY i.id DESC LIMIT 1) AS last_price
        FROM products p
        WHERE p.id NOT IN (SELECT product_id FROM product_photos WHERE product_id IS NOT NULL)
          AND (? IS NULL OR p.id IN (SELECT product_id FROM receipt_items WHERE receipt_id = ?))
        ORDER BY p.name COLLATE NOCASE
    """, (receipt_id, receipt_id)).fetchall()
    return [{"id": r[0], "name": r[1], "printed": r[2], "stores": r[3]}
            for r in rows if r[4] is None or r[4] >= 0]


def _ask(image: bytes, cands: list[dict]) -> dict:
    """Read the package; with cands, also pick which of them it is (product_id)."""
    import httpx

    image = receipts._shrink_for_model(image)
    props = dict(_INFO_PROPERTIES)
    prompt = _PROMPT
    if cands:
        lines = "\n".join(
            f'- id {c["id"]}: {c["name"]}'
            + (f' (printed as {c["printed"]})' if c["printed"] else "")
            + (f' [{c["stores"]}]' if c["stores"] else "")
            for c in cands
        )
        prompt += _MATCH_PROMPT + lines
        # Only an id from the list (or null) is a valid answer
        props["product_id"] = {"type": ["integer", "null"], "enum": [c["id"] for c in cands] + [None]}
    schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    try:
        resp = httpx.post(
            f"{delegated.SERVICE_DELEGATOR_URL}/api/ask",
            json={
                "task": AI_TASK,
                "project": "smart-home",
                "prompt": prompt,
                "images": [base64.standard_b64encode(image).decode("ascii")],
                "json_schema": schema,
            },
            timeout=900.0,
        )
    except httpx.HTTPError as e:
        raise PhotoError(f"Could not reach the service delegator at {delegated.SERVICE_DELEGATOR_URL}: {e}")
    try:
        body = resp.json()
    except ValueError:
        raise PhotoError(f"Service delegator returned HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code != 200:
        notes = "; ".join(body.get("notes") or [])
        raise PhotoError((body.get("error") or f"Service delegator returned HTTP {resp.status_code}")
                         + (f" ({notes})" if notes else ""))
    text = body.get("text") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise PhotoError(f"Unparseable response from {body.get('model')}: {text[:200]}")


def process(db_path: str, photo_id: int, match: bool = True) -> None:
    """Read one photo's package info, and match it to a product on its receipt
    if it has none (and `match`). Records the outcome on its row. Callers queue
    it with _run_in_background, which marks it running."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("SELECT image_path, receipt_id, product_id FROM product_photos WHERE id=?",
                           (photo_id,)).fetchone()
        if row is None:
            raise PhotoError(f"Photo {photo_id} not found")
        path, rid, current = row
        match = match and current is None
        conn.execute("UPDATE product_photos SET info_status='reading', info_error=NULL WHERE id=?", (photo_id,))
        if match:
            conn.execute("UPDATE product_photos SET status='processing', error=NULL WHERE id=?", (photo_id,))
        conn.commit()
        try:
            with open(path, "rb") as f:
                image = f.read()
            cands = candidates(conn, rid) if match and rid is not None else []
            data = _ask(image, cands)
            info = {k: data.get(k) for k in INFO_FIELDS}
            conn.execute(
                "UPDATE product_photos SET info_json=?, info_status='done', info_error=NULL, model_seen=?, "
                "model_notes=?, processed_at=? WHERE id=?",
                (json.dumps(info), info.get("name"), data.get("notes"), _now(), photo_id))
            if match:
                _record_match(conn, photo_id, rid, cands, data)
        except Exception as e:
            msg = str(e) if isinstance(e, PhotoError) else f"{type(e).__name__}: {e}"
            conn.execute("UPDATE product_photos SET info_status='error', info_error=?, processed_at=? WHERE id=?",
                         (msg, _now(), photo_id))
            if match:
                conn.execute("UPDATE product_photos SET status='error', error=? WHERE id=?", (msg, photo_id))
            raise
        finally:
            conn.commit()
    finally:
        conn.close()
        with _running_lock:
            _running.discard(photo_id)


def _record_match(conn: sqlite3.Connection, photo_id: int, rid: int | None, cands: list[dict], data: dict) -> None:
    pid = data.get("product_id")
    if pid not in {c["id"] for c in cands}:
        pid = None
    claimed = 0
    if pid:
        # Only claim the product if it still has no picture: another photo
        # may have taken it while the model ran. Never cover an existing picture.
        claimed = conn.execute(
            "UPDATE product_photos SET status='matched', product_id=?, match_source='model', model_product_id=? "
            "WHERE id=? AND NOT EXISTS (SELECT 1 FROM product_photos o WHERE o.product_id=? AND o.id != ?)",
            (pid, pid, photo_id, pid, photo_id),
        ).rowcount
    if claimed:
        return
    if pid:
        why = "That product got a picture from another photo while this one was being read"
    elif rid is None:
        why = "Not paired with a receipt, so there was nothing to match it to"
    elif not cands:
        why = f"Every product on receipt {rid} already has a picture"
    else:
        why = None
    notes = data.get("notes")
    if why:
        notes = f"{why}. {notes}" if notes else why + "."
    conn.execute(
        "UPDATE product_photos SET status='unmatched', product_id=NULL, match_source=NULL, "
        "model_product_id=?, model_notes=? WHERE id=?", (pid, notes, photo_id))


def _run_in_background(db_path: str, photo_ids: list[int], match: bool = True) -> None:
    def _bg():
        for photo_id in photo_ids:
            with _match_lock:
                try:
                    process(db_path, photo_id, match)
                except Exception as e:
                    # Also recorded on the photo's row and shown in the app
                    print(f"Product photo {photo_id} reading failed: {e}", flush=True)

    # Marked running now, so the photos don't look interrupted while queued
    with _running_lock:
        busy = _running.intersection(photo_ids)
        if busy:
            raise PhotoError(f"Photo {min(busy)} is already being read")
        _running.update(photo_ids)
    threading.Thread(target=_bg, daemon=True).start()


def _queue(conn: sqlite3.Connection, photo_ids: list[int], match: bool = True) -> None:
    """Mark photos as queued for reading (and matching, for those without a product)."""
    for i in photo_ids:
        conn.execute("UPDATE product_photos SET info_status='reading', info_error=NULL WHERE id=?", (i,))
        if match:
            conn.execute("UPDATE product_photos SET status='processing', error=NULL "
                         "WHERE id=? AND product_id IS NULL", (i,))
    conn.commit()


def start(db_path: str, photo_id: int, match: bool = True) -> None:
    """Read a new (or re-read) photo. One without a product (and `match`) is
    paired with its receipt and matched too, or left waiting for the next
    receipt. A re-read photo keeps the receipt it had."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with _pair_lock:
            row = conn.execute("SELECT uploaded_at, receipt_id, product_id FROM product_photos WHERE id=?",
                               (photo_id,)).fetchone()
            if row is None:
                raise PhotoError(f"Photo {photo_id} not found")
            uploaded, rid, current = row
            match = match and current is None
            if match and rid is None:
                since = (datetime.datetime.strptime(uploaded, "%Y-%m-%d %H:%M:%S")
                         - RECEIPT_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
                recent = conn.execute(
                    "SELECT id, status FROM receipts WHERE kind='receipt' AND image_path != '' "
                    "AND uploaded_at BETWEEN ? AND ? ORDER BY uploaded_at DESC LIMIT 1",
                    (since, uploaded)).fetchone()
                if recent is None or recent[1] != "done":
                    # None yet, or still being read: on_receipt_scanned picks it up
                    conn.execute("UPDATE product_photos SET status='waiting', error=NULL, product_id=NULL, "
                                 "match_source=NULL WHERE id=?", (photo_id,))
                    conn.commit()
                    return
                rid = recent[0]
                conn.execute("UPDATE product_photos SET receipt_id=? WHERE id=?", (rid, photo_id))
            _queue(conn, [photo_id], match)
        _run_in_background(db_path, [photo_id], match)
    finally:
        conn.close()


def assign_taken_for(db_path: str, photo_id: int, product_id: int, receipt_id: int | None) -> None:
    """The app asked for a photo of this product after scanning its receipt, so
    it's that product (match_source 'receipt'). A product that got a picture in
    the meantime keeps it; this photo is then left without a product."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with _pair_lock:
            claimed = conn.execute(
                "UPDATE product_photos SET status='matched', product_id=?, match_source='receipt', receipt_id=?, "
                "processed_at=? WHERE id=? AND NOT EXISTS (SELECT 1 FROM product_photos o WHERE o.product_id=?)",
                (product_id, receipt_id, _now(), photo_id, product_id)).rowcount
            if not claimed:
                name = (conn.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone() or ["?"])[0]
                conn.execute(
                    "UPDATE product_photos SET status='unmatched', receipt_id=?, model_notes=?, processed_at=? WHERE id=?",
                    (receipt_id, f"Taken for {name}, which already has a picture. Pick it by hand to replace that one.",
                     _now(), photo_id))
            conn.commit()
    finally:
        conn.close()
    start(db_path, photo_id, match=False)


def read_unread(db_path: str) -> int:
    """Read the package info of every photo never read (e.g. from before photos
    were read). Doesn't match them. Returns how many were queued."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with _pair_lock:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM product_photos WHERE info_status IS NULL ORDER BY id") if not is_running(r[0])]
            if not ids:
                return 0
            _queue(conn, ids, match=False)
        _run_in_background(db_path, ids, match=False)
        return len(ids)
    finally:
        conn.close()


def clean_info(data: dict) -> tuple[dict, str | None]:
    """Validate edited info fields (a subset of INFO_FIELDS). Returns (fields, error)."""
    out = {}
    for k, v in data.items():
        if k not in INFO_FIELDS:
            return {}, f"Unknown field {k!r}"
        if k in ("quantity", "price"):
            if v in (None, ""):
                v = None
            else:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    return {}, f"{k} must be a number"
                if v <= 0:
                    return {}, f"{k} must be more than zero"
        elif k == "unit":
            v = v or None
            if v is not None and v not in receipts.UNITS:
                return {}, f'unit must be one of: {", ".join(receipts.UNITS)}'
        else:
            v = (str(v).strip() if v is not None else "") or None
            if k == "upc" and v is not None and not v.isdigit():
                return {}, "upc must be digits only"
        out[k] = v
    return out, None


def on_receipt_scanned(db_path: str, receipt_id: int) -> None:
    """A receipt finished scanning: pair every waiting photo with it and match
    them. A re-read of an older receipt (uploaded over RECEIPT_WINDOW ago)
    isn't the trip the waiting photos came from, so it leaves them waiting."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with _pair_lock:
            row = conn.execute("SELECT uploaded_at FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None or (datetime.datetime.now()
                               - datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")) > RECEIPT_WINDOW:
                return
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM product_photos WHERE status='waiting' ORDER BY id") if not is_running(r[0])]
            if not ids:
                return
            conn.executemany("UPDATE product_photos SET receipt_id=? WHERE id=?", [(receipt_id, i) for i in ids])
            _queue(conn, ids)
        _run_in_background(db_path, ids)
    finally:
        conn.close()
