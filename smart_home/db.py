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
            battery     INTEGER,
            raw_reading TEXT,
            UNIQUE(ts, label)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS temperature_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            event_type  TEXT    NOT NULL,
            value       REAL,
            details     TEXT,
            UNIQUE(ts, event_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS process_stats (
            ts          TEXT PRIMARY KEY,
            cpu_percent REAL,
            mem_mb      REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS garage_events (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    TEXT NOT NULL,
            name  TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('open','closed'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS garage_events_name_ts ON garage_events (name, ts DESC)")
    conn.commit()
    return conn


def insert_reading(conn: sqlite3.Connection, reading) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity, rssi, battery, raw_reading) VALUES (?,?,?,?,?,?,?,?)",
        (ts, reading.address, reading.label, reading.temp_f, reading.humidity, reading.rssi, reading.battery, reading.raw_reading),
    )
    conn.commit()


def insert_no_reading(conn: sqlite3.Connection, label: str, address: str | None = None) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
