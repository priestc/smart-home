from __future__ import annotations
import sqlite3
import datetime
from pathlib import Path


def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT    NOT NULL,
            address   TEXT    NOT NULL,
            label     TEXT,
            temp_f    REAL    NOT NULL,
            humidity  REAL    NOT NULL,
            rssi      INTEGER
        )
    """)
    conn.commit()
    return conn


def insert_reading(conn: sqlite3.Connection, reading) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO readings (ts, address, label, temp_f, humidity, rssi) VALUES (?,?,?,?,?,?)",
        (ts, reading.address, reading.label, reading.temp_f, reading.humidity, reading.rssi),
    )
    conn.commit()
