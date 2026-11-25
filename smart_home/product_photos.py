"""Product pictures, photographed in the iOS app.

Product photos are taken together with a receipt: the products from a
shopping trip are photographed around the time its receipt is scanned. A
photo is paired with a receipt scanned (uploaded) within RECEIPT_WINDOW
before it; if there isn't one, or it's still being read, the photo waits
(status 'waiting') and is paired with the next receipt that finishes
scanning. Waiting photos are matched one at a time once it does.

Each photo is sent to the local vision model (the same service delegator task
that reads receipts) with the products on its receipt that don't have a
picture yet: their names, the text printed for them on receipts, and the
stores they were bought at. The model picks the product the photo shows, or
says none of them match. Either way the photo can be moved to another product by hand (the
Products tab in the app), which is also how an unmatched photo gets a product.

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

_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "seen": {"type": "string"},
        "product_id": {"type": ["integer", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["seen", "product_id", "notes"],
    "additionalProperties": False,
}

_PROMPT = """This is a photo of a grocery or household product, taken at home. Below is a list of products bought on earlier store receipts that don't have a picture yet. Each has an id, a name, and the text the store printed for it on the receipt (often abbreviated, e.g. "GV 2% MLK GAL" is Great Value 2% milk, 1 gallon; "RB CL SUPR" is Red Baron Classic Supreme pizza).

- seen: what the photo shows, as printed on the package: brand, product, flavor or variety, and size (e.g. "Red Baron Classic Crust Supreme pizza, 22.3 oz").
- product_id: the id of the product in the list that this is. Brand, variety and size should agree; a different flavor or variety of the same brand is a different product. Use null if none of them is clearly this product; a wrong match is worse than none.
- notes: anything uncertain, e.g. two products that could both be it, otherwise null.

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
    import httpx

    image = receipts._shrink_for_model(image)
    lines = "\n".join(
        f'- id {c["id"]}: {c["name"]}'
        + (f' (printed as {c["printed"]})' if c["printed"] else "")
        + (f' [{c["stores"]}]' if c["stores"] else "")
        for c in cands
    )
    schema = json.loads(json.dumps(_SCHEMA_BASE))
    # Only an id from the list (or null) is a valid answer
    schema["properties"]["product_id"]["enum"] = [c["id"] for c in cands] + [None]
    try:
        resp = httpx.post(
            f"{delegated.SERVICE_DELEGATOR_URL}/api/ask",
            json={
                "task": AI_TASK,
                "project": "smart-home",
                "prompt": _PROMPT + (lines or "(none)"),
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


def process(db_path: str, photo_id: int) -> None:
    """Match one photo to a product and record the outcome on its row.
    Callers queue it with _match_all_in_background, which marks it running."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("SELECT image_path, receipt_id FROM product_photos WHERE id=?", (photo_id,)).fetchone()
        if row is None:
            raise PhotoError(f"Photo {photo_id} not found")
        rid = row[1]
        conn.execute("UPDATE product_photos SET status='processing', error=NULL WHERE id=?", (photo_id,))
        conn.commit()
        try:
            with open(row[0], "rb") as f:
                image = f.read()
            if rid is None:
                raise PhotoError("This photo hasn't been paired with a receipt yet")
            cands = candidates(conn, rid)
            if not cands:
                raise PhotoError(f"Every product on receipt {rid} already has a picture; pick the product by hand")
            data = _ask(image, cands)
            pid = data.get("product_id")
            if pid not in {c["id"] for c in cands}:
                pid = None
            claimed = 0
            if pid:
                # Only claim the product if it still has no picture: another photo
                # matched at the same time may have taken it while the model ran.
                # Never cover an existing picture.
                claimed = conn.execute(
                    "UPDATE product_photos SET status='matched', product_id=?, match_source='model', "
                    "model_product_id=?, model_seen=?, model_notes=?, processed_at=? "
                    "WHERE id=? AND NOT EXISTS (SELECT 1 FROM product_photos o WHERE o.product_id=? AND o.id != ?)",
                    (pid, pid, data.get("seen"), data.get("notes"), _now(), photo_id, pid, photo_id),
                ).rowcount
            if not claimed:
                notes = data.get("notes")
                if pid:
                    taken = "That product got a picture from another photo while this one was being matched"
                    notes = f"{taken}. {notes}" if notes else taken
                conn.execute(
                    "UPDATE product_photos SET status='unmatched', product_id=NULL, match_source=NULL, "
                    "model_product_id=?, model_seen=?, model_notes=?, processed_at=? WHERE id=?",
                    (pid, data.get("seen"), notes, _now(), photo_id),
                )
        except Exception as e:
            msg = str(e) if isinstance(e, PhotoError) else f"{type(e).__name__}: {e}"
            conn.execute("UPDATE product_photos SET status='error', error=?, processed_at=? WHERE id=?",
                         (msg, _now(), photo_id))
            raise
        finally:
            conn.commit()
    finally:
        conn.close()
        with _running_lock:
            _running.discard(photo_id)


def _match_all_in_background(db_path: str, photo_ids: list[int]) -> None:
    def _bg():
        for photo_id in photo_ids:
            with _match_lock:
                try:
                    process(db_path, photo_id)
                except Exception as e:
                    # Also recorded on the photo's row and shown in the app
                    print(f"Product photo {photo_id} matching failed: {e}", flush=True)

    # Marked running now, so the photos don't look interrupted while queued
    with _running_lock:
        busy = _running.intersection(photo_ids)
        if busy:
            raise PhotoError(f"Photo {min(busy)} is already being matched")
        _running.update(photo_ids)
    threading.Thread(target=_bg, daemon=True).start()


def _queue(conn: sqlite3.Connection, photo_ids: list[int]) -> None:
    """Mark photos as queued; process() re-adds them to _running itself."""
    conn.executemany("UPDATE product_photos SET status='processing', error=NULL WHERE id=?",
                     [(i,) for i in photo_ids])
    conn.commit()


def start(db_path: str, photo_id: int) -> None:
    """Pair a new (or retried) photo with its receipt and match it, or leave it
    waiting for the next receipt. A retried photo keeps the receipt it had."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with _pair_lock:
            row = conn.execute("SELECT uploaded_at, receipt_id FROM product_photos WHERE id=?",
                               (photo_id,)).fetchone()
            if row is None:
                raise PhotoError(f"Photo {photo_id} not found")
            uploaded, rid = row
            if rid is None:
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
            _queue(conn, [photo_id])
        _match_all_in_background(db_path, [photo_id])
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
        _match_all_in_background(db_path, ids)
    finally:
        conn.close()
