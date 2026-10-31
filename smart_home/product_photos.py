"""Product pictures, photographed in the iOS app.

Each photo is sent to the local vision model (the same service delegator task
that reads receipts) with the list of products that don't have a picture yet:
their names, the text printed for them on receipts, and the stores they were
bought at. The model picks the product the photo shows, or says none of them
match. Either way the photo can be moved to another product by hand (the
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


_running: set[int] = set()
_running_lock = threading.Lock()


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


def candidates(conn: sqlite3.Connection) -> list[dict]:
    """Products without a picture, minus savings/coupon lines (negative price)."""
    rows = conn.execute("""
        SELECT p.id, p.name,
               (SELECT GROUP_CONCAT(t, ' | ') FROM (SELECT DISTINCT i.raw_text AS t FROM receipt_items i
                    WHERE i.product_id = p.id AND i.raw_text IS NOT NULL)) AS printed,
               (SELECT GROUP_CONCAT(DISTINCT r.merchant) FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id
                    WHERE i.product_id = p.id) AS stores,
               (SELECT i.total_price FROM receipt_items i WHERE i.product_id = p.id
                    ORDER BY i.id DESC LIMIT 1) AS last_price
        FROM products p
        WHERE p.id NOT IN (SELECT product_id FROM product_photos WHERE product_id IS NOT NULL)
        ORDER BY p.name COLLATE NOCASE
    """).fetchall()
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
    """Match one photo to a product and record the outcome on its row."""
    with _running_lock:
        if photo_id in _running:
            raise PhotoError("This photo is already being matched")
        _running.add(photo_id)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("SELECT image_path FROM product_photos WHERE id=?", (photo_id,)).fetchone()
        if row is None:
            raise PhotoError(f"Photo {photo_id} not found")
        conn.execute("UPDATE product_photos SET status='processing', error=NULL WHERE id=?", (photo_id,))
        conn.commit()
        try:
            with open(row[0], "rb") as f:
                image = f.read()
            cands = candidates(conn)
            if not cands:
                raise PhotoError("Every product already has a picture; pick the product by hand")
            data = _ask(image, cands)
            pid = data.get("product_id")
            if pid not in {c["id"] for c in cands}:
                pid = None
            conn.execute(
                "UPDATE product_photos SET status=?, product_id=?, match_source=?, model_product_id=?, "
                "model_seen=?, model_notes=?, processed_at=? WHERE id=?",
                ("matched" if pid else "unmatched", pid, "model" if pid else None, pid,
                 data.get("seen"), data.get("notes"), _now(), photo_id),
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


def process_in_background(db_path: str, photo_id: int) -> None:
    def _bg():
        try:
            process(db_path, photo_id)
        except Exception as e:
            # Also recorded on the photo's row and shown in the app
            print(f"Product photo {photo_id} matching failed: {e}", flush=True)

    threading.Thread(target=_bg, daemon=True).start()
