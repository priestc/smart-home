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
            relay_id    TEXT PRIMARY KEY,
            ts          TEXT NOT NULL
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS property_polygon (
            id           INTEGER PRIMARY KEY,
            polygon_json TEXT,
            center_lat   REAL,
            center_lon   REAL,
            updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gas_stations (
            place_id   TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            address    TEXT,
            city       TEXT,
            lat        REAL NOT NULL,
            lon        REAL NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL
        )
    """)
    # One row per station / fuel type / fetch day. price_updated is Google's
    # own timestamp for when the price was last reported, which can lag the
    # fetch date by days for less-trafficked stations.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gas_prices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date    TEXT NOT NULL,
            place_id      TEXT NOT NULL,
            fuel_type     TEXT NOT NULL,
            price         REAL NOT NULL,
            price_updated TEXT,
            UNIQUE(fetch_date, place_id, fuel_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS gas_prices_place_type_date ON gas_prices (place_id, fuel_type, fetch_date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS gas_prices_date_type ON gas_prices (fetch_date, fuel_type)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gas_fetch_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            fetch_date TEXT NOT NULL,
            ok         INTEGER NOT NULL,
            n_requests INTEGER,
            n_stations INTEGER,
            n_prices   INTEGER,
            error      TEXT
        )
    """)
    # Receipts photographed in the iOS app and read by a local vision model. status is
    # 'processing', 'done' or 'error'; purchase_date falls back to the upload
    # date in summaries when the receipt doesn't print one.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            uploaded_at   TEXT NOT NULL,
            status        TEXT NOT NULL,
            error         TEXT,
            processed_at  TEXT,
            image_path    TEXT NOT NULL,
            media_type    TEXT NOT NULL,
            merchant      TEXT,
            purchase_date TEXT,
            subtotal      REAL,
            tax           REAL,
            total         REAL,
            notes         TEXT,
            raw_json      TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipt_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id  INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
            position    INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity    REAL,
            total_price REAL NOT NULL,
            category    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS receipt_items_receipt ON receipt_items (receipt_id)")
    # kind is 'receipt' or 'bill'. A utility bill also records which utility
    # it is and the usage printed on it (e.g. 1440 gal); its amount is total.
    # due_date is no longer used.
    # source is 'email' for a bill read from Gmail (email_id is its message;
    # see gmail.py), otherwise NULL. period_months is how many months a bill
    # covers (e.g. 6 for car insurance); NULL means one.
    for col in ("kind TEXT NOT NULL DEFAULT 'receipt'", "utility TEXT", "usage_amount REAL",
                "usage_unit TEXT", "due_date TEXT", "source TEXT", "email_id TEXT", "period_months INTEGER"):
        try:
            conn.execute(f"ALTER TABLE receipts ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # Every email gmail.py has looked at. status is 'processing', 'imported'
    # (receipt_id is its bill), 'skipped' (reason says why) or 'error'.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_bills (
            message_id   TEXT PRIMARY KEY,
            received_at  TEXT NOT NULL,
            sender       TEXT NOT NULL,
            subject      TEXT NOT NULL,
            body_text    TEXT NOT NULL,
            status       TEXT NOT NULL,
            email_type   TEXT,
            reason       TEXT,
            receipt_id   INTEGER,
            processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_bill_checks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            ok         INTEGER NOT NULL,
            error      TEXT,
            n_new      INTEGER,
            n_imported INTEGER
        )
    """)
    # raw_text and item_code are the line as printed (e.g. Walmart's 12-digit
    # item number); name_key is derived from them and links the item to a
    # product (see product_keys). NULL for hand-added items.
    for col in ("raw_text TEXT", "item_code TEXT", "name_key TEXT"):
        try:
            conn.execute(f"ALTER TABLE receipt_items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS receipt_items_name_key ON receipt_items (name_key)")
    # A line's quantity is an amount of product in `unit` ('ea', 'oz' or 'lb'),
    # set when the receipt prints a weight or count, or entered by hand. It
    # overrides the product's default quantity.
    try:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN unit TEXT")
        # Earlier readings put a guessed count of 1 on nearly every line
        conn.execute("UPDATE receipt_items SET quantity=NULL WHERE quantity=1")
        conn.execute("UPDATE receipt_items SET unit='ea' WHERE quantity IS NOT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # GPU/CPU temperatures recorded once a second while a receipt is read
    # (smart_home/gpu_monitor.py). A run with no ended_at never finished:
    # the machine crashed or the service was restarted mid-scan.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id  INTEGER,
            kind        TEXT NOT NULL DEFAULT 'scan',  -- 'scan' or 'test' (photo re-read, results discarded)
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            outcome     TEXT,
            error       TEXT,
            note        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_samples (
            run_id   INTEGER NOT NULL REFERENCES gpu_runs(id) ON DELETE CASCADE,
            t        REAL NOT NULL,
            edge     REAL,
            junction REAL,
            mem      REAL,
            power    REAL,
            sclk     REAL,
            fan_rpm  REAL,
            cpu      REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS gpu_samples_run ON gpu_samples (run_id)")
    # Before products existed, friendly names and default quantities were kept
    # per printed line in receipt_known_items (earlier still, receipt_item_names);
    # _migrate_known_items_to_products turns them into products.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_item_names'").fetchone():
        conn.execute("CREATE TABLE IF NOT EXISTS receipt_known_items (name_key TEXT PRIMARY KEY, name TEXT, "
                     "quantity REAL, unit TEXT, updated_at TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO receipt_known_items (name_key, name, updated_at) "
                     "SELECT name_key, name, updated_at FROM receipt_item_names")
        conn.execute("DROP TABLE receipt_item_names")
    # A product is the thing a receipt-item (a line on a receipt) describes.
    # Many receipt-items point to one product, including lines from different
    # stores (the same pizza bought at Walmart and at Albertsons). quantity +
    # unit is the amount in one purchase (e.g. 21.7 oz); a receipt-item's own
    # quantity overrides it. receipt_items.description mirrors the product name.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            quantity   REAL,
            unit       TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Which product a printed line (receipt_items.name_key) is. Kept apart from
    # receipt_items so the link survives a receipt being re-read or deleted,
    # and the next receipt printing the same line finds its product.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_keys (
            name_key   TEXT PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id)
        )
    """)
    try:
        conn.execute("ALTER TABLE receipt_items ADD COLUMN product_id INTEGER REFERENCES products(id)")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS receipt_items_product ON receipt_items (product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS product_keys_product ON product_keys (product_id)")
    _migrate_known_items_to_products(conn)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _migrate_known_items_to_products(conn: sqlite3.Connection) -> None:
    """One-time: every name_key in receipt_known_items (and every receipt-item
    without a product) gets a product, keeping its friendly name and default
    quantity. receipt_known_items is dropped afterwards."""
    known = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_known_items'").fetchone():
        known = {r[0]: r[1:] for r in conn.execute("SELECT name_key, name, quantity, unit FROM receipt_known_items")}
    if not known and conn.execute("SELECT 1 FROM receipt_items WHERE product_id IS NULL LIMIT 1").fetchone() is None:
        conn.execute("DROP TABLE IF EXISTS receipt_known_items")
        return
    now = _now()
    for key, (name, qty, unit) in known.items():
        if conn.execute("SELECT 1 FROM product_keys WHERE name_key=?", (key,)).fetchone():
            continue
        latest = conn.execute("SELECT description FROM receipt_items WHERE name_key=? ORDER BY id DESC LIMIT 1",
                              (key,)).fetchone()
        name = name or (latest[0] if latest else None)
        if not name:
            continue
        pid = conn.execute("INSERT INTO products (name, quantity, unit, created_at, updated_at) VALUES (?,?,?,?,?)",
                           (name, qty, unit, now, now)).lastrowid
        conn.execute("INSERT INTO product_keys (name_key, product_id) VALUES (?,?)", (key, pid))
    # Most recent line first, so a new product takes the latest reading's name
    for iid, key, desc in conn.execute(
            "SELECT id, name_key, description FROM receipt_items WHERE product_id IS NULL ORDER BY id DESC").fetchall():
        pid = match_product(conn, key, desc)
        conn.execute("UPDATE receipt_items SET product_id=?, description=(SELECT name FROM products WHERE id=?) "
                     "WHERE id=?", (pid, pid, iid))
    conn.execute("DROP TABLE IF EXISTS receipt_known_items")


def match_product(conn: sqlite3.Connection, name_key: str | None, description: str) -> int:
    """The product a receipt-item describes: the one its printed line
    (name_key) is linked to, else a product with exactly the same name (so
    "Bananas" from two stores is one product), else a new product named
    `description`. A keyed line is linked to the result for next time."""
    pid = None
    if name_key:
        row = conn.execute("SELECT product_id FROM product_keys WHERE name_key=?", (name_key,)).fetchone()
        pid = row[0] if row else None
    if pid is None:
        row = conn.execute("SELECT id FROM products WHERE name = ? COLLATE NOCASE ORDER BY id LIMIT 1",
                           (description.strip(),)).fetchone()
        pid = row[0] if row else None
    if pid is None:
        now = _now()
        pid = conn.execute("INSERT INTO products (name, created_at, updated_at) VALUES (?,?,?)",
                           (description.strip(), now, now)).lastrowid
    if name_key:
        conn.execute("INSERT OR REPLACE INTO product_keys (name_key, product_id) VALUES (?,?)", (name_key, pid))
    return pid


def prune_products(conn: sqlite3.Connection) -> None:
    """Delete products nothing points to any more (no receipt-item and no
    printed line), e.g. after a receipt-item was moved to another product."""
    conn.execute("DELETE FROM products WHERE id NOT IN (SELECT product_id FROM receipt_items WHERE product_id IS NOT NULL) "
                 "AND id NOT IN (SELECT product_id FROM product_keys)")


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
