"""Receipt scanning for the cost-of-living tracker.

The iOS app uploads a photo of a receipt; it's saved next to the database and
sent to a local vision model on Ollama, which reads it and returns the
merchant, date, line items (each assigned a spending category), tax and total
as structured JSON. Items are stored in receipt_items so spending can be
summed by category over time. A small local model will misread some lines, so
everything it extracts can be corrected by hand in the app.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import sqlite3
import threading

OLLAMA_URL = "http://127.0.0.1:11434"
MODEL = "qwen3-vl:8b-instruct"
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
    "Other",
]
# Not an item category: receipts.tax is reported under this name in summaries
TAX_CATEGORY = "Sales Tax"

_SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean"},
        "merchant": {"type": ["string", "null"]},
        "purchase_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "total_price": {"type": "number"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["description", "quantity", "total_price", "category"],
                "additionalProperties": False,
            },
        },
        "subtotal": {"type": ["number", "null"]},
        "tax": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["is_receipt", "merchant", "purchase_date", "items", "subtotal", "tax", "total", "notes"],
    "additionalProperties": False,
}

_PROMPT = f"""This is a photo of a store or restaurant receipt. Extract it for a personal cost-of-living tracker.

- merchant: the store or restaurant name as a person would say it (e.g. "Costco", not "COSTCO WHOLESALE #1234").
- purchase_date: the transaction date as YYYY-MM-DD, or null if it isn't printed.
- items: one entry per purchased line. Expand cryptic abbreviations into a readable description (e.g. "GV 2% MLK GAL" -> "Great Value 2% milk, 1 gal"). total_price is the line's final amount in dollars. Apply discounts, coupons and instant savings as their own lines with a negative total_price, in the same category as the item they discount. Bottle deposits, bag fees and similar go in "Fees & Deposits". Do not include sales tax, subtotal, total, payment or change lines as items.
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


def _extract(image: bytes) -> dict:
    import httpx

    image = _shrink_for_model(image)
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": [{
                    "role": "user",
                    "content": _PROMPT,
                    "images": [base64.standard_b64encode(image).decode("ascii")],
                }],
                "format": _SCHEMA,
                "stream": False,
                "think": False,
                "keep_alive": "2m",
                "options": {"temperature": 0, "num_ctx": 8192},
            },
            timeout=600.0,
        )
    except httpx.HTTPError as e:
        raise ReceiptError(f"Could not reach Ollama at {OLLAMA_URL}: {e}")
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error") or resp.text
        except ValueError:
            msg = resp.text
        raise ReceiptError(f"Ollama error {resp.status_code}: {msg[:300]}")
    body = resp.json()
    if body.get("done_reason") == "length":
        raise ReceiptError("Receipt too long: the model's response was cut off")
    text = (body.get("message") or {}).get("content") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ReceiptError(f"Unparseable response from {MODEL}: {text[:200]}")


def _store_result(conn: sqlite3.Connection, rid: int, data: dict) -> None:
    if not data.get("is_receipt"):
        raise ReceiptError(data.get("notes") or "This doesn't look like a receipt")
    date = data.get("purchase_date")
    try:
        datetime.date.fromisoformat(date or "")
    except ValueError:
        date = None
    conn.execute("DELETE FROM receipt_items WHERE receipt_id=?", (rid,))
    conn.executemany(
        "INSERT INTO receipt_items (receipt_id, position, description, quantity, total_price, category) "
        "VALUES (?,?,?,?,?,?)",
        [(rid, i, it["description"], it.get("quantity"), round(float(it["total_price"]), 2),
          it["category"] if it["category"] in CATEGORIES else "Other")
         for i, it in enumerate(data.get("items") or [])],
    )
    conn.execute(
        "UPDATE receipts SET status='done', error=NULL, processed_at=?, merchant=?, purchase_date=?, "
        "subtotal=?, tax=?, total=?, notes=?, raw_json=? WHERE id=?",
        (_now(), data.get("merchant"), date, data.get("subtotal"), data.get("tax"),
         data.get("total"), data.get("notes"), json.dumps(data), rid),
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
            _store_result(conn, rid, _extract(image))
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
