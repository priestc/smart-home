from __future__ import annotations
import sqlite3
from flask import Flask, jsonify, request

app = Flask(__name__)
_db_path: str = ""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/current")
def current():
    """Latest reading for each sensor label."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT label, temp_f, humidity, rssi, ts
            FROM readings
            WHERE id IN (
                SELECT MAX(id) FROM readings GROUP BY label
            )
            ORDER BY label
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/history")
def history():
    """Historical readings. Query params:
      label  - filter by sensor label (optional)
      start  - ISO datetime lower bound (optional)
      end    - ISO datetime upper bound (optional)
      limit  - max rows returned (default 1000, max 10000)
    """
    label = request.args.get("label")
    start = request.args.get("start")
    end   = request.args.get("end")
    try:
        limit = min(int(request.args.get("limit", 1000)), 10000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    where, params = [], []
    if label:
        where.append("label = ?")
        params.append(label)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)

    sql = "SELECT ts, label, temp_f, humidity, rssi FROM readings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


def run(db_path: str, host: str, port: int, debug: bool) -> None:
    global _db_path
    _db_path = db_path
    app.run(host=host, port=port, debug=debug)
