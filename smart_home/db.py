from __future__ import annotations
import sqlite3
import datetime
from pathlib import Path


def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            address     TEXT,
            label       TEXT,
            temp_f      REAL,
            humidity    REAL,
            rssi        INTEGER,
            raw_reading TEXT,
            UNIQUE(ts, label)
        )
    """)
    conn.commit()
    # Migrate older schemas that had NOT NULL on temp_f/humidity.
    col_info = conn.execute("PRAGMA table_info(readings)").fetchall()
    temp_col = next((c for c in col_info if c[1] == "temp_f"), None)
    if temp_col and temp_col[3] == 1:  # notnull flag
        conn.execute("ALTER TABLE readings RENAME TO _readings_old")
        conn.execute("""
            CREATE TABLE readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                address     TEXT,
                label       TEXT,
                temp_f      REAL,
                humidity    REAL,
                rssi        INTEGER,
                raw_reading TEXT,
                UNIQUE(ts, label)
            )
        """)
        conn.execute("INSERT INTO readings SELECT * FROM _readings_old")
        conn.execute("DROP TABLE _readings_old")
        conn.commit()
    return conn


def insert_reading(conn: sqlite3.Connection, reading) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity, rssi, raw_reading) VALUES (?,?,?,?,?,?,?)",
        (ts, reading.address, reading.label, reading.temp_f, reading.humidity, reading.rssi, reading.raw_reading),
    )
    conn.commit()


def insert_no_reading(conn: sqlite3.Connection, label: str, address: str | None = None) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity) VALUES (?,?,?,NULL,NULL)",
        (ts, address, label),
    )
    conn.commit()


def bulk_insert(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert (ts, label, temp_f, humidity) tuples. Returns number of rows inserted."""
    before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO readings (ts, label, temp_f, humidity) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    return after - before
