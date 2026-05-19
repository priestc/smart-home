from __future__ import annotations
import sqlite3
import datetime
from pathlib import Path


def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
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
            watts_calc    REAL,
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
    for col in ("today_kwh REAL", "yesterday_kwh REAL", "watts_calc REAL"):
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bandwidth_readings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            router_label TEXT    NOT NULL,
            mac          TEXT    NOT NULL,
            hostname     TEXT,
            down         INTEGER NOT NULL,
            up           INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS bandwidth_readings_router_ts ON bandwidth_readings (router_label, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS bandwidth_readings_mac_ts ON bandwidth_readings (mac, ts DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pool_readings (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            address  TEXT,
            label    TEXT,
            zone     TEXT,
            temp_c   REAL,
            ph       REAL,
            ec       INTEGER,
            tds      INTEGER,
            orp      INTEGER,
            chlorine REAL,
            battery  INTEGER,
            rssi     INTEGER,
            UNIQUE(ts, label)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS pool_readings_label_ts ON pool_readings (label, ts DESC)")
    try:
        conn.execute("ALTER TABLE pool_readings ADD COLUMN zone TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS pool_readings_zone_ts ON pool_readings (zone, ts DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wc_zones (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS db_size_readings (
            ts    TEXT NOT NULL,
            name  TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            PRIMARY KEY (ts, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS camera_process_stats (
            ts          TEXT    NOT NULL,
            camera      TEXT    NOT NULL,
            cpu_percent REAL,
            mem_mb      REAL,
            PRIMARY KEY (ts, camera)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ble_rssi (
            label   TEXT PRIMARY KEY,
            address TEXT,
            rssi    INTEGER,
            ts      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gatt_tasks (
            id          TEXT PRIMARY KEY,
            ts          TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
            address     TEXT NOT NULL,
            device_type TEXT NOT NULL,
            label       TEXT,
            relay_id    TEXT NOT NULL,
            status      TEXT DEFAULT 'pending',
            result_hex  TEXT,
            error       TEXT,
            updated_ts  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relay_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            relay_id     TEXT NOT NULL,
            batch_ts     TEXT,
            n_adverts    INTEGER,
            n_inserted   INTEGER,
            presence_json TEXT,
            labeled_json  TEXT,
            rev           INTEGER
        )
    """)
    for col, defn in [("labeled_json", "TEXT"), ("rev", "INTEGER"), ("server_cmd", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE relay_log ADD COLUMN {col} {defn}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relay_checkin (
            relay_id TEXT PRIMARY KEY,
            ts       TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relay_presence_sighting (
            relay_id     TEXT NOT NULL,
            label        TEXT NOT NULL,
            ts           TEXT NOT NULL,
            last_seen_ts TEXT,
            PRIMARY KEY (relay_id, label)
        )
    """)
    try:
        conn.execute("ALTER TABLE relay_presence_sighting ADD COLUMN last_seen_ts TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE wc_zones ADD COLUMN mode TEXT NOT NULL DEFAULT 'continuous'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE wc_zones ADD COLUMN zone_type TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def upsert_ble_rssi(conn: sqlite3.Connection, label: str, address: str | None, rssi: int) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR REPLACE INTO ble_rssi (label, address, rssi, ts) VALUES (?,?,?,?)",
        (label, address, rssi, ts),
    )
    conn.commit()


def insert_reading(conn: sqlite3.Connection, reading, ts: str | None = None) -> None:
    if ts is None:
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
                        today_kwh: float | None = None, yesterday_kwh: float | None = None,
                        watts_calc: float | None = None) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO plug_readings "
        "(ts, address, label, watts, watts_calc, volts, amps, energy_wh, power_factor, is_on, today_kwh, yesterday_kwh) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, address, label, watts, watts_calc, volts, amps, energy_wh, power_factor,
         int(is_on) if is_on is not None else None, today_kwh, yesterday_kwh),
    )
    conn.commit()


def insert_bandwidth_readings(conn: sqlite3.Connection, router_label: str,
                              ts: str, devices: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO bandwidth_readings (ts, router_label, mac, hostname, down, up) VALUES (?,?,?,?,?,?)",
        [(ts, router_label, d["mac"], d.get("hostname"), d["down"], d["up"]) for d in devices],
    )
    conn.commit()


def insert_pool_reading(conn: sqlite3.Connection, reading, zone: str | None = None) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO pool_readings "
        "(ts, address, label, zone, temp_c, ph, ec, tds, orp, chlorine, battery, rssi) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, reading.address, reading.label, zone, reading.temp_c, reading.ph,
         reading.ec, reading.tds, reading.orp, reading.chlorine, reading.battery, reading.rssi),
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
