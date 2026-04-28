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
        CREATE TABLE IF NOT EXISTS plug_readings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            address       TEXT,
            label         TEXT,
            watts         REAL,
            volts         REAL,
            amps          REAL,
            energy_wh     REAL,
            power_factor  INTEGER,
            is_on         INTEGER,
            today_kwh     REAL,
            yesterday_kwh REAL,
            UNIQUE(ts, label)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS plug_readings_label_ts ON plug_readings (label, ts DESC)")
    for col in ("today_kwh REAL", "yesterday_kwh REAL"):
        try:
            conn.execute(f"ALTER TABLE plug_readings ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS garage_events (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    TEXT NOT NULL,
            name  TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('open','closed'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS garage_events_name_ts ON garage_events (name, ts DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS camera_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            camera     TEXT NOT NULL,
            zone       TEXT NOT NULL,
            pct        REAL,
            screenshot BLOB
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS camera_events_camera_ts ON camera_events (camera, ts DESC)")
    # Migrate: add screenshot column to existing DBs
    try:
        conn.execute("ALTER TABLE camera_events ADD COLUMN screenshot BLOB")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS camera_vitals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            camera         TEXT NOT NULL,
            temp_c         REAL,
            wifi_rssi      INTEGER,
            free_heap_kb   INTEGER,
            uptime_s       INTEGER,
            psram_total_kb INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS camera_vitals_camera_ts ON camera_vitals (camera, ts DESC)")
    # Migrate: add psram_total_kb column to existing DBs
    try:
        conn.execute("ALTER TABLE camera_vitals ADD COLUMN psram_total_kb INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
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


def insert_plug_reading(conn: sqlite3.Connection, label: str, address: str | None,
                        watts: float | None, volts: float | None, amps: float | None,
                        energy_wh: float | None, power_factor: int | None, is_on: bool | None,
                        today_kwh: float | None = None, yesterday_kwh: float | None = None) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO plug_readings "
        "(ts, address, label, watts, volts, amps, energy_wh, power_factor, is_on, today_kwh, yesterday_kwh) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, address, label, watts, volts, amps, energy_wh, power_factor,
         int(is_on) if is_on is not None else None, today_kwh, yesterday_kwh),
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
