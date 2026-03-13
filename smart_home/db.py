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
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT    NOT NULL,
            address   TEXT,
            label     TEXT,
            temp_f    REAL    NOT NULL,
            humidity  REAL    NOT NULL,
            rssi      INTEGER,
            UNIQUE(ts, label)
        )
    """)
    conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any schema migrations needed on existing databases."""
    # Normalize space-separated timestamps to T-separated ISO format
    conn.execute("UPDATE OR IGNORE readings SET ts = REPLACE(ts, ' ', 'T') WHERE ts LIKE '% %'")
    conn.commit()

    # Check if address column is NOT NULL (old schema) and migrate if so
    cols = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(readings)")}
    if cols.get("address") == 1:  # 1 = NOT NULL
        conn.executescript("""
            PRAGMA foreign_keys=off;
            BEGIN;
            ALTER TABLE readings RENAME TO _readings_old;
            CREATE TABLE readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT    NOT NULL,
                address   TEXT,
                label     TEXT,
                temp_f    REAL    NOT NULL,
                humidity  REAL    NOT NULL,
                rssi      INTEGER,
                UNIQUE(ts, label)
            );
            INSERT INTO readings SELECT id, ts, address, label, temp_f, humidity, rssi
              FROM _readings_old;
            DROP TABLE _readings_old;
            COMMIT;
            PRAGMA foreign_keys=on;
        """)
        conn.commit()


def insert_reading(conn: sqlite3.Connection, reading) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity, rssi) VALUES (?,?,?,?,?,?)",
        (ts, reading.address, reading.label, reading.temp_f, reading.humidity, reading.rssi),
    )
    conn.commit()


def bulk_insert(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert (ts, label, temp_f, humidity) tuples. Returns number of rows inserted."""
    # Normalize timestamps to ISO format with T separator
    normalized = [(ts.replace(" ", "T"), label, temp_f, humidity)
                  for ts, label, temp_f, humidity in rows]
    before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO readings (ts, label, temp_f, humidity) VALUES (?,?,?,?)",
        normalized,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    return after - before
