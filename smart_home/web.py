from __future__ import annotations
import datetime
import sqlite3
from flask import Flask, jsonify, request, Response
from flask_compress import Compress
from smart_home.db import open_db

app = Flask(__name__)
Compress(app)
_db_path: str = ""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/current")
def current():
    """Latest reading for each sensor label, including water chemistry sensors."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT label, temp_f, humidity, rssi, ts, 'reading' AS source
            FROM readings
            WHERE id IN (
                SELECT MAX(id) FROM readings WHERE temp_f IS NOT NULL GROUP BY label
            )
            UNION ALL
            SELECT label,
                   ROUND(temp_c * 9.0/5.0 + 32, 2) AS temp_f,
                   NULL AS humidity, rssi, ts, 'pool' AS source
            FROM pool_readings
            WHERE id IN (
                SELECT MAX(id) FROM pool_readings GROUP BY label
            )
            ORDER BY label
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/ble-relay")
def ble_relay():
    """Receive batched BLE advertisements from ESP32 relay devices."""
    from smart_home import relay as _relay
    from smart_home.scanner import is_govee_h5074, is_pvvx_lywsd03mmc
    from smart_home.decoder import (
        decode_advertisement, decode_pvvx_advertisement, Reading,
    )
    from smart_home import labels as _labels
    from smart_home import ble_types as _ble_types
    from smart_home.db import insert_reading

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing or invalid Authorization header"}), 401
    token = auth[len("Bearer "):]

    relay_cfg = _relay.find_relay_by_token(token)
    if relay_cfg is None:
        return jsonify({"error": "unknown token"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON body"}), 400

    advertisements = data.get("advertisements") or []
    label_map = _labels.load()

    # batch_ts is a UTC timestamp from the relay's NTP-synced clock.
    # Convert to server local time so it's consistent with datetime.now() readings.
    raw_batch_ts = data.get("batch_ts")
    batch_ts_local: str | None = None
    if raw_batch_ts:
        try:
            dt_utc = datetime.datetime.strptime(raw_batch_ts, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
            # Round to minute boundary to match snapshot_loop cadence; this lets
            # INSERT OR IGNORE deduplicate against main-scanner readings and only
            # fill gaps where the main scanner was offline.
            dt_local = dt_utc.astimezone().replace(second=0, microsecond=0)
            batch_ts_local = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            pass

    # Minimal stand-ins for bleak's BLEDevice / AdvertisementData so we can
    # reuse the existing is_* detection functions without modification.
    class _Dev:
        __slots__ = ("name", "address")
    class _Adv:
        __slots__ = ("local_name", "manufacturer_data", "service_data", "rssi")

    def _normalize_uuid(s: str) -> str:
        s = s.lower().strip()
        if len(s) == 4:   # "181a"
            return f"0000{s}-0000-1000-8000-00805f9b34fb"
        if len(s) == 8:   # "0000181a"
            return f"{s}-0000-1000-8000-00805f9b34fb"
        return s

    inserted = 0
    labeled_seen: dict = {}  # {label: rssi} for all labeled devices seen this batch
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO relay_checkin (relay_id, ts) "
            "VALUES (?, strftime('%Y-%m-%d %H:%M:%S','now'))",
            (relay_cfg["id"],)
        )

        for adv_json in advertisements:
            address = (adv_json.get("address") or "").upper()
            name = adv_json.get("name") or ""

            raw_mfr = adv_json.get("manufacturer_data") or {}
            manufacturer_data = {}
            for k, v in raw_mfr.items():
                try:
                    manufacturer_data[int(k)] = bytes.fromhex(v)
                except (ValueError, TypeError):
                    pass

            raw_svc = adv_json.get("service_data") or {}
            service_data = {}
            for k, v in raw_svc.items():
                try:
                    service_data[_normalize_uuid(k)] = bytes.fromhex(v)
                except (ValueError, TypeError):
                    pass

            rssi = adv_json.get("rssi")

            dev = _Dev()
            dev.name = name
            dev.address = address
            adv = _Adv()
            adv.local_name = name
            adv.manufacturer_data = manufacturer_data
            adv.service_data = service_data
            adv.rssi = rssi

            reading = None
            if is_govee_h5074(dev, adv):
                reading = decode_advertisement(address, name, manufacturer_data, rssi)
            elif is_pvvx_lywsd03mmc(dev, adv):
                reading = decode_pvvx_advertisement(address, name, service_data, rssi)

            if reading is not None:
                reading.label = label_map.get(address)
                _ble_types.record(address, reading.device_type)
                if reading.label:
                    insert_reading(conn, reading, batch_ts_local)
                    inserted += 1
                    if rssi is not None:
                        labeled_seen[reading.label] = rssi

        # Handle BLE-YC01 reading if included in the batch.
        pool_reading = data.get("ble_yc01_reading") or {}
        _pool_reading_stored = False
        if pool_reading:
            pool_result_hex = pool_reading.get("result_hex") or ""
            pool_label = pool_reading.get("label") or ""
            pool_address = (pool_reading.get("address") or "").upper()
            pool_rssi = pool_reading.get("rssi")
            try:
                raw = bytes.fromhex(pool_result_hex)
            except ValueError:
                raw = None
            if raw:
                from smart_home import pool as _pool
                from smart_home.db import insert_pool_reading
                reading = _pool.parse_gatt_data(raw)
                if reading:
                    reading.address = pool_address
                    reading.label = pool_label
                    reading.rssi = pool_rssi
                    insert_pool_reading(conn, reading, zone=_pool.get_device_zone(pool_label, pool_address))
                    _pool_reading_stored = True
                    if pool_rssi is not None:
                        labeled_seen[pool_label] = pool_rssi

        # Fire sensor_online/offline events immediately from the relay's pool state.
        # The relay detects the pool's presence in every scan, so we don't need to
        # wait for the snapshot_loop's 10-minute timer to catch state transitions.
        _relay_id = relay_cfg.get("id", "")
        if _relay_id:
            from smart_home import pool as _pool_mod
            _relay_pool = next(
                (m for m in _pool_mod.load_config() if m.get("node") == _relay_id),
                None,
            )
            if _relay_pool:
                _evt_label = _relay_pool.get("label", "")
            if _relay_pool and _evt_label:
                _last_pool_evt = conn.execute(
                    "SELECT event_type FROM temperature_events "
                    "WHERE event_type IN ('sensor_offline','sensor_online') "
                    "AND (details=? OR details LIKE ?) ORDER BY ts DESC LIMIT 1",
                    (_evt_label, f"{_evt_label} — %"),
                ).fetchone()
                _now_ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                if _pool_reading_stored:
                    # Successful read — fire online if we were offline.
                    if _last_pool_evt and _last_pool_evt[0] == "sensor_offline":
                        _off_row = conn.execute(
                            "SELECT ts FROM temperature_events WHERE event_type='sensor_offline' AND details=? ORDER BY ts DESC LIMIT 1",
                            (_evt_label,),
                        ).fetchone()
                        if _off_row:
                            _off_dt = datetime.datetime.strptime(_off_row[0], "%Y-%m-%d %H:%M:%S")
                            _secs = int((datetime.datetime.utcnow() - _off_dt).total_seconds())
                            _hrs, _rem = divmod(_secs, 3600)
                            _m = _rem // 60
                            _dur = f"{_hrs}h {_m}m" if _hrs else f"{_m}m"
                            _online_details = f"{_evt_label} — offline for {_dur}"
                        else:
                            _online_details = _evt_label
                        conn.execute(
                            "INSERT OR IGNORE INTO temperature_events (ts, event_type, value, details) VALUES (?,?,?,?)",
                            (_now_ts, "sensor_online", None, _online_details),
                        )
                        from smart_home import push as _push
                        _push.send_notification(
                            title="BLE-YC01 Online",
                            body=f"{_evt_label} is back online",
                        )

                elif data.get("ble_yc01_offline") and not data.get("ble_yc01_seen") and not data.get("ble_yc01_skip"):
                    # Device not found in scan — fire offline if we were online.
                    if _last_pool_evt is None or _last_pool_evt[0] == "sensor_online":
                        conn.execute(
                            "INSERT OR IGNORE INTO temperature_events (ts, event_type, value, details) VALUES (?,?,?,?)",
                            (_now_ts, "sensor_offline", None, _evt_label),
                        )
                        from smart_home import push as _push
                        _push.send_notification(
                            title="BLE-YC01 Offline",
                            body=f"{_evt_label} has stopped responding",
                        )

        # Log this relay check-in for `smart-home relay-log`
        import json as _json
        if data.get("ble_yc01_skip"):
            labeled_seen["_pool_skip"] = True
        if data.get("ble_yc01_offline"):
            labeled_seen["_pool_offline"] = True
        if data.get("ble_yc01_seen"):
            labeled_seen["_pool_seen"] = True
        if data.get("ble_yc01_status"):
            labeled_seen["_pool_status"] = data["ble_yc01_status"]
        if data.get("buffered"):
            labeled_seen["_buffered"] = True
        conn.execute(
            "INSERT INTO relay_log (ts, relay_id, batch_ts, n_adverts, n_inserted, presence_json, labeled_json, rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                relay_cfg["id"],
                raw_batch_ts,
                len(advertisements),
                inserted,
                None,
                _json.dumps(labeled_seen) if labeled_seen else None,
                data.get("rev"),
            ),
        )
        conn.execute(
            "DELETE FROM relay_log WHERE n_adverts >= 0 AND ("
            "  (labeled_json NOT LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-10 minutes'))"
            "  OR (labeled_json LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-60 minutes'))"
            ")"
        )

        # Process buffered batches bundled into this POST.
        # Each is already a dict (Flask parsed the nested JSON via serialized() in firmware).
        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _bb = data.get("buffered_batches", [])
        if data.get("buffered_batches") is not None:
            print(f"[ble-relay] relay={relay_cfg['id']} buffered_batches count={len(_bb)} body_len={request.content_length}", flush=True)
        if data.get("buffer_size") is not None and data["buffer_size"] > 0:
            print(f"[ble-relay] relay={relay_cfg['id']} buffer_size={data['buffer_size']}", flush=True)
        for raw_batch in _bb:
            try:
                bd = raw_batch if isinstance(raw_batch, dict) else _json.loads(raw_batch)
            except Exception:
                continue
            bd_adverts = bd.get("advertisements") or []
            bd_raw_ts = bd.get("batch_ts")
            bd_ts_local = None
            if bd_raw_ts:
                try:
                    bd_dt_utc = datetime.datetime.strptime(bd_raw_ts, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc
                    )
                    bd_dt_local = bd_dt_utc.astimezone().replace(second=0, microsecond=0)
                    bd_ts_local = bd_dt_local.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    pass
            bd_inserted = 0
            bd_labeled: dict = {"_buffered": True}
            for adv_json in bd_adverts:
                address = (adv_json.get("address") or "").upper()
                name = adv_json.get("name") or ""
                raw_mfr = adv_json.get("manufacturer_data") or {}
                manufacturer_data = {}
                for k, v in raw_mfr.items():
                    try:
                        manufacturer_data[int(k)] = bytes.fromhex(v)
                    except (ValueError, TypeError):
                        pass
                raw_svc = adv_json.get("service_data") or {}
                service_data = {}
                for k, v in raw_svc.items():
                    try:
                        service_data[_normalize_uuid(k)] = bytes.fromhex(v)
                    except (ValueError, TypeError):
                        pass
                rssi = adv_json.get("rssi")
                dev = _Dev(); dev.name = name; dev.address = address
                adv = _Adv(); adv.local_name = name
                adv.manufacturer_data = manufacturer_data
                adv.service_data = service_data; adv.rssi = rssi
                reading = None
                if is_govee_h5074(dev, adv):
                    reading = decode_advertisement(address, name, manufacturer_data, rssi)
                elif is_pvvx_lywsd03mmc(dev, adv):
                    reading = decode_pvvx_advertisement(address, name, service_data, rssi)
                if reading is not None:
                    reading.label = label_map.get(address)
                    if reading.label:
                        insert_reading(conn, reading, bd_ts_local)
                        bd_inserted += 1
                        if rssi is not None:
                            bd_labeled[reading.label] = rssi
            conn.execute(
                "INSERT INTO relay_log (ts, relay_id, batch_ts, n_adverts, n_inserted, labeled_json, rev) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_utc, relay_cfg["id"], bd_raw_ts, len(bd_adverts), bd_inserted,
                 _json.dumps(bd_labeled), bd.get("rev")),
            )


    response: dict = {
        "ok": True,
        "inserted": inserted,
    }

    # If pair mode was requested for this relay, include it once then clear it.
    pair_label = relay_cfg.get("pair_mode")
    if pair_label:
        response["pair_mode"] = {"label": pair_label}
        relays = _relay.load_relays()
        for r in relays:
            if r.get("token") == token:
                r.pop("pair_mode", None)
                break
        _relay.save_relays(relays)

    # Tell the relay which water chemistry device (if any) it is assigned to handle.
    from smart_home import pool as _pool
    water_chemistry_devices = _pool.load_config()
    assigned = next(
        (m for m in water_chemistry_devices if m.get("node") == relay_cfg["id"]), None
    )
    response["ble_yc01"] = {
        "address": assigned["address"],
        "label": assigned.get("label", assigned["address"]),
        "poll_skip_cycles": max(0, assigned.get("poll_interval_s", 60) // 30 - 1),
    } if assigned else None

    # Compute stagger offset so relays share the 30-second period evenly.
    # relay_offset is the seconds into the 30-s window when this relay should fire.
    all_relays = _relay.load_relays()
    n = max(len(all_relays), 1)
    idx = next((i for i, r in enumerate(all_relays) if r.get("token") == token), 0)
    response["relay_offset"] = (idx * 30) // n

    return jsonify(response)


@app.post("/api/ble-relay/crash")
def ble_relay_crash():
    """Receive a crash report from an ESP32 relay."""
    from smart_home import relay as _relay

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing or invalid Authorization header"}), 401
    token = auth[len("Bearer "):]
    relay_cfg = _relay.find_relay_by_token(token)
    if relay_cfg is None:
        return jsonify({"error": "unknown token"}), 401

    import json as _json

    data = request.get_json(silent=True) or {}
    reason = data.get("reason") or "unknown"
    op = data.get("op")
    uptime_s = data.get("uptime_s")

    crash_info: dict = {"_crash": reason}
    if op:
        crash_info["_op"] = op
    if uptime_s is not None:
        crash_info["_uptime"] = uptime_s

    with _conn() as conn:
        conn.execute(
            "INSERT INTO relay_log "
            "(ts, relay_id, batch_ts, n_adverts, n_inserted, presence_json, labeled_json, rev) "
            "VALUES (strftime('%Y-%m-%d %H:%M:%S','now'), ?, NULL, -1, 0, NULL, ?, NULL)",
            (relay_cfg["id"], _json.dumps(crash_info)),
        )
        conn.execute(
            "DELETE FROM relay_log WHERE n_adverts >= 0 AND ("
            "  (labeled_json NOT LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-10 minutes'))"
            "  OR (labeled_json LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-60 minutes'))"
            ")"
        )

    return jsonify({"ok": True})


@app.get("/api/relay-startup")
def relay_startup():
    """Return the list of tracked BLE devices so the relay can filter its scan output."""
    from smart_home import relay as _relay
    from smart_home import labels as _labels

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing or invalid Authorization header"}), 401
    token = auth[len("Bearer "):]
    if _relay.find_relay_by_token(token) is None:
        return jsonify({"error": "unknown token"}), 401

    label_map = _labels.load()

    return jsonify({
        "tracked_macs": list(label_map.keys()),
    })


@app.post("/api/register-push-token")
def register_push_token():
    """Register an iOS device token for push notifications."""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    from smart_home.push import register_token
    register_token(token)
    return jsonify({"ok": True})


@app.post("/api/bandwidth")
def bandwidth_ingest():
    """Receive bandwidth readings from BandwidthByDevice (OpenWRT)."""
    from smart_home import bandwidth as _bandwidth
    from smart_home.db import insert_bandwidth_readings

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing or invalid Authorization header"}), 401
    token = auth[len("Bearer "):]

    monitor = _bandwidth.find_monitor_by_token(token)
    if monitor is None:
        return jsonify({"error": "unknown token"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON body"}), 400

    raw_ts = data.get("ts")
    devices = data.get("devices", [])
    if raw_ts is None:
        return jsonify({"error": "missing ts field"}), 400

    ts = datetime.datetime.fromtimestamp(int(raw_ts)).strftime("%Y-%m-%d %H:%M:%S")

    if devices:
        with _conn() as conn:
            insert_bandwidth_readings(conn, monitor["label"], ts, devices)

    return "", 204


@app.get("/api/bandwidth/devices")
def bandwidth_devices():
    """Return distinct devices seen in bandwidth_readings with their most recent hostname."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT mac, hostname, router_label
            FROM bandwidth_readings
            WHERE id IN (SELECT MAX(id) FROM bandwidth_readings GROUP BY mac)
            ORDER BY COALESCE(hostname, mac)
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/bandwidth/history")
def bandwidth_history():
    """Bandwidth history. Query params: start, end, limit, bucket_minutes, mac."""
    start  = (request.args.get("start") or "").replace("T", " ") or None
    end    = (request.args.get("end")   or "").replace("T", " ") or None
    mac    = request.args.get("mac")
    try:
        limit  = min(int(request.args.get("limit",  8000)), 200000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    try:
        bucket = max(1, int(request.args.get("bucket_minutes", 1)))
    except ValueError:
        return jsonify({"error": "bucket_minutes must be an integer"}), 400

    where, params = [], []
    if mac:
        where.append("mac = ?")
        params.append(mac)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    bucket_secs = bucket * 60
    if bucket > 1:
        sql = f"""
            SELECT
                strftime('%Y-%m-%d %H:%M:%S', CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs}, 'unixepoch') AS ts,
                mac, hostname, router_label,
                ROUND(AVG(down) / 10.0 / 1024.0, 3) AS down_kbps,
                ROUND(AVG(up)   / 10.0 / 1024.0, 3) AS up_kbps
            FROM bandwidth_readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, mac
            ORDER BY ts ASC LIMIT ?
        """
    else:
        sql = f"""
            SELECT ts, mac, hostname, router_label,
                   ROUND(down / 10.0 / 1024.0, 3) AS down_kbps,
                   ROUND(up   / 10.0 / 1024.0, 3) AS up_kbps
            FROM bandwidth_readings{where_sql}
            ORDER BY ts ASC LIMIT ?
        """
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/bandwidth/history/month")
def bandwidth_history_month():
    """Bandwidth for a calendar month, ts normalized to year 2000, averaged across all years."""
    import datetime as _dt
    month = max(1, min(12, request.args.get("month", 1, type=int)))
    bucket_minutes = max(1, request.args.get("bucket_minutes", 60, type=int))
    bucket_secs = bucket_minutes * 60
    month_str = f"{month:02d}"
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / {bucket_secs} * {bucket_secs} AS bucket,
                mac, hostname,
                ROUND(AVG(down) / 10.0 / 1024.0, 3) AS down_kbps,
                ROUND(AVG(up)   / 10.0 / 1024.0, 3) AS up_kbps
            FROM bandwidth_readings
            WHERE strftime('%m', ts) = ?
            GROUP BY bucket, mac
            ORDER BY bucket ASC
        """, (month_str,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"ts": ts, "mac": d["mac"], "hostname": d["hostname"],
                        "down_kbps": d["down_kbps"], "up_kbps": d["up_kbps"]})
    return jsonify(result)


@app.get("/api/bandwidth/history/year")
def bandwidth_history_year():
    """All bandwidth normalized to year 2000 for year-over-year overlay."""
    import datetime as _dt
    bucket_minutes = max(1, request.args.get("bucket_minutes", 360, type=int))
    bucket_secs = bucket_minutes * 60
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / {bucket_secs} * {bucket_secs} AS bucket,
                mac, hostname,
                ROUND(AVG(down) / 10.0 / 1024.0, 3) AS down_kbps,
                ROUND(AVG(up)   / 10.0 / 1024.0, 3) AS up_kbps
            FROM bandwidth_readings
            GROUP BY bucket, mac
            ORDER BY bucket ASC
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"ts": ts, "mac": d["mac"], "hostname": d["hostname"],
                        "down_kbps": d["down_kbps"], "up_kbps": d["up_kbps"]})
    return jsonify(result)


@app.get("/api/presence/history")
def presence_history_api():
    """Per-device presence stats (7d / 30d) and recent away periods."""
    import datetime as _datetime
    from smart_home.presence import load_history, load_iphone_devices, load_state
    entries = load_history()
    devices = load_iphone_devices()
    state   = load_state()
    if not devices:
        return jsonify([])
    now = _datetime.datetime.now()
    by_device: dict = {}
    for e in entries:
        by_device.setdefault(e["ble_name"], []).append(e)

    def _periods(dev_entries, window_start):
        pre    = [e for e in dev_entries if e["ts"] <  window_start.isoformat()]
        in_win = [e for e in dev_entries if e["ts"] >= window_start.isoformat()]
        initial = pre[-1]["status"] if pre else "unknown"
        trans = [(window_start, initial)]
        for e in in_win:
            trans.append((_datetime.datetime.fromisoformat(e["ts"]), e["status"]))
        trans.append((now, None))
        out = []
        for i in range(len(trans) - 1):
            s, status = trans[i]
            e2 = trans[i + 1][0]
            if status and status != "unknown":
                out.append((s, e2, status))
        return out

    result = []
    for name in sorted(devices.keys()):
        s = state.get(name, {})
        info = devices[name]
        dev_entries = sorted(by_device.get(name, []), key=lambda e: e["ts"])
        windows = {}
        for days in (7, 30):
            periods = _periods(dev_entries, now - _datetime.timedelta(days=days))
            home_secs = sum((e - s).total_seconds() for s, e, st in periods if st == "home")
            away_secs = sum((e - s).total_seconds() for s, e, st in periods if st == "away")
            total = home_secs + away_secs
            windows[str(days)] = {
                "away_count": sum(1 for _, _, st in periods if st == "away"),
                "home_secs":  round(home_secs),
                "away_secs":  round(away_secs),
                "home_pct":   round(100 * home_secs / total) if total else None,
                "away_pct":   round(100 * away_secs / total) if total else None,
            }
        away_list = [
            {
                "start": s.isoformat(timespec="seconds"),
                "end":   e.isoformat(timespec="seconds"),
                "duration_secs": round((e - s).total_seconds()),
            }
            for s, e, st in _periods(dev_entries, now - _datetime.timedelta(days=90))
            if st == "away"
        ]
        result.append({
            "name":          name,
            "ble_name":      name,
            "model_name":    info.get("model_name", ""),
            "status":        s.get("status", "unknown"),
            "last_seen":     s.get("last_seen"),
            "ble_last_seen": s.get("ble_last_seen"),
            "net_last_seen": s.get("net_last_seen"),
            "windows":       windows,
            "recent_away":   list(reversed(away_list))[:25],
        })
    return jsonify(result)


@app.delete("/api/presence/history")
def presence_delete_away():
    from smart_home.presence import delete_away_period
    body = request.get_json(silent=True) or {}
    ble_name = body.get("ble_name")
    start    = body.get("start")
    end      = body.get("end")
    if not (ble_name and start and end):
        return ("Missing ble_name, start, or end", 400)
    removed = delete_away_period(ble_name, start, end)
    return jsonify({"removed": removed})


@app.post("/api/register-presence-device")
def register_presence_device():
    """Register an iPhone as a presence device (name, local_ip, bluetooth_name)."""
    from smart_home import presence as _presence
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    devices = _presence.load_iphone_devices()
    devices[name] = {
        "local_ip": data.get("local_ip", "").strip(),
        "bluetooth_name": data.get("bluetooth_name", "").strip(),
        "model_name": data.get("model_name", "").strip(),
    }
    _presence.save_iphone_devices(devices)
    return jsonify({"ok": True})


@app.get("/api/presence")
def presence():
    """Current presence status for all registered iPhone devices."""
    from smart_home.presence import load_iphone_devices, load_state
    devices = load_iphone_devices()
    state = load_state()
    result = []
    for name, info in devices.items():
        s = state.get(name, {})
        result.append({
            "name": name,
            "model_name": info.get("model_name", ""),
            "status": s.get("status", "unknown"),
            "last_seen": s.get("last_seen"),
        })
    return jsonify(sorted(result, key=lambda x: x["name"]))


@app.get("/api/history")
def history():
    """Historical readings. Query params:
      label          - filter by sensor label (optional)
      start          - ISO datetime lower bound (optional)
      end            - ISO datetime upper bound (optional)
      limit          - max rows returned (default 1000, max 200000)
      bucket_minutes - group readings into N-minute buckets (optional)
    """
    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None
    try:
        limit = min(int(request.args.get("limit", 1000)), 200000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    try:
        bucket = max(1, int(request.args.get("bucket_minutes", 1)))
    except ValueError:
        return jsonify({"error": "bucket_minutes must be an integer"}), 400

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

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    if bucket > 1:
        bucket_secs = bucket * 60
        sql = f"""
            SELECT
                strftime('%Y-%m-%d %H:%M:%S', CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs}, 'unixepoch') AS ts,
                label,
                ROUND(AVG(temp_f), 2)   AS temp_f,
                ROUND(AVG(humidity), 2) AS humidity,
                ROUND(AVG(rssi), 0)     AS rssi,
                ROUND(AVG(battery), 0)  AS battery
            FROM readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, label
            UNION ALL
            SELECT
                strftime('%Y-%m-%d %H:%M:%S', CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs}, 'unixepoch') AS ts,
                label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                NULL AS humidity,
                ROUND(AVG(rssi), 0)    AS rssi,
                ROUND(AVG(battery), 0) AS battery
            FROM pool_readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, label
            ORDER BY ts ASC LIMIT ?
        """
    else:
        sql = f"""
            SELECT ts, label, temp_f, humidity, rssi, battery FROM readings{where_sql}
            UNION ALL
            SELECT ts, label, ROUND(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END, 2) AS temp_f, NULL AS humidity, rssi, battery FROM pool_readings{where_sql}
            ORDER BY ts ASC
        """

    # params appears twice in the SQL (once per UNION branch); limit goes at the end for bucketed
    query_params = params * 2 + ([limit] if bucket > 1 else [])
    with _conn() as conn:
        rows = conn.execute(sql, query_params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/history/month")
def history_month():
    """All temperature readings for a given calendar month across all years.
    Returns rows with ts normalized to year 2000 so all years overlay on the same axis.
    Query params: month (1-12), bucket_minutes (default 60).
    """
    import datetime as _dt
    month = max(1, min(12, request.args.get("month", 1, type=int)))
    bucket_minutes = max(1, request.args.get("bucket_minutes", 60, type=int))
    bucket_secs = bucket_minutes * 60
    month_str = f"{month:02d}"
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(temp_f), 1) AS temp_f
            FROM readings
            WHERE strftime('%m', ts) = ?
              AND temp_f IS NOT NULL AND label IS NOT NULL
            GROUP BY bucket, label, year
            ORDER BY bucket ASC
        """, (bucket_secs, bucket_secs, month_str)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"year": d["year"], "ts": ts, "label": d["label"], "temp_f": d["temp_f"]})
    return jsonify(result)


@app.get("/api/history/years")
def history_years():
    """Return distinct years present in the readings table."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT strftime('%Y', ts) AS year FROM readings WHERE temp_f IS NOT NULL ORDER BY year DESC"
        ).fetchall()
    return jsonify([r["year"] for r in rows])


@app.get("/api/history/day")
def history_day():
    """All temperature readings for a specific calendar date (YYYY-MM-DD).
    Query params: year, month (1-12), day (1-31), bucket_minutes (default 5).
    """
    year  = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    day   = request.args.get("day", type=int)
    if not (year and month and day):
        return jsonify([])
    bucket_minutes = max(1, request.args.get("bucket_minutes", 5, type=int))
    bucket_secs = bucket_minutes * 60
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                CAST(strftime('%s', ts) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(temp_f), 1) AS temp_f
            FROM readings
            WHERE DATE(ts) = ?
              AND temp_f IS NOT NULL AND label IS NOT NULL
            GROUP BY bucket, label
            ORDER BY bucket ASC
        """, (bucket_secs, bucket_secs, date_str)).fetchall()
    import datetime as _dt
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"ts": ts, "label": d["label"], "temp_f": d["temp_f"]})
    return jsonify(result)


@app.get("/api/history/typical-day")
def history_typical_day():
    """Average all readings into a single representative day.
    Groups readings by time-of-day bucket, averaging across all days in the selected range.
    Query params:
      range_type: 'all', 'days', 'month'
      days: 7 or 30 (when range_type=days)
      month: 1-12 (when range_type=month)
      bucket_minutes: default 10
    """
    import datetime as _dt
    range_type = request.args.get("range_type", "all")
    bucket_minutes = max(1, request.args.get("bucket_minutes", 10, type=int))
    bucket_secs = bucket_minutes * 60

    where_parts = ["temp_f IS NOT NULL", "label IS NOT NULL"]
    params = [bucket_secs, bucket_secs]

    if range_type == "days":
        days = max(2, request.args.get("days", 7, type=int))
        where_parts.append(f"ts >= DATE('now', '-{days} days')")
    elif range_type == "month":
        month = max(1, min(12, request.args.get("month", 1, type=int)))
        where_parts.append("strftime('%m', ts) = ?")
        params.append(f"{month:02d}")

    where_sql = " AND ".join(where_parts)

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                (CAST(strftime('%H', ts) AS INTEGER) * 3600 +
                 CAST(strftime('%M', ts) AS INTEGER) * 60) / ? * ? AS time_bucket,
                label,
                ROUND(AVG(temp_f), 2) AS temp_f
            FROM readings
            WHERE {where_sql}
            GROUP BY time_bucket, label
            ORDER BY time_bucket ASC
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        ts = (_dt.datetime(2000, 1, 1) + _dt.timedelta(seconds=d["time_bucket"])).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"ts": ts, "label": d["label"], "temp_f": d["temp_f"]})
    return jsonify(result)


@app.get("/api/history/year")
def history_year():
    """All temperature readings across the full year, normalized to year 2000 for overlay.
    Returns rows grouped by calendar year, each with ts normalized to 2000-MM-DDTHH:MM:SS.
    Query param: bucket_minutes (default 360).
    """
    import datetime as _dt
    bucket_minutes = max(1, request.args.get("bucket_minutes", 360, type=int))
    bucket_secs = bucket_minutes * 60
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(temp_f), 1) AS temp_f
            FROM readings
            WHERE temp_f IS NOT NULL AND label IS NOT NULL
            GROUP BY bucket, label, year
            ORDER BY bucket ASC
        """, (bucket_secs, bucket_secs)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"year": d["year"], "ts": ts, "label": d["label"], "temp_f": d["temp_f"]})
    return jsonify(result)


@app.get("/api/trends")
def trends():
    """Daily min/max/avg temperature per label for the past year."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                DATE(ts) AS date,
                label,
                ROUND(MIN(temp_f), 1) AS min_f,
                ROUND(MAX(temp_f), 1) AS max_f,
                ROUND(AVG(temp_f), 1) AS avg_f
            FROM readings
            WHERE ts >= DATE('now', '-1 year')
              AND label IS NOT NULL
            GROUP BY DATE(ts), label
            ORDER BY date ASC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/plug_history")
def plug_history():
    """Historical plug readings. Query params:
      label          - filter by plug label (optional)
      start          - ISO datetime lower bound (optional)
      end            - ISO datetime upper bound (optional)
      limit          - max rows returned (default 1000, max 200000)
      bucket_minutes - group readings into N-minute buckets (optional)
    """
    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None
    try:
        limit = min(int(request.args.get("limit", 1000)), 200000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    try:
        bucket = max(1, int(request.args.get("bucket_minutes", 1)))
    except ValueError:
        return jsonify({"error": "bucket_minutes must be an integer"}), 400

    where, params = ["label IS NOT NULL"], []
    if label:
        where.append("label = ?")
        params.append(label)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)

    where_sql = " WHERE " + " AND ".join(where)

    if bucket > 1:
        bucket_secs = bucket * 60
        sql = f"""
            SELECT
                strftime('%Y-%m-%d %H:%M:%S', CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs}, 'unixepoch') AS ts,
                label,
                ROUND(AVG(COALESCE(watts_calc, watts)), 2) AS watts,
                ROUND(AVG(amps), 3)         AS amps,
                ROUND(AVG(volts), 1)        AS volts,
                ROUND(AVG(power_factor), 0) AS power_factor
            FROM plug_readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, label
            ORDER BY ts ASC LIMIT ?
        """
    else:
        sql = f"SELECT ts, label, COALESCE(watts_calc, watts) AS watts, amps, volts, power_factor FROM plug_readings{where_sql} ORDER BY ts ASC LIMIT ?"

    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/plug_history/month")
def plug_history_month():
    """Plug readings for a calendar month across all years, ts normalized to year 2000.
    Query params: month (1-12), bucket_minutes (default 60).
    """
    import datetime as _dt
    month = max(1, min(12, request.args.get("month", 1, type=int)))
    bucket_minutes = max(1, request.args.get("bucket_minutes", 60, type=int))
    bucket_secs = bucket_minutes * 60
    month_str = f"{month:02d}"
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(COALESCE(watts_calc, watts)), 2) AS watts
            FROM plug_readings
            WHERE strftime('%m', ts) = ?
              AND (watts_calc IS NOT NULL OR watts IS NOT NULL) AND label IS NOT NULL
            GROUP BY bucket, label, year
            ORDER BY bucket ASC
        """, (bucket_secs, bucket_secs, month_str)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"year": d["year"], "ts": ts, "label": d["label"], "watts": d["watts"]})
    return jsonify(result)


@app.get("/api/plug_history/year")
def plug_history_year():
    """All plug readings normalized to year 2000 for year-over-year overlay.
    Query params: bucket_minutes (default 360).
    """
    import datetime as _dt
    bucket_minutes = max(1, request.args.get("bucket_minutes", 360, type=int))
    bucket_secs = bucket_minutes * 60
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(COALESCE(watts_calc, watts)), 2) AS watts
            FROM plug_readings
            WHERE (watts_calc IS NOT NULL OR watts IS NOT NULL) AND label IS NOT NULL
            GROUP BY bucket, label, year
            ORDER BY bucket ASC
        """, (bucket_secs, bucket_secs)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"year": d["year"], "ts": ts, "label": d["label"], "watts": d["watts"]})
    return jsonify(result)


@app.get("/api/plug_daily")
def plug_daily():
    """Daily energy totals from the device's own accumulator.
    Returns the MAX(today_kwh) per calendar day per label — the end-of-day
    device reading, which is more accurate than integrating sampled watts.
    Query params: start, end (ISO date), label.
    """
    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None

    where, params = ["today_kwh IS NOT NULL", "label IS NOT NULL"], []
    if label:
        where.append("label = ?")
        params.append(label)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)

    where_sql = " WHERE " + " AND ".join(where)
    sql = f"""
        SELECT DATE(ts) AS date, label, ROUND(MAX(today_kwh), 3) AS kwh
        FROM plug_readings{where_sql}
        GROUP BY DATE(ts), label
        ORDER BY date ASC
    """
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/plug_cumulative_on")
def plug_cumulative_on():
    """Daily on-time in hours per plug, computed from watt readings vs per-device threshold.
    Query params: start, end (ISO datetime), label.
    """
    from smart_home import smart_plug as _sp
    thresholds = _sp.load_thresholds()

    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None

    where, params = ["label IS NOT NULL"], []
    if label:
        where.append("label = ?")
        params.append(label)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    where_sql = " WHERE " + " AND ".join(where)

    with _conn() as conn:
        rows = conn.execute(
            f"SELECT strftime('%Y-%m-%d', ts) AS date, label, COALESCE(watts_calc, watts) AS w "
            f"FROM plug_readings{where_sql} ORDER BY ts",
            params,
        ).fetchall()

    POLL_INTERVAL_SECS = 30
    totals: dict = {}
    for row in rows:
        d, lbl, w = row["date"], row["label"], row["w"]
        if w is not None and w > thresholds.get(lbl, 0):
            key = (d, lbl)
            totals[key] = totals.get(key, 0) + POLL_INTERVAL_SECS / 3600

    result = [
        {"date": k[0], "label": k[1], "on_hours": round(v, 3)}
        for k, v in sorted(totals.items())
    ]
    return jsonify(result)


@app.get("/api/plug_on_off_stats")
def plug_on_off_stats():
    """Per-device on/off stats for an interval.
    Query params: start, end (ISO datetime), month (1-12), label.
    Returns per-device: on_hours, off_hours, avg_watts_on, avg_watts_off.
    """
    from smart_home import smart_plug as _sp
    thresholds = _sp.load_thresholds()

    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None
    month = request.args.get("month", type=int)

    where, params = ["label IS NOT NULL", "(watts_calc IS NOT NULL OR watts IS NOT NULL)"], []
    if label:
        where.append("label = ?")
        params.append(label)
    if month:
        where.append("strftime('%m', ts) = ?")
        params.append(f"{month:02d}")
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    where_sql = " WHERE " + " AND ".join(where)

    POLL_INTERVAL_SECS = 30
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT label, COALESCE(watts_calc, watts) AS w FROM plug_readings{where_sql}",
            params,
        ).fetchall()

    stats: dict = {}
    for row in rows:
        lbl, w = row["label"], row["w"]
        if lbl not in stats:
            stats[lbl] = {"on_count": 0, "off_count": 0, "on_watts_sum": 0.0, "off_watts_sum": 0.0}
        if w > thresholds.get(lbl, 0):
            stats[lbl]["on_count"] += 1
            stats[lbl]["on_watts_sum"] += w
        else:
            stats[lbl]["off_count"] += 1
            stats[lbl]["off_watts_sum"] += w

    result = []
    for lbl, s in sorted(stats.items()):
        on_h  = s["on_count"]  * POLL_INTERVAL_SECS / 3600
        off_h = s["off_count"] * POLL_INTERVAL_SECS / 3600
        result.append({
            "label":         lbl,
            "on_hours":      round(on_h,  3),
            "off_hours":     round(off_h, 3),
            "avg_watts_on":  round(s["on_watts_sum"]  / s["on_count"],  2) if s["on_count"]  else None,
            "avg_watts_off": round(s["off_watts_sum"] / s["off_count"], 2) if s["off_count"] else None,
        })
    return jsonify(result)


@app.get("/presence")
def presence_page():
    return Response("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Presence &mdash; Smart Home</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .device { background: #fff; border-radius: 12px; padding: 1.4rem 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .device-header { display: flex; align-items: center; gap: .8rem; margin-bottom: 1.2rem; }
    .dot { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
    .dot.home { background: #2a9d6e; } .dot.away { background: #c0392b; } .dot.unknown { background: #aabbc8; }
    .device-name  { font-size: 1.1rem; font-weight: 700; }
    .device-model { font-size: .8rem; color: #4a6080; margin-top: .1rem; font-weight: 500; }
    .device-sub   { font-size: .78rem; color: #7a90a8; margin-top: .1rem; }
    .stats { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.2rem; }
    .stat-box { background: #f0f4f8; border-radius: 8px; padding: .7rem 1rem; min-width: 130px; }
    .stat-box .sb-label { font-size: .7rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; }
    .stat-box .sb-val   { font-size: 1.3rem; font-weight: 700; margin-top: .2rem; color: #1a2535; }
    .stat-box .sb-sub   { font-size: .75rem; color: #7a90a8; margin-top: .1rem; }
    .away-title { font-size: .75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: .6rem; }
    .away-list { display: flex; flex-direction: column; gap: .4rem; }
    .away-row { display: flex; justify-content: space-between; align-items: center; font-size: .85rem; padding: .4rem .6rem; border-radius: 6px; background: #f8f9fb; gap: 1rem; }
    .away-row .ar-time { color: #4a6080; flex: 1; }
    .away-row .ar-dur  { color: #7a90a8; font-size: .78rem; white-space: nowrap; }
    .away-row .ar-del  { background: none; border: none; cursor: pointer; color: #c0392b; font-size: 1rem; line-height: 1; padding: 0 .2rem; opacity: .5; transition: opacity .15s; flex-shrink: 0; }
    .away-row .ar-del:hover { opacity: 1; }
    .empty { color: #7a90a8; font-size: .9rem; }
    .win-tabs { display: flex; gap: .4rem; margin-bottom: .9rem; }
    .win-tab { background: #f0f4f8; color: #4a6080; border: none; border-radius: 6px; padding: .3rem .9rem; cursor: pointer; font-size: .8rem; font-weight: 600; transition: all .15s; }
    .win-tab.active { background: #2e7dd4; color: #fff; }
    .signal-badges { display: flex; gap: .6rem; flex-wrap: wrap; margin-bottom: 1.2rem; }
    .signal-badge { display: inline-flex; align-items: center; gap: .35rem; font-size: .75rem; font-weight: 600; padding: .25rem .65rem; border-radius: 20px; }
    .signal-badge .sig-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .signal-badge.sig-home { background: #e8f5ef; color: #1e7a50; }
    .signal-badge.sig-home .sig-dot { background: #2a9d6e; }
    .signal-badge.sig-away { background: #fdecea; color: #a93226; }
    .signal-badge.sig-away .sig-dot { background: #c0392b; }
    .signal-badge.sig-unknown { background: #f0f4f8; color: #7a90a8; }
    .signal-badge.sig-unknown .sig-dot { background: #aabbc8; }
  </style>
</head>
<body>
  <h1>Presence</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div id="content"><p class="empty">Loading&hellip;</p></div>

<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
function fmtDur(s) {
  s = Math.round(s);
  if (s < 60)    return `${s}s`;
  if (s < 3600)  return `${Math.floor(s/60)}m`;
  const d = Math.floor(s/86400), h = Math.floor((s%86400)/3600), m = Math.floor((s%3600)/60);
  if (d)  return h ? `${d}d ${h}h` : `${d}d`;
  return m ? `${h}h ${m}m` : `${h}h`;
}
function fmtDt(iso) {
  return new Date(iso).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
}
function timeSince(iso) {
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60)    return `${s}s ago`;
  if (s < 3600)  return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ago`;
  return `${Math.floor(s/86400)}d ago`;
}

function signalBadgesHtml(d) {
  const BLE_TIMEOUT = 15, NET_TIMEOUT = 30;
  function badge(label, lastSeenIso, timeout) {
    if (!lastSeenIso) {
      return `<span class="signal-badge sig-unknown"><span class="sig-dot"></span>${label}: Unavailable</span>`;
    }
    const age = (Date.now() - new Date(lastSeenIso)) / 1000;
    const present = age < timeout;
    const cls = present ? "sig-home" : "sig-away";
    const state = present ? "Home" : "Away";
    return `<span class="signal-badge ${cls}"><span class="sig-dot"></span>${label}: ${state} &middot; ${timeSince(lastSeenIso)}</span>`;
  }
  return `<div class="signal-badges">
    ${badge("Bluetooth", d.ble_last_seen, BLE_TIMEOUT)}
    ${badge("Network", d.net_last_seen, NET_TIMEOUT)}
  </div>`;
}

function renderDevice(d) {
  const sub = d.last_seen ? `Last seen ${timeSince(d.last_seen)}` : "Never seen";
  const tabs = [7, 30].map(n => `<button class="win-tab${n===7?' active':''}" onclick="switchWin(this,'${d.name}',${n})">${n}d</button>`).join("");

  function statsHtml(w) {
    if (!w || w.total_secs === 0) return '<p class="empty" style="margin:.5rem 0">No data for this window.</p>';
    return `<div class="stats">
      <div class="stat-box"><div class="sb-label">Away events</div><div class="sb-val">${w.away_count}</div></div>
      <div class="stat-box"><div class="sb-label">Time home</div><div class="sb-val">${fmtDur(w.home_secs)}</div><div class="sb-sub">${w.home_pct ?? '?'}%</div></div>
      <div class="stat-box"><div class="sb-label">Time away</div><div class="sb-val">${fmtDur(w.away_secs)}</div><div class="sb-sub">${w.away_pct ?? '?'}%</div></div>
    </div>`;
  }

  const awayHtml = d.recent_away.length === 0
    ? '<p class="empty">No away periods recorded.</p>'
    : '<div class="away-list">' + d.recent_away.map(a =>
        `<div class="away-row" data-start="${a.start}" data-end="${a.end}" data-ble="${d.ble_name}">
          <span class="ar-time">${fmtDt(a.start)} &rarr; ${fmtDt(a.end)}</span>
          <span class="ar-dur">${fmtDur(a.duration_secs)}</span>
          <button class="ar-del" title="Delete this away event" onclick="deleteAway(this)">&#x2715;</button>
        </div>`).join("") + "</div>";

  return `<div class="device" data-name="${d.name}" data-windows='${JSON.stringify(d.windows)}'>
    <div class="device-header">
      <div class="dot ${d.status}"></div>
      <div>
        <div class="device-name">${d.name}</div>
        ${d.model_name ? `<div class="device-model">${d.model_name}</div>` : ''}
        <div class="device-sub">${d.status} &middot; ${sub}</div>
      </div>
    </div>
    ${signalBadgesHtml(d)}
    <div class="win-tabs">${tabs}</div>
    <div class="win-stats">${statsHtml(d.windows["7"])}</div>
    <div class="away-title">Recent away periods (last 90 days)</div>
    ${awayHtml}
  </div>`;
}

function switchWin(btn, name, days) {
  const card = [...document.querySelectorAll(".device")].find(el => el.dataset.name === name);
  card.querySelectorAll(".win-tab").forEach(b => b.classList.toggle("active", b === btn));
  const w = JSON.parse(card.dataset.windows)[String(days)];
  function fmtDur(s) {
    s = Math.round(s);
    if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s/60)}m`;
    const d = Math.floor(s/86400), h = Math.floor((s%86400)/3600), m = Math.floor((s%3600)/60);
    if (d) return h ? `${d}d ${h}h` : `${d}d`; return m ? `${h}h ${m}m` : `${h}h`;
  }
  card.querySelector(".win-stats").innerHTML = (!w || w.total_secs === 0)
    ? '<p class="empty" style="margin:.5rem 0">No data for this window.</p>'
    : `<div class="stats">
        <div class="stat-box"><div class="sb-label">Away events</div><div class="sb-val">${w.away_count}</div></div>
        <div class="stat-box"><div class="sb-label">Time home</div><div class="sb-val">${fmtDur(w.home_secs)}</div><div class="sb-sub">${w.home_pct ?? '?'}%</div></div>
        <div class="stat-box"><div class="sb-label">Time away</div><div class="sb-val">${fmtDur(w.away_secs)}</div><div class="sb-sub">${w.away_pct ?? '?'}%</div></div>
      </div>`;
}

async function deleteAway(btn) {
  const row = btn.closest(".away-row");
  const { start, end, ble } = row.dataset;
  btn.disabled = true;
  btn.textContent = "…";
  const r = await fetch("/api/presence/history", {
    method: "DELETE",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ble_name: ble, start, end}),
  });
  if (r.ok) {
    row.style.transition = "opacity .2s";
    row.style.opacity = "0";
    setTimeout(() => row.remove(), 200);
  } else {
    btn.disabled = false;
    btn.textContent = "\u2715";
    alert("Delete failed: " + await r.text());
  }
}

async function load() {
  const data = await fetchJSON("/api/presence/history");
  const el = document.getElementById("content");
  if (!data.length) { el.innerHTML = '<p class="empty">No presence devices registered.</p>'; return; }
  el.innerHTML = data.map(renderDevice).join("");
}
load();
</script>
</body>
</html>""", mimetype="text/html")


@app.get("/api/minmax-tod")
def minmax_tod():
    """For each day and label, the time-of-day (decimal hour) when the daily
    min and max temperature occurred."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                DATE(ts) AS date,
                label,
                ROUND(CAST(strftime('%H', MIN(CASE WHEN temp_f = day_min THEN ts END)) AS REAL)
                    + CAST(strftime('%M', MIN(CASE WHEN temp_f = day_min THEN ts END)) AS REAL) / 60.0, 2) AS min_hour,
                ROUND(CAST(strftime('%H', MIN(CASE WHEN temp_f = day_max THEN ts END)) AS REAL)
                    + CAST(strftime('%M', MIN(CASE WHEN temp_f = day_max THEN ts END)) AS REAL) / 60.0, 2) AS max_hour
            FROM (
                SELECT ts, label, temp_f,
                    MIN(temp_f) OVER (PARTITION BY DATE(ts), label) AS day_min,
                    MAX(temp_f) OVER (PARTITION BY DATE(ts), label) AS day_max
                FROM readings
                WHERE temp_f IS NOT NULL AND label IS NOT NULL
                  AND ts >= DATE('now', '-1 year')
            )
            GROUP BY DATE(ts), label
            ORDER BY date ASC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/trends")
def trends_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trends — Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .toolbar { margin-bottom: 1.5rem; }
    .toggle-btn {
      background: #fff; color: #4a6080; border: 1px solid #d0dce8;
      border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem;
      font-weight: 500; transition: all .15s;
    }
    .toggle-btn:hover { background: #f0f4f8; border-color: #aabbc8; }
    .toggle-btn.active { background: #9b4dca; color: #fff; border-color: #9b4dca; }
    .chart-wrap {
      background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem;
      margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05);
    }
    .chart-wrap h2 { font-size: 1rem; font-weight: 700; color: #1a2535; margin-bottom: 1rem; }
    .empty { color: #7a90a8; font-size: .9rem; padding: .5rem 0; }
  </style>
</head>
<body>
  <h1>Temperature Trends</h1>
  <div class="nav"><a href="/">&larr; Back to dashboard</a></div>
  <div class="toolbar">
    <button class="toggle-btn" id="maBtn" onclick="toggleMA()">5-day moving average</button>
  </div>
  <div id="charts"></div>

  <h1 style="margin-top:2rem;margin-bottom:1rem">Daily Min/Max Hour</h1>
  <p style="font-size:.85rem;color:#7a90a8;margin-bottom:1.5rem">Time of day when the daily minimum and maximum temperature was recorded.</p>
  <div id="tod-charts"></div>

<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = {
  max: "#e07820",
  avg: "#2e7dd4",
  min: "#2a9d6e",
  ma:  "#9b4dca",
};

const toDate = s => new Date(s + "T12:00:00");

let showMA = false;
const charts = [];     // {chart, rows} for each sensor (min/max/avg)
const todCharts = [];  // {chart, rows} for each sensor (time-of-day)

function movingAvg(rows, field, window) {
  return rows.map((r, i) => {
    const slice = rows.slice(Math.max(0, i - window + 1), i + 1);
    const avg = slice.reduce((s, d) => s + d[field], 0) / slice.length;
    return { x: toDate(r.date), y: Math.round(avg * 10) / 10 };
  });
}

function buildDatasets(rows, ma) {
  if (ma) {
    return [
      { label: "Max (5d avg)", data: movingAvg(rows, "max_f", 5), borderColor: COLORS.max, backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 3], pointRadius: 0, tension: 0 },
      { label: "Avg (5d avg)", data: movingAvg(rows, "avg_f", 5), borderColor: COLORS.avg, backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 3], pointRadius: 0, tension: 0 },
      { label: "Min (5d avg)", data: movingAvg(rows, "min_f", 5), borderColor: COLORS.min, backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 3], pointRadius: 0, tension: 0 },
    ];
  }
  return [
    { label: "Max", data: rows.map(r => ({ x: toDate(r.date), y: r.max_f })), borderColor: COLORS.max, backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, tension: 0 },
    { label: "Avg", data: rows.map(r => ({ x: toDate(r.date), y: r.avg_f })), borderColor: COLORS.avg, backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, tension: 0 },
    { label: "Min", data: rows.map(r => ({ x: toDate(r.date), y: r.min_f })), borderColor: COLORS.min, backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, tension: 0 },
  ];
}

function buildTODDatasets(rows, ma) {
  if (ma) {
    return [
      { label: "Time of daily max (5d avg)", data: movingAvg(rows, "max_hour", 5), borderColor: "#e07820", backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 3], pointRadius: 0, tension: 0 },
      { label: "Time of daily min (5d avg)", data: movingAvg(rows, "min_hour", 5), borderColor: "#2e7dd4", backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 3], pointRadius: 0, tension: 0 },
    ];
  }
  return [
    { label: "Time of daily max", data: rows.map(r => ({ x: toDate(r.date), y: r.max_hour })), borderColor: "#e07820", backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, tension: 0 },
    { label: "Time of daily min", data: rows.map(r => ({ x: toDate(r.date), y: r.min_hour })), borderColor: "#2e7dd4", backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, tension: 0 },
  ];
}

function toggleMA() {
  showMA = !showMA;
  document.getElementById("maBtn").classList.toggle("active", showMA);
  for (const { chart, rows } of charts) {
    chart.data.datasets = buildDatasets(rows, showMA);
    chart.update();
  }
  for (const { chart, rows } of todCharts) {
    chart.data.datasets = buildTODDatasets(rows, showMA);
    chart.update();
  }
}

function makeChart(ctx, label) {
  return new Chart(ctx, {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      parsing: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#4a6080" } },
        title: { display: false }
      },
      scales: {
        x: {
          type: "time",
          time: { unit: "month", tooltipFormat: "MMM d, yyyy" },
          ticks: { color: "#7a90a8", maxTicksLimit: 12 },
          grid:  { color: "#e8eef4" }
        },
        y: {
          ticks: { color: "#7a90a8", callback: v => (+v).toFixed(1) + "°F" },
          grid:  { color: "#e8eef4" }
        }
      }
    }
  });
}

async function load() {
  const data = await fetchJSON("/api/trends");
  const container = document.getElementById("charts");

  // group by label
  const byLabel = {};
  for (const row of data) {
    (byLabel[row.label] ??= []).push(row);
  }

  if (Object.keys(byLabel).length === 0) {
    container.innerHTML = '<p class="empty">No trend data yet. Data will appear after at least one full day of readings.</p>';
    return;
  }

  // find overall date range across all labels for consistent x-axis
  const allDates = data.map(r => toDate(r.date));
  const xMin = new Date(Math.min(...allDates));
  const xMax = new Date(Math.max(...allDates));

  for (const label of Object.keys(byLabel).sort()) {
    const rows = byLabel[label];

    const wrap = document.createElement("div");
    wrap.className = "chart-wrap";
    wrap.innerHTML = `<h2>${label.charAt(0).toUpperCase() + label.slice(1)}</h2><canvas></canvas>`;
    container.appendChild(wrap);

    const chart = makeChart(wrap.querySelector("canvas").getContext("2d"), label);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    charts.push({ chart, rows });

    chart.data.datasets = buildDatasets(rows, showMA);
    chart.update();
  }
}

function fmtHour(v) {
  const h = Math.floor(v), m = Math.round((v - h) * 60);
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12 = h % 12 || 12;
  return `${h12}:${String(m).padStart(2,'0')} ${ampm}`;
}

function makeTODChart(ctx) {
  return new Chart(ctx, {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      parsing: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#4a6080" } },
        tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${fmtHour(ctx.parsed.y)}` } }
      },
      scales: {
        x: {
          type: "time",
          time: { unit: "month", tooltipFormat: "MMM d, yyyy" },
          ticks: { color: "#7a90a8", maxTicksLimit: 12 },
          grid:  { color: "#e8eef4" }
        },
        y: {
          min: 0, max: 24,
          ticks: {
            stepSize: 6,
            color: "#7a90a8",
            callback: v => fmtHour(v)
          },
          grid: { color: ctx => ctx.tick.value === 12 ? "#c8d8e8" : "#e8eef4" }
        }
      }
    }
  });
}

async function loadTOD() {
  const data = await fetchJSON("/api/minmax-tod");
  const container = document.getElementById("tod-charts");

  const byLabel = {};
  for (const row of data) {
    (byLabel[row.label] ??= []).push(row);
  }

  if (Object.keys(byLabel).length === 0) {
    container.innerHTML = '<p class="empty">No data yet.</p>';
    return;
  }

  const allDates = data.map(r => toDate(r.date));
  const xMin = new Date(Math.min(...allDates));
  const xMax = new Date(Math.max(...allDates));

  for (const label of Object.keys(byLabel).sort()) {
    const rows = byLabel[label];

    const wrap = document.createElement("div");
    wrap.className = "chart-wrap";
    wrap.innerHTML = `<h2>${label.charAt(0).toUpperCase() + label.slice(1)}</h2><canvas></canvas>`;
    container.appendChild(wrap);

    const chart = makeTODChart(wrap.querySelector("canvas").getContext("2d"));
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    todCharts.push({ chart, rows });
    chart.data.datasets = buildTODDatasets(rows, showMA);
    chart.update();
  }
}

load();
loadTOD();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


_CHART_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CHART_TITLE &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .range-btns { margin-bottom: 1.5rem; display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
  </style>
</head>
<body>
  <h1>CHART_TITLE</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="range-btns">
    <button onclick="setRange(0.125)">3h</button>
    <button onclick="setRange(1)" class="active">24h</button>
    <button onclick="setRange(3)">3d</button>
    <button onclick="setRange(7)">7d</button>
    <button onclick="setRange(30)">30d</button>
  </div>
  CHART_CANVAS
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b"];
const colorMap = {};
function labelColor(lbl) { return colorMap[lbl] ?? COLORS[0]; }
let rangeDays = 1;
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function loadColors() {
  const data = await fetchJSON("/api/current");
  data.map(s => s.label).filter(Boolean).sort()
    .forEach((lbl, i) => { colorMap[lbl] = COLORS[i % COLORS.length]; });
}
function setRange(days) {
  rangeDays = days;
  document.querySelectorAll(".range-btns button").forEach((b,i) => {
    b.classList.toggle("active", [0.125,1,3,7,30][i] === days);
  });
  loadChart();
}
CHART_JS
loadColors().then(loadChart);
setInterval(() => loadColors().then(loadChart), 30000);
</script>
</body>
</html>"""


def _chart_page(title, canvas, js):
    return Response(
        _CHART_PAGE
            .replace("CHART_TITLE", title)
            .replace("CHART_CANVAS", canvas)
            .replace("CHART_JS", js),
        mimetype="text/html",
    )


_HISTORY_FETCH = """\
  const start = localISO(new Date(Date.now() - rangeDays * 86400000));
  const bucket = ({0.125:1,1:2,3:10,7:20,30:60})[rangeDays] || 1;
  const data = await fetchJSON(`/api/history?start=${start}&limit=8000&bucket_minutes=${bucket}`);
  const xMin = new Date(Date.now() - rangeDays * 86400000), xMax = new Date();
  const timeUnit = rangeDays >= 3 ? "day" : "hour";"""

_AXIS_UPDATE = """\
  chart.options.scales.x.min = xMin;
  chart.options.scales.x.max = xMax;
  chart.options.scales.x.time.unit = timeUnit;
  chart.update();"""


_DIFF_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Differentials &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .range-btns button:disabled { opacity: 0.3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Differentials</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
  </div>
  <div class="btn-group">
    <div class="range-btns" id="sensor-btns">
      <button onclick="setSensorMode('diff', this)" id="btn-diff" class="active">Inside/outside difference</button>
      <button onclick="setSensorMode('sun-shade-diff', this)" id="btn-sun-shade-diff">Shade/sun differential</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(0.125)" data-days="0.125">3h</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>Differential (&deg;F)</h2><canvas id="chart" height="120"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
function labelColor(lbl) { return colorMap[lbl] ?? COLORS[0]; }
let mode = "recent", rangeDays = 1, activeMonth = null, offsetMs = 0;
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = {
  recent: {
    low:    {0.125:10, 1:30,  3:60,  7:120, 30:360},
    medium: {0.125:3,  1:10,  3:20,  7:30,  30:60 },
    max:    {0.125:1,  1:2,   3:5,   7:10,  30:20 },
  },
  month:  { low: 240, medium: 60, max: 10 },
  year:   { low: 1440, medium: 360, max: 60 },
};
function getBucket() {
  if (mode === "recent") return BUCKETS.recent[resolution][rangeDays] || 60;
  return BUCKETS[mode][resolution];
}
const activeModes = new Set(['diff']);
function setSensorMode(mode, btn) {
  if (activeModes.has(mode)) {
    activeModes.delete(mode);
    btn.classList.remove('active');
  } else {
    activeModes.add(mode);
    btn.classList.add('active');
  }
  loadChart();
}
function isIndoorLabel(l) {
  const lo = l.toLowerCase();
  return lo.startsWith('indoor-') || lo.startsWith('inside-');
}
function splitDiff(allPts, crossings) {
  const crossTimes = crossings
    .map(ev => new Date(ev.ts.replace(' ', 'T')))
    .sort((a, b) => a - b);
  if (!crossTimes.length) {
    return {
      warmer: allPts.map(p => ({ x: p.x, y: p.y > 0 ? p.y : null })),
      cooler:  allPts.map(p => ({ x: p.x, y: p.y < 0 ? p.y : null })),
    };
  }
  const pts = [...allPts];
  for (const ev of crossings) {
    const t = new Date(ev.ts.replace(' ', 'T'));
    pts.push({ x: t, y: 0, _crossing: true, _parityTemp: ev.value });
  }
  pts.sort((a, b) => a.x - b.x);
  const beforeFirst = allPts.filter(p => p.x < crossTimes[0]);
  const initialAvg = beforeFirst.length
    ? beforeFirst.reduce((s, p) => s + p.y, 0) / beforeFirst.length
    : 0;
  const startsWarmer = initialAvg >= 0;
  const warmer = [], cooler = [];
  for (const p of pts) {
    if (p._crossing) {
      warmer.push({ x: p.x, y: 0, _parity: true, _parityTemp: p._parityTemp });
      cooler.push({ x: p.x, y: 0, _parityHidden: true });
      continue;
    }
    const nCrossed = crossTimes.filter(t => t <= p.x).length;
    const isWarmerSide = startsWarmer ? nCrossed % 2 === 0 : nCrossed % 2 !== 0;
    warmer.push({ x: p.x, y: isWarmerSide ? p.y : null });
    cooler.push({ x: p.x, y: isWarmerSide ? null : p.y });
  }
  return { warmer, cooler };
}
function buildSensorDatasets(data, events, isMonth) {
  const allLabels = [...new Set(data.map(r => r.label).filter(Boolean))];
  const indoorLabels = allLabels.filter(isIndoorLabel);
  const datasets = [];
  function makePoints(rows, labelKey, tsKey) {
    const byKey = {};
    for (const row of rows) {
      const k = isMonth ? `${row[labelKey]} ${row.year}` : row[labelKey];
      (byKey[k] ??= []).push({ x: new Date(row[tsKey]), y: row.temp_f });
    }
    return byKey;
  }
  if (activeModes.has('diff')) {
    const shadeLbl = allLabels.find(l => l.toLowerCase().replace(/[_\\s]/g,'-') === 'outside-shade');
    if (shadeLbl && indoorLabels.length > 0) {
      if (isMonth) {
        const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
        years.forEach((year, yi) => {
          const shadeMap = {};
          data.filter(r => r.label === shadeLbl && r.year === year).forEach(r => { shadeMap[r.ts] = r.temp_f; });
          const indoorMap = {};
          data.filter(r => indoorLabels.includes(r.label) && r.year === year && r.temp_f != null)
            .forEach(r => { (indoorMap[r.ts] ??= []).push(r.temp_f); });
          const allPts = Object.entries(indoorMap)
            .filter(([ts]) => shadeMap[ts] != null)
            .map(([ts, vals]) => ({ x: new Date(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length - shadeMap[ts] }))
            .sort((a,b) => a.x - b.x);
          const dash = yi > 0 ? [4,3] : [];
          const { warmer, cooler } = splitDiff(allPts, []);
          datasets.push({ label: `Degrees warmer inside ${year}`, backgroundColor: 'transparent',
            data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
          datasets.push({ label: `Degrees cooler inside ${year}`, backgroundColor: 'transparent',
            data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
        });
      } else {
        const shadeMap = {};
        data.filter(r => r.label === shadeLbl).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        const indoorMap = {};
        data.filter(r => indoorLabels.includes(r.label) && r.temp_f != null)
          .forEach(r => { (indoorMap[r.ts] ??= []).push(r.temp_f); });
        const allTs = new Set([...Object.keys(indoorMap), ...Object.keys(shadeMap)]);
        const allPts = [...allTs]
          .map(ts => { const sv = shadeMap[ts], iv = indoorMap[ts]; return { x: new Date(ts), y: (sv != null && iv) ? iv.reduce((a,b)=>a+b,0)/iv.length - sv : null }; })
          .sort((a,b) => a.x - b.x);
        const ioCrossings = events.filter(e => e.event_type === 'inside_outside_parity');
        const { warmer, cooler } = splitDiff(allPts, ioCrossings);
        datasets.push({ label: 'Degrees warmer inside', backgroundColor: 'transparent',
          data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0 });
        datasets.push({ label: 'Degrees cooler inside', backgroundColor: 'transparent',
          data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0 });
      }
    }
  }
  if (activeModes.has('sun-shade-diff')) {
    const shadeLbl = allLabels.find(l => l.toLowerCase().replace(/[_\\s]/g,'-') === 'outside-shade');
    const sunLbl   = allLabels.find(l => l.toLowerCase().replace(/[_\\s]/g,'-') === 'outside-sun');
    if (shadeLbl && sunLbl) {
      if (isMonth) {
        const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
        years.forEach((year, yi) => {
          const shadeMap = {}, sunMap = {};
          data.filter(r => r.label === shadeLbl && r.year === year).forEach(r => { shadeMap[r.ts] = r.temp_f; });
          data.filter(r => r.label === sunLbl   && r.year === year).forEach(r => { sunMap[r.ts]   = r.temp_f; });
          const allPts = Object.keys(sunMap)
            .filter(ts => shadeMap[ts] != null)
            .map(ts => ({ x: new Date(ts), y: sunMap[ts] - shadeMap[ts] }))
            .sort((a,b) => a.x - b.x);
          const dash = yi > 0 ? [4,3] : [];
          const { warmer, cooler } = splitDiff(allPts, []);
          datasets.push({ label: `Degrees warmer in sun ${year}`, backgroundColor: 'transparent',
            data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
          datasets.push({ label: `Degrees cooler in sun ${year}`, backgroundColor: 'transparent',
            data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
        });
      } else {
        const shadeMap = {}, sunMap = {};
        data.filter(r => r.label === shadeLbl).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        data.filter(r => r.label === sunLbl  ).forEach(r => { sunMap[r.ts]   = r.temp_f; });
        const allTs = new Set([...Object.keys(sunMap), ...Object.keys(shadeMap)]);
        const allPts = [...allTs]
          .map(ts => ({ x: new Date(ts), y: (sunMap[ts] != null && shadeMap[ts] != null) ? sunMap[ts] - shadeMap[ts] : null }))
          .sort((a,b) => a.x - b.x);
        const ssCrossings = events.filter(e => e.event_type === 'sun_shade_parity');
        const { warmer, cooler } = splitDiff(allPts, ssCrossings);
        datasets.push({ label: 'Degrees warmer in sun', backgroundColor: 'transparent',
          data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0 });
        datasets.push({ label: 'Degrees cooler in sun', backgroundColor: 'transparent',
          data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0 });
      }
    }
  }
  return datasets;
}
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function loadColors() {
  const data = await fetchJSON("/api/current");
  data.map(s => s.label).filter(Boolean).sort()
    .forEach((lbl, i) => { colorMap[lbl] = COLORS[i % COLORS.length]; });
}
function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  loadChart();
}
function setRange(days) {
  mode = "recent"; rangeDays = days; offsetMs = 0;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  loadChart();
}
function setAllMonths() {
  mode = "year";
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) =>
    b.classList.toggle("active", i === 0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
function setMonth(m) {
  mode = "month"; activeMonth = m;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) =>
    b.classList.toggle("active", i === m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
Chart.Interaction.modes.nearestXPerDataset = function(chart, e, options, useFinalPosition) {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  const items = [];
  chart.data.datasets.forEach((_, datasetIndex) => {
    if (!chart.isDatasetVisible(datasetIndex)) return;
    const meta = chart.getDatasetMeta(datasetIndex);
    let nearest = null, nearestDist = Infinity;
    meta.data.forEach((element, index) => {
      const { x } = element.getProps(['x'], useFinalPosition);
      const dist = Math.abs(x - pos.x);
      if (dist < nearestDist) { nearestDist = dist; nearest = { element, datasetIndex, index }; }
    });
    if (nearest) items.push(nearest);
  });
  return items;
};
const chart = new Chart(document.getElementById("chart"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "nearestXPerDataset", intersect: false },
    plugins: {
      legend: { labels: { color: "#4a6080" } },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            const raw = item.raw;
            if (!raw || raw._parityHidden) continue;
            if (raw._parity) {
              const temp = raw._parityTemp != null ? raw._parityTemp.toFixed(1) + '\\u00b0F' : '';
              const lbl = '\\u2696\\ufe0f Parity' + (temp ? ': ' + temp : '');
              html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:#b06ed0;border:1px solid #b06ed0;flex-shrink:0;"></span><span style="color:#b06ed0;font-weight:bold;">' + lbl + '</span></div>';
              continue;
            }
            if (raw.y == null) continue;
            const color = item.dataset.borderColor || '#ccc';
            const lbl = (item.dataset.label || '') + ': ' + Math.abs(raw.y).toFixed(1) + '\\u00b0F';
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';border:1px solid ' + color + ';flex-shrink:0;"></span><span>' + lbl + '</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 25 }, grid: { color: "#e8eef4" } },
      y: { ticks: { color: "#7a90a8", callback: v => (+v).toFixed(1) + "\\u00b0F" }, grid: { color: ctx => ctx.tick.value === 0 ? "#aabbc8" : "#e8eef4" } }
    }
  }
});
async function loadChart() {
  if (mode === "recent") {
    const xMax = new Date(Date.now() + offsetMs);
    const xMin = new Date(xMax - rangeDays * 86400000);
    const params = `start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`;
    const [data, events] = await Promise.all([
      fetchJSON(`/api/history?${params}`),
      fetchJSON(`/api/events?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=200`),
    ]);
    chart.data.datasets = buildSensorDatasets(data, events, false);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    if (rangeDays === 0.125) {
      chart.options.scales.x.time.unit = "minute";
      chart.options.scales.x.ticks.stepSize = 30;
    } else if (rangeDays === 1) {
      chart.options.scales.x.time.unit = "hour";
      chart.options.scales.x.ticks.stepSize = 1;
    } else {
      chart.options.scales.x.time.unit = "day";
      chart.options.scales.x.ticks.stepSize = 1;
    }
    const peek = await fetchJSON(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`);
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    const data = await fetchJSON(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
    chart.data.datasets = buildSensorDatasets(data, [], true);
    const xMin = new Date(2000, activeMonth - 1, 1);
    const xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "day";
  } else {
    const data = await fetchJSON(`/api/history/year?bucket_minutes=${getBucket()}`);
    chart.data.datasets = buildSensorDatasets(data, [], true);
    chart.options.scales.x.min = new Date(2000, 0, 1);
    chart.options.scales.x.max = new Date(2000, 11, 31, 23, 59, 59);
    chart.options.scales.x.time.unit = "month";
  }
  chart.update();
}
loadColors().then(loadChart);
setInterval(() => loadColors().then(loadChart), 30000);
</script>
</body>
</html>"""

_TEMP_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Temperature &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .range-btns button:disabled { opacity: 0.3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
    #resp-size { font-size: .72rem; color: #4a6080; }
    .day-row { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
    .day-row select, .day-row input[type=number] { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
    .day-row input[type=number] { width: 5rem; }
    .day-row button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .day-row button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .day-row button.active { background: #e07820; color: #fff; border-color: #e07820; }
  </style>
</head>
<body>
  <h1>Temperature</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a> &nbsp;|&nbsp; <a href="/chart/typical-day">Typical Day &rarr;</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
    <span id="resp-size"></span>
  </div>
  <div class="btn-group">
    <div class="range-btns" id="sensor-btns">
      <button onclick="setSensorMode('outside-sun', this)" id="btn-outside-sun">Outside (sun)</button>
      <button onclick="setSensorMode('outside-shade', this)" id="btn-outside-shade" class="active">Outside (shade)</button>
      <button onclick="setSensorMode('indoor-avg', this)" id="btn-indoor-avg" class="active">Indoor average</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(0.125)" data-days="0.125">3h</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Day</div>
    <div class="day-row">
      <select id="day-month">
        <option value="1">January</option><option value="2">February</option>
        <option value="3">March</option><option value="4">April</option>
        <option value="5">May</option><option value="6">June</option>
        <option value="7">July</option><option value="8">August</option>
        <option value="9">September</option><option value="10">October</option>
        <option value="11">November</option><option value="12">December</option>
      </select>
      <input type="number" id="day-day" min="1" max="31" placeholder="Day" style="width:5rem">
      <select id="day-year"><option value="">Year</option></select>
      <button id="day-go-btn" onclick="applyDay()">Go</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>Temperature (&deg;F)</h2><canvas id="chart" height="120"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
function labelColor(lbl) { return colorMap[lbl] ?? COLORS[0]; }
const SENSOR_COLORS = { 'outside-sun': '#e07820', 'outside-shade': '#2e7dd4', 'indoor-avg': '#2a9d6e' };
function modeColor(m) { return SENSOR_COLORS[m] ?? colorMap[m] ?? COLORS[0]; }
function hexToRgb(hex) { return [parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)]; }
function applyBtnColor(btn, color, active) {
  const [r,g,b] = hexToRgb(color);
  if (active) {
    btn.style.background = color; btn.style.borderColor = color; btn.style.color = '#fff';
  } else {
    btn.style.background = `rgba(${r},${g},${b},0.06)`;
    btn.style.borderColor = `rgba(${r},${g},${b},0.2)`;
    btn.style.color = '#7a90a8';
  }
}
let mode = "recent", rangeDays = 1, activeMonth = null, activeDay = null, offsetMs = 0;
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname) || /\\.local$/.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = {
  recent: {
    low:    {0.125:10, 1:30,  3:60,  7:120, 30:360},
    medium: {0.125:3,  1:10,  3:20,  7:30,  30:60 },
    max:    {0.125:1,  1:2,   3:5,   7:10,  30:20 },
  },
  month:  { low: 240, medium: 60, max: 10 },
  year:   { low: 1440, medium: 360, max: 60 },
  day:    { low: 30, medium: 10, max: 2 },
};
function getBucket() {
  if (mode === "recent") return BUCKETS.recent[resolution][rangeDays] || 60;
  return BUCKETS[mode][resolution];
}
// Sensor view mode: which of the 4 buttons are active (multi-select)
const activeModes = new Set(['outside-shade', 'indoor-avg']);
const poolLabels = new Set();
function setSensorMode(mode, btn) {
  if (activeModes.has(mode)) {
    activeModes.delete(mode);
    btn.classList.remove('active');
  } else {
    activeModes.add(mode);
    btn.classList.add('active');
  }
  applyBtnColor(btn, modeColor(mode), activeModes.has(mode));
  loadChart();
}

// Returns true if a label belongs to an indoor/inside sensor
function isIndoorLabel(l) {
  const lo = l.toLowerCase();
  return lo.startsWith('indoor-') || lo.startsWith('inside-');
}

// Split allPts into warmer/cooler datasets using crossing events as boundaries.
// Points are assigned to a side by chronological segment (not by individual sign),
// so a bucket that briefly averages to the wrong sign near a crossing still
// connects cleanly to the y=0 crossing point — no gap.
function splitDiff(allPts, crossings) {
  const crossTimes = crossings
    .map(ev => new Date(ev.ts.replace(' ', 'T')))
    .sort((a, b) => a - b);

  if (!crossTimes.length) {
    // No crossing events — simple sign-based split
    return {
      warmer: allPts.map(p => ({ x: p.x, y: p.y > 0 ? p.y : null })),
      cooler:  allPts.map(p => ({ x: p.x, y: p.y < 0 ? p.y : null })),
    };
  }

  // Insert crossing points (y=0) into the sequence and re-sort
  const pts = [...allPts];
  for (const ev of crossings) {
    const t = new Date(ev.ts.replace(' ', 'T'));
    pts.push({ x: t, y: 0, _crossing: true, _parityTemp: ev.value });
  }
  pts.sort((a, b) => a.x - b.x);

  // Determine which side comes first: look at the average of data before
  // the first crossing to decide if the initial segment is warmer or cooler.
  const beforeFirst = allPts.filter(p => p.x < crossTimes[0]);
  const initialAvg = beforeFirst.length
    ? beforeFirst.reduce((s, p) => s + p.y, 0) / beforeFirst.length
    : 0;
  const startsWarmer = initialAvg >= 0;

  const warmer = [], cooler = [];
  for (const p of pts) {
    if (p._crossing) {
      // Only mark the warmer entry as parity so the tooltip shows once, not twice
      warmer.push({ x: p.x, y: 0, _parity: true, _parityTemp: p._parityTemp });
      cooler.push({ x: p.x, y: 0, _parityHidden: true });
      continue;
    }
    // Number of crossings at or before this point determines which side we're on
    const nCrossed = crossTimes.filter(t => t <= p.x).length;
    const isWarmerSide = startsWarmer ? nCrossed % 2 === 0 : nCrossed % 2 !== 0;
    warmer.push({ x: p.x, y: isWarmerSide ? p.y : null });
    cooler.push({ x: p.x, y: isWarmerSide ? null : p.y });
  }
  return { warmer, cooler };
}

// Build datasets from raw API data according to active sensor modes.
// All lines use tension:0 (straight point-to-point) with no smoothing applied.
function buildSensorDatasets(data, events, isMonth) {
  const allLabels = [...new Set(data.map(r => r.label).filter(Boolean))];
  const indoorLabels = allLabels.filter(isIndoorLabel);
  const datasets = [];

  function makePoints(rows, labelKey, tsKey) {
    const byKey = {};
    for (const row of rows) {
      const k = isMonth ? `${row[labelKey]} ${row.year}` : row[labelKey];
      (byKey[k] ??= []).push({ x: new Date(row[tsKey]), y: row.temp_f });
    }
    return byKey;
  }

  // Outside (sun)
  if (activeModes.has('outside-sun')) {
    const lbl = allLabels.find(l => l.toLowerCase().replace(/[_\s]/g,'-') === 'outside-sun');
    if (lbl) {
      const pts = makePoints(data.filter(r => r.label === lbl), 'label', 'ts');
      Object.entries(pts).forEach(([key, points]) => {
        datasets.push({ label: isMonth ? `Outside (sun) ${key.split(' ').pop()}` : 'Outside (sun)',
          data: points, borderColor: '#e07820', backgroundColor: 'transparent',
          borderWidth: 1.5, pointRadius: 0, tension: 0 });
      });
    }
  }

  // Outside (shade)
  if (activeModes.has('outside-shade')) {
    const lbl = allLabels.find(l => l.toLowerCase().replace(/[_\s]/g,'-') === 'outside-shade');
    if (lbl) {
      const pts = makePoints(data.filter(r => r.label === lbl), 'label', 'ts');
      Object.entries(pts).forEach(([key, points]) => {
        datasets.push({ label: isMonth ? `Outside (shade) ${key.split(' ').pop()}` : 'Outside (shade)',
          data: points, borderColor: '#2e7dd4', backgroundColor: 'transparent',
          borderWidth: 1.5, pointRadius: 0, tension: 0 });
      });
    }
  }

  // Indoor average — raw mean per timestamp, no smoothing, tension:0
  if (activeModes.has('indoor-avg') && indoorLabels.length > 0) {
    if (isMonth) {
      const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
      years.forEach(year => {
        const yearRows = data.filter(r => r.year === year && indoorLabels.includes(r.label));
        const tsMap = {};
        for (const row of yearRows) (tsMap[row.ts] ??= []).push(row.temp_f);
        const pts = Object.entries(tsMap)
          .map(([ts, vals]) => ({ x: new Date(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length }))
          .sort((a,b) => a.x - b.x);
        datasets.push({ label: `Indoor average ${year}`, data: pts,
          borderColor: '#2a9d6e', backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: 0, tension: 0 });
      });
    } else {
      const tsMap = {};
      for (const row of data) {
        if (!indoorLabels.includes(row.label) || row.temp_f == null) continue;
        (tsMap[row.ts] ??= []).push(row.temp_f);
      }
      const pts = Object.entries(tsMap)
        .map(([ts, vals]) => ({ x: new Date(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length }))
        .sort((a,b) => a.x - b.x);
      datasets.push({ label: 'Indoor average', data: pts,
        borderColor: '#2a9d6e', backgroundColor: 'transparent',
        borderWidth: 2, pointRadius: 0, tension: 0 });
    }
  }

  // Individual rooms — one line per toggled indoor-* sensor
  indoorLabels.sort().forEach(lbl => {
    if (!activeModes.has(lbl)) return;
    const color = labelColor(lbl);
    if (isMonth) {
      const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
      years.forEach((year, yi) => {
        const rows = data.filter(r => r.label === lbl && r.year === year);
        const pts = rows.map(r => ({ x: new Date(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
        datasets.push({ label: `${lbl} ${year}`, data: pts,
          borderColor: color, backgroundColor: 'transparent',
          borderWidth: 1.5, pointRadius: 0, tension: 0,
          borderDash: yi > 0 ? [4,3] : [] });
      });
    } else {
      const pts = data.filter(r => r.label === lbl)
        .map(r => ({ x: new Date(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
      datasets.push({ label: lbl, data: pts,
        borderColor: color, backgroundColor: 'transparent',
        borderWidth: 1.5, pointRadius: 0, tension: 0 });
    }
  });

  // Pool sensors — one line per toggled pool sensor
  [...poolLabels].sort().forEach(lbl => {
    if (!activeModes.has(lbl)) return;
    const color = labelColor(lbl);
    if (isMonth) {
      const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
      years.forEach((year, yi) => {
        const rows = data.filter(r => r.label === lbl && r.year === year && r.temp_f != null);
        const pts = rows.map(r => ({ x: new Date(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
        datasets.push({ label: `${lbl} ${year}`, data: pts,
          borderColor: color, backgroundColor: 'transparent',
          borderWidth: 1.5, pointRadius: 0, tension: 0,
          borderDash: yi > 0 ? [4,3] : [] });
      });
    } else {
      const pts = data.filter(r => r.label === lbl && r.temp_f != null)
        .map(r => ({ x: new Date(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
      datasets.push({ label: lbl, data: pts,
        borderColor: color, backgroundColor: 'transparent',
        borderWidth: 1.5, pointRadius: 0, tension: 0 });
    }
  });

  // Inside/outside difference — split into warmer (positive) and cooler (negative)
  if (activeModes.has('diff')) {
    const shadeLbl = allLabels.find(l => l.toLowerCase().replace(/[_\s]/g,'-') === 'outside-shade');
    if (shadeLbl && indoorLabels.length > 0) {
      if (isMonth) {
        const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
        years.forEach((year, yi) => {
          const shadeMap = {};
          data.filter(r => r.label === shadeLbl && r.year === year).forEach(r => { shadeMap[r.ts] = r.temp_f; });
          const indoorMap = {};
          data.filter(r => indoorLabels.includes(r.label) && r.year === year && r.temp_f != null)
            .forEach(r => { (indoorMap[r.ts] ??= []).push(r.temp_f); });
          const allPts = Object.entries(indoorMap)
            .filter(([ts]) => shadeMap[ts] != null)
            .map(([ts, vals]) => ({ x: new Date(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length - shadeMap[ts] }))
            .sort((a,b) => a.x - b.x);
          const dash = yi > 0 ? [4,3] : [];
          const { warmer, cooler } = splitDiff(allPts, []);
          datasets.push({ label: `Degrees warmer inside ${year}`, backgroundColor: 'transparent',
            data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
          datasets.push({ label: `Degrees cooler inside ${year}`, backgroundColor: 'transparent',
            data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
        });
      } else {
        const shadeMap = {};
        data.filter(r => r.label === shadeLbl).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        const indoorMap = {};
        data.filter(r => indoorLabels.includes(r.label) && r.temp_f != null)
          .forEach(r => { (indoorMap[r.ts] ??= []).push(r.temp_f); });
        const allTs = new Set([...Object.keys(indoorMap), ...Object.keys(shadeMap)]);
        const allPts = [...allTs]
          .map(ts => { const sv = shadeMap[ts], iv = indoorMap[ts]; return { x: new Date(ts), y: (sv != null && iv) ? iv.reduce((a,b)=>a+b,0)/iv.length - sv : null }; })
          .sort((a,b) => a.x - b.x);
        const ioCrossings = events.filter(e => e.event_type === 'inside_outside_parity');
        const { warmer, cooler } = splitDiff(allPts, ioCrossings);
        datasets.push({ label: 'Degrees warmer inside', backgroundColor: 'transparent',
          data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0 });
        datasets.push({ label: 'Degrees cooler inside', backgroundColor: 'transparent',
          data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0 });
      }
    }
  }

  // Shade/sun differential — outside-sun minus outside-shade
  if (activeModes.has('sun-shade-diff')) {
    const shadeLbl = allLabels.find(l => l.toLowerCase().replace(/[_\s]/g,'-') === 'outside-shade');
    const sunLbl   = allLabels.find(l => l.toLowerCase().replace(/[_\s]/g,'-') === 'outside-sun');
    if (shadeLbl && sunLbl) {
      if (isMonth) {
        const years = [...new Set(data.map(r => r.year).filter(Boolean))].sort();
        years.forEach((year, yi) => {
          const shadeMap = {}, sunMap = {};
          data.filter(r => r.label === shadeLbl && r.year === year).forEach(r => { shadeMap[r.ts] = r.temp_f; });
          data.filter(r => r.label === sunLbl   && r.year === year).forEach(r => { sunMap[r.ts]   = r.temp_f; });
          const allPts = Object.keys(sunMap)
            .filter(ts => shadeMap[ts] != null)
            .map(ts => ({ x: new Date(ts), y: sunMap[ts] - shadeMap[ts] }))
            .sort((a,b) => a.x - b.x);
          const dash = yi > 0 ? [4,3] : [];
          const { warmer, cooler } = splitDiff(allPts, []);
          datasets.push({ label: `Degrees warmer in sun ${year}`, backgroundColor: 'transparent',
            data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
          datasets.push({ label: `Degrees cooler in sun ${year}`, backgroundColor: 'transparent',
            data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: dash });
        });
      } else {
        const shadeMap = {}, sunMap = {};
        data.filter(r => r.label === shadeLbl).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        data.filter(r => r.label === sunLbl  ).forEach(r => { sunMap[r.ts]   = r.temp_f; });
        const allTs = new Set([...Object.keys(sunMap), ...Object.keys(shadeMap)]);
        const allPts = [...allTs]
          .map(ts => ({ x: new Date(ts), y: (sunMap[ts] != null && shadeMap[ts] != null) ? sunMap[ts] - shadeMap[ts] : null }))
          .sort((a,b) => a.x - b.x);
        const ssCrossings = events.filter(e => e.event_type === 'sun_shade_parity');
        const { warmer, cooler } = splitDiff(allPts, ssCrossings);
        datasets.push({ label: 'Degrees warmer in sun', backgroundColor: 'transparent',
          data: warmer, borderColor: '#e74c3c', borderWidth: 1.5, pointRadius: 0, tension: 0 });
        datasets.push({ label: 'Degrees cooler in sun', backgroundColor: 'transparent',
          data: cooler, borderColor: '#2980b9', borderWidth: 1.5, pointRadius: 0, tension: 0 });
      }
    }
  }

  return datasets;
}

function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function loadColors() {
  const data = await fetchJSON("/api/current");
  const reservedColors = new Set(Object.values(SENSOR_COLORS));
  const roomColors = COLORS.filter(c => !reservedColors.has(c));
  data.map(s => s.label).filter(Boolean).sort()
    .forEach((lbl, i) => { colorMap[lbl] = roomColors[i % roomColors.length]; });
  const indoorLabels = data.map(s => s.label).filter(l => l && isIndoorLabel(l)).sort();
  const newPoolLabels = data.filter(s => s.source === 'pool' && s.label).map(s => s.label).sort();
  newPoolLabels.forEach(l => poolLabels.add(l));
  const sensorBtns = document.getElementById('sensor-btns');

  // Indoor room buttons
  const existingRoomBtns = [...sensorBtns.querySelectorAll('button[data-room]')];
  const existing = existingRoomBtns.map(b => b.dataset.room);
  if (JSON.stringify(existing) !== JSON.stringify(indoorLabels)) {
    existingRoomBtns.forEach(b => b.remove());
    indoorLabels.forEach(lbl => {
      const btn = document.createElement('button');
      btn.dataset.room = lbl;
      btn.textContent = lbl.replace(/^in(door|side)-/i, '').replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      btn.onclick = () => setSensorMode(lbl, btn);
      if (activeModes.has(lbl)) btn.classList.add('active');
      const diffBtn = document.getElementById('btn-diff');
      sensorBtns.insertBefore(btn, diffBtn);
      applyBtnColor(btn, modeColor(lbl), activeModes.has(lbl));
    });
  } else {
    existingRoomBtns.forEach(b => applyBtnColor(b, modeColor(b.dataset.room), activeModes.has(b.dataset.room)));
  }

  // Pool sensor buttons
  const existingPoolBtns = [...sensorBtns.querySelectorAll('button[data-pool]')];
  const existingPool = existingPoolBtns.map(b => b.dataset.pool);
  if (JSON.stringify(existingPool) !== JSON.stringify(newPoolLabels)) {
    existingPoolBtns.forEach(b => b.remove());
    newPoolLabels.forEach(lbl => {
      const btn = document.createElement('button');
      btn.dataset.pool = lbl;
      btn.textContent = lbl;
      btn.onclick = () => setSensorMode(lbl, btn);
      if (activeModes.has(lbl)) btn.classList.add('active');
      sensorBtns.appendChild(btn);
      applyBtnColor(btn, modeColor(lbl), activeModes.has(lbl));
    });
  } else {
    existingPoolBtns.forEach(b => applyBtnColor(b, modeColor(b.dataset.pool), activeModes.has(b.dataset.pool)));
  }

  // Apply colors to static sensor buttons
  [['btn-outside-sun','outside-sun'],['btn-outside-shade','outside-shade'],['btn-indoor-avg','indoor-avg']].forEach(([id,m]) => {
    const btn = document.getElementById(id);
    if (btn) applyBtnColor(btn, modeColor(m), activeModes.has(m));
  });
}
function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  loadChart();
}
function clearDayActive() { document.getElementById('day-go-btn').classList.remove('active'); }
function setRange(days) {
  mode = "recent"; rangeDays = days; offsetMs = 0;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  clearDayActive();
  loadChart();
}
function setAllMonths() {
  mode = "year";
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) =>
    b.classList.toggle("active", i === 0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  clearDayActive();
  loadChart();
}
function setMonth(m) {
  mode = "month"; activeMonth = m;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) =>
    b.classList.toggle("active", i === m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  clearDayActive();
  loadChart();
}
function applyDay() {
  const m = parseInt(document.getElementById('day-month').value);
  const d = parseInt(document.getElementById('day-day').value);
  const y = parseInt(document.getElementById('day-year').value);
  if (!m || !d || !y || d < 1 || d > 31) return;
  mode = "day"; activeDay = {year: y, month: m, day: d};
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  document.getElementById('day-go-btn').classList.add('active');
  loadChart();
}

// Custom interaction mode: find the nearest point by x-pixel for each dataset
// independently, so tooltip stays synchronized on time-series with unequal lengths.
Chart.Interaction.modes.nearestXPerDataset = function(chart, e, options, useFinalPosition) {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  const items = [];
  chart.data.datasets.forEach((_, datasetIndex) => {
    if (!chart.isDatasetVisible(datasetIndex)) return;
    const meta = chart.getDatasetMeta(datasetIndex);
    let nearest = null, nearestDist = Infinity;
    meta.data.forEach((element, index) => {
      const { x } = element.getProps(['x'], useFinalPosition);
      const dist = Math.abs(x - pos.x);
      if (dist < nearestDist) { nearestDist = dist; nearest = { element, datasetIndex, index }; }
    });
    if (nearest) items.push(nearest);
  });
  return items;
};

const chart = new Chart(document.getElementById("chart"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "nearestXPerDataset", intersect: false },
    plugins: {
      legend: { labels: { color: "#4a6080" } },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            const raw = item.raw;
            if (!raw || raw._parityHidden) continue;
            if (raw._parity) {
              const temp = raw._parityTemp != null ? raw._parityTemp.toFixed(1) + '\\u00b0F' : '';
              const lbl = '\\u2696\\ufe0f Parity' + (temp ? ': ' + temp : '');
              html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:#b06ed0;border:1px solid #b06ed0;flex-shrink:0;"></span><span style="color:#b06ed0;font-weight:bold;">' + lbl + '</span></div>';
              continue;
            }
            if (raw.y == null) continue;
            const color = item.dataset.borderColor || '#ccc';
            const lbl = (item.dataset.label || '') + ': ' + Math.abs(raw.y).toFixed(1) + '\\u00b0F';
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';border:1px solid ' + color + ';flex-shrink:0;"></span><span>' + lbl + '</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 25 }, grid: { color: "#e8eef4" } },
      y: { ticks: { color: "#7a90a8", callback: v => (+v).toFixed(1) + "\\u00b0F" }, grid: { color: "#e8eef4" } }
    }
  }
});

function fmtBytes(n) {
  return n >= 1048576 ? (n/1048576).toFixed(1) + ' MB'
       : n >= 1024    ? (n/1024).toFixed(1) + ' KB'
       :                n + ' B';
}
async function fetchJSONBytes(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    const cl = r.headers.get('content-length');
    const text = await r.text();
    const bytes = cl !== null ? parseInt(cl) : new TextEncoder().encode(text).length;
    return { data: JSON.parse(text), bytes };
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
async function loadChart() {
  let totalBytes = 0;
  if (mode === "recent") {
    const xMax = new Date(Date.now() + offsetMs);
    const xMin = new Date(xMax - rangeDays * 86400000);
    const params = `start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`;
    const [hist, evts] = await Promise.all([
      fetchJSONBytes(`/api/history?${params}`),
      fetchJSONBytes(`/api/events?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=200`),
    ]);
    totalBytes = hist.bytes + evts.bytes;
    chart.data.datasets = buildSensorDatasets(hist.data, evts.data, false);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    if (rangeDays === 0.125) {
      chart.options.scales.x.time.unit = "minute";
      chart.options.scales.x.ticks.stepSize = 30;
    } else if (rangeDays === 1) {
      chart.options.scales.x.time.unit = "hour";
      chart.options.scales.x.ticks.stepSize = 1;
    } else {
      chart.options.scales.x.time.unit = "day";
      chart.options.scales.x.ticks.stepSize = 1;
    }
    const peek = await fetchJSON(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`);
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    const { data, bytes } = await fetchJSONBytes(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
    totalBytes = bytes;
    chart.data.datasets = buildSensorDatasets(data, [], true);
    const xMin = new Date(2000, activeMonth - 1, 1);
    const xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "day";
  } else if (mode === "day" && activeDay) {
    const { year, month, day } = activeDay;
    const xMin = new Date(year, month - 1, day, 0, 0, 0);
    const xMax = new Date(year, month - 1, day, 23, 59, 59);
    const [hist, evts] = await Promise.all([
      fetchJSONBytes(`/api/history?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`),
      fetchJSONBytes(`/api/events?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=200`),
    ]);
    totalBytes = hist.bytes + evts.bytes;
    chart.data.datasets = buildSensorDatasets(hist.data, evts.data, false);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "hour";
    chart.options.scales.x.ticks.stepSize = 1;
  } else {
    const { data, bytes } = await fetchJSONBytes(`/api/history/year?bucket_minutes=${getBucket()}`);
    totalBytes = bytes;
    chart.data.datasets = buildSensorDatasets(data, [], true);
    chart.options.scales.x.min = new Date(2000, 0, 1);
    chart.options.scales.x.max = new Date(2000, 11, 31, 23, 59, 59);
    chart.options.scales.x.time.unit = "month";
  }
  document.getElementById('resp-size').textContent = fmtBytes(totalBytes);
  chart.update();
}
async function populateYears() {
  const years = await fetchJSON("/api/history/years");
  const sel = document.getElementById('day-year');
  const now = new Date();
  // Pre-fill controls with today if not already set
  if (!document.getElementById('day-day').value) {
    document.getElementById('day-month').value = now.getMonth() + 1;
    document.getElementById('day-day').value = now.getDate();
  }
  const cur = sel.value || String(now.getFullYear());
  sel.innerHTML = years.map(y => `<option value="${y}"${String(y) === cur ? ' selected' : ''}>${y}</option>`).join('');
}
loadColors().then(() => { populateYears(); loadChart(); });
setInterval(() => { if (mode !== "day") loadColors().then(loadChart); }, 30000);
</script>
</body>
</html>"""


@app.get("/chart/temperature")
def chart_temperature():
    return Response(_TEMP_PAGE, mimetype="text/html")


_TYPICAL_DAY_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Typical Temperature Day &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
    #resp-size { font-size: .72rem; color: #4a6080; }
  </style>
</head>
<body>
  <h1>Typical Temperature Day</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a> &nbsp;|&nbsp; <a href="/chart/temperature">&larr; Temperature</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
    <span id="resp-size"></span>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Sensors</div>
    <div class="range-btns" id="sensor-btns">
      <button onclick="toggleSensor('outside-sun', this)" id="btn-outside-sun">Outside (sun)</button>
      <button onclick="toggleSensor('outside-shade', this)" id="btn-outside-shade">Outside (shade)</button>
      <button onclick="toggleSensor('indoor-avg', this)" id="btn-indoor-avg">Indoor average</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Range</div>
    <div class="range-btns" id="range-btns">
      <button onclick="setRange('days', 7, this)">Last 7 Days</button>
      <button onclick="setRange('days', 30, this)">Last 30 Days</button>
      <button onclick="setRange('all', null, this)" id="btn-all-time">All Time</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setRange('month', 1, this)">Jan</button>
      <button onclick="setRange('month', 2, this)">Feb</button>
      <button onclick="setRange('month', 3, this)">Mar</button>
      <button onclick="setRange('month', 4, this)">Apr</button>
      <button onclick="setRange('month', 5, this)">May</button>
      <button onclick="setRange('month', 6, this)">Jun</button>
      <button onclick="setRange('month', 7, this)">Jul</button>
      <button onclick="setRange('month', 8, this)">Aug</button>
      <button onclick="setRange('month', 9, this)">Sep</button>
      <button onclick="setRange('month', 10, this)">Oct</button>
      <button onclick="setRange('month', 11, this)">Nov</button>
      <button onclick="setRange('month', 12, this)">Dec</button>
    </div>
  </div>
  <div class="chart-wrap"><h2 id="chart-title">Typical Day &mdash; All Time</h2><canvas id="chart" height="120"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\\u26a0 Network error: ' + msg;
}
async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
async function fetchJSONBytes(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    const cl = r.headers.get('content-length');
    const text = await r.text();
    const bytes = cl !== null ? parseInt(cl) : new TextEncoder().encode(text).length;
    return { data: JSON.parse(text), bytes };
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
function labelColor(lbl) { return colorMap[lbl] ?? COLORS[0]; }
const SENSOR_COLORS = { 'outside-sun': '#e07820', 'outside-shade': '#2e7dd4', 'indoor-avg': '#2a9d6e' };
function modeColor(m) { return SENSOR_COLORS[m] ?? colorMap[m] ?? COLORS[0]; }
function hexToRgb(hex) { return [parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)]; }
function applyBtnColor(btn, color, active) {
  const [r,g,b] = hexToRgb(color);
  if (active) {
    btn.style.background = color; btn.style.borderColor = color; btn.style.color = '#fff';
  } else {
    btn.style.background = `rgba(${r},${g},${b},0.06)`;
    btn.style.borderColor = `rgba(${r},${g},${b},0.2)`;
    btn.style.color = '#7a90a8';
  }
}
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname) || /\\.local$/.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = { low: 30, medium: 10, max: 5 };
function getBucket() { return BUCKETS[resolution]; }

const activeModes = new Set(['outside-shade', 'indoor-avg']);
function toggleSensor(key, btn) {
  if (activeModes.has(key)) { activeModes.delete(key); } else { activeModes.add(key); }
  applyBtnColor(btn, modeColor(key), activeModes.has(key));
  loadChart();
}

function isIndoorLabel(l) {
  const lo = l.toLowerCase();
  return lo.startsWith('indoor-') || lo.startsWith('inside-');
}

let currentRange = { type: 'all', value: null };
const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function setRange(type, value, clickedBtn) {
  currentRange = { type, value };
  document.querySelectorAll('#range-btns button, #month-btns button').forEach(b => b.classList.remove('active'));
  clickedBtn.classList.add('active');
  const label = type === 'month' ? MONTH_NAMES[value - 1] : (type === 'days' ? `Last ${value} Days` : 'All Time');
  document.getElementById('chart-title').textContent = 'Typical Day \\u2014 ' + label;
  loadChart();
}

function buildTypicalDatasets(data) {
  const allLabels = [...new Set(data.map(r => r.label).filter(Boolean))];
  const indoorLabels = allLabels.filter(isIndoorLabel);
  const datasets = [];
  function toDate(ts) { return new Date(ts.replace(' ', 'T')); }

  if (activeModes.has('outside-sun')) {
    const lbl = allLabels.find(l => l.toLowerCase().replace(/[_\\s]/g,'-') === 'outside-sun');
    if (lbl) {
      const pts = data.filter(r => r.label === lbl).map(r => ({ x: toDate(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
      datasets.push({ label: 'Outside (sun)', data: pts, borderColor: '#e07820',
        backgroundColor: 'transparent', borderWidth: 2, pointRadius: 0, tension: 0 });
    }
  }

  if (activeModes.has('outside-shade')) {
    const lbl = allLabels.find(l => l.toLowerCase().replace(/[_\\s]/g,'-') === 'outside-shade');
    if (lbl) {
      const pts = data.filter(r => r.label === lbl).map(r => ({ x: toDate(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
      datasets.push({ label: 'Outside (shade)', data: pts, borderColor: '#2e7dd4',
        backgroundColor: 'transparent', borderWidth: 2, pointRadius: 0, tension: 0 });
    }
  }

  if (activeModes.has('indoor-avg') && indoorLabels.length > 0) {
    const tsMap = {};
    for (const row of data) {
      if (!indoorLabels.includes(row.label) || row.temp_f == null) continue;
      (tsMap[row.ts] ??= []).push(row.temp_f);
    }
    const pts = Object.entries(tsMap)
      .map(([ts, vals]) => ({ x: toDate(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length }))
      .sort((a,b) => a.x - b.x);
    datasets.push({ label: 'Indoor average', data: pts, borderColor: '#2a9d6e',
      backgroundColor: 'transparent', borderWidth: 2, pointRadius: 0, tension: 0 });
  }

  indoorLabels.sort().forEach(lbl => {
    if (!activeModes.has(lbl)) return;
    const color = labelColor(lbl);
    const pts = data.filter(r => r.label === lbl).map(r => ({ x: toDate(r.ts), y: r.temp_f })).sort((a,b)=>a.x-b.x);
    datasets.push({ label: lbl, data: pts, borderColor: color,
      backgroundColor: 'transparent', borderWidth: 1.5, pointRadius: 0, tension: 0 });
  });

  return datasets;
}

Chart.Interaction.modes.nearestXPerDataset = function(chart, e, options, useFinalPosition) {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  const items = [];
  chart.data.datasets.forEach((_, datasetIndex) => {
    if (!chart.isDatasetVisible(datasetIndex)) return;
    const meta = chart.getDatasetMeta(datasetIndex);
    let nearest = null, nearestDist = Infinity;
    meta.data.forEach((element, index) => {
      const { x } = element.getProps(['x'], useFinalPosition);
      const dist = Math.abs(x - pos.x);
      if (dist < nearestDist) { nearestDist = dist; nearest = { element, datasetIndex, index }; }
    });
    if (nearest) items.push(nearest);
  });
  return items;
};

const chart = new Chart(document.getElementById("chart"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "nearestXPerDataset", intersect: false },
    plugins: {
      legend: { labels: { color: "#4a6080" } },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            const raw = item.raw;
            if (!raw || raw.y == null) continue;
            const color = item.dataset.borderColor || '#ccc';
            const lbl = (item.dataset.label || '') + ': ' + raw.y.toFixed(1) + '\\u00b0F';
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';border:1px solid ' + color + ';flex-shrink:0;"></span><span>' + lbl + '</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time",
           time: { unit: "hour", stepSize: 2, tooltipFormat: "h:mm a", displayFormats: { hour: "h a" } },
           ticks: { color: "#7a90a8", maxTicksLimit: 13 },
           grid: { color: "#e8eef4" },
           min: new Date(2000, 0, 1, 0, 0, 0),
           max: new Date(2000, 0, 1, 23, 59, 59) },
      y: { ticks: { color: "#7a90a8", callback: v => (+v).toFixed(1) + "\\u00b0F" }, grid: { color: "#e8eef4" } }
    }
  }
});

function fmtBytes(n) {
  return n >= 1048576 ? (n/1048576).toFixed(1) + ' MB'
       : n >= 1024    ? (n/1024).toFixed(1) + ' KB'
       :                n + ' B';
}

async function loadChart() {
  let url = `/api/history/typical-day?bucket_minutes=${getBucket()}`;
  if (currentRange.type === 'days') url += `&range_type=days&days=${currentRange.value}`;
  else if (currentRange.type === 'month') url += `&range_type=month&month=${currentRange.value}`;
  else url += '&range_type=all';
  const { data, bytes } = await fetchJSONBytes(url);
  chart.data.datasets = buildTypicalDatasets(data);
  document.getElementById('resp-size').textContent = fmtBytes(bytes);
  chart.update();
}

async function loadColors() {
  const data = await fetchJSON("/api/current");
  const reservedColors = new Set(Object.values(SENSOR_COLORS));
  const roomColors = COLORS.filter(c => !reservedColors.has(c));
  data.map(s => s.label).filter(Boolean).sort()
    .forEach((lbl, i) => { colorMap[lbl] = roomColors[i % roomColors.length]; });
  const indoorLabels = data.map(s => s.label).filter(l => l && isIndoorLabel(l)).sort();
  const sensorBtns = document.getElementById('sensor-btns');
  const existingRoomBtns = [...sensorBtns.querySelectorAll('button[data-room]')];
  const existing = existingRoomBtns.map(b => b.dataset.room);
  if (JSON.stringify(existing) !== JSON.stringify(indoorLabels)) {
    existingRoomBtns.forEach(b => b.remove());
    indoorLabels.forEach(lbl => {
      const btn = document.createElement('button');
      btn.dataset.room = lbl;
      btn.textContent = lbl.replace(/^in(door|side)-/i, '').split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
      btn.onclick = () => toggleSensor(lbl, btn);
      if (activeModes.has(lbl)) btn.classList.add('active');
      sensorBtns.appendChild(btn);
      applyBtnColor(btn, modeColor(lbl), activeModes.has(lbl));
    });
  } else {
    existingRoomBtns.forEach(b => applyBtnColor(b, modeColor(b.dataset.room), activeModes.has(b.dataset.room)));
  }
  [['btn-outside-sun','outside-sun'],['btn-outside-shade','outside-shade'],['btn-indoor-avg','indoor-avg']].forEach(([id,m]) => {
    const btn = document.getElementById(id);
    if (btn) applyBtnColor(btn, modeColor(m), activeModes.has(m));
  });
}

document.getElementById('btn-all-time').classList.add('active');
loadColors().then(loadChart);
setInterval(() => loadColors().then(loadChart), 30000);
</script>
</body>
</html>"""


@app.get("/chart/typical-day")
def chart_typical_day():
    return Response(_TYPICAL_DAY_PAGE, mimetype="text/html")


_BANDWIDTH_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bandwidth &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #2e7dd4; color: #fff; border-color: #2e7dd4; }
    .range-btns button:disabled { opacity: 0.3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
    #resp-size { font-size: .72rem; color: #4a6080; }
  </style>
</head>
<body>
  <h1>Bandwidth</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
    <span id="resp-size"></span>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Devices &mdash; solid line = download &nbsp; dashed line = upload</div>
    <div class="range-btns" id="device-btns"></div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(0.125)" data-days="0.125">3h</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>Download &amp; Upload (KB/s)</h2><canvas id="chart" height="120"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\\u26a0 Network error: ' + msg;
}
async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) { showNetworkError(e.message); throw e; }
}
async function fetchJSONBytes(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    const cl = r.headers.get('content-length');
    const text = await r.text();
    const bytes = cl !== null ? parseInt(cl) : new TextEncoder().encode(text).length;
    return { data: JSON.parse(text), bytes };
  } catch(e) { showNetworkError(e.message); throw e; }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
const hiddenMacs = new Set();
let colorIdx = 0;
let mode = "recent", rangeDays = 1, activeMonth = null, offsetMs = 0;
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname) || /\\.local$/.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = {
  recent: {
    low:    {0.125:10, 1:30,  3:60,  7:120, 30:360},
    medium: {0.125:3,  1:10,  3:20,  7:30,  30:60 },
    max:    {0.125:1,  1:2,   3:5,   7:10,  30:20 },
  },
  month: { low: 240, medium: 60, max: 10 },
  year:  { low: 1440, medium: 360, max: 60 },
};
function getBucket() {
  if (mode === "recent") return BUCKETS.recent[resolution][rangeDays] || 60;
  return (BUCKETS[mode] || BUCKETS.month)[resolution];
}
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtKbps(v) {
  return v >= 1024 ? (v/1024).toFixed(2)+' MB/s' : v.toFixed(1)+' KB/s';
}
function fmtBytes(n) {
  return n >= 1048576 ? (n/1048576).toFixed(1)+' MB' : n >= 1024 ? (n/1024).toFixed(1)+' KB' : n+' B';
}
function hexToRgb(hex) { return [parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)]; }
function applyBtnColor(btn, color, active) {
  const [r,g,b] = hexToRgb(color);
  if (active) {
    btn.style.background = color; btn.style.borderColor = color; btn.style.color = '#fff';
  } else {
    btn.style.background = `rgba(${r},${g},${b},0.06)`;
    btn.style.borderColor = `rgba(${r},${g},${b},0.2)`;
    btn.style.color = '#7a90a8';
  }
}
function toggleDevice(mac, btn) {
  if (hiddenMacs.has(mac)) hiddenMacs.delete(mac); else hiddenMacs.add(mac);
  applyBtnColor(btn, colorMap[mac] || COLORS[0], !hiddenMacs.has(mac));
  chart.data.datasets.forEach((ds, i) => chart.setDatasetVisibility(i, !hiddenMacs.has(ds.mac)));
  chart.update();
}
function updateDeviceButtons(devices) {
  const container = document.getElementById('device-btns');
  for (const dev of devices) {
    const btnId = 'dev-btn-' + dev.mac.replace(/:/g,'');
    let btn = document.getElementById(btnId);
    if (!btn) {
      btn = document.createElement('button');
      btn.id = btnId;
      btn.onclick = () => toggleDevice(dev.mac, btn);
      container.appendChild(btn);
    }
    btn.textContent = dev.hostname || dev.mac;
    applyBtnColor(btn, colorMap[dev.mac] || COLORS[0], !hiddenMacs.has(dev.mac));
  }
}
function buildDatasets(data) {
  const byMac = {};
  for (const row of data) {
    if (!byMac[row.mac]) byMac[row.mac] = {hostname: row.hostname, down: [], up: []};
    const t = new Date(row.ts.replace(' ', 'T'));
    byMac[row.mac].down.push({x: t, y: row.down_kbps});
    byMac[row.mac].up.push({x: t, y: row.up_kbps});
  }
  const datasets = [];
  for (const [mac, grp] of Object.entries(byMac)) {
    const color = colorMap[mac] || COLORS[0];
    const name = grp.hostname || mac;
    grp.down.sort((a,b) => a.x - b.x);
    grp.up.sort((a,b) => a.x - b.x);
    datasets.push({label: name+' \\u2193', mac, data: grp.down, borderColor: color,
      backgroundColor: 'transparent', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: []});
    datasets.push({label: name+' \\u2191', mac, data: grp.up, borderColor: color,
      backgroundColor: 'transparent', borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: [4,3]});
  }
  return datasets;
}
const chart = new Chart(document.getElementById("chart"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label(item) {
            if (item.raw == null || item.raw.y == null || item.raw.y === 0) return null;
            return item.dataset.label + ': ' + fmtKbps(item.raw.y);
          }
        }
      }
    },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 8 }, grid: { color: "#e8eef4" } },
      y: { min: 0, ticks: { color: "#7a90a8", callback: v => v >= 1024 ? (v/1024).toFixed(1)+' MB/s' : v+' KB/s' }, grid: { color: "#e8eef4" } }
    }
  }
});
function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  loadChart();
}
function setRange(days) {
  mode = "recent"; rangeDays = days; offsetMs = 0;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  document.getElementById('btn-prev').disabled = false;
  document.getElementById('btn-next').disabled = offsetMs >= 0;
  loadChart();
}
function setAllMonths() {
  mode = "year";
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i===0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
function setMonth(m) {
  mode = "month"; activeMonth = m;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i===m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
async function loadDevices() {
  const devices = await fetchJSON('/api/bandwidth/devices');
  for (const dev of devices) {
    if (!(dev.mac in colorMap)) colorMap[dev.mac] = COLORS[colorIdx++ % COLORS.length];
  }
  updateDeviceButtons(devices);
}
async function loadChart() {
  let url, xMin, xMax, timeUnit, totalBytes;
  if (mode === "recent") {
    const xEnd = new Date(Date.now() + offsetMs);
    const xStart = new Date(xEnd - rangeDays * 86400000);
    xMin = xStart; xMax = xEnd;
    const params = `start=${localISO(xStart)}&end=${localISO(xEnd)}&bucket_minutes=${getBucket()}&limit=8000`;
    const { data, bytes } = await fetchJSONBytes(`/api/bandwidth/history?${params}`);
    totalBytes = bytes;
    chart.data.datasets = buildDatasets(data);
    timeUnit = rangeDays <= 0.125 ? "minute" : rangeDays <= 1 ? "hour" : "day";
    const peek = await fetchJSON(`/api/bandwidth/history?end=${localISO(xStart)}&limit=1&bucket_minutes=${getBucket()}`);
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    const { data, bytes } = await fetchJSONBytes(`/api/bandwidth/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
    totalBytes = bytes;
    chart.data.datasets = buildDatasets(data);
    xMin = new Date(2000, activeMonth - 1, 1);
    xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    timeUnit = "day";
  } else {
    const { data, bytes } = await fetchJSONBytes(`/api/bandwidth/history/year?bucket_minutes=${getBucket()}`);
    totalBytes = bytes;
    chart.data.datasets = buildDatasets(data);
    xMin = new Date(2000, 0, 1);
    xMax = new Date(2000, 11, 31, 23, 59, 59);
    timeUnit = "month";
  }
  chart.data.datasets.forEach((ds, i) => chart.setDatasetVisibility(i, !hiddenMacs.has(ds.mac)));
  chart.options.scales.x.min = xMin;
  chart.options.scales.x.max = xMax;
  chart.options.scales.x.time.unit = timeUnit;
  chart.update();
  document.getElementById('resp-size').textContent = fmtBytes(totalBytes);
}
async function refresh() { await loadDevices(); await loadChart(); }
refresh();
setInterval(() => { if (mode === "recent") refresh(); }, 30000);
</script>
</body>
</html>"""


@app.get("/chart/bandwidth")
def chart_bandwidth():
    return Response(_BANDWIDTH_PAGE, mimetype="text/html")


@app.get("/chart/humidity")
def chart_humidity():
    return _chart_page(
        "Humidity",
        '<div class="chart-wrap"><h2>Humidity (%)</h2><canvas id="chart" height="120"></canvas></div>',
        """
const chart = new Chart(document.getElementById("chart"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: "#4a6080" } } },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 8 }, grid: { color: "#e8eef4" } },
      y: { ticks: { color: "#7a90a8", callback: v => v + "%" }, grid: { color: "#e8eef4" } }
    }
  }
});
async function loadChart() {
""" + _HISTORY_FETCH + """
  const byLabel = {};
  for (const row of data) (byLabel[row.label] ??= []).push({ x: new Date(row.ts), y: row.humidity });
  for (const pts of Object.values(byLabel)) pts.sort((a,b) => a.x - b.x);
  chart.data.datasets = Object.keys(byLabel).sort().map(lbl => ({
    label: lbl, data: byLabel[lbl], borderColor: labelColor(lbl),
    backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, tension: 0,
  }));
""" + _AXIS_UPDATE + """
}""",
    )


_ENERGY_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Energy Usage &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .range-btns button:disabled { opacity: 0.3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
    .cost-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; flex-wrap: wrap; }
    .cost-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .cost-row input[type=number] { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; width: 110px; }
    .cost-row button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; font-size: .85rem; font-weight: 500; cursor: pointer; transition: all .15s; }
    .cost-row button:disabled { opacity: 0.4; cursor: default; pointer-events: none; }
    .cost-row button.active { background: #2e7dd4; color: #fff; border-color: #2e7dd4; }
    .stats-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); overflow-x: auto; }
    .stats-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .stats-table { border-collapse: collapse; width: 100%; font-size: .85rem; }
    .stats-table th { color: #7a90a8; font-weight: 600; font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; border-bottom: 2px solid #e8eef4; padding: .4rem .7rem; text-align: left; white-space: nowrap; }
    .stats-table td { padding: .45rem .7rem; border-bottom: 1px solid #f0f4f8; color: #1a2535; white-space: nowrap; }
    .stats-table tr:last-child td { border-bottom: none; }
    .stats-table td.device-cell { font-weight: 600; }
    .stats-table td.on-cell { color: #2a9d6e; }
    .stats-table td.off-cell { color: #7a90a8; }
  </style>
</head>
<body>
  <h1>Energy Usage</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Devices</div>
    <div class="range-btns" id="device-btns"></div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(0.125)" data-days="0.125">3h</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>Power (W)</h2><canvas id="chart-watts" height="120"></canvas></div>
  <div id="stats-wrap" class="stats-wrap" style="display:none;">
    <h2>Interval Summary</h2>
    <table class="stats-table" id="stats-table">
      <thead><tr id="stats-thead"></tr></thead>
      <tbody id="stats-tbody"></tbody>
    </table>
  </div>
  <div class="cost-row">
    <label for="cost-rate">Electricity cost ($/kWh)</label>
    <input type="number" id="cost-rate" min="0" step="0.001" placeholder="e.g. 0.12" oninput="onCostChange()">
    <button id="cost-toggle" onclick="toggleCostMode()" disabled>Show $</button>
  </div>
  <div class="chart-wrap"><h2 id="daily-chart-title">Daily Energy (kWh) &mdash; device accumulator</h2><canvas id="chart-daily" height="80"></canvas></div>
  <div class="chart-wrap"><h2>Daily On-Time (hours)</h2><canvas id="chart-ontime" height="80"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
const hiddenDevices = new Set();
let mode = "recent", rangeDays = 1, activeMonth = null, offsetMs = 0;
let showCost = false, lastDailyRaw = [], lastStatsRaw = [], dailyXMin = null, dailyXMax = null, dailyXUnit = "day";
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname) || /\\.local$/.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = {
  recent: {
    low:    {0.125:10, 1:30,  3:60,  7:120, 30:360},
    medium: {0.125:3,  1:10,  3:20,  7:30,  30:60 },
    max:    {0.125:1,  1:2,   3:5,   7:10,  30:20 },
  },
  month:  { low: 240, medium: 60, max: 10 },
  year:   { low: 1440, medium: 360, max: 60 },
};
function getBucket() {
  if (mode === "recent") return BUCKETS.recent[resolution][rangeDays] || 60;
  return BUCKETS[mode][resolution];
}
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function hexToRgb(hex) { return [parseInt(hex.slice(1,3),16), parseInt(hex.slice(3,5),16), parseInt(hex.slice(5,7),16)]; }
function applyBtnColor(btn, color, active) {
  const [r,g,b] = hexToRgb(color);
  if (active) {
    btn.style.background = color; btn.style.borderColor = color; btn.style.color = '#fff';
  } else {
    btn.style.background = `rgba(${r},${g},${b},0.06)`;
    btn.style.borderColor = `rgba(${r},${g},${b},0.2)`;
    btn.style.color = '#7a90a8';
  }
}
function toggleDevice(device, btn) {
  if (hiddenDevices.has(device)) { hiddenDevices.delete(device); }
  else { hiddenDevices.add(device); }
  const active = !hiddenDevices.has(device);
  applyBtnColor(btn, colorMap[device] || COLORS[0], active);
  for (const ch of [wattsChart, dailyChart, onTimeChart]) {
    ch.data.datasets.forEach((ds, i) => ch.setDatasetVisibility(i, !hiddenDevices.has(ds.device)));
    ch.update();
  }
}
function updateDeviceButtons(devices) {
  const container = document.getElementById('device-btns');
  for (const dev of devices) {
    let btn = document.getElementById('dev-btn-' + dev);
    if (!btn) {
      btn = document.createElement('button');
      btn.id = 'dev-btn-' + dev;
      btn.textContent = dev;
      btn.onclick = () => toggleDevice(dev, btn);
      container.appendChild(btn);
    }
    applyBtnColor(btn, colorMap[dev] || COLORS[0], !hiddenDevices.has(dev));
  }
}
function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  loadChart();
}
function setRange(days) {
  mode = "recent"; rangeDays = days; offsetMs = 0;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  loadChart();
}
function setAllMonths() {
  mode = "year";
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i === 0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
function setMonth(m) {
  mode = "month"; activeMonth = m;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i === m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
const wattsChart = new Chart(document.getElementById("chart-watts"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            if (item.raw == null || item.raw.y == null) continue;
            const color = item.dataset.borderColor || '#ccc';
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';border:1px solid ' + color + ';flex-shrink:0;"></span><span>' + (item.dataset.label || '') + ': ' + item.raw.y.toFixed(1) + ' W</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 8 }, grid: { color: "#e8eef4" } },
      y: { min: 0, ticks: { color: "#7a90a8", callback: v => v + " W" }, grid: { color: "#e8eef4" } }
    }
  }
});
const dailyChart = new Chart(document.getElementById("chart-daily"), {
  type: "bar", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt-daily');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt-daily';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            if (item.raw == null || item.raw.y == null) continue;
            const color = item.dataset.backgroundColor || '#ccc';
            const rate = parseFloat(document.getElementById('cost-rate').value) || 0;
            const useCost = showCost && rate > 0;
            const valStr = useCost ? '$' + item.raw.y.toFixed(2) : item.raw.y.toFixed(3) + ' kWh';
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';flex-shrink:0;"></span><span>' + (item.dataset.label || '') + ': ' + valStr + '</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time", time: { unit: "day", tooltipFormat: "MMM d, yyyy" }, ticks: { color: "#7a90a8", maxTicksLimit: 14 }, grid: { color: "#e8eef4" } },
      y: { min: 0, ticks: { color: "#7a90a8", callback: v => v + " kWh" }, grid: { color: "#e8eef4" } }
    }
  }
});
const onTimeChart = new Chart(document.getElementById("chart-ontime"), {
  type: "bar", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt-ontime');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt-ontime';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            if (item.raw == null || item.raw.y == null) continue;
            const color = item.dataset.backgroundColor || '#ccc';
            const h = Math.floor(item.raw.y);
            const m = Math.round((item.raw.y - h) * 60);
            const valStr = h > 0 ? `${h}h ${m}m` : `${m}m`;
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';flex-shrink:0;"></span><span>' + (item.dataset.label || '') + ': ' + valStr + '</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time", time: { unit: "day", tooltipFormat: "MMM d, yyyy" }, ticks: { color: "#7a90a8", maxTicksLimit: 14 }, grid: { color: "#e8eef4" } },
      y: { min: 0, max: 24, ticks: { color: "#7a90a8", callback: v => v + "h" }, grid: { color: "#e8eef4" } }
    }
  }
});
function buildDatasets(data) {
  const byKey = {};
  const yearMode = mode !== "recent";
  for (const row of data) {
    if (row.label == null) continue;
    const key = yearMode && row.year ? row.label + " (" + row.year + ")" : row.label;
    if (!byKey[key]) byKey[key] = { device: row.label, points: [] };
    byKey[key].points.push({ x: new Date(row.ts), y: row.watts });
  }
  const keys = Object.keys(byKey).sort();
  const devices = [...new Set(keys.map(k => byKey[k].device))].sort();
  devices.forEach((dev, i) => { colorMap[dev] = COLORS[i % COLORS.length]; });
  updateDeviceButtons(devices);
  return keys.map(k => ({
    label: k, device: byKey[k].device, data: byKey[k].points,
    borderColor: colorMap[byKey[k].device], backgroundColor: "transparent",
    borderWidth: 1.5, pointRadius: 0, tension: 0,
  }));
}
function buildDailyDatasets(daily, costRate) {
  const byDevice = {};
  for (const row of daily) {
    if (row.label == null) continue;
    const y = costRate ? row.kwh * costRate : row.kwh;
    (byDevice[row.label] ??= []).push({ x: new Date(row.date), y });
  }
  return Object.keys(byDevice).sort().map(dev => ({
    label: dev, device: dev, data: byDevice[dev],
    backgroundColor: colorMap[dev] || COLORS[0],
    borderWidth: 0,
  }));
}
function onCostChange() {
  const val = parseFloat(document.getElementById('cost-rate').value);
  const btn = document.getElementById('cost-toggle');
  const valid = val > 0;
  btn.disabled = !valid;
  if (!valid && showCost) { showCost = false; btn.textContent = 'Show $'; btn.classList.remove('active'); }
  if (lastDailyRaw.length) renderDailyChart();
  renderStatsTable();
}
function toggleCostMode() {
  showCost = !showCost;
  const btn = document.getElementById('cost-toggle');
  btn.textContent = showCost ? 'Show kWh' : 'Show $';
  btn.classList.toggle('active', showCost);
  renderDailyChart();
  renderStatsTable();
}
function renderDailyChart() {
  const rate = parseFloat(document.getElementById('cost-rate').value) || 0;
  const useCost = showCost && rate > 0;
  document.getElementById('daily-chart-title').textContent =
    (useCost ? 'Daily Cost ($)' : 'Daily Energy (kWh)') + ' — device accumulator';
  dailyChart.data.datasets = buildDailyDatasets(lastDailyRaw, useCost ? rate : null);
  dailyChart.data.datasets.forEach((ds, i) =>
    dailyChart.setDatasetVisibility(i, !hiddenDevices.has(ds.device)));
  dailyChart.options.scales.x.min = dailyXMin;
  dailyChart.options.scales.x.max = dailyXMax;
  dailyChart.options.scales.x.time.unit = dailyXUnit;
  dailyChart.options.scales.y.ticks.callback = useCost ? v => '$' + v.toFixed(2) : v => v + ' kWh';
  dailyChart.update();
}
let lastOnTimeRaw = [];
function buildOnTimeDatasets(ontime) {
  const byDevice = {};
  for (const row of ontime) {
    if (row.label == null) continue;
    (byDevice[row.label] ??= []).push({ x: new Date(row.date), y: row.on_hours });
  }
  return Object.keys(byDevice).sort().map(dev => ({
    label: dev, device: dev, data: byDevice[dev],
    backgroundColor: colorMap[dev] || COLORS[0],
    borderWidth: 0,
  }));
}
function renderOnTimeChart(xMin, xMax, xUnit) {
  onTimeChart.data.datasets = buildOnTimeDatasets(lastOnTimeRaw);
  onTimeChart.data.datasets.forEach((ds, i) =>
    onTimeChart.setDatasetVisibility(i, !hiddenDevices.has(ds.device)));
  onTimeChart.options.scales.x.min = xMin;
  onTimeChart.options.scales.x.max = xMax;
  onTimeChart.options.scales.x.time.unit = xUnit || "day";
  onTimeChart.update();
}
function fmtHours(h) {
  if (h == null) return '—';
  const hrs = Math.floor(h), mins = Math.round((h - hrs) * 60);
  return hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;
}
function renderStatsTable() {
  const wrap = document.getElementById('stats-wrap');
  if (!lastStatsRaw.length) { wrap.style.display = 'none'; return; }
  const rate = parseFloat(document.getElementById('cost-rate').value) || 0;
  const showRate = rate > 0;
  const thead = document.getElementById('stats-thead');
  const tbody = document.getElementById('stats-tbody');
  const costCols = showRate ? '<th>Cost/hr (on)</th><th>Cost/hr (off)</th>' : '';
  thead.innerHTML = `<th>Device</th><th>On Time</th><th>Off Time</th><th>Avg W (on)</th><th>Avg W (off)</th>${costCols}`;
  tbody.innerHTML = '';
  for (const row of lastStatsRaw) {
    const costOnStr  = showRate && row.avg_watts_on  != null ? '$' + (row.avg_watts_on  / 1000 * rate).toFixed(4) : '';
    const costOffStr = showRate && row.avg_watts_off != null ? '$' + (row.avg_watts_off / 1000 * rate).toFixed(4) : '';
    const costCells = showRate ? `<td class="on-cell">${costOnStr || '—'}</td><td class="off-cell">${costOffStr || '—'}</td>` : '';
    const dot = colorMap[row.label] ? `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${colorMap[row.label]};margin-right:5px;vertical-align:middle;"></span>` : '';
    tbody.innerHTML += `<tr>
      <td class="device-cell">${dot}${row.label}</td>
      <td class="on-cell">${fmtHours(row.on_hours)}</td>
      <td class="off-cell">${fmtHours(row.off_hours)}</td>
      <td class="on-cell">${row.avg_watts_on != null ? row.avg_watts_on.toFixed(1) + ' W' : '—'}</td>
      <td class="off-cell">${row.avg_watts_off != null ? row.avg_watts_off.toFixed(1) + ' W' : '—'}</td>
      ${costCells}
    </tr>`;
  }
  wrap.style.display = '';
}
async function loadChart() {
  let data, xMin, xMax, timeUnit, statsParams;
  if (mode === "recent") {
    xMax = new Date(Date.now() + offsetMs);
    xMin = new Date(xMax - rangeDays * 86400000);
    const params = `start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`;
    [data] = await Promise.all([
      fetchJSON(`/api/plug_history?${params}`),
    ]);
    timeUnit = rangeDays >= 3 ? "day" : "hour";
    const peek = await fetchJSON(`/api/plug_history?end=${localISO(xMin)}&limit=1`);
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
    statsParams = `start=${localISO(xMin)}&end=${localISO(xMax)}`;
  } else if (mode === "month") {
    data = await fetchJSON(`/api/plug_history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
    xMin = new Date(2000, activeMonth - 1, 1);
    xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    timeUnit = "day";
    statsParams = `month=${activeMonth}`;
  } else {
    data = await fetchJSON(`/api/plug_history/year?bucket_minutes=${getBucket()}`);
    xMin = new Date(2000, 0, 1);
    xMax = new Date(2000, 11, 31, 23, 59, 59);
    timeUnit = "month";
    statsParams = '';
  }
  wattsChart.data.datasets = buildDatasets(data);
  wattsChart.data.datasets.forEach((ds, i) =>
    wattsChart.setDatasetVisibility(i, !hiddenDevices.has(ds.device)));
  wattsChart.options.scales.x.min = xMin;
  wattsChart.options.scales.x.max = xMax;
  wattsChart.options.scales.x.time.unit = timeUnit;
  wattsChart.update();

  const dailyStart = mode === "recent" ? localISO(new Date(Date.now() + offsetMs - Math.max(rangeDays, 7) * 86400000)) : null;
  const dailyEnd   = mode === "recent" ? localISO(new Date(Date.now() + offsetMs)) : null;
  const dailyParams = [dailyStart && `start=${dailyStart}`, dailyEnd && `end=${dailyEnd}`].filter(Boolean).join("&");
  const [daily, ontime, stats] = await Promise.all([
    fetchJSON(`/api/plug_daily${dailyParams ? "?" + dailyParams : ""}`),
    fetchJSON(`/api/plug_cumulative_on${dailyParams ? "?" + dailyParams : ""}`),
    fetchJSON(`/api/plug_on_off_stats${statsParams ? "?" + statsParams : ""}`),
  ]);
  lastDailyRaw = daily;
  lastOnTimeRaw = ontime;
  lastStatsRaw = stats;
  dailyXMin = mode === "recent" ? new Date(Date.now() + offsetMs - Math.max(rangeDays, 7) * 86400000) : xMin;
  dailyXMax = mode === "recent" ? new Date(Date.now() + offsetMs) : xMax;
  dailyXUnit = mode === "year" ? "month" : "day";
  renderDailyChart();
  renderOnTimeChart(dailyXMin, dailyXMax, dailyXUnit);
  renderStatsTable();
}
fetchJSON('/api/energy-cost').then(d => {
  if (d.rate != null) {
    document.getElementById('cost-rate').value = d.rate;
    onCostChange();
  }
}).catch(() => {});
loadChart();
setInterval(loadChart, 30000);
</script>
</body>
</html>"""


@app.get("/api/energy-cost")
def get_energy_cost():
    from smart_home import smart_plug as _sp
    rate = _sp.load_energy_cost()
    return jsonify({"rate": rate})


@app.get("/chart/energy")
def chart_energy():
    return Response(_ENERGY_PAGE, mimetype="text/html")


@app.get("/chart/differential")
def chart_differential():
    return Response(_DIFF_PAGE, mimetype="text/html")


_SENSORS_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sensor Battery Life &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .range-btns button:disabled { opacity: 0.3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Sensor Battery Life</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Battery</div>
    <div class="range-btns" id="battery-btns"></div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(0.125)" data-days="0.125">3h</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>Battery (%)</h2><canvas id="chart-battery" height="80"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
let mode = "recent", rangeDays = 1, activeMonth = null, offsetMs = 0;
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = {
  recent: {
    low:    {0.125:10, 1:30,  3:60,  7:120, 30:360},
    medium: {0.125:3,  1:10,  3:20,  7:30,  30:60 },
    max:    {0.125:1,  1:2,   3:5,   7:10,  30:20 },
  },
  month:  { low: 240, medium: 60, max: 10 },
  year:   { low: 1440, medium: 360, max: 60 },
};
function getBucket() {
  if (mode === "recent") return BUCKETS.recent[resolution][rangeDays] || 60;
  return BUCKETS[mode][resolution];
}

const activeBattery = new Set();

function toggleBattery(lbl, btn) {
  if (activeBattery.has(lbl)) { activeBattery.delete(lbl); btn.classList.remove('active'); }
  else { activeBattery.add(lbl); btn.classList.add('active'); }
  loadChart();
}

function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  loadChart();
}
function setRange(days) {
  mode = "recent"; rangeDays = days; offsetMs = 0;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  loadChart();
}
function setAllMonths() {
  mode = "year";
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i === 0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
function setMonth(m) {
  mode = "month"; activeMonth = m;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i === m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}

function makeTooltip(valueFormatter) {
  return {
    enabled: false,
    external: function({ chart, tooltip }) {
      const id = 'tt-' + chart.canvas.id;
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement('div');
        el.id = id;
        el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
        chart.canvas.parentNode.style.position = 'relative';
        chart.canvas.parentNode.appendChild(el);
      }
      if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
      const title = (tooltip.title || [])[0] || '';
      let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
      for (const item of (tooltip.dataPoints || [])) {
        if (item.raw == null || item.raw.y == null) continue;
        const color = item.dataset.borderColor || '#ccc';
        html += '<div style="display:flex;align-items:center;gap:5px;">' +
          '<span style="display:inline-block;width:10px;height:10px;background:' + color + ';border:1px solid ' + color + ';flex-shrink:0;"></span>' +
          '<span>' + (item.dataset.label || '') + ': ' + valueFormatter(item.raw.y) + '</span></div>';
      }
      el.innerHTML = html;
      el.style.display = 'block';
      const pw = chart.canvas.parentNode.offsetWidth;
      const tw = el.offsetWidth || 160;
      el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
      el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
    }
  };
}

Chart.Interaction.modes.nearestXPerDataset = function(chart, e, options, useFinalPosition) {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  const items = [];
  chart.data.datasets.forEach((_, di) => {
    if (!chart.isDatasetVisible(di)) return;
    const meta = chart.getDatasetMeta(di);
    let nearest = null, nearestDist = Infinity;
    meta.data.forEach((el, idx) => {
      const dist = Math.abs(el.getProps(['x'], useFinalPosition).x - pos.x);
      if (dist < nearestDist) { nearestDist = dist; nearest = { element: el, datasetIndex: di, index: idx }; }
    });
    if (nearest) items.push(nearest);
  });
  return items;
};

const batteryChart = new Chart(document.getElementById("chart-battery"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "nearestXPerDataset", intersect: false },
    plugins: { legend: { labels: { color: "#4a6080" } }, tooltip: makeTooltip(v => (+v).toFixed(0) + "%") },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 25 }, grid: { color: "#e8eef4" } },
      y: { min: 0, max: 100, ticks: { color: "#7a90a8", callback: v => v + "%" }, grid: { color: "#e8eef4" } }
    }
  }
});


function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function loadSensors() {
  const data = await fetchJSON("/api/current");
  const labels = data.map(s => s.label).filter(Boolean).sort();
  labels.forEach((lbl, i) => { colorMap[lbl] = COLORS[i % COLORS.length]; });

  const btnsEl = document.getElementById('battery-btns');
  const existing = [...btnsEl.querySelectorAll('button')].map(b => b.dataset.label);
  if (JSON.stringify(existing) !== JSON.stringify(labels)) {
    btnsEl.innerHTML = '';
    labels.forEach(lbl => {
      activeBattery.add(lbl);
      const btn = document.createElement('button');
      btn.dataset.label = lbl;
      btn.textContent = lbl;
      btn.className = 'active';
      btn.onclick = () => toggleBattery(lbl, btn);
      btnsEl.appendChild(btn);
    });
  }
}

async function loadChart() {
  let data;
  if (mode === "recent") {
    const xMax = new Date(Date.now() + offsetMs);
    const xMin = new Date(xMax - rangeDays * 86400000);
    const params = `start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`;
    data = await fetchJSON(`/api/history?${params}`);
    batteryChart.options.scales.x.min = xMin;
    batteryChart.options.scales.x.max = xMax;
    batteryChart.options.scales.x.time.unit = rangeDays === 0.125 ? "minute" : rangeDays === 1 ? "hour" : "day";
    batteryChart.options.scales.x.ticks.stepSize = rangeDays === 0.125 ? 30 : 1;
    const peek = await fetchJSON(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`);
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    data = await fetchJSON(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
    const xMin = new Date(2000, activeMonth - 1, 1), xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    batteryChart.options.scales.x.min = xMin;
    batteryChart.options.scales.x.max = xMax;
    batteryChart.options.scales.x.time.unit = "day";
  } else {
    data = await fetchJSON(`/api/history/year?bucket_minutes=${getBucket()}`);
    batteryChart.options.scales.x.min = new Date(2000, 0, 1);
    batteryChart.options.scales.x.max = new Date(2000, 11, 31, 23, 59, 59);
    batteryChart.options.scales.x.time.unit = "month";
  }

  const allLabels = [...new Set(data.map(r => r.label).filter(Boolean))].sort();
  const isMonth = mode !== "recent";

  // Battery datasets
  batteryChart.data.datasets = allLabels
    .filter(lbl => activeBattery.has(lbl))
    .map(lbl => {
      const color = colorMap[lbl] || COLORS[0];
      const pts = data
        .filter(r => r.label === lbl)
        .map(r => ({ x: new Date(r.ts), y: r.battery ?? null }))
        .sort((a, b) => a.x - b.x);
      return { label: lbl, data: pts, borderColor: color, backgroundColor: 'transparent', borderWidth: 1.5, pointRadius: 0, tension: 0 };
    });
  batteryChart.update();

}

loadSensors().then(loadChart);
setInterval(() => loadSensors().then(loadChart), 30000);
</script>
</body>
</html>"""


@app.get("/chart/sensors")
def chart_sensors():
    return Response(_SENSORS_PAGE, mimetype="text/html")


_SIGNAL_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signal Strength &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .range-btns button:disabled { opacity: 0.3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Signal Strength</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; loadChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
  </div>
  <div class="btn-group">
    <div class="range-btns" id="sensor-btns"></div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(0.125)" data-days="0.125">3h</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>
  <div class="btn-group" style="margin-top:8px">
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:0.9em;color:#4a6080">
      <input type="checkbox" id="chk-mavg" onchange="loadChart()">
      3 hour moving average
    </label>
  </div>
  <div class="chart-wrap"><h2>RSSI (dBm) <small style="font-size:0.55em;color:#7a90a8;font-weight:normal">higher is better</small></h2><canvas id="chart" height="120"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad","#27ae60","#2980b9","#e74c3c","#f39c12"];
const colorMap = {};
let mode = "recent", rangeDays = 1, activeMonth = null, offsetMs = 0;
const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal = /^192\\.168\\./.test(location.hostname);
let resolution = isLocal ? "max" : isMobile ? "low" : "medium";
document.getElementById("res").value = resolution;
const BUCKETS = {
  recent: {
    low:    {0.125:10, 1:30,  3:60,  7:120, 30:360},
    medium: {0.125:3,  1:10,  3:20,  7:30,  30:60 },
    max:    {0.125:1,  1:2,   3:5,   7:10,  30:20 },
  },
  month:  { low: 240, medium: 60, max: 10 },
  year:   { low: 1440, medium: 360, max: 60 },
};
function getBucket() {
  if (mode === "recent") return BUCKETS.recent[resolution][rangeDays] || 60;
  return BUCKETS[mode][resolution];
}
const activeLabels = new Set();
function toggleLabel(lbl, btn) {
  if (activeLabels.has(lbl)) { activeLabels.delete(lbl); btn.classList.remove('active'); }
  else { activeLabels.add(lbl); btn.classList.add('active'); }
  loadChart();
}
function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  loadChart();
}
function setRange(days) {
  mode = "recent"; rangeDays = days; offsetMs = 0;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  document.querySelectorAll("#month-btns button").forEach(b => b.classList.remove("active"));
  loadChart();
}
function setAllMonths() {
  mode = "year";
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i === 0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
function setMonth(m) {
  mode = "month"; activeMonth = m;
  document.querySelectorAll("#recent-btns button[data-days]").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#month-btns button").forEach((b,i) => b.classList.toggle("active", i === m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  loadChart();
}
Chart.Interaction.modes.nearestXPerDataset = function(chart, e, options, useFinalPosition) {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  const items = [];
  chart.data.datasets.forEach((_, di) => {
    if (!chart.isDatasetVisible(di)) return;
    const meta = chart.getDatasetMeta(di);
    let nearest = null, nearestDist = Infinity;
    meta.data.forEach((el, idx) => {
      const dist = Math.abs(el.getProps(['x'], useFinalPosition).x - pos.x);
      if (dist < nearestDist) { nearestDist = dist; nearest = { element: el, datasetIndex: di, index: idx }; }
    });
    if (nearest) items.push(nearest);
  });
  return items;
};
const chart = new Chart(document.getElementById("chart"), {
  type: "line", data: { datasets: [] },
  options: {
    animation: false, parsing: false,
    interaction: { mode: "nearestXPerDataset", intersect: false },
    plugins: {
      legend: { labels: { color: "#4a6080" } },
      tooltip: {
        enabled: false,
        external: function({ chart, tooltip }) {
          let el = document.getElementById('chartjs-tt');
          if (!el) {
            el = document.createElement('div');
            el.id = 'chartjs-tt';
            el.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,.75);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;font-family:system-ui,sans-serif;white-space:nowrap;z-index:10;';
            chart.canvas.parentNode.style.position = 'relative';
            chart.canvas.parentNode.appendChild(el);
          }
          if (tooltip.opacity === 0) { el.style.display = 'none'; return; }
          const title = (tooltip.title || [])[0] || '';
          let html = title ? '<div style="font-weight:600;margin-bottom:3px;">' + title + '</div>' : '';
          for (const item of (tooltip.dataPoints || [])) {
            if (item.raw == null || item.raw.y == null) continue;
            const color = item.dataset.borderColor || '#ccc';
            html += '<div style="display:flex;align-items:center;gap:5px;"><span style="display:inline-block;width:10px;height:10px;background:' + color + ';border:1px solid ' + color + ';flex-shrink:0;"></span><span>' + (item.dataset.label || '') + ': ' + (+item.raw.y).toFixed(0) + ' dBm</span></div>';
          }
          el.innerHTML = html;
          el.style.display = 'block';
          const pw = chart.canvas.parentNode.offsetWidth;
          const tw = el.offsetWidth || 160;
          el.style.left = (tooltip.caretX + tw + 14 > pw ? tooltip.caretX - tw - 4 : tooltip.caretX + 14) + 'px';
          el.style.top = Math.max(0, tooltip.caretY - 20) + 'px';
        }
      }
    },
    scales: {
      x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" }, ticks: { color: "#7a90a8", maxTicksLimit: 25 }, grid: { color: "#e8eef4" } },
      y: { ticks: { color: "#7a90a8", callback: v => v + " dBm" }, grid: { color: "#e8eef4" } }
    }
  }
});
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function loadSensors() {
  const data = await fetchJSON("/api/current");
  const labels = data.map(s => s.label).filter(Boolean).sort();
  labels.forEach((lbl, i) => { colorMap[lbl] = COLORS[i % COLORS.length]; });
  const btnsEl = document.getElementById('sensor-btns');
  const existing = [...btnsEl.querySelectorAll('button')].map(b => b.dataset.label);
  if (JSON.stringify(existing) !== JSON.stringify(labels)) {
    btnsEl.innerHTML = '';
    labels.forEach(lbl => {
      activeLabels.add(lbl);
      const btn = document.createElement('button');
      btn.dataset.label = lbl;
      btn.textContent = lbl;
      btn.className = 'active';
      btn.onclick = () => toggleLabel(lbl, btn);
      btnsEl.appendChild(btn);
    });
  }
}
function movingAvg(pts, windowMs) {
  // For each point, average all points within windowMs/2 on either side
  return pts.map((p, i) => {
    if (p.y == null) return { x: p.x, y: null };
    const half = windowMs / 2;
    const tMs = p.x.getTime();
    let sum = 0, cnt = 0;
    for (let j = i; j >= 0 && tMs - pts[j].x.getTime() <= half; j--) {
      if (pts[j].y != null) { sum += pts[j].y; cnt++; }
    }
    for (let j = i + 1; j < pts.length && pts[j].x.getTime() - tMs <= half; j++) {
      if (pts[j].y != null) { sum += pts[j].y; cnt++; }
    }
    return { x: p.x, y: cnt ? sum / cnt : null };
  });
}
async function loadChart() {
  let data;
  if (mode === "recent") {
    const xMax = new Date(Date.now() + offsetMs);
    const xMin = new Date(xMax - rangeDays * 86400000);
    const params = `start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`;
    data = await fetchJSON(`/api/history?${params}`);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = rangeDays === 0.125 ? "minute" : rangeDays === 1 ? "hour" : "day";
    chart.options.scales.x.ticks.stepSize = rangeDays === 0.125 ? 30 : 1;
    const peek = await fetchJSON(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`);
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    data = await fetchJSON(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
    const xMin = new Date(2000, activeMonth - 1, 1), xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "day";
  } else {
    data = await fetchJSON(`/api/history/year?bucket_minutes=${getBucket()}`);
    chart.options.scales.x.min = new Date(2000, 0, 1);
    chart.options.scales.x.max = new Date(2000, 11, 31, 23, 59, 59);
    chart.options.scales.x.time.unit = "month";
  }
  const useMavg = document.getElementById('chk-mavg').checked;
  const MAVG_MS = 3 * 60 * 60 * 1000; // 3 hours in ms
  const allLabels = [...new Set(data.map(r => r.label).filter(Boolean))].sort();
  chart.data.datasets = allLabels
    .filter(lbl => activeLabels.has(lbl))
    .map(lbl => {
      const color = colorMap[lbl] || COLORS[0];
      let pts = data
        .filter(r => r.label === lbl)
        .map(r => ({ x: new Date(r.ts), y: r.rssi ?? null }))
        .sort((a, b) => a.x - b.x);
      if (useMavg) pts = movingAvg(pts, MAVG_MS);
      return { label: lbl, data: pts, borderColor: color, backgroundColor: 'transparent', borderWidth: 1.5, pointRadius: 0, tension: 0 };
    });
  chart.update();
}
loadSensors().then(loadChart);
setInterval(() => loadSensors().then(loadChart), 30000);
</script>
</body>
</html>"""


@app.get("/chart/signal")
def chart_signal():
    return Response(_SIGNAL_PAGE, mimetype="text/html")


@app.get("/api/events")
def events_api():
    """Recent temperature parity events."""
    limit = min(int(request.args.get("limit", 50)), 200)
    start = request.args.get("start", "").replace("T", " ") or None
    end   = request.args.get("end",   "").replace("T", " ") or None
    event_type = request.args.get("event_type", "").strip() or None
    with _conn() as conn:
        query = "SELECT id, ts, event_type, value, details FROM temperature_events"
        params: list = []
        clauses: list[str] = []
        if start:
            clauses.append("ts >= ?")
            params.append(start)
        if end:
            clauses.append("ts <= ?")
            params.append(end)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    events = [{"id": r[0], "ts": r[1], "event_type": r[2], "value": r[3], "details": r[4]} for r in rows]
    return jsonify(events)


@app.get("/events")
def events_page():
    EVENT_LABELS = {
        "sun_shade_parity": "Sun / Shade Parity",
        "inside_outside_parity": "Inside / Outside Parity",
        "sensor_offline": "Sensor Offline",
        "sensor_online": "Sensor Online",
        "battery_low": "Battery Low",
    }
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Temperature Events &mdash; Smart Home</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .back { font-size: .85rem; font-weight: 500; color: #2e7dd4; text-decoration: none; margin-left: .6rem; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    th { background: #f7fafc; font-size: .72rem; font-weight: 600; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; padding: .6rem 1rem; text-align: left; border-bottom: 1px solid #e8eef4; }
    td { padding: .7rem 1rem; font-size: .88rem; border-bottom: 1px solid #f0f4f8; vertical-align: top; }
    tr:last-child td { border-bottom: none; }
    .badge { display: inline-block; padding: .2rem .55rem; border-radius: 20px; font-size: .72rem; font-weight: 600; letter-spacing: .03em; }
    .badge-sun  { background: #fff3e0; color: #d4760a; }
    .badge-io   { background: #e8f4fd; color: #1a6db5; }
    .badge-off  { background: #fde8e8; color: #c0392b; }
    .badge-on   { background: #e8fdf0; color: #1a7a4a; }
    .badge-bat  { background: #fff8e1; color: #b8860b; }
    .val  { font-weight: 700; color: #e07820; }
    .det  { color: #7a90a8; font-size: .8rem; margin-top: .15rem; }
    .empty { text-align: center; color: #aabbc8; padding: 3rem 1rem; font-size: .9rem; }
  </style>
</head>
<body>
  <h1>Temperature Events <a href="/" class="back">&larr; Dashboard</a></h1>
  <table>
    <thead><tr><th>Time</th><th>Event</th><th>Temperature</th><th>Details</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="4" class="empty">Loading&hellip;</td></tr></tbody>
  </table>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const EVENT_LABELS = """ + str(EVENT_LABELS).replace("'", '"') + """;
async function load() {
  const data = await fetchJSON("/api/events?limit=100");
  const tbody = document.getElementById("tbody");
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">No events recorded yet.</td></tr>';
    return;
  }
  tbody.innerHTML = data.map(e => {
    const label = EVENT_LABELS[e.event_type] || e.event_type;
    const badgeClass = e.event_type === "sun_shade_parity" ? "badge-sun" : e.event_type === "sensor_offline" ? "badge-off" : e.event_type === "sensor_online" ? "badge-on" : e.event_type === "battery_low" ? "badge-bat" : "badge-io";
    const ts = e.ts.replace(" ", "T");
    const timeStr = new Date(ts).toLocaleString();
    const val = e.value != null ? `${e.value.toFixed(1)}&deg;F` : "&mdash;";
    return `<tr>
      <td>${timeStr}</td>
      <td><span class="badge ${badgeClass}">${label}</span></td>
      <td class="val">${val}</td>
      <td><div class="det">${e.details || ""}</div></td>
    </tr>`;
  }).join("");
}
load();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/api/devices")
def api_devices():
    from smart_home import labels as _labels
    from smart_home import smart_plug as _plug
    from smart_home import camera as _camera
    from smart_home import garage as _garage
    from smart_home import presence as _presence
    from smart_home import pool as _pool

    from smart_home import ble_types as _ble_types
    label_map = _labels.load()
    type_map = _ble_types.load()
    ble = [
        {"address": addr, "label": lbl, "model": type_map.get(addr, "")}
        for addr, lbl in sorted(label_map.items(), key=lambda x: x[1])
    ]

    plugs = [
        {"id": p["name"], "name": p["name"], "model": p.get("type", ""), "address": p.get("ip", "")}
        for p in _plug.load_config()
    ]

    def _camera_ip(c):
        url = c.get("url", "")
        return url.replace("https://", "").replace("http://", "").split("/")[0]

    cameras = [
        {"id": c["name"], "name": c["name"], "model": c.get("model", ""), "address": _camera_ip(c)}
        for c in _camera.load_config()
    ]

    garages = [
        {"id": g["name"], "name": g["name"], "model": g.get("model", ""), "address": g.get("ip", "")}
        for g in _garage.load_config()
    ]

    presence_devs = [
        {"id": name, "name": name, "model": "", "address": info.get("local_ip", "")}
        for name, info in _presence.load_iphone_devices().items()
    ]

    water_chemistry_devices = [
        {"id": m["label"], "label": m["label"], "model": "BLE-YC01", "address": m.get("address", "")}
        for m in _pool.load_config()
    ]

    return jsonify({
        "ble_sensors": ble,
        "smart_plugs": plugs,
        "cameras": cameras,
        "garages": garages,
        "presence": presence_devs,
        "water_chemistry": water_chemistry_devices,
    })


@app.post("/api/devices/rename")
def api_devices_rename():
    from smart_home import labels as _labels
    from smart_home import smart_plug as _plug
    from smart_home import camera as _camera
    from smart_home import garage as _garage
    from smart_home import presence as _presence
    from smart_home import pool as _pool

    body = request.get_json(force=True)
    device_type = body.get("type")
    device_id   = body.get("id")
    new_name    = (body.get("new_name") or "").strip()

    if not device_type or not device_id or not new_name:
        return jsonify({"error": "type, id, and new_name are required"}), 400

    if device_type == "ble_sensor":
        label_map = _labels.load()
        if device_id not in label_map:
            return jsonify({"error": "device not found"}), 404
        label_map[device_id] = new_name
        _labels.save(label_map)

    elif device_type == "smart_plug":
        plugs = _plug.load_config()
        match = next((p for p in plugs if p["name"] == device_id), None)
        if match is None:
            return jsonify({"error": "device not found"}), 404
        match["name"] = new_name
        _plug.save_config(plugs)

    elif device_type == "camera":
        cameras = _camera.load_config()
        match = next((c for c in cameras if c["name"] == device_id), None)
        if match is None:
            return jsonify({"error": "device not found"}), 404
        match["name"] = new_name
        _camera.save_config(cameras)

    elif device_type == "garage":
        garages = _garage.load_config()
        match = next((g for g in garages if g["name"] == device_id), None)
        if match is None:
            return jsonify({"error": "device not found"}), 404
        match["name"] = new_name
        _garage.save_config(garages)

    elif device_type == "presence":
        devices = _presence.load_iphone_devices()
        if device_id not in devices:
            return jsonify({"error": "device not found"}), 404
        devices[new_name] = devices.pop(device_id)
        _presence.save_iphone_devices(devices)

    elif device_type == "water_chemistry":
        monitors = _pool.load_config()
        match = next((m for m in monitors if m["label"] == device_id), None)
        if match is None:
            return jsonify({"error": "device not found"}), 404
        old_label = match["label"]
        match["label"] = new_name
        _pool.save_config(monitors)
        with _conn() as conn:
            conn.execute(
                "UPDATE pool_readings SET label=? WHERE label=?",
                (new_name, old_label),
            )
            conn.commit()

    else:
        return jsonify({"error": f"unknown device type: {device_type}"}), 400

    return jsonify({"ok": True})


@app.get("/")
def index():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Smart Home</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .card { background: #fff; border-radius: 12px; padding: 1.1rem 1.5rem; min-width: 190px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .card .label { font-size: 0.75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .card .temp  { font-size: 2.4rem; font-weight: 700; color: #e07820; margin: .2rem 0 .1rem; line-height: 1; }
    .card .hum   { font-size: 1rem; color: #2e7dd4; font-weight: 500; }
    .card .ts    { font-size: 0.72rem; color: #aabbc8; margin-top: .5rem; }
    .presence-cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .presence-card { background: #fff; border-radius: 12px; padding: 1rem 1.5rem; min-width: 160px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); display: flex; align-items: center; gap: .9rem; }
    .presence-dot { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
    .presence-dot.home { background: #2a9d6e; }
    .presence-dot.away { background: #c0392b; }
    .presence-dot.unknown { background: #aabbc8; }
    .presence-info .name { font-size: .85rem; font-weight: 600; color: #1a2535; }
    .presence-info .status { font-size: .75rem; color: #7a90a8; margin-top: .15rem; }
    .section-title { font-size: .75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .75rem; }
    .events-list { display: flex; flex-direction: column; gap: .4rem; margin-bottom: 2rem; }
    .ev-row { background: #fff; border-radius: 10px; padding: .65rem 1.1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); display: flex; align-items: center; gap: .75rem; }
    .ev-badge { display: inline-block; padding: .18rem .5rem; border-radius: 20px; font-size: .7rem; font-weight: 700; letter-spacing: .03em; white-space: nowrap; }
    .ev-badge.b-off  { background: #fde8e8; color: #c0392b; }
    .ev-badge.b-on   { background: #e8fdf0; color: #1a7a4a; }
    .ev-badge.b-bat  { background: #fff8e1; color: #b8860b; }
    .ev-badge.b-sun  { background: #fff3e0; color: #d4760a; }
    .ev-badge.b-io   { background: #e8f4fd; color: #1a6db5; }
    .ev-detail { font-size: .85rem; color: #1a2535; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ev-time   { font-size: .75rem; color: #aabbc8; white-space: nowrap; margin-left: auto; }
    .garage-cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .garage-card { background: #fff; border-radius: 12px; padding: 1.1rem 1.5rem; min-width: 160px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); text-decoration: none; color: inherit; display: block; }
    .garage-card .label { font-size: 0.75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .garage-card .gstate { font-size: 1.6rem; font-weight: 800; margin: .25rem 0 .1rem; line-height: 1; }
    .garage-card .gstate.closed { color: #2a9d6e; }
    .garage-card .gstate.open   { color: #c0392b; }
    .garage-card .gstate.unknown { color: #aabbc8; }
    .garage-card .gtimer { font-size: 0.75rem; color: #c0392b; font-weight: 600; margin-top: .2rem; min-height: 1em; }
    .pool-cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .pool-card { background: #fff; border-radius: 12px; padding: 1.1rem 1.5rem; min-width: 200px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); text-decoration: none; color: inherit; display: block; }
    .pool-card .pc-label { font-size: .75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .35rem; }
    .pool-card .pc-status { font-size: .8rem; font-weight: 700; display: flex; align-items: center; gap: .45rem; margin-bottom: .3rem; }
    .pool-card .pc-status.online  { color: #2a9d6e; }
    .pool-card .pc-status.offline { color: #c0392b; }
    .pool-card .pc-metrics { font-size: .92rem; color: #1a2535; font-weight: 600; }
    .pool-card .pc-ts { font-size: .72rem; color: #aabbc8; margin-top: .3rem; }
    .chart-links { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
    .chart-link { display: flex; align-items: center; justify-content: space-between; gap: 1.5rem; background: #fff; border-radius: 12px; padding: 1rem 1.5rem; min-width: 220px; text-decoration: none; color: #1a2535; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); transition: box-shadow .15s, transform .15s; }
    .chart-link:hover { box-shadow: 0 2px 8px rgba(0,0,0,.12), 0 6px 18px rgba(0,0,0,.08); transform: translateY(-1px); }
    .chart-link .cl-title { font-size: .9rem; font-weight: 600; }
    .chart-link .cl-arrow { color: #aabbc8; font-size: 1.1rem; }
    #error-bar { display:none; background:#fde8e8; color:#c0392b; border-radius:8px; padding:.6rem 1rem; margin-bottom:1rem; font-size:.85rem; font-weight:500; }
  </style>
</head>
<body>
  <div id="error-bar"></div>
  <h1>Smart Home &nbsp;<a href="/trends" style="font-size:.85rem;font-weight:500;color:#2e7dd4;text-decoration:none;">Trends &rarr;</a>&nbsp;<a href="/devices" style="font-size:.85rem;font-weight:500;color:#7a90a8;text-decoration:none;">Devices &#9881;</a>&nbsp;<a href="/zones" style="font-size:.85rem;font-weight:500;color:#7a90a8;text-decoration:none;">Zones</a></h1>

  <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:2rem">
    <div class="cards" id="cards" style="margin-bottom:0;flex-wrap:wrap;display:flex;gap:1rem"></div>
    <div class="garage-cards" id="garage-cards" style="margin-bottom:0"></div>
  </div>
  <div class="presence-cards" id="presence-cards"></div>
  <div class="pool-cards"    id="pool-cards"    style="display:none"></div>

  <div class="section-title">Charts</div>
  <div class="chart-links">
    <a href="/chart/temperature" class="chart-link"><span class="cl-title">Temperature</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/humidity"    class="chart-link"><span class="cl-title">Humidity</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/energy"      class="chart-link"><span class="cl-title">Energy Usage</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/differential" class="chart-link"><span class="cl-title">Differentials</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/sensors"     class="chart-link"><span class="cl-title">Sensor Battery Life</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/bandwidth"   class="chart-link"><span class="cl-title">Bandwidth</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/signal"      class="chart-link"><span class="cl-title">Signal Strength</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/events"            class="chart-link"><span class="cl-title">Temperature Events</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/process-stats"     class="chart-link"><span class="cl-title">Process Stats</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/db-sizes"   class="chart-link"><span class="cl-title">Database Growth</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/camera"            class="chart-link"><span class="cl-title">Cameras</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/garage"            class="chart-link"><span class="cl-title">Garage Door</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/water-chemistry"   class="chart-link" id="pool-link" style="display:none"><span class="cl-title">Water Chemistry</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/rssi"              class="chart-link"><span class="cl-title">Bluetooth Signal</span><span class="cl-arrow">&#8594;</span></a>
  </div>

  <div id="events-wrap" style="display:none">
    <div class="section-title">Events &nbsp;<a href="/events" style="font-size:.75rem;font-weight:500;color:#2e7dd4;text-decoration:none;">View all &rarr;</a></div>
    <div class="events-list" id="events-list"></div>
  </div>

<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const _errors = new Set();
function showError(section) {
  if (_errors.has(section)) return;
  _errors.add(section);
  const bar = document.getElementById("error-bar");
  bar.style.display = "";
  bar.textContent = "Failed to load: " + [..._errors].join(", ") + ". Check network connection.";
}
async function loadCurrent() {
  try {
    const data = await fetchJSON("/api/current");
    document.getElementById("cards").innerHTML = data.map(s => `
      <div class="card">
        <div class="label">${s.label || s.address}</div>
        <div class="temp">${s.temp_f.toFixed(1)}&deg;F</div>
        <div class="hum">${s.humidity.toFixed(1)}% RH</div>
        <div class="ts">${new Date(s.ts).toLocaleString()}</div>
      </div>`).join("");
  } catch(e) { showError("sensors"); }
}
async function loadPresence() {
  try {
    const data = await fetchJSON("/api/presence");
    const el = document.getElementById("presence-cards");
    if (!data.length) { el.innerHTML = ""; return; }
    el.innerHTML = data.map(d => {
      const ago = d.last_seen ? timeSince(new Date(d.last_seen)) : "never";
      return `<a href="/presence" class="presence-card" style="text-decoration:none;color:inherit">
        <div class="presence-dot ${d.status}"></div>
        <div class="presence-info">
          <div class="name">${d.name}</div>
          ${d.model_name ? `<div class="status" style="color:#4a6080;font-weight:500">${d.model_name}</div>` : ''}
          <div class="status">${d.status} &middot; ${ago}</div>
        </div>
      </a>`;
    }).join("");
  } catch(e) { showError("presence"); }
}
function timeSince(date) {
  const s = Math.floor((Date.now() - date) / 1000);
  if (s < 60)    return `${s}s ago`;
  if (s < 3600)  return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ago`;
  return `${Math.floor(s/86400)}d ago`;
}
const EV_LABELS = {
  sensor_offline:        ["Sensor Offline",          "b-off"],
  sensor_online:         ["Sensor Online",           "b-on"],
  battery_low:           ["Battery Low",             "b-bat"],
  sun_shade_parity:      ["Sun / Shade Parity",      "b-sun"],
  inside_outside_parity: ["Inside / Outside Parity", "b-io"],
};
async function loadEvents() {
  try {
    const data = await fetchJSON("/api/events?limit=15");
    const wrap = document.getElementById("events-wrap");
    const el = document.getElementById("events-list");
    if (!data.length) { wrap.style.display = "none"; return; }
    wrap.style.display = "";
    el.innerHTML = data.map(e => {
      const [label, cls] = EV_LABELS[e.event_type] || [e.event_type, "b-io"];
      const timeStr = new Date(e.ts.replace(" ", "T")).toLocaleString();
      const detail = e.details || (e.value != null ? `${e.value.toFixed(1)}°F` : "");
      return `<div class="ev-row">
        <span class="ev-badge ${cls}">${label}</span>
        <span class="ev-detail">${detail}</span>
        <span class="ev-time">${timeStr}</span>
      </div>`;
    }).join("");
  } catch(e) { showError("events"); }
}
const garageOpenSince = {};    // name -> ms timestamp when last opened
const garageClosedSince = {};  // name -> ms timestamp when last closed
function fmtDur(ms) {
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sc = s % 60;
  if (d > 0) return `${d}d ${h}h ${m}m ${sc}s`;
  if (h > 0) return `${h}h ${m}m ${sc}s`;
  if (m > 0) return `${m}m ${sc}s`;
  return `${sc}s`;
}
function tickGarageTimers() {
  const now = Date.now();
  for (const [name, since] of Object.entries(garageOpenSince)) {
    const el = document.getElementById(`gtimer-${name}`);
    if (el) el.textContent = "Open " + fmtDur(now - since);
  }
  for (const [name, since] of Object.entries(garageClosedSince)) {
    const el = document.getElementById(`gtimer-${name}`);
    if (el) el.textContent = "Closed " + fmtDur(now - since);
  }
}
setInterval(tickGarageTimers, 1000);
async function loadGarage() {
  const garages = await fetchJSON("/api/garage");
  if (!garages.length) return;
  const results = await Promise.all(garages.map(g =>
    fetchJSON(`/api/garage/${encodeURIComponent(g.name)}/status`)
      .then(s => ({ name: g.name, ...s })).catch(() => ({ name: g.name, ok: false }))
  ));
  const el = document.getElementById("garage-cards");
  el.innerHTML = results.map(d => {
    let stateClass = "unknown", stateText = "?";
    if (d.ok) {
      if (d.door_closed === true)  { stateClass = "closed"; stateText = "CLOSED"; }
      else if (d.door_closed === false) { stateClass = "open"; stateText = "OPEN"; }
    } else { stateText = "⚠"; }
    if (d.door_closed === false) {
      delete garageClosedSince[d.name];
      if (d.last_opened) garageOpenSince[d.name] = new Date(d.last_opened.replace(" ", "T")).getTime();
      else if (!garageOpenSince[d.name]) garageOpenSince[d.name] = Date.now();
    } else if (d.door_closed === true) {
      delete garageOpenSince[d.name];
      if (d.last_closed) garageClosedSince[d.name] = new Date(d.last_closed.replace(" ", "T")).getTime();
      else if (!garageClosedSince[d.name]) garageClosedSince[d.name] = Date.now();
    } else {
      delete garageOpenSince[d.name];
      delete garageClosedSince[d.name];
    }
    return `<a href="/garage" class="garage-card">
      <div class="label">${d.name}</div>
      <div class="gstate ${stateClass}">${stateText}</div>
      <div class="gtimer" id="gtimer-${d.name}"></div>
    </a>`;
  }).join("");
  tickGarageTimers();
}
async function loadPool() {
  try {
    const rows = await fetchJSON('/api/pool/current');
    if (!rows.length) return;
    document.getElementById('pool-link').style.display = '';
    const wrap = document.getElementById('pool-cards');
    wrap.style.display = '';
    wrap.innerHTML = rows.map(r => {
      const offline = r.offline;
      const statusClass = offline ? 'offline' : 'online';
      const statusDot   = offline ? '●' : '●';
      const statusText  = offline ? 'Offline' : 'Online';
      const metrics = offline ? '' :
        [r.temp_f != null ? r.temp_f.toFixed(1) + '°F' : null,
         r.ph     != null ? 'pH ' + r.ph.toFixed(2) : null]
        .filter(Boolean).join('  ');
      const tsText = offline
        ? 'Last reading ' + timeSince(new Date(r.ts.replace(' ', 'T')))
        : r.ts.slice(11, 16);
      return `<a href="/water-chemistry" class="pool-card">
        <div class="pc-label">${r.label}</div>
        <div class="pc-status ${statusClass}">${statusDot} ${statusText}</div>
        ${metrics ? `<div class="pc-metrics">${metrics}</div>` : ''}
        <div class="pc-ts">${tsText}</div>
      </a>`;
    }).join('');
  } catch(e) { /* pool not configured — silently skip */ }
}
loadCurrent();
loadPresence();
loadEvents();
loadGarage();
loadPool();
setInterval(loadCurrent, 30000);
setInterval(loadPresence, 30000);
setInterval(loadEvents, 60000);
setInterval(loadGarage, 15000);
setInterval(loadPool, 60000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/devices")
def devices_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Device Settings</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .back { font-size: .85rem; font-weight: 500; color: #2e7dd4; text-decoration: none; margin-left: .75rem; }
    .section { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); margin-bottom: 1.5rem; overflow: hidden; }
    .section-header { font-size: .75rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: #7a90a8; padding: .75rem 1.25rem; background: #f8fafc; border-bottom: 1px solid #e8edf3; }
    .device-row { display: flex; align-items: center; gap: 1rem; padding: .75rem 1.25rem; border-bottom: 1px solid #f0f4f8; flex-wrap: wrap; }
    .wc-controls { width: 100%; display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; padding-top: .4rem; }
    .wc-ctrl-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .wc-select { font-size: .85rem; padding: .3rem .6rem; border: 1px solid #d0dce8; border-radius: 8px; background: #fff; color: #1a2535; cursor: pointer; }
    .wc-msg { font-size: .78rem; }
    .wc-msg.ok  { color: #2a9d6e; }
    .wc-msg.err { color: #c0392b; }
    .device-row:last-child { border-bottom: none; }
    .device-name { flex: 1; font-size: .95rem; font-weight: 600; color: #1a2535; }
    .device-sub  { font-size: .78rem; color: #7a90a8; font-weight: 400; margin-top: .1rem; }
    .edit-btn { font-size: .8rem; color: #2e7dd4; background: none; border: none; cursor: pointer; padding: .3rem .6rem; border-radius: 6px; font-weight: 600; }
    .edit-btn:hover { background: #e8f1fb; }
    .edit-form { display: none; flex: 1; align-items: center; gap: .5rem; }
    .edit-input { flex: 1; font-size: .9rem; padding: .35rem .65rem; border: 1.5px solid #2e7dd4; border-radius: 7px; color: #1a2535; outline: none; }
    .save-btn   { font-size: .8rem; font-weight: 700; background: #2e7dd4; color: #fff; border: none; border-radius: 7px; padding: .35rem .75rem; cursor: pointer; }
    .save-btn:hover { background: #2469b8; }
    .cancel-btn { font-size: .8rem; color: #7a90a8; background: none; border: none; cursor: pointer; padding: .35rem .5rem; border-radius: 7px; }
    .cancel-btn:hover { background: #f0f4f8; }
    .empty { padding: 1rem 1.25rem; font-size: .88rem; color: #aabbc8; }
    #error-bar { display:none; background:#fde8e8; color:#c0392b; border-radius:8px; padding:.6rem 1rem; margin-bottom:1rem; font-size:.85rem; font-weight:500; }
  </style>
</head>
<body>
  <div id="error-bar"></div>
  <h1>Device Settings &nbsp;<a href="/" class="back">&larr; Dashboard</a></h1>
  <div id="sections"></div>
<script>
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

const TYPE_LABELS = {
  ble_sensors:   "BLE Temperature Sensors",
  smart_plugs:   "Smart Plugs",
  cameras:       "Cameras",
  garages:       "Garage Doors",
  presence:      "Presence Devices (iPhones)",
  water_chemistry: "Water Chemistry",
};

function deviceId(type, d) {
  if (type === "ble_sensors")   return d.address;
  if (type === "water_chemistry") return d.id;
  return d.id;
}

function deviceLabel(type, d) {
  if (type === "ble_sensors")   return d.label || d.address;
  if (type === "water_chemistry") return d.label;
  return d.name;
}

function deviceSub(type, d) {
  return [d.model, d.address].filter(Boolean).join(" · ");
}

function apiType(type) {
  if (type === "ble_sensors")   return "ble_sensor";
  if (type === "smart_plugs")   return "smart_plug";
  if (type === "water_chemistry") return "water_chemistry";
  return type.replace(/s$/, "");
}

function showError(msg) {
  const bar = document.getElementById("error-bar");
  bar.style.display = "";
  bar.textContent = msg;
}

async function renameDevice(type, id, newName, row) {
  try {
    const r = await fetch("/api/devices/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({type: apiType(type), id, new_name: newName}),
    });
    const data = await r.json();
    if (!r.ok) { showError(data.error || "Rename failed"); return false; }
    return true;
  } catch(e) { showError("Network error: " + e.message); return false; }
}

function buildRow(type, d) {
  const id    = deviceId(type, d);
  const label = deviceLabel(type, d);
  const sub   = deviceSub(type, d);

  const row = document.createElement("div");
  row.className = "device-row";
  row.dataset.currentLabel = label;

  const nameEl = document.createElement("div");
  nameEl.className = "device-name";
  nameEl.textContent = label;
  if (sub) {
    const subEl = document.createElement("div");
    subEl.className = "device-sub";
    subEl.textContent = sub;
    nameEl.appendChild(subEl);
  }

  const editBtn = document.createElement("button");
  editBtn.className = "edit-btn";
  editBtn.textContent = "Rename";
  editBtn.onclick = () => startEdit(row, type, id);

  const form = document.createElement("div");
  form.className = "edit-form";
  form.style.display = "none";

  const inp = document.createElement("input");
  inp.className = "edit-input";
  inp.type = "text";

  const saveBtn = document.createElement("button");
  saveBtn.className = "save-btn";
  saveBtn.textContent = "Save";
  saveBtn.onclick = () => saveEdit(row, type, id);

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "cancel-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.onclick = () => cancelEdit(row);

  form.append(inp, saveBtn, cancelBtn);
  row.append(nameEl, editBtn, form);

  if (type === "water_chemistry") {
    const controls = document.createElement("div");
    controls.className = "wc-controls";
    controls.dataset.label = id;
    controls.innerHTML = `
      <span class="wc-ctrl-label">Connection node</span>
      <select class="wc-select wc-node-sel" onchange="setWcNode(this)">
        <option value="">Loading…</option>
      </select>
      <span class="wc-ctrl-label" style="margin-left:.5rem">Poll rate</span>
      <select class="wc-select wc-poll-sel" onchange="setWcPollRate(this)">
        <option value="30">30 seconds</option>
        <option value="60">60 seconds</option>
      </select>
      <span class="wc-ctrl-label" style="margin-left:.5rem">Zone</span>
      <select class="wc-select wc-zone-sel" onchange="setWcZone(this)">
        <option value="">Loading…</option>
      </select>
      <span class="wc-msg"></span>`;
    row.appendChild(controls);
  }

  return row;
}

function startEdit(row, type, id) {
  const currentLabel = row.dataset.currentLabel;
  row.querySelector(".device-name").style.display = "none";
  row.querySelector(".edit-btn").style.display = "none";
  const form = row.querySelector(".edit-form");
  form.style.display = "flex";
  const inp = row.querySelector(".edit-input");
  inp.value = currentLabel;
  inp.focus();
  inp.select();
  inp.onkeydown = e => {
    if (e.key === "Enter") saveEdit(row, type, id);
    if (e.key === "Escape") cancelEdit(row);
  };
}

function cancelEdit(row) {
  row.querySelector(".device-name").style.display = "";
  row.querySelector(".edit-btn").style.display = "";
  row.querySelector(".edit-form").style.display = "none";
}

async function saveEdit(row, type, id) {
  const inp = row.querySelector(".edit-input");
  const newName = inp.value.trim();
  if (!newName) { inp.focus(); return; }
  inp.disabled = true;
  const ok = await renameDevice(type, id, newName, row);
  inp.disabled = false;
  if (!ok) return;
  row.dataset.currentLabel = newName;
  const nameEl = row.querySelector(".device-name");
  const sub = nameEl.querySelector(".device-sub");
  nameEl.textContent = newName;
  if (sub) nameEl.appendChild(sub);
  row.querySelector(".device-name").style.display = "";
  row.querySelector(".edit-btn").style.display = "";
  row.querySelector(".edit-form").style.display = "none";
}

async function loadWaterChemistrySettings() {
  try {
    const [nodeData, zones] = await Promise.all([
      fetchJSON('/api/pool/node'),
      fetchJSON('/api/water-chemistry/zones'),
    ]);
    const zoneOptions = '<option value="">-- No zone --</option>'
      + zones.map(z => `<option value="${z.name}">${z.name}</option>`).join('');
    for (const [label, info] of Object.entries(nodeData)) {
      const controls = document.querySelector(`.wc-controls[data-label="${CSS.escape(label)}"]`);
      if (!controls) continue;
      const nodeSel = controls.querySelector('.wc-node-sel');
      nodeSel.innerHTML = info.relay_options.map(opt => {
        const name = opt.id === 'server' ? 'Server (tank2)' : opt.id;
        const selected = opt.id === info.node ? ' selected' : '';
        return `<option value="${opt.id}"${selected}>${name}</option>`;
      }).join('');
      const pollSel = controls.querySelector('.wc-poll-sel');
      const interval = info.poll_interval_s ?? 60;
      for (const opt of pollSel.options) {
        if (parseInt(opt.value) === interval) { opt.selected = true; break; }
      }
      const zoneSel = controls.querySelector('.wc-zone-sel');
      zoneSel.innerHTML = zoneOptions;
      zoneSel.value = info.current_zone || '';
    }
  } catch(e) { showError('Failed to load water chemistry settings: ' + e.message); }
}

async function setWcNode(sel) {
  const controls = sel.closest('.wc-controls');
  const label = controls.dataset.label;
  const node = sel.value;
  const msg = controls.querySelector('.wc-msg');
  msg.className = 'wc-msg'; msg.textContent = 'Saving…';
  try {
    const r = await fetch('/api/pool/node', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label, node}),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
    msg.className = 'wc-msg ok';
    msg.textContent = node === 'server'
      ? 'Server will handle on next restart'
      : `Relay '${node}' will activate on its next check-in (~18s)`;
    setTimeout(() => { msg.textContent = ''; }, 5000);
  } catch(e) {
    msg.className = 'wc-msg err'; msg.textContent = 'Error: ' + e.message;
    showError('Failed to set node: ' + e.message);
  }
}

async function setWcPollRate(sel) {
  const controls = sel.closest('.wc-controls');
  const label = controls.dataset.label;
  const interval_s = parseInt(sel.value);
  const msg = controls.querySelector('.wc-msg');
  msg.className = 'wc-msg'; msg.textContent = 'Saving…';
  try {
    const r = await fetch('/api/pool/poll-rate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label, interval_s}),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
    msg.className = 'wc-msg ok';
    msg.textContent = 'Poll rate saved — relay will apply on next check-in';
    setTimeout(() => { msg.textContent = ''; }, 5000);
  } catch(e) {
    msg.className = 'wc-msg err'; msg.textContent = 'Error: ' + e.message;
    showError('Failed to set poll rate: ' + e.message);
  }
}

async function setWcZone(sel) {
  const controls = sel.closest('.wc-controls');
  const label = controls.dataset.label;
  const zone = sel.value || null;
  const msg = controls.querySelector('.wc-msg');
  msg.className = 'wc-msg'; msg.textContent = 'Saving…';
  try {
    const r = await fetch('/api/water-chemistry/move', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label, zone}),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
    msg.className = 'wc-msg ok'; msg.textContent = 'Zone saved';
    setTimeout(() => { msg.textContent = ''; }, 3000);
  } catch(e) {
    msg.className = 'wc-msg err'; msg.textContent = 'Error: ' + e.message;
    showError('Failed to set zone: ' + e.message);
  }
}


async function load() {
  let data;
  try {
    const r = await fetch("/api/devices");
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch(e) { showError("Failed to load devices: " + e.message); return; }

  const container = document.getElementById("sections");
  for (const [type, devices] of Object.entries(data)) {
    const section = document.createElement("div");
    section.className = "section";
    const header = document.createElement("div");
    header.className = "section-header";
    header.textContent = TYPE_LABELS[type] || type;
    section.appendChild(header);
    if (!devices.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No devices configured";
      section.appendChild(empty);
    } else {
      devices.forEach(d => section.appendChild(buildRow(type, d)));
    }
    container.appendChild(section);
  }
  if (data.water_chemistry && data.water_chemistry.length) {
    loadWaterChemistrySettings();
  }
}
load();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/api/process-stats")
def api_process_stats():
    days = float(request.args.get("days", 1))
    start = request.args.get("start")
    end = request.args.get("end")
    with _conn() as conn:
        if start and end:
            main_rows = conn.execute(
                "SELECT ts, cpu_percent, mem_mb FROM process_stats WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (start, end),
            ).fetchall()
            cam_rows = conn.execute(
                "SELECT ts, camera, cpu_percent, mem_mb FROM camera_process_stats WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (start, end),
            ).fetchall()
        else:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            main_rows = conn.execute(
                "SELECT ts, cpu_percent, mem_mb FROM process_stats WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
            cam_rows = conn.execute(
                "SELECT ts, camera, cpu_percent, mem_mb FROM camera_process_stats WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
    cameras: dict = {}
    for r in cam_rows:
        cameras.setdefault(r[1], []).append({"ts": r[0], "cpu": r[2], "mem": r[3]})
    return jsonify({
        "main": [{"ts": r[0], "cpu": r[1], "mem": r[2]} for r in main_rows],
        "cameras": cameras,
    })


_PROCESS_STATS_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Process Stats &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
  </style>
</head>
<body>
  <h1>Process Stats</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="btn-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns">
      <button onclick="setRange(0.25)" data-days="0.25">6h</button>
      <button onclick="setRange(1)"    data-days="1" class="active">24h</button>
      <button onclick="setRange(7)"    data-days="7">7d</button>
      <button onclick="setRange(30)"   data-days="30">30d</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>CPU %</h2><canvas id="cpu-chart" height="80"></canvas></div>
  <div class="chart-wrap"><h2>Memory (MB)</h2><canvas id="mem-chart" height="80"></canvas></div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const CAM_COLORS = ["#2db37a", "#9b59b6", "#e74c3c", "#1abc9c", "#f39c12"];
let rangeDays = 1;

function makeChart(id, mainLabel, mainColor, yLabel) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: [{ label: mainLabel, data: [], borderColor: mainColor,
                         backgroundColor: mainColor + "22", borderWidth: 1.5,
                         pointRadius: 0, tension: 0, fill: true }] },
    options: {
      animation: false, parsing: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: true, labels: { color: "#4a6080", font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: ${ctx.raw.y != null ? ctx.raw.y.toFixed(1) : "—"} ${yLabel}` } } },
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" },
             ticks: { color: "#7a90a8", maxTicksLimit: 20 }, grid: { color: "#e8eef4" } },
        y: { min: 0, ticks: { color: "#7a90a8", callback: v => v + " " + yLabel }, grid: { color: "#e8eef4" } }
      }
    }
  });
}

const cpuChart = makeChart("cpu-chart", "Main process", "#e07820", "%");
const memChart = makeChart("mem-chart", "Main process", "#2e7dd4", "MB");

function syncCamDatasets(chart, cameras, colorBase) {
  const camNames = Object.keys(cameras);
  // Remove stale camera datasets (keep index 0 = main)
  chart.data.datasets.splice(1);
  camNames.forEach((name, i) => {
    const color = CAM_COLORS[i % CAM_COLORS.length];
    chart.data.datasets.push({
      label: `cam: ${name}`, data: [],
      borderColor: color, backgroundColor: color + "22",
      borderWidth: 1.5, pointRadius: 0, tension: 0, fill: false,
    });
  });
  return camNames;
}

function setRange(days) {
  rangeDays = days;
  document.querySelectorAll(".range-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  load();
}

async function load() {
  const data = await fetchJSON(`/api/process-stats?days=${rangeDays}`);
  const now = new Date();
  const xMin = new Date(now - rangeDays * 86400000);
  [cpuChart, memChart].forEach(c => { c.options.scales.x.min = xMin; c.options.scales.x.max = now;
    c.options.scales.x.time.unit = rangeDays <= 1 ? "hour" : "day"; });
  cpuChart.data.datasets[0].data = data.main.map(r => ({ x: new Date(r.ts), y: r.cpu }));
  memChart.data.datasets[0].data = data.main.map(r => ({ x: new Date(r.ts), y: r.mem }));
  const camNames = syncCamDatasets(cpuChart, data.cameras || {});
  syncCamDatasets(memChart, data.cameras || {});
  camNames.forEach((name, i) => {
    const rows = data.cameras[name] || [];
    cpuChart.data.datasets[i + 1].data = rows.map(r => ({ x: new Date(r.ts), y: r.cpu }));
    memChart.data.datasets[i + 1].data = rows.map(r => ({ x: new Date(r.ts), y: r.mem }));
  });
  cpuChart.update();
  memChart.update();
}

load();
setInterval(load, 60000);
</script>
</body>
</html>"""


@app.get("/process-stats")
def process_stats_page():
    return Response(_PROCESS_STATS_PAGE, mimetype="text/html")


@app.get("/api/db-sizes")
def api_db_sizes():
    days = float(request.args.get("days", 7))
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, name, bytes FROM db_size_readings WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    result = {}
    for ts, name, size in rows:
        result.setdefault(name, []).append({"ts": ts, "bytes": size})
    return jsonify(result)


@app.get("/api/db-sizes/stats")
def api_db_sizes_stats():
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT name,
              FIRST_VALUE(ts)    OVER (PARTITION BY name ORDER BY ts ASC)  AS first_ts,
              FIRST_VALUE(bytes) OVER (PARTITION BY name ORDER BY ts ASC)  AS first_bytes,
              FIRST_VALUE(ts)    OVER (PARTITION BY name ORDER BY ts DESC) AS last_ts,
              FIRST_VALUE(bytes) OVER (PARTITION BY name ORDER BY ts DESC) AS last_bytes
            FROM db_size_readings
        """).fetchall()
    result = {}
    for name, first_ts, first_bytes, last_ts, last_bytes in rows:
        result[name] = {
            "first_ts": first_ts, "first_bytes": first_bytes,
            "last_ts": last_ts, "last_bytes": last_bytes,
        }
    return jsonify(result)


_DB_SIZE_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Database Growth &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
    .proj-wrap { background: #fff; border-radius: 12px; padding: 1.4rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); overflow-x: auto; }
    .proj-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .proj-table { width: 100%; border-collapse: collapse; font-size: .875rem; }
    .proj-table th { text-align: left; color: #7a90a8; font-weight: 600; font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; padding: .4rem .75rem; border-bottom: 1px solid #e8eef4; white-space: nowrap; }
    .proj-table td { padding: .55rem .75rem; border-bottom: 1px solid #f0f4f8; color: #1a2535; white-space: nowrap; }
    .proj-table tr:last-child td { border-bottom: none; }
    .proj-table td.name { font-weight: 600; font-family: monospace; font-size: .82rem; }
    .proj-table td.num { text-align: right; }
    .proj-table th.num { text-align: right; }
    .proj-note { font-size: .75rem; color: #7a90a8; margin-top: .75rem; }
  </style>
</head>
<body>
  <h1>Database Growth</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="btn-group">
    <div class="btn-group-label">Time Range</div>
    <div class="range-btns">
      <button onclick="setRange(1)"   data-days="1">24h</button>
      <button onclick="setRange(7)"   data-days="7" class="active">7d</button>
      <button onclick="setRange(30)"  data-days="30">30d</button>
      <button onclick="setRange(90)"  data-days="90">90d</button>
      <button onclick="setRange(365)" data-days="365">1yr</button>
    </div>
  </div>
  <div class="chart-wrap"><h2>Database Sizes</h2><canvas id="db-chart" height="80"></canvas></div>
  <div class="proj-wrap">
    <h2>Growth Projections</h2>
    <table class="proj-table">
      <thead>
        <tr>
          <th>Database</th>
          <th class="num">Current Size</th>
          <th class="num">MB / hour</th>
          <th class="num">MB / day</th>
          <th class="num">MB / month</th>
          <th class="num">MB / year</th>
          <th class="num">Size at End of Year</th>
        </tr>
      </thead>
      <tbody id="proj-tbody"><tr><td colspan="7" style="color:#7a90a8;padding:.75rem">Loading&hellip;</td></tr></tbody>
    </table>
    <div class="proj-note" id="proj-note"></div>
  </div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}

function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return '\\u2014';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function fmtMB(mb) {
  if (mb === null || !isFinite(mb) || mb < 0) return '\\u2014';
  if (mb < 0.001) return '< 0.001';
  if (mb < 1) return mb.toFixed(3);
  if (mb < 1000) return mb.toFixed(2);
  return mb.toFixed(0);
}

const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400"];
let rangeDays = 7;
let chart = null;

function setRange(days) {
  rangeDays = days;
  document.querySelectorAll('.range-btns button[data-days]').forEach(b =>
    b.classList.toggle('active', parseFloat(b.dataset.days) === days));
  load();
}

function makeChart(names) {
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('db-chart'), {
    type: 'line',
    data: {
      datasets: names.map((name, i) => ({
        label: name,
        data: [],
        borderColor: COLORS[i % COLORS.length],
        backgroundColor: COLORS[i % COLORS.length] + '18',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0,
        fill: false,
      }))
    },
    options: {
      animation: false,
      parsing: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, position: 'top',
          labels: { color: '#1a2535', usePointStyle: true, pointStyleWidth: 10 } },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${fmtBytes(ctx.raw.y)}`
          }
        }
      },
      scales: {
        x: {
          type: 'time',
          time: { tooltipFormat: 'MMM d, h:mm a' },
          ticks: { color: '#7a90a8', maxTicksLimit: 20 },
          grid: { color: '#e8eef4' }
        },
        y: {
          min: 0,
          ticks: { color: '#7a90a8', callback: v => fmtBytes(v) },
          grid: { color: '#e8eef4' }
        }
      }
    }
  });
  return chart;
}

async function load() {
  const data = await fetchJSON(`/api/db-sizes?days=${rangeDays}`);
  const names = Object.keys(data).sort();
  if (!chart || chart.data.datasets.length !== names.length ||
      chart.data.datasets.some((ds, i) => ds.label !== names[i])) {
    makeChart(names);
  }
  const now = new Date();
  const xMin = new Date(now - rangeDays * 86400000);
  chart.options.scales.x.min = xMin;
  chart.options.scales.x.max = now;
  chart.options.scales.x.time.unit = rangeDays <= 1 ? 'hour' : rangeDays <= 14 ? 'day' : 'week';
  names.forEach((name, i) => {
    chart.data.datasets[i].data = (data[name] || []).map(r => ({ x: new Date(r.ts), y: r.bytes }));
  });
  chart.update();
}

async function loadStats() {
  const stats = await fetchJSON('/api/db-sizes/stats');
  const names = Object.keys(stats).sort();
  const now = new Date();
  const endOfYear = new Date(now.getFullYear(), 11, 31, 23, 59, 59);
  const secsToEOY = (endOfYear - now) / 1000;
  const MB = 1024 * 1024;

  let earliestDate = null;
  const rows = names.map(name => {
    const s = stats[name];
    const firstDate = new Date(s.first_ts);
    const lastDate  = new Date(s.last_ts);
    if (!earliestDate || firstDate < earliestDate) earliestDate = firstDate;

    const elapsedSecs = (lastDate - firstDate) / 1000;
    const growthBytes = s.last_bytes - s.first_bytes;

    if (elapsedSecs < 60 || growthBytes <= 0) {
      return `<tr>
        <td class="name">${name}</td>
        <td class="num">${fmtBytes(s.last_bytes)}</td>
        <td class="num" colspan="5" style="color:#7a90a8">not enough data yet</td>
      </tr>`;
    }

    const bps       = growthBytes / elapsedSecs;
    const mbPerHour = bps * 3600 / MB;
    const mbPerDay  = bps * 86400 / MB;
    const mbPerMonth = bps * 86400 * 30.44 / MB;
    const mbPerYear  = bps * 86400 * 365.25 / MB;
    const projBytes  = s.last_bytes + bps * secsToEOY;

    return `<tr>
      <td class="name">${name}</td>
      <td class="num">${fmtBytes(s.last_bytes)}</td>
      <td class="num">${fmtMB(mbPerHour)}</td>
      <td class="num">${fmtMB(mbPerDay)}</td>
      <td class="num">${fmtMB(mbPerMonth)}</td>
      <td class="num">${fmtMB(mbPerYear)}</td>
      <td class="num"><strong>${fmtBytes(projBytes)}</strong></td>
    </tr>`;
  });

  document.getElementById('proj-tbody').innerHTML = rows.join('') || '<tr><td colspan="7" style="color:#7a90a8;padding:.75rem">No data yet.</td></tr>';

  if (earliestDate) {
    const daysTracked = ((now - earliestDate) / 86400000).toFixed(1);
    document.getElementById('proj-note').textContent =
      `Rates calculated from ${earliestDate.toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'})} to now (${daysTracked} days of data). End-of-year projection is ${endOfYear.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}.`;
  }
}

load();
loadStats();
setInterval(load, 300000);
setInterval(loadStats, 300000);
</script>
</body>
</html>"""


@app.get("/chart/db-sizes")
def chart_db_sizes():
    return Response(_DB_SIZE_PAGE, mimetype="text/html")


# ---------------------------------------------------------------------------
# Camera motion zones
# ---------------------------------------------------------------------------

@app.get("/api/camera/snapshot/<name>")
def api_camera_snapshot(name):
    from smart_home import camera as _camera
    cameras = _camera.load_config()
    cam = next((c for c in cameras if c["name"] == name), None)
    if cam is None:
        return ("Camera not found", 404)
    jpeg, err = _camera.get_snapshot_jpeg(cam["url"], cam.get("snapshot_path", "/snapshot"))
    if jpeg is None:
        return (f"Could not grab frame: {err}", 502)
    return Response(jpeg, mimetype="image/jpeg")


@app.post("/api/camera/flip/<name>")
def api_camera_flip(name):
    import httpx
    from smart_home import camera as _camera
    cameras = _camera.load_config()
    cam = next((c for c in cameras if c["name"] == name), None)
    if cam is None:
        return ("Camera not found", 404)
    flipped = not cam.get("flipped", False)
    cam["flipped"] = flipped
    _camera.save_config(cameras)
    val = 1 if flipped else 0
    base = cam["url"].rstrip("/")
    try:
        httpx.get(f"{base}/control?var=vflip&val={val}", timeout=3)
        httpx.get(f"{base}/control?var=hmirror&val={val}", timeout=3)
    except Exception:
        pass
    return jsonify({"flipped": flipped})


@app.get("/api/camera/zones/<name>")
def api_camera_zones_get(name):
    from smart_home import camera as _camera
    cameras = _camera.load_config()
    cam = next((c for c in cameras if c["name"] == name), None)
    if cam is None:
        return jsonify([])
    return jsonify(cam.get("zones", []))


@app.post("/api/camera/zones/<name>")
def api_camera_zones_set(name):
    from smart_home import camera as _camera
    zones = request.get_json(silent=True)
    if not isinstance(zones, list):
        return ("Expected JSON array", 400)
    cameras = _camera.load_config()
    cam = next((c for c in cameras if c["name"] == name), None)
    if cam is None:
        return ("Camera not found", 404)
    cam["zones"] = zones
    _camera.save_config(cameras)
    return jsonify({"ok": True})


@app.get("/api/cameras")
def api_cameras():
    from smart_home import camera as _camera
    cameras = _camera.load_config()
    return jsonify([{"name": c["name"], "zones": c.get("zones", []), "flipped": c.get("flipped", False), "url": c.get("url", "")} for c in cameras])


@app.get("/api/camera/events/<name>")
def api_camera_events(name):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, zone, pct, screenshot IS NOT NULL AS has_image FROM camera_events WHERE camera=? ORDER BY ts DESC LIMIT 10",
            (name,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/camera/vitals/<name>")
def api_camera_vitals(name):
    days = request.args.get("days", 1, type=float)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, temp_c, wifi_rssi, free_heap_kb, uptime_s, psram_total_kb FROM camera_vitals WHERE camera=? AND ts >= datetime('now', 'localtime', ?) ORDER BY ts ASC",
            (name, f"-{days} days"),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/camera/events/<name>/<int:event_id>/image")
def api_camera_event_image(name, event_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT screenshot FROM camera_events WHERE id=? AND camera=?",
            (event_id, name),
        ).fetchone()
    if not row or not row["screenshot"]:
        return Response("No image", status=404)
    return Response(bytes(row["screenshot"]), mimetype="image/jpeg")


_CAMERA_VIEW_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cameras &mdash; Smart Home</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.2rem; display: flex; gap: 1.2rem; align-items: center; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .cam-tabs { display: flex; gap: .4rem; margin-bottom: 1rem; flex-wrap: wrap; }
    .cam-tab { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px;
               padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .cam-tab:hover { background: #f0f4f8; }
    .cam-tab.active { background: #e07820; color: #fff; border-color: #e07820; }
    .panel { background: #fff; border-radius: 12px; padding: 1.2rem;
             box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); margin-bottom: 1.2rem; }
    .feed-wrap { position: relative; }
    .feed-wrap img { display: block; max-width: 100%; border-radius: 6px; }
    .feed-actions { margin-top: .8rem; display: flex; gap: .8rem; align-items: center; }
    .btn { padding: .38rem 1.1rem; border-radius: 6px; border: 1px solid #d0dce8; background: #fff;
           color: #4a6080; font-size: .85rem; font-weight: 500; cursor: pointer; transition: all .15s;
           text-decoration: none; display: inline-block; }
    .btn:hover { background: #f0f4f8; }
    .btn.primary { background: #2e7dd4; color: #fff; border-color: #2e7dd4; }
    .btn.primary:hover { background: #2568b8; }
    .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #c0392b;
                display: inline-block; margin-right: .4rem; animation: blink 1.2s infinite; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
    .section-title { font-size: .75rem; color: #7a90a8; text-transform: uppercase;
                     letter-spacing: .07em; font-weight: 600; margin-bottom: .8rem; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th { text-align: left; color: #7a90a8; font-size: .72rem; text-transform: uppercase;
         letter-spacing: .06em; font-weight: 600; padding: .4rem .6rem; border-bottom: 1px solid #e8eef4; }
    td { padding: .5rem .6rem; border-bottom: 1px solid #f0f4f8; color: #4a6080; }
    td:first-child { color: #1a2535; }
    tr:last-child td { border-bottom: none; }
    tr.has-image { cursor: pointer; }
    tr.has-image:hover td { background: #f5f8fc; }
    .empty { color: #7a90a8; font-size: .85rem; }
    #no-cameras { color: #7a90a8; font-size: .9rem; }
    .img-icon { font-size: .7rem; color: #2e7dd4; margin-left: .3rem; }
    /* Modal */
    #modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7);
                     z-index: 1000; align-items: center; justify-content: center; }
    #modal-overlay.open { display: flex; }
    #modal-box { background: #fff; border-radius: 12px; padding: 1rem; max-width: 90vw;
                 max-height: 90vh; overflow: auto; box-shadow: 0 8px 32px rgba(0,0,0,.3); }
    #modal-box img { display: block; max-width: 100%; border-radius: 6px; }
    #modal-meta { font-size: .8rem; color: #7a90a8; margin-bottom: .6rem; }
    #modal-close { float: right; cursor: pointer; font-size: 1.2rem; color: #7a90a8;
                   line-height: 1; margin-left: 1rem; }
    #modal-close:hover { color: #1a2535; }
  </style>
</head>
<body>
  <div id="modal-overlay" onclick="closeModal(event)">
    <div id="modal-box">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.6rem">
        <div id="modal-meta"></div>
        <span id="modal-close" onclick="closeModal()">&times;</span>
      </div>
      <img id="modal-img" src="" alt="Motion screenshot">
    </div>
  </div>
  <h1>Cameras</h1>
  <div class="nav">
    <a href="/">&larr; Dashboard</a>
    <a href="/camera/zones" id="zones-link" style="display:none">Edit Zones &rarr;</a>
  </div>
  <div class="cam-tabs" id="cam-tabs"></div>
  <div id="no-cameras" style="display:none">
    No cameras configured. Run <code>smart-home configure-camera</code> on the server.
  </div>
  <div id="main" style="display:none">
    <div class="panel">
      <div class="feed-wrap">
        <img id="feed" src="" alt="Camera feed">
      </div>
      <div class="feed-actions">
        <span><span class="live-dot"></span>Live</span>
        <button class="btn" onclick="toggleLive()">Pause</button>
        <button class="btn" id="flip-btn" onclick="flipCam()">Flip 180°</button>
        <a class="btn" id="settings-link" href="#" target="_blank" rel="noopener">Camera Settings &rarr;</a>
      </div>
    </div>
    <div class="panel">
      <div class="section-title">Recent Motion Events</div>
      <div id="events-wrap"><p class="empty">Loading&hellip;</p></div>
    </div>
    <div class="panel" id="vitals-panel" style="display:none">
      <div class="section-title" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem">
        <span>Camera Vitals</span>
        <span id="vitals-range-btns" style="display:flex;gap:.4rem">
          <button onclick="loadVitals(0.125)" data-days="0.125" style="background:#fff;border:1px solid #d0dce8;border-radius:5px;padding:.2rem .6rem;cursor:pointer;font-size:.75rem;color:#4a6080">3h</button>
          <button onclick="loadVitals(1)" data-days="1" style="background:#2e7dd4;border:1px solid #2e7dd4;border-radius:5px;padding:.2rem .6rem;cursor:pointer;font-size:.75rem;color:#fff">24h</button>
          <button onclick="loadVitals(7)" data-days="7" style="background:#fff;border:1px solid #d0dce8;border-radius:5px;padding:.2rem .6rem;cursor:pointer;font-size:.75rem;color:#4a6080">7d</button>
        </span>
      </div>
      <div style="font-size:.72rem;color:#7a90a8;text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin-bottom:.3rem">Temperature (&deg;C)</div>
      <canvas id="chart-temp" height="80"></canvas>
      <div style="font-size:.72rem;color:#7a90a8;text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:.9rem 0 .3rem">WiFi Signal (dBm)</div>
      <canvas id="chart-rssi" height="80"></canvas>
      <div style="font-size:.72rem;color:#7a90a8;text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:.9rem 0 .3rem">Free Heap (KB)</div>
      <canvas id="chart-heap" height="80"></canvas>
      <div style="font-size:.72rem;color:#7a90a8;text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:.9rem 0 .3rem">Uptime (seconds)</div>
      <canvas id="chart-uptime" height="80"></canvas>
      <div style="font-size:.72rem;color:#7a90a8;text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin:.9rem 0 .3rem">PSRAM Total (KB)</div>
      <canvas id="chart-psram" height="80"></canvas>
    </div>
  </div>

<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
let cameras = [], activeCam = null, liveInterval = null, live = true;

function fmtDt(ts) {
  return new Date(ts).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'});
}

function toggleLive() {
  live = !live;
  document.querySelector(".feed-actions button").textContent = live ? "Pause" : "Resume";
  if (live) startLive(); else { clearInterval(liveInterval); liveInterval = null; }
}

function startLive() {
  clearInterval(liveInterval);
  refreshFrame();
  liveInterval = setInterval(refreshFrame, 2000);
}

function refreshFrame() {
  if (!activeCam) return;
  document.getElementById("feed").src =
    `/api/camera/snapshot/${encodeURIComponent(activeCam)}?t=${Date.now()}`;
}

async function flipCam() {
  if (!activeCam) return;
  const data = await fetchJSON(`/api/camera/flip/${encodeURIComponent(activeCam)}`, { method: "POST" });
  document.getElementById("flip-btn").textContent = data.flipped ? "Unflip" : "Flip 180°";
  refreshFrame();
}

async function loadEvents() {
  if (!activeCam) return;
  const data = await fetchJSON(`/api/camera/events/${encodeURIComponent(activeCam)}`);
  const wrap = document.getElementById("events-wrap");
  if (!data.length) {
    wrap.innerHTML = '<p class="empty">No motion events recorded yet.</p>';
    return;
  }
  const tbody = document.createElement("tbody");
  data.forEach(e => {
    const tr = document.createElement("tr");
    if (e.has_image) {
      tr.className = "has-image";
      tr.onclick = () => openModal(e);
    }
    tr.innerHTML = `
      <td>${fmtDt(e.ts)}</td>
      <td>${e.zone}${e.has_image ? '<span class="img-icon">&#128247;</span>' : ''}</td>
      <td>${e.pct != null ? e.pct.toFixed(1) + "%" : "—"}</td>`;
    tbody.appendChild(tr);
  });
  wrap.innerHTML = "";
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Time</th><th>Zone</th><th>Changed</th></tr></thead>";
  table.appendChild(tbody);
  wrap.appendChild(table);
}

function openModal(e) {
  document.getElementById("modal-meta").textContent = `${fmtDt(e.ts)} — ${e.zone}`;
  document.getElementById("modal-img").src =
    `/api/camera/events/${encodeURIComponent(activeCam)}/${e.id}/image`;
  document.getElementById("modal-overlay").classList.add("open");
}

function closeModal(evt) {
  if (evt && evt.target !== document.getElementById("modal-overlay") &&
      evt.target !== document.getElementById("modal-close")) return;
  document.getElementById("modal-overlay").classList.remove("open");
  document.getElementById("modal-img").src = "";
}

const vitalsCharts = {};
let vitalsRangeDays = 1;

function fmtUptime(s) {
  if (s == null) return "";
  if (s < 120)        return s + "s";
  if (s < 7200)       return Math.round(s / 60) + "m";
  if (s < 172800)     return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
}

function makeVitalsChart(id, color, yTickCb) {
  const yTicks = yTickCb ? { color: "#7a90a8", callback: yTickCb } : { color: "#7a90a8" };
  const yTooltip = yTickCb ? {
    plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmtUptime(ctx.parsed.y) } } }
  } : { plugins: { legend: { display: false } } };
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: [{ data: [], borderColor: color, backgroundColor: "transparent",
                         borderWidth: 1.5, pointRadius: 0, tension: 0 }] },
    options: {
      ...yTooltip,
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" },
             grid: { color: "#f0f4f8" }, ticks: { color: "#7a90a8", maxTicksLimit: 8 } },
        y: { grid: { color: "#f0f4f8" }, ticks: yTicks },
      },
    },
  });
}

async function loadVitals(days) {
  if (!activeCam) return;
  if (days !== undefined) {
    vitalsRangeDays = days;
    document.querySelectorAll("#vitals-range-btns button").forEach(b => {
      const active = parseFloat(b.dataset.days) === days;
      b.style.background = active ? "#2e7dd4" : "#fff";
      b.style.color = active ? "#fff" : "#4a6080";
      b.style.borderColor = active ? "#2e7dd4" : "#d0dce8";
    });
  }
  const data = await fetchJSON(`/api/camera/vitals/${encodeURIComponent(activeCam)}?days=${vitalsRangeDays}`);
  const panel = document.getElementById("vitals-panel");
  if (!data.length) { panel.style.display = "none"; return; }
  panel.style.display = "";
  if (!vitalsCharts.temp) {
    vitalsCharts.temp   = makeVitalsChart("chart-temp",   "#e07820");
    vitalsCharts.rssi   = makeVitalsChart("chart-rssi",   "#2e7dd4");
    vitalsCharts.heap   = makeVitalsChart("chart-heap",   "#2a9d6e");
    vitalsCharts.uptime = makeVitalsChart("chart-uptime", "#9b4dca", fmtUptime);
    vitalsCharts.psram  = makeVitalsChart("chart-psram",  "#c0392b");
  }
  const now = new Date();
  const minTime = new Date(now - vitalsRangeDays * 86400000);
  vitalsCharts.temp.data.datasets[0].data   = data.map(r => ({ x: new Date(r.ts.replace(' ', 'T')), y: r.temp_c }));
  vitalsCharts.rssi.data.datasets[0].data   = data.map(r => ({ x: new Date(r.ts.replace(' ', 'T')), y: r.wifi_rssi }));
  vitalsCharts.heap.data.datasets[0].data   = data.map(r => ({ x: new Date(r.ts.replace(' ', 'T')), y: r.free_heap_kb }));
  vitalsCharts.uptime.data.datasets[0].data = data.map(r => ({ x: new Date(r.ts.replace(' ', 'T')), y: r.uptime_s }));
  vitalsCharts.psram.data.datasets[0].data  = data.map(r => ({ x: new Date(r.ts.replace(' ', 'T')), y: r.psram_total_kb }));
  Object.values(vitalsCharts).forEach(c => {
    c.options.scales.x.min = minTime;
    c.options.scales.x.max = now;
    c.update();
  });
}

function switchCam(name) {
  activeCam = name;
  document.querySelectorAll(".cam-tab").forEach(b =>
    b.classList.toggle("active", b.dataset.cam === name));
  document.getElementById("zones-link").href = `/camera/zones?cam=${encodeURIComponent(name)}`;
  document.getElementById("main").style.display = "";
  live = true;
  document.querySelector(".feed-actions button").textContent = "Pause";
  const cam = cameras.find(c => c.name === name);
  document.getElementById("flip-btn").textContent = cam && cam.flipped ? "Unflip" : "Flip 180°";
  document.getElementById("settings-link").href = cam && cam.url ? cam.url + "/" : "#";
  startLive();
  loadEvents();
  loadVitals();
  setInterval(loadEvents, 30000);
  setInterval(loadVitals, 60000);
}

async function init() {
  const data = await fetchJSON("/api/cameras");
  cameras = data;
  if (!cameras.length) {
    document.getElementById("no-cameras").style.display = "";
    return;
  }
  document.getElementById("zones-link").style.display = "";
  const tabs = document.getElementById("cam-tabs");
  cameras.forEach(c => {
    const btn = document.createElement("button");
    btn.className = "cam-tab";
    btn.dataset.cam = c.name;
    btn.textContent = c.name;
    btn.onclick = () => switchCam(c.name);
    tabs.appendChild(btn);
  });
  switchCam(cameras[0].name);
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeModal();
});

init();
</script>
</body>
</html>"""


_CAMERA_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Camera Zones &mdash; Smart Home</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.2rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .cam-tabs { display: flex; gap: .4rem; margin-bottom: 1rem; flex-wrap: wrap; }
    .cam-tab { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px;
               padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .cam-tab:hover { background: #f0f4f8; }
    .cam-tab.active { background: #e07820; color: #fff; border-color: #e07820; }
    .editor { background: #fff; border-radius: 12px; padding: 1.2rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); margin-bottom: 1rem; }
    .canvas-wrap { position: relative; display: inline-block; max-width: 100%; }
    canvas { display: block; max-width: 100%; cursor: crosshair; border-radius: 6px; }
    .controls { margin-top: 1rem; display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }
    .btn { padding: .38rem 1.1rem; border-radius: 6px; border: 1px solid #d0dce8; background: #fff;
           color: #4a6080; font-size: .85rem; font-weight: 500; cursor: pointer; transition: all .15s; }
    .btn:hover { background: #f0f4f8; border-color: #aabbc8; }
    .btn.primary { background: #2e7dd4; color: #fff; border-color: #2e7dd4; }
    .btn.primary:hover { background: #2568b8; }
    .btn.danger { background: #c0392b; color: #fff; border-color: #c0392b; }
    .btn.danger:hover { background: #a93226; }
    .zones-list { margin-top: 1.2rem; }
    .zone-row { display: flex; align-items: center; gap: .6rem; padding: .5rem .8rem;
                background: #f8fafc; border-radius: 8px; margin-bottom: .4rem; font-size: .85rem; }
    .zone-color { width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }
    .zone-name { flex: 1; font-weight: 600; }
    .zone-sens { color: #7a90a8; font-size: .78rem; }
    .sens-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-right: .3rem; }
    .hint { font-size: .78rem; color: #7a90a8; margin-top: .5rem; }
    #status { font-size: .85rem; color: #2a9d6e; margin-top: .5rem; min-height: 1.2em; }
  </style>
</head>
<body>
  <h1>Camera Motion Zones</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div class="cam-tabs" id="cam-tabs"></div>
  <div id="no-cameras" style="display:none;color:#7a90a8;font-size:.9rem;">
    No cameras configured. Run <code>smart-home configure-camera</code> on the server.
  </div>
  <div class="editor" id="editor" style="display:none">
    <div class="canvas-wrap">
      <canvas id="canvas"></canvas>
    </div>
    <p class="hint" id="hint">Click on the frame to place polygon points. Click near the first point to close. Escape to cancel.</p>
    <div class="controls">
      <button class="btn" onclick="refreshFrame()">&#8635; Refresh Frame</button>
      <button class="btn" id="btn-cancel" onclick="cancelDraw()" style="display:none">Cancel</button>
      <button class="btn danger" id="btn-delete" onclick="deleteSelected()" disabled>Delete Zone</button>
      <span class="sens-label">Sensitivity</span>
      <input id="sens" type="range" min="1" max="30" value="5" style="width:100px">
      <span id="sens-val" style="font-size:.82rem;color:#4a6080">5%</span>
      <button class="btn primary" onclick="saveZones()">Save Zones</button>
    </div>
    <div id="status"></div>
    <div class="zones-list" id="zones-list"></div>
  </div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b","#16a085","#d35400","#8e44ad"];
let cameras = [], activeCam = null;
let zones = [], selectedIdx = -1;
// Polygon drawing state
let drawing = false, currentPoly = [], mousePos = null;
const CLOSE_RADIUS = 0.02; // normalized distance to snap-close

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const img = new Image();

img.onload = () => {
  canvas.width  = img.naturalWidth;
  canvas.height = img.naturalHeight;
  redraw();
};

document.getElementById("sens").oninput = function() {
  document.getElementById("sens-val").textContent = this.value + "%";
  if (selectedIdx >= 0) {
    zones[selectedIdx].sensitivity = parseFloat(this.value) / 100;
  }
};

function clientToCanvas(e) {
  const r = canvas.getBoundingClientRect();
  return [
    (e.clientX - r.left) / r.width,
    (e.clientY - r.top)  / r.height,
  ];
}

function dist(ax, ay, bx, by) {
  return Math.sqrt((ax - bx) ** 2 + (ay - by) ** 2);
}

function pointInPolygon(px, py, points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [xi, yi] = points[i], [xj, yj] = points[j];
    if ((yi > py) !== (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi)
      inside = !inside;
  }
  return inside;
}

canvas.addEventListener("mousemove", e => {
  mousePos = clientToCanvas(e);
  if (drawing) redraw();
});

canvas.addEventListener("mouseleave", () => { mousePos = null; });

canvas.addEventListener("click", e => {
  const [px, py] = clientToCanvas(e);

  if (drawing) {
    // Close polygon if near first point and have >= 3 points
    if (currentPoly.length >= 3 && dist(px, py, currentPoly[0][0], currentPoly[0][1]) < CLOSE_RADIUS) {
      finishPolygon();
      return;
    }
    currentPoly.push([px, py]);
    redraw();
    return;
  }

  // Check if clicking an existing zone
  for (let i = zones.length - 1; i >= 0; i--) {
    if (pointInPolygon(px, py, zones[i].points)) {
      selectZone(i);
      return;
    }
  }

  // Start new polygon
  drawing = true;
  currentPoly = [[px, py]];
  selectedIdx = -1;
  document.getElementById("btn-delete").disabled = true;
  document.getElementById("btn-cancel").style.display = "";
  document.getElementById("hint").textContent = "Keep clicking to add points. Click near the first point (shown in white) to close the polygon.";
  redraw();
});

document.addEventListener("keydown", e => {
  if (e.key === "Escape") cancelDraw();
  if ((e.key === "Enter" || e.key === "Return") && drawing && currentPoly.length >= 3) finishPolygon();
});

function cancelDraw() {
  drawing = false;
  currentPoly = [];
  document.getElementById("btn-cancel").style.display = "none";
  document.getElementById("hint").textContent = "Click on the frame to place polygon points. Click near the first point to close. Escape to cancel.";
  redraw();
}

function finishPolygon() {
  const name = prompt("Zone name:", `zone-${zones.length + 1}`);
  if (!name) { cancelDraw(); return; }
  const sens = parseFloat(document.getElementById("sens").value) / 100;
  zones.push({ name, points: currentPoly.slice(), sensitivity: sens });
  selectedIdx = zones.length - 1;
  document.getElementById("btn-delete").disabled = false;
  drawing = false;
  currentPoly = [];
  document.getElementById("btn-cancel").style.display = "none";
  document.getElementById("hint").textContent = "Click on the frame to place polygon points. Click near the first point to close. Escape to cancel.";
  renderZoneList();
  redraw();
}

function redraw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0);
  const cw = canvas.width, ch = canvas.height;
  const lw = Math.max(1.5, cw / canvas.getBoundingClientRect().width * 1.5);

  // Draw completed zones
  zones.forEach((z, i) => {
    if (!z.points || z.points.length < 2) return;
    const color = COLORS[i % COLORS.length];
    const sel = i === selectedIdx;
    ctx.beginPath();
    ctx.moveTo(z.points[0][0] * cw, z.points[0][1] * ch);
    for (let k = 1; k < z.points.length; k++)
      ctx.lineTo(z.points[k][0] * cw, z.points[k][1] * ch);
    ctx.closePath();
    ctx.fillStyle = color + (sel ? "55" : "33");
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = sel ? lw * 2 : lw;
    ctx.setLineDash([]);
    ctx.stroke();
    // Vertex dots
    z.points.forEach(([px, py]) => {
      ctx.beginPath();
      ctx.arc(px * cw, py * ch, sel ? lw * 2.5 : lw * 1.5, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    });
    // Label
    const cx = z.points.reduce((s, p) => s + p[0], 0) / z.points.length;
    const cy = z.points.reduce((s, p) => s + p[1], 0) / z.points.length;
    ctx.fillStyle = "#fff";
    ctx.font = `bold ${Math.max(12, lw * 5)}px system-ui`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(z.name, cx * cw, cy * ch);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  });

  // Draw in-progress polygon
  if (drawing && currentPoly.length > 0) {
    const color = COLORS[zones.length % COLORS.length];
    ctx.strokeStyle = color;
    ctx.lineWidth = lw;
    ctx.setLineDash([6 * lw, 3 * lw]);
    ctx.beginPath();
    ctx.moveTo(currentPoly[0][0] * cw, currentPoly[0][1] * ch);
    for (let k = 1; k < currentPoly.length; k++)
      ctx.lineTo(currentPoly[k][0] * cw, currentPoly[k][1] * ch);
    if (mousePos) ctx.lineTo(mousePos[0] * cw, mousePos[1] * ch);
    ctx.stroke();
    ctx.setLineDash([]);
    // Vertex dots; first one is white to show close target
    currentPoly.forEach(([px, py], k) => {
      ctx.beginPath();
      ctx.arc(px * cw, py * ch, lw * 2.5, 0, Math.PI * 2);
      ctx.fillStyle = k === 0 ? "#fff" : color;
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = lw;
      ctx.stroke();
    });
    // Snap indicator
    if (mousePos && currentPoly.length >= 3 &&
        dist(mousePos[0], mousePos[1], currentPoly[0][0], currentPoly[0][1]) < CLOSE_RADIUS) {
      ctx.beginPath();
      ctx.arc(currentPoly[0][0] * cw, currentPoly[0][1] * ch, lw * 5, 0, Math.PI * 2);
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = lw * 1.5;
      ctx.stroke();
    }
  }
}

function renderZoneList() {
  document.getElementById("zones-list").innerHTML = zones.map((z, i) => `
    <div class="zone-row" onclick="selectZone(${i})" style="cursor:pointer;outline:${i===selectedIdx?'2px solid '+COLORS[i%COLORS.length]:'none'}">
      <span class="zone-color" style="background:${COLORS[i % COLORS.length]}"></span>
      <span class="zone-name">${z.name}</span>
      <span class="zone-sens">sensitivity: ${Math.round((z.sensitivity||0.05)*100)}%  &middot;  ${(z.points||[]).length} points</span>
    </div>`).join("");
}

function selectZone(i) {
  if (drawing) return;
  selectedIdx = i;
  document.getElementById("btn-delete").disabled = false;
  document.getElementById("sens").value = Math.round((zones[i].sensitivity || 0.05) * 100);
  document.getElementById("sens-val").textContent = Math.round((zones[i].sensitivity || 0.05) * 100) + "%";
  renderZoneList();
  redraw();
}

function deleteSelected() {
  if (selectedIdx < 0) return;
  zones.splice(selectedIdx, 1);
  selectedIdx = -1;
  document.getElementById("btn-delete").disabled = true;
  renderZoneList();
  redraw();
}

async function refreshFrame() {
  img.src = `/api/camera/snapshot/${encodeURIComponent(activeCam)}?t=${Date.now()}`;
}

async function loadZones() {
  const data = await fetchJSON(`/api/camera/zones/${encodeURIComponent(activeCam)}`);
  zones = data;
  renderZoneList();
  redraw();
}

async function saveZones() {
  const r = await fetch(`/api/camera/zones/${encodeURIComponent(activeCam)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(zones),
  });
  const st = document.getElementById("status");
  if (r.ok) {
    st.textContent = "✓ Zones saved.";
    setTimeout(() => st.textContent = "", 3000);
  } else {
    st.style.color = "#c0392b";
    st.textContent = "Error saving zones.";
  }
}

function switchCam(name) {
  activeCam = name;
  document.querySelectorAll(".cam-tab").forEach(b => b.classList.toggle("active", b.dataset.cam === name));
  document.getElementById("editor").style.display = "";
  zones = []; selectedIdx = -1; drawing = false; currentPoly = [];
  document.getElementById("btn-delete").disabled = true;
  document.getElementById("btn-cancel").style.display = "none";
  refreshFrame();
  loadZones();
}

async function init() {
  const data = await fetchJSON("/api/cameras");
  cameras = data;
  const tabs = document.getElementById("cam-tabs");
  if (!cameras.length) {
    document.getElementById("no-cameras").style.display = "";
    return;
  }
  tabs.innerHTML = cameras.map(c =>
    `<button class="cam-tab" data-cam="${c.name}" onclick="switchCam('${c.name}')">${c.name}</button>`
  ).join("");
  switchCam(cameras[0].name);
}

init();
</script>
</body>
</html>"""


@app.get("/camera")
def camera_page():
    return Response(_CAMERA_VIEW_PAGE, mimetype="text/html")


@app.get("/camera/zones")
def camera_zones_page():
    return Response(_CAMERA_PAGE, mimetype="text/html")
# Garage door
# ---------------------------------------------------------------------------

@app.get("/api/garage")
def api_garage_list():
    from smart_home import garage as _garage
    return jsonify(_garage.load_config())


@app.get("/api/garage/<name>/status")
def api_garage_status(name):
    from smart_home import garage as _garage
    garages = _garage.load_config()
    g = next((x for x in garages if x["name"] == name), None)
    if g is None:
        return ("Not found", 404)
    try:
        status = _garage.get_status(g["ip"])
        with _conn() as conn:
            row_open = conn.execute(
                "SELECT ts FROM garage_events WHERE name=? AND state='open' ORDER BY ts DESC LIMIT 1",
                (name,),
            ).fetchone()
            row_closed = conn.execute(
                "SELECT ts FROM garage_events WHERE name=? AND state='closed' ORDER BY ts DESC LIMIT 1",
                (name,),
            ).fetchone()
        return jsonify({
            "ok": True,
            "output": status.get("output", False),
            "door_closed": status.get("door_closed"),
            "last_opened": row_open["ts"] if row_open else None,
            "last_closed": row_closed["ts"] if row_closed else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.get("/api/garage/<name>/events")
def api_garage_events(name):
    import datetime
    limit = min(int(request.args.get("limit", 200)), 1000)
    with _conn() as conn:
        # Fetch one extra row so the oldest visible event has a duration too
        rows = conn.execute(
            "SELECT ts, state FROM garage_events WHERE name=? ORDER BY ts DESC LIMIT ?",
            (name, limit + 1),
        ).fetchall()
    if not rows:
        return jsonify([])

    def fmt_duration(seconds):
        seconds = int(seconds)
        d, rem = divmod(seconds, 86400)
        h, rem = divmod(rem, 3600)
        m, s   = divmod(rem, 60)
        parts = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)

    result = []
    for i, r in enumerate(rows[:-1]):
        prev = rows[i + 1]
        t_current = datetime.datetime.fromisoformat(r["ts"])
        t_prev    = datetime.datetime.fromisoformat(prev["ts"])
        delta     = (t_current - t_prev).total_seconds()
        prev_state = "open" if r["state"] == "closed" else "closed"
        duration = f"{prev_state} for {fmt_duration(delta)}"
        result.append({"ts": r["ts"], "state": r["state"], "duration": duration})
    # oldest visible row has no prior event in this window
    if len(rows) <= limit:
        result.append({"ts": rows[-1]["ts"], "state": rows[-1]["state"], "duration": ""})
    return jsonify(result)


@app.post("/api/garage/<name>/auto")
def api_garage_auto(name):
    from smart_home import garage as _garage
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("auto", False))
    _garage.set_auto(name, enabled)
    return jsonify({"ok": True})


@app.post("/api/garage/<name>/presence-device")
def api_garage_set_presence_device(name):
    from smart_home import garage as _garage
    data = request.get_json(silent=True) or {}
    ble_name = data.get("ble_name") or None
    _garage.set_presence_device(name, ble_name)
    return jsonify({"ok": True})


@app.post("/api/garage/<name>/trigger")
def api_garage_trigger(name):
    from smart_home import garage as _garage
    garages = _garage.load_config()
    g = next((x for x in garages if x["name"] == name), None)
    if g is None:
        return ("Not found", 404)
    try:
        _garage.trigger(g["ip"], g.get("pulse_seconds", 0.5))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


_GARAGE_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Garage &mdash; Smart Home</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .doors { display: flex; gap: 1.2rem; flex-wrap: wrap; }
    .door-card { background: #fff; border-radius: 16px; padding: 1.8rem 2rem;
                 box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05);
                 display: flex; flex-direction: column; align-items: center; gap: 1rem;
                 min-width: 220px; transition: box-shadow .2s; }
    .door-name { font-size: 1rem; font-weight: 700; color: #1a2535; text-transform: uppercase;
                 letter-spacing: .06em; }
    .door-state { font-size: 1.4rem; font-weight: 800; letter-spacing: .04em; padding: .3rem .9rem;
                  border-radius: 8px; }
    .door-state.closed { color: #1a7a4a; background: #e8fdf0; }
    .door-state.open   { color: #c0392b; background: #fde8e8; }
    .door-state.unknown { color: #7a90a8; background: #f0f4f8; }
    .trigger-btn { background: #e07820; color: #fff; border: none; border-radius: 10px;
                   padding: .75rem 2rem; font-size: 1rem; font-weight: 700; cursor: pointer;
                   letter-spacing: .02em; transition: background .15s, transform .1s;
                   width: 100%; }
    .trigger-btn:hover { background: #c86a18; }
    .trigger-btn:active { transform: scale(.97); }
    .trigger-btn:disabled { background: #aabbc8; cursor: default; }
    .last-triggered { font-size: .75rem; color: #aabbc8; text-align: center; min-height: 1em; }
    .open-timer { font-size: .85rem; font-weight: 600; color: #c0392b; min-height: 1.2em; }
    .auto-label { display: flex; align-items: center; gap: .5rem; font-size: .8rem; color: #4a6080;
                  cursor: pointer; user-select: none; flex-wrap: wrap; }
    .auto-label input[type=checkbox] { width: 1rem; height: 1rem; cursor: pointer; accent-color: #2e7dd4; flex-shrink: 0; }
    .auto-label select { font-size: .8rem; color: #1a2535; border: 1px solid #d0dce8; border-radius: 6px; padding: .15rem .4rem; background: #f7fafc; cursor: pointer; }
    #no-garages { color: #7a90a8; font-size: .9rem; }
    .history { margin-top: 2rem; }
    .history h2 { font-size: 1rem; font-weight: 700; color: #1a2535; margin-bottom: .8rem;
                  letter-spacing: -.01em; }
    .history-table { width: 100%; border-collapse: collapse; font-size: .85rem; white-space: nowrap; }
    .history-table th { text-align: left; color: #7a90a8; font-weight: 600; padding: .3rem .6rem;
                        border-bottom: 1px solid #e0e8f0; }
    .history-table td { padding: .35rem .6rem; border-bottom: 1px solid #f0f4f8; color: #1a2535; }
    .history-table tr:last-child td { border-bottom: none; }
    .state-open   { color: #c0392b; font-weight: 700; }
    .state-closed { color: #1a7a4a; font-weight: 700; }
  </style>
</head>
<body>
  <h1>Garage Door</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div id="no-garages" style="display:none">
    No garage doors configured. Run <code>smart-home configure-garage</code> on the server.
  </div>
  <div class="doors" id="doors"></div>
  <div class="history" id="history" style="display:none">
    <h2>Event History</h2>
    <table class="history-table">
      <thead><tr><th>Door</th><th>State</th><th>Time</th><th>Duration</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>
<script>
function showNetworkError(msg) {
  let el = document.getElementById('_net_err');
  if (!el) {
    el = document.createElement('div');
    el.id = '_net_err';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b00;color:#fff;padding:8px 16px;z-index:9999;font-size:14px;text-align:center';
    document.body.prepend(el);
  }
  el.textContent = '\u26a0 Network error: ' + msg;
}
async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    return await r.json();
  } catch(e) {
    showNetworkError(e.message);
    throw e;
  }
}
const openSince = {};  // name -> Date when door first seen open

function fmtDuration(ms) {
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sc = s % 60;
  if (d > 0) return `${d}d ${h}h ${m}m ${sc}s`;
  if (h > 0) return `${h}h ${m}m ${sc}s`;
  if (m > 0) return `${m}m ${sc}s`;
  return `${sc}s`;
}

const closedSince = {};
function tickTimers() {
  const now = Date.now();
  for (const [name, since] of Object.entries(openSince)) {
    const el = document.getElementById(`timer-${name}`);
    if (el) el.textContent = "Open for " + fmtDuration(now - since);
  }
  for (const [name, since] of Object.entries(closedSince)) {
    const el = document.getElementById(`timer-${name}`);
    if (el) el.textContent = "Closed for " + fmtDuration(now - since);
  }
}
setInterval(tickTimers, 1000);

async function setAuto(name, enabled) {
  await fetch(`/api/garage/${encodeURIComponent(name)}/auto`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({auto: enabled}),
  });
}

async function setPresenceDevice(name, ble_name) {
  await fetch(`/api/garage/${encodeURIComponent(name)}/presence-device`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ble_name: ble_name || null}),
  });
}

async function trigger(name, btn, lastEl) {
  btn.disabled = true;
  btn.textContent = "Triggering…";
  try {
    const r = await fetch(`/api/garage/${encodeURIComponent(name)}/trigger`, { method: "POST" });
    const data = await r.json();
    if (data.ok) {
      lastEl.textContent = "Triggered at " + new Date().toLocaleTimeString();
      setTimeout(() => refreshStatus(name), 3000);
    } else {
      alert("Error: " + (data.error || "unknown"));
    }
  } catch(e) {
    alert("Network error: " + e);
  }
  btn.disabled = false;
  refreshStatus(name);
}

function applyStatus(name, data) {
  const stateEl = document.getElementById(`state-${name}`);
  const timerEl = document.getElementById(`timer-${name}`);
  const btnEl   = document.getElementById(`btn-${name}`);
  if (!data.ok) {
    stateEl.textContent = "⚠️ unreachable";
    stateEl.className = "door-state unknown";
    timerEl.textContent = "";
    delete openSince[name];
    return;
  }
  if (data.door_closed === true) {
    stateEl.textContent = "CLOSED";
    stateEl.className = "door-state closed";
    if (btnEl && !btnEl.disabled) btnEl.textContent = "Click to open";
    delete openSince[name];
    if (data.last_closed) {
      closedSince[name] = new Date(data.last_closed.replace(" ", "T")).getTime();
    } else if (!closedSince[name]) {
      closedSince[name] = Date.now();
    }
  } else if (data.door_closed === false) {
    stateEl.textContent = "OPEN";
    stateEl.className = "door-state open";
    if (btnEl && !btnEl.disabled) btnEl.textContent = "Click to close";
    delete closedSince[name];
    if (data.last_opened) {
      // Server timestamp is local time ("YYYY-MM-DD HH:MM:SS"); parse as local
      openSince[name] = new Date(data.last_opened.replace(" ", "T")).getTime();
    } else if (!openSince[name]) {
      openSince[name] = Date.now();
    }
  } else {
    stateEl.textContent = "UNKNOWN";
    stateEl.className = "door-state unknown";
    if (btnEl && !btnEl.disabled) btnEl.textContent = "Trigger";
    timerEl.textContent = "";
    delete openSince[name];
    delete closedSince[name];
  }
}

async function refreshStatus(name) {
  const data = await fetchJSON(`/api/garage/${encodeURIComponent(name)}/status`);
  applyStatus(name, data);
}

async function loadHistory(garages) {
  const allEvents = [];
  for (const g of garages) {
    const evts = await fetchJSON(`/api/garage/${encodeURIComponent(g.name)}/events`);
    for (const e of evts) allEvents.push({name: g.name, ...e});
  }
  allEvents.sort((a, b) => b.ts.localeCompare(a.ts));
  if (!allEvents.length) return;
  document.getElementById("history").style.display = "";
  document.getElementById("history-body").innerHTML = allEvents.map(e => `
    <tr>
      <td>${e.name}</td>
      <td class="state-${e.state}">${e.state === 'open' ? 'OPENED' : 'CLOSED'}</td>
      <td>${e.ts}</td>
      <td style="color:#7a90a8">${e.duration || ""}</td>
    </tr>`).join("");
}

async function load() {
  const [garages, presenceDevices] = await Promise.all([
    fetchJSON("/api/garage"),
    fetchJSON("/api/presence"),
  ]);
  const el = document.getElementById("doors");
  if (!garages.length) {
    document.getElementById("no-garages").style.display = "";
    return;
  }
  const deviceOptions = [
    `<option value="">any device</option>`,
    ...presenceDevices.map(d =>
      `<option value="${d.ble_name}">${d.name}</option>`
    ),
  ].join("");
  el.innerHTML = garages.map(g => `
    <div class="door-card">
      <div class="door-name">${g.name}</div>
      <div class="door-state unknown" id="state-${g.name}">…</div>
      <div class="open-timer" id="timer-${g.name}"></div>
      <button class="trigger-btn" id="btn-${g.name}"
        onclick="trigger('${g.name}', this, document.getElementById('last-${g.name}'))">…</button>
      <div class="last-triggered" id="last-${g.name}"></div>
      <label class="auto-label">
        <input type="checkbox" id="auto-${g.name}"
          onchange="setAuto('${g.name}', this.checked)">
        Automatically open/close when
        <select id="presence-${g.name}"
          onchange="setPresenceDevice('${g.name}', this.value)">${deviceOptions}</select>
        is detected to arrive/depart
      </label>
    </div>`).join("");

  for (const g of garages) {
    const autoEl = document.getElementById(`auto-${g.name}`);
    if (autoEl) autoEl.checked = !!g.auto;
    const presEl = document.getElementById(`presence-${g.name}`);
    if (presEl) presEl.value = g.presence_device || "";
    refreshStatus(g.name);
  }
  loadHistory(garages);
}

load();
setInterval(() => fetchJSON("/api/garage").then(gs => gs.forEach(g => refreshStatus(g.name))), 10000);
</script>
</body>
</html>"""


@app.get("/garage")
def garage_page():
    return Response(_GARAGE_PAGE, mimetype="text/html")


# Water chemistry
# ---------------------------------------------------------------------------

@app.get("/api/pool/events")
def api_pool_events():
    """Recent offline/online events for water chemistry devices."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, ts, event_type, value, details
            FROM temperature_events
            WHERE (event_type = 'sensor_offline' OR event_type = 'sensor_online')
              AND details IN (
                  SELECT DISTINCT label FROM pool_readings
              )
            ORDER BY ts DESC
            LIMIT 50
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/pool/current")
def api_pool_current():
    """Latest reading for each water chemistry device."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT label, address, temp_c, ph, ec, tds, orp, chlorine, battery, rssi, ts
            FROM pool_readings
            WHERE id IN (
                SELECT MAX(id) FROM pool_readings GROUP BY label
            )
            ORDER BY label
        """).fetchall()
    now = datetime.datetime.now()
    result = []
    for r in rows:
        d = dict(r)
        d["temp_f"] = round(d["temp_c"] * 9 / 5 + 32, 1) if d["temp_c"] is not None else None
        try:
            age = (now - datetime.datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            d["offline"] = age > 600
        except (ValueError, TypeError):
            d["offline"] = True
        result.append(d)
    return jsonify(result)


@app.get("/api/pool/history")
def api_pool_history():
    """Pool readings history. Query params: label, start, end, limit, bucket_minutes."""
    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None
    try:
        limit = min(int(request.args.get("limit", 2000)), 100000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    try:
        bucket = max(1, int(request.args.get("bucket_minutes", 1)))
    except ValueError:
        return jsonify({"error": "bucket_minutes must be an integer"}), 400

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
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    if bucket > 1:
        bucket_secs = bucket * 60
        params.append(limit)
        sql = f"""
            SELECT
                strftime('%Y-%m-%d %H:%M:%S',
                    CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs},
                    'unixepoch') AS ts,
                label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                ROUND(AVG(ph), 2)       AS ph,
                ROUND(AVG(ec), 0)       AS ec,
                ROUND(AVG(tds), 0)      AS tds,
                ROUND(AVG(orp), 0)      AS orp,
                ROUND(AVG(chlorine), 2) AS chlorine,
                ROUND(AVG(battery), 0)  AS battery
            FROM pool_readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, label
            ORDER BY ts ASC LIMIT ?
        """
        with _conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return jsonify([dict(r) for r in rows])
    else:
        params.append(limit)
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT ts, label, temp_c, ph, ec, tds, orp, chlorine, battery "
                f"FROM pool_readings{where_sql} ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["temp_f"] = round(d["temp_c"] * 9 / 5 + 32, 1) if d["temp_c"] is not None else None
            result.append(d)
        return jsonify(list(reversed(result)))


@app.get("/api/pool/history/years")
def api_pool_history_years():
    """Distinct years present in pool_readings."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT strftime('%Y', ts) AS year FROM pool_readings ORDER BY year DESC"
        ).fetchall()
    return jsonify([r["year"] for r in rows])


@app.get("/api/pool/history/month")
def api_pool_history_month():
    """Pool readings for a given calendar month across all years, ts normalised to year 2000.
    Query params: label, month (1-12), bucket_minutes (default 60).
    """
    import datetime as _dt
    label  = request.args.get("label")
    month  = max(1, min(12, request.args.get("month", 1, type=int)))
    bucket_minutes = max(1, request.args.get("bucket_minutes", 60, type=int))
    bucket_secs    = bucket_minutes * 60
    month_str = f"{month:02d}"

    where_extra = "AND label = ?" if label else ""
    params = [bucket_secs, bucket_secs, month_str]
    if label:
        params.append(label)

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                ROUND(AVG(ph), 2)       AS ph,
                ROUND(AVG(ec), 0)       AS ec,
                ROUND(AVG(tds), 0)      AS tds,
                ROUND(AVG(orp), 0)      AS orp,
                ROUND(AVG(chlorine), 2) AS chlorine,
                ROUND(AVG(battery), 0)  AS battery
            FROM pool_readings
            WHERE strftime('%m', ts) = ? {where_extra}
            GROUP BY bucket, label, year
            ORDER BY bucket ASC
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({
            "year": d["year"], "ts": ts, "label": d["label"],
            "temp_f": d["temp_f"], "ph": d["ph"], "ec": d["ec"],
            "tds": d["tds"], "orp": d["orp"], "chlorine": d["chlorine"], "battery": d["battery"],
        })
    return jsonify(result)


@app.get("/api/pool/history/year")
def api_pool_history_year():
    """Pool readings across all months, ts normalised to year 2000 for overlay.
    Query params: label, bucket_minutes (default 360).
    """
    import datetime as _dt
    label  = request.args.get("label")
    bucket_minutes = max(1, request.args.get("bucket_minutes", 360, type=int))
    bucket_secs    = bucket_minutes * 60

    where_extra = "AND label = ?" if label else ""
    params = [bucket_secs, bucket_secs]
    if label:
        params.append(label)

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                ROUND(AVG(ph), 2)       AS ph,
                ROUND(AVG(ec), 0)       AS ec,
                ROUND(AVG(tds), 0)      AS tds,
                ROUND(AVG(orp), 0)      AS orp,
                ROUND(AVG(chlorine), 2) AS chlorine,
                ROUND(AVG(battery), 0)  AS battery
            FROM pool_readings
            WHERE 1=1 {where_extra}
            GROUP BY bucket, label, year
            ORDER BY bucket ASC
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({
            "year": d["year"], "ts": ts, "label": d["label"],
            "temp_f": d["temp_f"], "ph": d["ph"], "ec": d["ec"],
            "tds": d["tds"], "orp": d["orp"], "chlorine": d["chlorine"], "battery": d["battery"],
        })
    return jsonify(result)


@app.get("/api/pool/recent")
def api_pool_recent():
    """Most recent pool readings for a zone, newest first."""
    zone = request.args.get("zone", "").strip()
    try:
        limit = min(int(request.args.get("limit", 50)), 500)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    if not zone:
        return jsonify({"error": "zone required"}), 400
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT ts, temp_c, ph, ec, tds, orp, chlorine, battery
            FROM pool_readings
            WHERE zone = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (zone, limit),
        ).fetchall()
    def to_f(c): return round(c * 9 / 5 + 32, 1) if c is not None else None
    return jsonify([{
        "ts": r["ts"],
        "temp_f": to_f(r["temp_c"]),
        "ph": r["ph"],
        "ec": r["ec"],
        "tds": r["tds"],
        "orp": r["orp"],
        "chlorine": r["chlorine"],
        "battery": r["battery"],
    } for r in rows])


@app.get("/api/pool/node")
def api_pool_node_get():
    """Return node assignment and available options for each water chemistry device."""
    from smart_home import pool as _pool
    monitors = _pool.load_config()
    with _conn() as conn:
        relay_ids = [r[0] for r in conn.execute(
            "SELECT relay_id FROM relay_checkin ORDER BY ts DESC"
        ).fetchall()]
    options = [{"id": "server"}] + [{"id": rid} for rid in relay_ids]
    result = {}
    for m in monitors:
        lbl = m.get("label") or m.get("address", "")
        result[lbl] = {
            "node": m.get("node", "server"),
            "poll_interval_s": m.get("poll_interval_s", 60),
            "relay_options": options,
            "current_zone": m.get("current_zone"),
        }
    return jsonify(result)


@app.post("/api/pool/node")
def api_pool_node_set():
    """Set the node for a water chemistry device."""
    from smart_home import pool as _pool
    from smart_home import relay as _relay
    data = request.get_json(silent=True) or {}
    label = data.get("label")
    node = data.get("node")
    if not label or not node:
        return jsonify({"error": "label and node required"}), 400
    valid = {"server"} | {r["id"] for r in _relay.load_relays()}
    if node not in valid:
        return jsonify({"error": f"unknown node: {node}"}), 400
    if not _pool.set_node(label, node):
        return jsonify({"error": f"BLE-YC01 device '{label}' not found"}), 404
    return jsonify({"ok": True, "label": label, "node": node})


@app.post("/api/pool/poll-rate")
def api_pool_poll_rate_set():
    """Set the poll interval for a water chemistry device."""
    from smart_home import pool as _pool
    data = request.get_json(silent=True) or {}
    label = data.get("label")
    interval_s = data.get("interval_s")
    valid = {30, 60}
    if not label or interval_s not in valid:
        return jsonify({"error": "label and valid interval_s (30/60) required"}), 400
    if not _pool.set_poll_interval(label, interval_s):
        return jsonify({"error": f"BLE-YC01 device '{label}' not found"}), 404
    return jsonify({"ok": True, "label": label, "interval_s": interval_s})


@app.post("/api/pool/relay-reading")
def api_pool_relay_reading():
    """Receive a pool reading POSTed directly by a relay in persistent water-chemistry mode."""
    from smart_home import relay as _relay
    from smart_home import pool as _pool
    from smart_home.db import insert_pool_reading

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing or invalid Authorization header"}), 401
    token = auth[len("Bearer "):]
    relay_cfg = _relay.find_relay_by_token(token)
    if relay_cfg is None:
        return jsonify({"error": "unknown token"}), 401

    data = request.get_json(silent=True) or {}
    address = (data.get("address") or "").upper()
    label = data.get("label") or ""
    result_hex = data.get("result_hex") or ""
    rssi = data.get("rssi")

    # Relay is reporting that the water chemistry device is offline (no reading available).
    if data.get("offline"):
        import json as _json
        with _conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO relay_checkin (relay_id, ts) "
                "VALUES (?, strftime('%Y-%m-%d %H:%M:%S','now'))",
                (relay_cfg["id"],),
            )
            conn.execute(
                "INSERT INTO relay_log "
                "(ts, relay_id, batch_ts, n_adverts, n_inserted, presence_json, labeled_json, rev) "
                "VALUES (strftime('%Y-%m-%d %H:%M:%S','now'), ?, NULL, 0, 0, NULL, NULL, NULL)",
                (relay_cfg["id"],),
            )
            conn.execute(
                "DELETE FROM relay_log WHERE n_adverts >= 0 AND ("
                "  (labeled_json NOT LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-10 minutes'))"
                "  OR (labeled_json LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-60 minutes'))"
                ")"
            )
        monitors = _pool.load_config()
        assigned = next(
            (m for m in monitors if m.get("node") == relay_cfg["id"]), None
        )
        return jsonify({
            "ok": True,
            "ble_yc01": {
                "address": assigned["address"],
                "label": assigned.get("label", assigned["address"]),
            } if assigned else None,
        })

    if not result_hex:
        return jsonify({"error": "result_hex required"}), 400

    try:
        raw = bytes.fromhex(result_hex)
    except ValueError:
        return jsonify({"error": "invalid result_hex"}), 400

    import json as _json
    reading = _pool.parse_gatt_data(raw)
    if reading:
        reading.address = address
        reading.label = label
        reading.rssi = rssi
        with _conn() as conn:
            insert_pool_reading(conn, reading, zone=_pool.get_device_zone(label))
            conn.execute(
                "INSERT OR REPLACE INTO relay_checkin (relay_id, ts) "
                "VALUES (?, strftime('%Y-%m-%d %H:%M:%S','now'))",
                (relay_cfg["id"],),
            )
            labeled_json = _json.dumps({label: rssi}) if rssi is not None else None
            conn.execute(
                "INSERT INTO relay_log "
                "(ts, relay_id, batch_ts, n_adverts, n_inserted, presence_json, labeled_json, rev) "
                "VALUES (strftime('%Y-%m-%d %H:%M:%S','now'), ?, NULL, 0, 1, NULL, ?, NULL)",
                (relay_cfg["id"], labeled_json),
            )
            conn.execute(
                "DELETE FROM relay_log WHERE n_adverts >= 0 AND ("
                "  (labeled_json NOT LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-10 minutes'))"
                "  OR (labeled_json LIKE '%\"_buffered\": true%' AND datetime(ts) < datetime('now', '-60 minutes'))"
                ")"
            )

    # Return current BLE-YC01 assignment so the relay knows if it should stop.
    monitors = _pool.load_config()
    assigned = next(
        (m for m in monitors if m.get("node") == relay_cfg["id"]), None
    )
    return jsonify({
        "ok": True,
        "ble_yc01": {
            "address": assigned["address"],
            "label": assigned.get("label", assigned["address"]),
            "poll_skip_cycles": max(0, assigned.get("poll_interval_s", 60) // 30 - 1),
        } if assigned else None,
    })


@app.get("/api/water-chemistry/zones")
def api_wc_zones_get():
    """List all water chemistry zones."""
    with _conn() as conn:
        rows = conn.execute("SELECT id, name, mode, zone_type, created_at FROM wc_zones ORDER BY id ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/water-chemistry/zones")
def api_wc_zones_create():
    """Create a new water chemistry zone."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    zone_type = (data.get("zone_type") or "").strip() or None
    valid_types = {"running_water", "pooling_water", "indoor_room", "outdoor_shade", "outdoor_sun", "unclassified"}
    if zone_type and zone_type not in valid_types:
        return jsonify({"error": f"invalid zone_type '{zone_type}'"}), 400
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        with _conn() as conn:
            conn.execute("INSERT INTO wc_zones (name, zone_type) VALUES (?, ?)", (name, zone_type))
            conn.commit()
            row = conn.execute("SELECT id, name, mode, zone_type, created_at FROM wc_zones WHERE name=?", (name,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": f"zone '{name}' already exists"}), 409
        return jsonify({"error": str(e)}), 500


@app.patch("/api/water-chemistry/zones/<int:zone_id>")
def api_wc_zones_update(zone_id):
    """Update name and/or zone_type for a zone. Cascades name change to pool_readings."""
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or "").strip() or None
    zone_type = data.get("zone_type", "__unset__")
    valid_types = {"running_water", "pooling_water", "indoor_room", "outdoor_shade", "outdoor_sun", "unclassified", None}
    if zone_type != "__unset__" and zone_type not in valid_types:
        return jsonify({"error": f"invalid zone_type '{zone_type}'"}), 400
    with _conn() as conn:
        row = conn.execute("SELECT id, name, mode, zone_type FROM wc_zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            return jsonify({"error": "zone not found"}), 404
        old_name = row["name"]
        updates, params = [], []
        if new_name and new_name != old_name:
            updates.append("name=?"); params.append(new_name)
        if zone_type != "__unset__":
            updates.append("zone_type=?"); params.append(zone_type)
        if updates:
            params.append(zone_id)
            try:
                conn.execute(f"UPDATE wc_zones SET {', '.join(updates)} WHERE id=?", params)
            except Exception as e:
                if "UNIQUE" in str(e):
                    return jsonify({"error": f"zone '{new_name}' already exists"}), 409
                return jsonify({"error": str(e)}), 500
            if new_name and new_name != old_name:
                conn.execute("UPDATE pool_readings SET zone=? WHERE zone=?", (new_name, old_name))
            conn.commit()
        row = conn.execute("SELECT id, name, mode, zone_type, created_at FROM wc_zones WHERE id=?", (zone_id,)).fetchone()
    return jsonify(dict(row))


@app.delete("/api/water-chemistry/zones/<int:zone_id>")
def api_wc_zones_delete(zone_id):
    """Delete a water chemistry zone by ID. ?purge=true also deletes all readings for that zone."""
    purge = request.args.get("purge") == "true"
    with _conn() as conn:
        row = conn.execute("SELECT name FROM wc_zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            return jsonify({"error": "zone not found"}), 404
        zone_name = row["name"]
        conn.execute("DELETE FROM wc_zones WHERE id=?", (zone_id,))
        if purge:
            conn.execute("DELETE FROM pool_readings WHERE zone=?", (zone_name,))
        conn.commit()
    return jsonify({"ok": True, "id": zone_id, "purged": purge})


@app.get("/api/water-chemistry/zone-list")
def api_wc_zone_list():
    """Zones typed running_water or pooling_water that have at least one reading."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT name FROM wc_zones
            WHERE zone_type IN ('running_water', 'pooling_water')
            ORDER BY name
            """
        ).fetchall()
        has_unzoned = conn.execute(
            "SELECT 1 FROM pool_readings WHERE zone IS NULL LIMIT 1"
        ).fetchone() is not None
        online_rows = conn.execute(
            """
            SELECT DISTINCT zone FROM pool_readings
            WHERE zone IS NOT NULL
              AND ts >= datetime('now', '-600 seconds')
            """
        ).fetchall()
        online_zones = {r[0] for r in online_rows}
    return jsonify({"zones": [r[0] for r in rows], "has_unzoned": has_unzoned, "online_zones": list(online_zones)})



@app.post("/api/water-chemistry/zones/<int:zone_id>/mode")
def api_wc_zone_set_mode(zone_id):
    """Set the measurement mode for a zone."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").strip()
    if mode not in ("continuous", "one_time"):
        return jsonify({"error": "mode must be 'continuous' or 'one_time'"}), 400
    with _conn() as conn:
        row = conn.execute("SELECT id FROM wc_zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            return jsonify({"error": "zone not found"}), 404
        conn.execute("UPDATE wc_zones SET mode=? WHERE id=?", (mode, zone_id))
        conn.commit()
    return jsonify({"ok": True, "id": zone_id, "mode": mode})


@app.post("/api/water-chemistry/move")
def api_wc_move():
    """Assign a device to a zone (or clear zone assignment)."""
    from smart_home import pool as _pool
    data = request.get_json(silent=True) or {}
    label = data.get("label")
    zone_name = data.get("zone")  # None/null to clear
    if not label:
        return jsonify({"error": "label required"}), 400
    if not _pool.set_device_zone(label, zone_name):
        return jsonify({"error": f"device '{label}' not found"}), 404
    return jsonify({"ok": True, "label": label, "zone": zone_name})


@app.get("/api/water-chemistry/current")
def api_wc_current():
    """Latest reading for each water chemistry device, with current zone from config."""
    from smart_home import pool as _pool
    OFFLINE_THRESHOLD = 600
    now = datetime.datetime.now()
    monitors = _pool.load_config()

    with _conn() as conn:
        rows = conn.execute("""
            SELECT label, address, temp_c, ph, ec, tds, orp, chlorine, battery, rssi, ts
            FROM pool_readings
            WHERE id IN (
                SELECT MAX(id) FROM pool_readings GROUP BY address
            )
            ORDER BY label
        """).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["temp_f"] = round(d["temp_c"] * 9 / 5 + 32, 1) if d["temp_c"] is not None else None
            try:
                last_ts = datetime.datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")
                age = (now - last_ts).total_seconds()
                d["offline"] = age > OFFLINE_THRESHOLD
            except (ValueError, TypeError):
                d["offline"] = True
                last_ts = None

            # Find start of current streak (online or offline) by walking back through readings
            streak_start = d["ts"]
            if last_ts and d["address"]:
                recent = conn.execute(
                    "SELECT ts FROM pool_readings WHERE address = ? ORDER BY ts DESC LIMIT 500",
                    (d["address"],)
                ).fetchall()
                prev = last_ts
                for rec in recent[1:]:
                    ts = datetime.datetime.strptime(rec["ts"], "%Y-%m-%d %H:%M:%S")
                    gap = (prev - ts).total_seconds()
                    if d["offline"]:
                        # Offline streak: stop when we find a reading within threshold of the next
                        if gap <= OFFLINE_THRESHOLD:
                            break
                    else:
                        # Online streak: stop when we find a gap (device was offline before)
                        if gap > OFFLINE_THRESHOLD:
                            break
                    streak_start = rec["ts"]
                    prev = ts
            d["streak_start"] = streak_start

            addr_upper = (d["address"] or "").upper()
            zone = None
            for m in monitors:
                if m.get("label") == d["label"] or (addr_upper and m.get("address", "").upper() == addr_upper):
                    zone = m.get("current_zone")
                    break
            d["current_zone"] = zone
            result.append(d)
    return jsonify(result)


@app.get("/api/water-chemistry/history")
def api_wc_history():
    """Water chemistry history. Params: zone, label, start, end, limit, bucket_minutes.
    zone='__unzoned__' filters to rows where zone IS NULL."""
    zone_filter = request.args.get("zone")
    label = request.args.get("label")
    start = (request.args.get("start") or "").replace("T", " ") or None
    end   = (request.args.get("end")   or "").replace("T", " ") or None
    try:
        limit = min(int(request.args.get("limit", 2000)), 100000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    try:
        bucket = max(1, int(request.args.get("bucket_minutes", 1)))
    except ValueError:
        return jsonify({"error": "bucket_minutes must be an integer"}), 400

    where, params = [], []
    if zone_filter == "__unzoned__":
        where.append("zone IS NULL")
    elif zone_filter is not None:
        where.append("zone = ?")
        params.append(zone_filter)
    if label:
        where.append("label = ?")
        params.append(label)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    if bucket > 1:
        bucket_secs = bucket * 60
        params.append(limit)
        sql = f"""
            SELECT
                strftime('%Y-%m-%d %H:%M:%S',
                    CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs},
                    'unixepoch') AS ts,
                zone, label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                ROUND(AVG(ph), 2)       AS ph,
                ROUND(AVG(ec), 0)       AS ec,
                ROUND(AVG(tds), 0)      AS tds,
                ROUND(AVG(orp), 0)      AS orp,
                ROUND(AVG(chlorine), 2) AS chlorine,
                ROUND(AVG(battery), 0)  AS battery
            FROM pool_readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, zone, label
            ORDER BY ts ASC LIMIT ?
        """
        with _conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return jsonify([dict(r) for r in rows])
    else:
        params.append(limit)
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT ts, zone, label, temp_c, ph, ec, tds, orp, chlorine, battery "
                f"FROM pool_readings{where_sql} ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["temp_f"] = round(d["temp_c"] * 9 / 5 + 32, 1) if d["temp_c"] is not None else None
            result.append(d)
        return jsonify(list(reversed(result)))


@app.get("/api/water-chemistry/history/month")
def api_wc_history_month():
    """Water chemistry readings for a given calendar month across all years, ts normalised to year 2000."""
    import datetime as _dt
    zone_filter = request.args.get("zone")
    label  = request.args.get("label")
    month  = max(1, min(12, request.args.get("month", 1, type=int)))
    bucket_minutes = max(1, request.args.get("bucket_minutes", 60, type=int))
    bucket_secs    = bucket_minutes * 60
    month_str = f"{month:02d}"

    where_extra_parts = [f"strftime('%m', ts) = ?"]
    params = [bucket_secs, bucket_secs, month_str]
    if zone_filter == "__unzoned__":
        where_extra_parts.append("zone IS NULL")
    elif zone_filter is not None:
        where_extra_parts.append("zone = ?")
        params.append(zone_filter)
    if label:
        where_extra_parts.append("label = ?")
        params.append(label)
    where_extra = "AND " + " AND ".join(where_extra_parts[1:]) if len(where_extra_parts) > 1 else ""

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                zone, label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                ROUND(AVG(ph), 2)       AS ph,
                ROUND(AVG(ec), 0)       AS ec,
                ROUND(AVG(tds), 0)      AS tds,
                ROUND(AVG(orp), 0)      AS orp,
                ROUND(AVG(chlorine), 2) AS chlorine,
                ROUND(AVG(battery), 0)  AS battery
            FROM pool_readings
            WHERE strftime('%m', ts) = ? {where_extra}
            GROUP BY bucket, zone, label, year
            ORDER BY bucket ASC
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({
            "year": d["year"], "ts": ts, "zone": d["zone"], "label": d["label"],
            "temp_f": d["temp_f"], "ph": d["ph"], "ec": d["ec"],
            "tds": d["tds"], "orp": d["orp"], "chlorine": d["chlorine"], "battery": d["battery"],
        })
    return jsonify(result)


@app.get("/api/water-chemistry/history/year")
def api_wc_history_year():
    """Water chemistry readings across all months, ts normalised to year 2000 for overlay."""
    import datetime as _dt
    zone_filter = request.args.get("zone")
    label  = request.args.get("label")
    bucket_minutes = max(1, request.args.get("bucket_minutes", 360, type=int))
    bucket_secs    = bucket_minutes * 60

    where_parts = []
    params = [bucket_secs, bucket_secs]
    if zone_filter == "__unzoned__":
        where_parts.append("zone IS NULL")
    elif zone_filter is not None:
        where_parts.append("zone = ?")
        params.append(zone_filter)
    if label:
        where_parts.append("label = ?")
        params.append(label)
    where_extra = ("AND " + " AND ".join(where_parts)) if where_parts else ""

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT
                strftime('%Y', ts) AS year,
                CAST(strftime('%s', '2000' || substr(ts, 5)) AS INTEGER) / ? * ? AS bucket,
                zone, label,
                ROUND(AVG(CASE WHEN temp_c IS NOT NULL THEN temp_c * 9.0/5.0 + 32 END), 2) AS temp_f,
                ROUND(AVG(ph), 2)       AS ph,
                ROUND(AVG(ec), 0)       AS ec,
                ROUND(AVG(tds), 0)      AS tds,
                ROUND(AVG(orp), 0)      AS orp,
                ROUND(AVG(chlorine), 2) AS chlorine,
                ROUND(AVG(battery), 0)  AS battery
            FROM pool_readings
            WHERE 1=1 {where_extra}
            GROUP BY bucket, zone, label, year
            ORDER BY bucket ASC
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        ts = _dt.datetime.utcfromtimestamp(d["bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        result.append({
            "year": d["year"], "ts": ts, "zone": d["zone"], "label": d["label"],
            "temp_f": d["temp_f"], "ph": d["ph"], "ec": d["ec"],
            "tds": d["tds"], "orp": d["orp"], "chlorine": d["chlorine"], "battery": d["battery"],
        })
    return jsonify(result)


_ZONES_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zones</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .back { font-size: .85rem; font-weight: 500; color: #2e7dd4; text-decoration: none; margin-left: .75rem; }
    #error-bar { display:none; background:#fde8e8; color:#c0392b; border-radius:8px; padding:.6rem 1rem; margin-bottom:1rem; font-size:.85rem; font-weight:500; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); margin-bottom: 1.5rem; overflow: hidden; }
    .card-header { font-size: .75rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: #7a90a8; padding: .75rem 1.25rem; background: #f8fafc; border-bottom: 1px solid #e8edf3; }
    .zone-row { display: flex; align-items: center; gap: .75rem; padding: .75rem 1.25rem; border-bottom: 1px solid #f0f4f8; }
    .zone-row:last-child { border-bottom: none; }
    .zone-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
    .zone-name { flex: 1; font-size: .95rem; font-weight: 600; color: #1a2535; }
    .zone-type-badge { font-size: .72rem; font-weight: 600; color: #5a6e84; background: #eef2f7; border-radius: 6px; padding: .15rem .5rem; white-space: nowrap; }
    .add-type-sel { font-size: .88rem; padding: .4rem .6rem; border: 1.5px solid #d0dce8; border-radius: 8px; background: #fff; color: #1a2535; cursor: pointer; }
    .add-type-sel:focus { border-color: #2e7dd4; outline: none; }
    .edit-btn { font-size: .8rem; color: #2e7dd4; background: none; border: none; cursor: pointer; padding: .3rem .6rem; border-radius: 6px; font-weight: 600; }
    .edit-btn:hover { background: #e8f1fb; }
    .del-btn { font-size: .8rem; color: #c0392b; background: none; border: none; cursor: pointer; padding: .3rem .6rem; border-radius: 6px; font-weight: 600; }
    .del-btn:hover { background: #fde8e8; }
    .empty { padding: 1rem 1.25rem; font-size: .88rem; color: #aabbc8; }
    .add-row { display: flex; gap: .6rem; align-items: center; padding: .75rem 1.25rem; border-top: 1px solid #f0f4f8; }
    .add-input { flex: 1; font-size: .9rem; padding: .4rem .75rem; border: 1.5px solid #d0dce8; border-radius: 8px; color: #1a2535; outline: none; background: #fff; max-width: 280px; }
    .add-input:focus { border-color: #2e7dd4; }
    .add-btn { font-size: .85rem; font-weight: 700; background: #2e7dd4; color: #fff; border: none; border-radius: 8px; padding: .4rem .9rem; cursor: pointer; }
    .add-btn:hover { background: #2569b5; }
  </style>
</head>
<body>
  <div id="error-bar"></div>
  <h1>Zones <a class="back" href="/">&larr; Home</a></h1>
  <div class="card">
    <div class="card-header">All Zones</div>
    <div id="zone-list"></div>
    <div class="add-row">
      <input class="add-input" id="add-input" type="text" placeholder="New zone name&hellip;"
             onkeydown="if(event.key==='Enter')addZone()">
      <select class="add-type-sel" id="add-type">
        <option value="">— Select type —</option>
        <option value="running_water">Running Water</option>
        <option value="pooling_water">Pooling Water</option>
        <option value="indoor_room">Indoor Room</option>
        <option value="outdoor_shade">Outdoor Shade</option>
        <option value="outdoor_sun">Outdoor Sun</option>
        <option value="unclassified">Unclassified</option>
      </select>
      <button class="add-btn" onclick="addZone()">Add Zone</button>
    </div>
  </div>
<script>
const ZONE_COLORS = ['#2e7dd4','#e07820','#2a9d6e','#7b4fb5','#c0392b','#16a085','#d35400'];
const ZONE_TYPE_LABELS = {
  running_water:  'Running Water',
  pooling_water:  'Pooling Water',
  indoor_room:    'Indoor Room',
  outdoor_shade:  'Outdoor Shade',
  outdoor_sun:    'Outdoor Sun',
  unclassified:   'Unclassified',
};

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function showError(msg) {
  const bar = document.getElementById('error-bar');
  bar.style.display = '';
  bar.textContent = msg;
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

let zones = [];

function renderZones() {
  const el = document.getElementById('zone-list');
  if (!zones.length) {
    el.innerHTML = '<div class="empty">No zones yet. Add one below.</div>';
    return;
  }
  el.innerHTML = zones.map((z, i) => {
    const typeBadge = z.zone_type
      ? `<span class="zone-type-badge">${esc(ZONE_TYPE_LABELS[z.zone_type] || z.zone_type)}</span>`
      : '';
    return `
    <div class="zone-row">
      <div class="zone-dot" style="background:${ZONE_COLORS[i % ZONE_COLORS.length]}"></div>
      <span class="zone-name">${esc(z.name)}</span>
      ${typeBadge}
      <button class="edit-btn" onclick="openEditZone(${z.id})">Edit</button>
      <button class="del-btn" onclick="confirmDelete(${z.id},'${z.name.replace(/'/g,"\\\\'")}')">Delete</button>
    </div>`;
  }).join('');
}

async function addZone() {
  const inp  = document.getElementById('add-input');
  const typeSel = document.getElementById('add-type');
  const name = inp.value.trim();
  if (!name) { inp.focus(); return; }
  const zone_type = typeSel.value || null;
  try {
    const r = await fetch('/api/water-chemistry/zones', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, zone_type}),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
    zones.push(body);
    inp.value = '';
    typeSel.value = '';
    renderZones();
  } catch(e) { showError('Failed to add zone: ' + e.message); }
}

function confirmDelete(id, name) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:999;display:flex;align-items:center;justify-content:center;';
  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem 1.75rem;max-width:360px;width:92%;box-shadow:0 8px 32px rgba(0,0,0,.18);';
  box.innerHTML = `
    <div style="font-size:1rem;font-weight:700;color:#1a2535;margin-bottom:.4rem">Delete zone &#8220;${esc(name)}&#8221;?</div>
    <div style="font-size:.85rem;color:#5a6e84;margin-bottom:1.25rem">What should happen to historical readings tagged with this zone?</div>
    <div style="display:flex;flex-direction:column;gap:.55rem">
      <button id="dz-keep"   style="text-align:left;padding:.6rem .9rem;border-radius:9px;border:1.5px solid #d0dce8;background:#fff;cursor:pointer;font-size:.88rem;font-weight:600;color:#1a2535">Keep historical data &mdash; <span style="font-weight:400;color:#5a6e84">readings stay, zone label removed</span></button>
      <button id="dz-purge"  style="text-align:left;padding:.6rem .9rem;border-radius:9px;border:1.5px solid #fba8a8;background:#fff5f5;cursor:pointer;font-size:.88rem;font-weight:600;color:#c0392b">Delete historical data &mdash; <span style="font-weight:400">permanently remove all readings for this zone</span></button>
      <button id="dz-cancel" style="padding:.5rem .9rem;border-radius:9px;border:1.5px solid #d0dce8;background:#f8fafc;cursor:pointer;font-size:.85rem;color:#7a90a8;margin-top:.2rem">Cancel</button>
    </div>`;
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  const close = () => document.body.removeChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  box.querySelector('#dz-cancel').onclick = close;
  box.querySelector('#dz-keep').onclick  = () => { close(); doDeleteZone(id, name, false); };
  box.querySelector('#dz-purge').onclick = () => { close(); doDeleteZone(id, name, true);  };
}

async function doDeleteZone(id, name, purge) {
  try {
    const url = '/api/water-chemistry/zones/' + id + (purge ? '?purge=true' : '');
    const r = await fetch(url, {method: 'DELETE'});
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
    zones = zones.filter(z => z.id !== id);
    renderZones();
  } catch(e) { showError('Failed to delete zone: ' + e.message); }
}

function openEditZone(id) {
  const z = zones.find(z => z.id === id);
  if (!z) return;

  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:999;display:flex;align-items:center;justify-content:center;';
  const typeOptions = [
    ['', '— Select type —'],
    ['running_water', 'Running Water'],
    ['pooling_water', 'Pooling Water'],
    ['indoor_room', 'Indoor Room'],
    ['outdoor_shade', 'Outdoor Shade'],
    ['outdoor_sun', 'Outdoor Sun'],
    ['unclassified', 'Unclassified'],
  ].map(([v, l]) => `<option value="${v}"${z.zone_type === v ? ' selected' : ''}>${l}</option>`).join('');

  const box = document.createElement('div');
  box.style.cssText = 'background:#fff;border-radius:14px;padding:1.5rem 1.75rem;max-width:380px;width:92%;box-shadow:0 8px 32px rgba(0,0,0,.18);';
  box.innerHTML = `
    <div style="font-size:1rem;font-weight:700;color:#1a2535;margin-bottom:1rem">Edit Zone</div>
    <div style="display:flex;flex-direction:column;gap:.75rem">
      <div>
        <label style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#7a90a8;display:block;margin-bottom:.3rem">Name</label>
        <input id="ez-name" type="text" value="${esc(z.name)}"
          style="width:100%;font-size:.95rem;padding:.4rem .75rem;border:1.5px solid #d0dce8;border-radius:8px;color:#1a2535;outline:none;box-sizing:border-box;">
      </div>
      <div>
        <label style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#7a90a8;display:block;margin-bottom:.3rem">Type</label>
        <select id="ez-type" style="width:100%;font-size:.9rem;padding:.4rem .6rem;border:1.5px solid #d0dce8;border-radius:8px;background:#fff;color:#1a2535;cursor:pointer;box-sizing:border-box;">${typeOptions}</select>
      </div>
      <div id="ez-err" style="font-size:.82rem;color:#c0392b;display:none"></div>
      <div style="display:flex;gap:.6rem;margin-top:.25rem">
        <button id="ez-save" style="flex:1;font-size:.88rem;font-weight:700;background:#2e7dd4;color:#fff;border:none;border-radius:8px;padding:.5rem .9rem;cursor:pointer;">Save</button>
        <button id="ez-cancel" style="font-size:.88rem;color:#7a90a8;background:none;border:1.5px solid #d0dce8;border-radius:8px;padding:.5rem .9rem;cursor:pointer;">Cancel</button>
      </div>
    </div>`;
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  const close = () => document.body.removeChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  box.querySelector('#ez-cancel').onclick = close;

  const nameInp = box.querySelector('#ez-name');
  nameInp.focus(); nameInp.select();
  nameInp.onkeydown = e => { if (e.key === 'Enter') doSave(); if (e.key === 'Escape') close(); };

  async function doSave() {
    const name = nameInp.value.trim();
    const zone_type = box.querySelector('#ez-type').value || null;
    const errEl = box.querySelector('#ez-err');
    if (!name) { nameInp.focus(); return; }
    const saveBtn = box.querySelector('#ez-save');
    saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
    try {
      const r = await fetch(`/api/water-chemistry/zones/${id}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, zone_type}),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
      const idx = zones.findIndex(z => z.id === id);
      if (idx !== -1) zones[idx] = body;
      renderZones();
      close();
    } catch(e) {
      errEl.textContent = e.message; errEl.style.display = '';
      saveBtn.disabled = false; saveBtn.textContent = 'Save';
    }
  }
  box.querySelector('#ez-save').onclick = doSave;
}

async function load() {
  try {
    zones = await fetchJSON('/api/water-chemistry/zones');
    renderZones();
  } catch(e) { showError('Failed to load zones: ' + e.message); }
}

load();
</script>
</body>
</html>"""


_WATER_CHEM_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Water Chemistry</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .back { font-size: .85rem; font-weight: 500; color: #2e7dd4; text-decoration: none; margin-left: .75rem; }
    .cards { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
    .card { background: #fff; border-radius: 12px; padding: 1.2rem 1.5rem; min-width: 180px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); }
    .card .metric-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .2rem; }
    .card .metric-value { font-size: 2rem; font-weight: 700; line-height: 1; }
    .card .metric-unit  { font-size: .85rem; color: #7a90a8; margin-left: .2rem; }
    .temp  { color: #e07820; }
    .ph    { color: #2e7dd4; }
    .orp   { color: #7b4fb5; }
    .cl    { color: #2a9d6e; }
    .ec    { color: #c0662b; }
    .tds   { color: #1a6db5; }
    .bat   { color: #7a90a8; }
    .section { font-size: .75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .75rem; }
    #error-bar { display:none; background:#fde8e8; color:#c0392b; border-radius:8px; padding:.6rem 1rem; margin-bottom:1rem; font-size:.85rem; font-weight:500; }
    .no-data { color: #aabbc8; font-style: italic; padding: 1rem; }
    .device-card { background:#fff; border-radius:12px; padding:1.2rem 1.5rem; margin-bottom:.75rem; box-shadow:0 1px 4px rgba(0,0,0,.08); }
    .device-header { display:flex; align-items:center; gap:1rem; flex-wrap:wrap; margin-bottom:.75rem; }
    .device-name { font-size:1rem; font-weight:700; color:#1a2535; }
    .device-status { font-size:.78rem; font-weight:600; padding:.2rem .6rem; border-radius:20px; }
    .device-status.online { background:#eafaf1; color:#2a9d6e; }
    .device-status.offline { background:#fde8e8; color:#c0392b; }
    .zone-list { display:flex; gap:1rem; flex-wrap:wrap; margin-top:.75rem; }
    .zone-card {
      display:block; text-decoration:none; background:#fff; border-radius:12px;
      padding:1.1rem 1.5rem; min-width:180px;
      box-shadow:0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05);
      border-left:4px solid var(--zc,#aabbc8);
      transition:box-shadow .15s, transform .1s;
    }
    .zone-card:hover { box-shadow:0 3px 10px rgba(0,0,0,.13); transform:translateY(-1px); }
    .zone-card .zc-name { font-size:1rem; font-weight:700; color:#1a2535; margin-bottom:.3rem; }
    .zone-card .zc-arrow { font-size:.85rem; color:#7a90a8; }
    .zone-card .zc-header { display:flex; align-items:center; gap:.5rem; }
    .zc-online { font-size:.7rem; font-weight:600; padding:.15rem .45rem; border-radius:20px; background:#eafaf1; color:#2a9d6e; }
  </style>
</head>
<body>
  <div id="error-bar"></div>
  <h1>Water Chemistry <a class="back" href="/">&larr; Home</a></h1>

  <div class="section" style="margin-bottom:.75rem">Devices</div>
  <div id="devices-wrap"><span class="no-data">Loading&hellip;</span></div>

  <div style="margin-top:2rem">
    <div class="section" style="margin-bottom:.75rem">Zones</div>
    <div class="zone-list" id="zone-list"><span class="no-data">Loading&hellip;</span></div>
  </div>

<script>
const ZONE_COLORS = ['#2e7dd4','#e07820','#2a9d6e','#7b4fb5','#c0392b','#16a085','#d35400'];
const UNZONED_COLOR = '#aabbc8';

function showError(msg) {
  const el = document.getElementById('error-bar');
  el.textContent = '⚠ ' + msg;
  el.style.display = 'block';
}
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function ago(ts) {
  const secs = Math.floor((Date.now() - new Date(ts.replace(' ','T')).getTime()) / 1000);
  if (secs < 0)     return 'just now';
  if (secs < 60)    return secs + 's ago';
  if (secs < 3600)  return Math.floor(secs/60) + 'm ago';
  if (secs < 86400) return Math.floor(secs/3600) + 'h ' + Math.floor((secs%3600)/60) + 'm ago';
  return Math.floor(secs/86400) + 'd ago';
}
function dur(ts) {
  const secs = Math.floor((Date.now() - new Date(ts.replace(' ','T')).getTime()) / 1000);
  if (secs < 60)    return secs + 's';
  if (secs < 3600)  return Math.floor(secs/60) + 'm';
  if (secs < 86400) return Math.floor(secs/3600) + 'h ' + Math.floor((secs%3600)/60) + 'm';
  return Math.floor(secs/86400) + 'd ' + Math.floor((secs%86400)/3600) + 'h';
}
function phColor(ph) {
  if (ph < 7.0 || ph > 7.8) return '#c0392b';
  if (ph >= 7.2 && ph <= 7.6) return '#2a9d6e';
  return '#e07820';
}
function orpColor(orp) {
  if (orp >= 650 && orp <= 750) return '#2a9d6e';
  if (orp < 400 || orp > 900)  return '#c0392b';
  return '#e07820';
}
function clColor(cl) {
  if (cl >= 1.0 && cl <= 3.0) return '#2a9d6e';
  if (cl < 0.5  || cl > 5.0)  return '#c0392b';
  return '#e07820';
}

async function loadZoneList() {
  try {
    const data = await fetchJSON('/api/water-chemistry/zone-list');
    const container = document.getElementById('zone-list');
    const onlineSet = new Set(data.online_zones || []);
    const cards = data.zones.map((name, i) => {
      const color = ZONE_COLORS[i % ZONE_COLORS.length];
      const badge = onlineSet.has(name) ? '<span class="zc-online">&#9679; Online</span>' : '';
      return `<a class="zone-card" href="/water-chemistry/${encodeURIComponent(name)}" style="--zc:${color}">
        <div class="zc-header"><div class="zc-name">${esc(name)}</div>${badge}</div>
        <div class="zc-arrow">View chart &rarr;</div>
      </a>`;
    });
    if (data.has_unzoned) {
      cards.push(`<a class="zone-card" href="/water-chemistry/__unzoned__" style="--zc:${UNZONED_COLOR}">
        <div class="zc-header"><div class="zc-name" style="color:#7a90a8">Unzoned</div></div>
        <div class="zc-arrow">View chart &rarr;</div>
      </a>`);
    }
    container.innerHTML = cards.length ? cards.join('') : '<span class="no-data">No readings yet.</span>';
  } catch(e) { showError('Failed to load zones: ' + e.message); }
}

async function loadCurrent() {
  try {
    const rows = await fetchJSON('/api/water-chemistry/current');
    const wrap = document.getElementById('devices-wrap');
    if (!rows.length) { wrap.innerHTML = '<span class="no-data">No devices yet.</span>'; return; }
    wrap.innerHTML = rows.map(r => `
      <div class="device-card">
        <div class="device-header">
          <span class="device-name">BLE-YC01</span>
          <span class="device-status ${r.offline ? 'offline' : 'online'}">${r.offline ? '&#9679; Offline' : '&#9679; Online'}</span>
          <span style="font-size:.75rem;color:#aabbc8">for ${dur(r.streak_start)}</span>
          <span style="font-size:.78rem;color:${r.current_zone ? '#7a90a8' : '#aabbc8'}">&rarr; ${r.current_zone ? esc(r.current_zone) : 'No zone'}</span>
        </div>
        <div class="cards" style="margin-bottom:0;gap:.75rem">
          <div class="card"><div class="metric-label">Temperature</div><div class="metric-value temp">${r.temp_f != null ? r.temp_f.toFixed(1) : '—'}<span class="metric-unit">°F</span></div></div>
          <div class="card"><div class="metric-label">pH</div><div class="metric-value" style="color:${phColor(r.ph)}">${r.ph != null ? r.ph.toFixed(2) : '—'}</div></div>
          <div class="card"><div class="metric-label">ORP</div><div class="metric-value" style="color:${orpColor(r.orp)}">${r.orp != null ? r.orp : '—'}<span class="metric-unit">mV</span></div></div>
          <div class="card"><div class="metric-label">Free Cl</div><div class="metric-value" style="color:${clColor(r.chlorine)}">${r.chlorine != null ? r.chlorine.toFixed(1) : '—'}<span class="metric-unit">mg/L</span></div></div>
          <div class="card"><div class="metric-label">EC</div><div class="metric-value ec">${r.ec != null ? r.ec : '—'}<span class="metric-unit">µS/cm</span></div></div>
          <div class="card"><div class="metric-label">TDS</div><div class="metric-value tds">${r.tds != null ? r.tds : '—'}<span class="metric-unit">ppm</span></div></div>
          <div class="card"><div class="metric-label">Battery</div><div class="metric-value bat">${r.battery != null ? r.battery : '—'}<span class="metric-unit">%</span></div></div>
        </div>
      </div>
    `).join('');
  } catch(e) { showError('Failed to load current readings: ' + e.message); }
}

loadCurrent();
loadZoneList();
setInterval(loadCurrent, 30000);
</script>
</body>
</html>"""


_RUNNING_WATER_ZONE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__ZONE_TITLE__ — Water Chemistry</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .back { font-size: .85rem; font-weight: 500; color: #2e7dd4; text-decoration: none; margin-left: .75rem; }
    .zone-type-label { font-size: .8rem; color: #7a90a8; margin-bottom: 1.5rem; }
    #error-bar { display:none; background:#fde8e8; color:#c0392b; border-radius:8px; padding:.6rem 1rem; margin-bottom:1rem; font-size:.85rem; font-weight:500; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); overflow: hidden; }
    .card-header { font-size: .75rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: #7a90a8; padding: .75rem 1.25rem; background: #f8fafc; border-bottom: 1px solid #e8edf3; display: flex; justify-content: space-between; align-items: center; }
    .card-header .refresh-note { font-size: .7rem; color: #aabbc8; font-weight: 400; text-transform: none; letter-spacing: 0; }
    table { width: 100%; border-collapse: collapse; font-size: .88rem; }
    th { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: #7a90a8; padding: .55rem 1rem; text-align: right; border-bottom: 1px solid #e8edf3; white-space: nowrap; }
    th:first-child { text-align: left; }
    td { padding: .6rem 1rem; border-bottom: 1px solid #f0f4f8; text-align: right; color: #1a2535; white-space: nowrap; }
    td:first-child { text-align: left; color: #5a6e84; font-size: .82rem; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f8fafc; }
    .no-data { padding: 1.5rem 1.25rem; color: #aabbc8; font-size: .88rem; }
    .val-ph { color: #2e7dd4; font-weight: 600; }
    .val-orp { color: #7b4fb5; font-weight: 600; }
    .val-cl { color: #2a9d6e; font-weight: 600; }
    .val-temp { color: #e07820; font-weight: 600; }
    .val-ec { color: #c0662b; font-weight: 600; }
    .val-tds { color: #1a6db5; font-weight: 600; }
  </style>
</head>
<body>
  <div id="error-bar"></div>
  <h1>__ZONE_TITLE__ <a class="back" href="/water-chemistry">&larr; Water Chemistry</a> <span id="status-badge"></span></h1>
  <p class="zone-type-label">Running Water</p>
  <div class="card">
    <div class="card-header">
      Recent Readings
      <span class="refresh-note" id="refresh-note">Updating every 30s</span>
    </div>
    <div id="table-wrap">
      <div class="no-data">Loading&hellip;</div>
    </div>
  </div>
<script>
const ZONE = __ZONE_JSON__;

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function showError(msg) {
  const el = document.getElementById('error-bar');
  el.textContent = msg; el.style.display = '';
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function fmt(v, dec=2) { return v != null ? Number(v).toFixed(dec) : '—'; }

function tsLabel(ts) {
  const d = new Date(ts.replace(' ', 'T'));
  const now = new Date();
  const diffMs = now - d;
  const diffMin = Math.floor(diffMs / 60000);
  const time = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const date = d.toLocaleDateString([], {month: 'short', day: 'numeric'});
  const ago = diffMin < 1 ? 'just now' : diffMin < 60 ? diffMin + 'm ago' : date;
  return `<span title="${esc(ts)}">${time} <span style="color:#aabbc8;font-size:.75rem">${ago}</span></span>`;
}

async function load() {
  try {
    const [rows, devices] = await Promise.all([
      (await fetch('/api/pool/recent?zone=' + encodeURIComponent(ZONE) + '&limit=50')).json(),
      fetchJSON('/api/water-chemistry/current'),
    ]);
    const monitored = devices.some(d => d.current_zone === ZONE && !d.offline);
    const badge = document.getElementById('status-badge');
    badge.innerHTML = monitored
      ? '<span style="color:#2a9d6e;font-size:.8rem;font-weight:600;margin-left:.5rem">&#9679; Online</span>'
      : '<span style="color:#aabbc8;font-size:.8rem;font-weight:600;margin-left:.5rem">&#9679; Offline</span>';
    const wrap = document.getElementById('table-wrap');
    if (!Array.isArray(rows) || !rows.length) {
      wrap.innerHTML = '<div class="no-data">No readings yet for this zone.</div>';
      return;
    }
    wrap.innerHTML = `<table>
      <thead><tr>
        <th>Time</th>
        <th>Temp</th>
        <th>pH</th>
        <th>ORP</th>
        <th>Free Cl</th>
        <th>EC</th>
        <th>TDS</th>
        <th>Batt</th>
      </tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td>${tsLabel(r.ts)}</td>
        <td class="val-temp">${fmt(r.temp_f, 1)}<span style="color:#aabbc8;font-size:.78rem"> °F</span></td>
        <td class="val-ph">${fmt(r.ph)}</td>
        <td class="val-orp">${fmt(r.orp, 0)}<span style="color:#aabbc8;font-size:.78rem"> mV</span></td>
        <td class="val-cl">${fmt(r.chlorine)}<span style="color:#aabbc8;font-size:.78rem"> mg/L</span></td>
        <td class="val-ec">${fmt(r.ec, 0)}<span style="color:#aabbc8;font-size:.78rem"> µS</span></td>
        <td class="val-tds">${fmt(r.tds, 0)}<span style="color:#aabbc8;font-size:.78rem"> ppm</span></td>
        <td>${fmt(r.battery, 0)}<span style="color:#aabbc8;font-size:.78rem"> %</span></td>
      </tr>`).join('')}</tbody>
    </table>`;
  } catch(e) { showError('Failed to load readings: ' + e.message); }
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""


_WATER_CHEM_ZONE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__ZONE_TITLE__ — Water Chemistry</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .back { font-size: .85rem; font-weight: 500; color: #2e7dd4; text-decoration: none; margin-left: .75rem; }
    #error-bar { display:none; background:#fde8e8; color:#c0392b; border-radius:8px; padding:.6rem 1rem; margin-bottom:1rem; font-size:.85rem; font-weight:500; }
    .metric-btns { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; }
    .metric-btn {
      font-size: .8rem; font-weight: 600; padding: .4rem .9rem; border-radius: 20px;
      border: 2px solid transparent; cursor: pointer; background: #fff;
      color: #7a90a8; transition: all .15s;
      box-shadow: 0 1px 3px rgba(0,0,0,.08);
    }
    .metric-btn:hover { background: #f0f4f8; }
    .metric-btn.active { color: #fff; border-color: transparent; }
    .metric-btn[data-metric="temp_f"].active  { background: #e07820; }
    .metric-btn[data-metric="ph"].active      { background: #2e7dd4; }
    .metric-btn[data-metric="orp"].active     { background: #7b4fb5; }
    .metric-btn[data-metric="chlorine"].active{ background: #2a9d6e; }
    .metric-btn[data-metric="ec"].active      { background: #c0662b; }
    .metric-btn[data-metric="tds"].active     { background: #1a6db5; }
    .metric-btn[data-metric="battery"].active { background: #7a90a8; }
    .metric-desc { font-size: .82rem; color: #5a6e84; background: #f0f4f8; border-left: 3px solid #d0dce8; border-radius: 0 6px 6px 0; padding: .5rem .85rem; margin-bottom: .75rem; display: none; }
    .chart-wrap { background: #fff; border-radius: 12px; padding: 1.25rem 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 2rem; }
    .btn-group { margin-bottom: 1.2rem; }
    .btn-group-label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; margin-bottom: .4rem; }
    .range-btns { display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem; font-weight: 500; transition: all .15s; }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #2e7dd4; color: #fff; border-color: #2e7dd4; }
    .range-btns button:disabled { opacity: .3; cursor: default; pointer-events: none; }
    .res-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.2rem; }
    .res-row label { font-size: .72rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .res-row select { background: #fff; color: #4a6080; border: 1px solid #d0dce8; border-radius: 6px; padding: .3rem .7rem; font-size: .85rem; font-weight: 500; cursor: pointer; }
    #resp-size { font-size: .72rem; color: #4a6080; }
  </style>
</head>
<body>
  <div id="error-bar"></div>
  <h1>__ZONE_TITLE__ <a class="back" href="/water-chemistry">&larr; Water Chemistry</a> <span id="status-badge"></span></h1>

  <div class="res-row">
    <label for="res">Resolution</label>
    <select id="res" onchange="resolution=this.value; invalidateHistoryCache(); loadHistoryForChart()">
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="max">Max</option>
    </select>
    <span id="resp-size"></span>
  </div>
  <div class="btn-group" id="recent-group">
    <div class="btn-group-label">Most Recent</div>
    <div class="range-btns" id="recent-btns">
      <button id="btn-prev" onclick="shiftView(-1)">&#8592;</button>
      <button onclick="setRange(1)" data-days="1" class="active">24h</button>
      <button onclick="setRange(3)" data-days="3">3d</button>
      <button onclick="setRange(7)" data-days="7">7d</button>
      <button onclick="setRange(30)" data-days="30">30d</button>
      <button id="btn-next" onclick="shiftView(1)" disabled>&#8594;</button>
    </div>
  </div>
  <div class="btn-group">
    <div class="btn-group-label">By Month</div>
    <div class="range-btns" id="month-btns">
      <button onclick="setAllMonths()">All Months</button>
      <button onclick="setMonth(1)">Jan</button>
      <button onclick="setMonth(2)">Feb</button>
      <button onclick="setMonth(3)">Mar</button>
      <button onclick="setMonth(4)">Apr</button>
      <button onclick="setMonth(5)">May</button>
      <button onclick="setMonth(6)">Jun</button>
      <button onclick="setMonth(7)">Jul</button>
      <button onclick="setMonth(8)">Aug</button>
      <button onclick="setMonth(9)">Sep</button>
      <button onclick="setMonth(10)">Oct</button>
      <button onclick="setMonth(11)">Nov</button>
      <button onclick="setMonth(12)">Dec</button>
    </div>
  </div>

  <div class="metric-btns" id="metric-btns">
    <button class="metric-btn active" data-metric="temp_f"  data-label="Temperature" data-unit="°F"    data-desc="Water temperature.">Temperature</button>
    <button class="metric-btn"        data-metric="ph"       data-label="pH"          data-unit=""           data-desc="Acidity/alkalinity of the water. Ideal range: 7.2–7.6. Outside 7.0–7.8 indicates a problem.">pH</button>
    <button class="metric-btn"        data-metric="orp"      data-label="ORP"         data-unit="mV"         data-desc="Oxidation-Reduction Potential — how effective the sanitizer is at killing bacteria. Higher = more sanitizing power. Ideal: 650–750 mV.">ORP</button>
    <button class="metric-btn"        data-metric="chlorine" data-label="Free Cl"     data-unit="mg/L"       data-desc="Free Chlorine — the active chlorine available to sanitize the water. Ideal: 1–3 mg/L.">Free Cl</button>
    <button class="metric-btn"        data-metric="ec"       data-label="EC"          data-unit="µS/cm" data-desc="Electrical Conductivity — measures dissolved minerals and salts.">EC</button>
    <button class="metric-btn"        data-metric="tds"      data-label="TDS"         data-unit="ppm"        data-desc="Total Dissolved Solids — the sum of all dissolved substances in parts per million.">TDS</button>
    <button class="metric-btn"        data-metric="battery"  data-label="Battery"     data-unit="%"          data-desc="Battery level of the BLE-YC01 sensor.">Battery</button>
  </div>

  <div id="metric-desc" class="metric-desc"></div>

  <div class="chart-wrap">
    <canvas id="pool-chart"></canvas>
  </div>

<script>
const ZONE = __ZONE_JSON__;
const ZONE_COLOR = __ZONE_COLOR_JSON__;
const YEAR_PALETTE = ['#2e7dd4','#e07820','#2a9d6e','#7b4fb5','#c0392b','#16a085','#d35400'];

let historyCache = {};
let poolChart = null;
let chartXMin = null, chartXMax = null, chartXUnit = 'hour';
let mode = 'recent', rangeDays = 1, activeMonth = null, offsetMs = 0;

const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const isLocal  = /^192\\.168\\./.test(location.hostname) || /\\.local$/.test(location.hostname);
let resolution = isLocal ? 'max' : isMobile ? 'low' : 'medium';
document.getElementById('res').value = resolution;

const BUCKETS = {
  recent: {
    low:    {1: 30,  3: 60,  7: 120, 30: 360},
    medium: {1: 5,   3: 20,  7: 30,  30: 60},
    max:    {1: 1,   3: 5,   7: 10,  30: 20},
  },
  month:  { low: 240, medium: 60, max: 10 },
  year:   { low: 1440, medium: 360, max: 60 },
};
function getBucket() {
  if (mode === 'recent') return BUCKETS.recent[resolution][rangeDays] ?? 60;
  return BUCKETS[mode][resolution];
}

function showError(msg) {
  const el = document.getElementById('error-bar');
  el.textContent = '⚠ ' + msg;
  el.style.display = 'block';
}
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function setRange(days) {
  mode = 'recent'; rangeDays = days; offsetMs = 0;
  document.querySelectorAll('#recent-btns button[data-days]').forEach(b =>
    b.classList.toggle('active', parseFloat(b.dataset.days) === days));
  document.querySelectorAll('#month-btns button').forEach(b => b.classList.remove('active'));
  invalidateHistoryCache();
  loadHistoryForChart();
}
function shiftView(dir) {
  offsetMs += dir * rangeDays * 86400000;
  if (offsetMs > 0) offsetMs = 0;
  invalidateHistoryCache();
  loadHistoryForChart();
}
function setAllMonths() {
  mode = 'year';
  document.querySelectorAll('#recent-btns button[data-days]').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('#month-btns button').forEach((b,i) => b.classList.toggle('active', i === 0));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  invalidateHistoryCache();
  loadHistoryForChart();
}
function setMonth(m) {
  mode = 'month'; activeMonth = m;
  document.querySelectorAll('#recent-btns button[data-days]').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('#month-btns button').forEach((b,i) => b.classList.toggle('active', i === m));
  document.getElementById('btn-prev').disabled = true;
  document.getElementById('btn-next').disabled = true;
  invalidateHistoryCache();
  loadHistoryForChart();
}
function invalidateHistoryCache() { historyCache = {}; }

function buildTimePoints(rows, metric) {
  const GAP_MS = 15 * 60 * 1000;
  const points = [];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (i > 0) {
      const prev = new Date(rows[i-1].ts.replace(' ','T')).getTime();
      const curr = new Date(r.ts.replace(' ','T')).getTime();
      if (curr - prev > GAP_MS) points.push({ x: new Date(prev + (curr-prev)/2), y: null });
    }
    if (r[metric] != null) points.push({ x: new Date(r.ts.replace(' ','T')), y: r[metric] });
  }
  return points;
}

function buildDatasets() {
  const btn    = document.querySelector('.metric-btn.active');
  const metric = btn?.dataset.metric || 'temp_f';
  const unit   = btn?.dataset.unit   || '';
  const isGrouped = mode === 'month' || mode === 'year';
  const rows   = historyCache[ZONE] || [];
  if (!rows.length) return [];

  if (isGrouped) {
    const byYear = {};
    for (const r of rows) (byYear[r.year] ??= []).push({ x: new Date(r.ts.replace(' ','T')), y: r[metric] });
    return Object.keys(byYear).sort().map((year, yi) => {
      const c = yi === 0 ? ZONE_COLOR : YEAR_PALETTE[(1 + yi) % YEAR_PALETTE.length];
      return {
        label: year,
        data: byYear[year],
        borderColor: c, backgroundColor: 'transparent',
        borderWidth: yi === 0 ? 2 : 1.5, borderDash: yi > 0 ? [4,3] : [],
        pointRadius: 0, tension: 0, spanGaps: false,
      };
    });
  }
  const points = buildTimePoints(rows, metric);
  return [{
    label: (ZONE === '__unzoned__' ? 'Unzoned' : ZONE) + (unit ? ` (${unit})` : ''),
    data: points,
    borderColor: ZONE_COLOR, backgroundColor: ZONE_COLOR + '22',
    borderWidth: 2, pointRadius: 2, pointHoverRadius: 4,
    fill: true, tension: 0, spanGaps: false,
  }];
}

function renderChart() {
  const btn  = document.querySelector('.metric-btn.active');
  const unit = btn?.dataset.unit || '';
  const datasets = buildDatasets();

  if (!poolChart) {
    const ctx = document.getElementById('pool-chart').getContext('2d');
    poolChart = new Chart(ctx, {
      type: 'line',
      data: { datasets },
      options: {
        animation: false, parsing: false, responsive: true,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            type: 'time',
            time: { tooltipFormat: 'MMM d, h:mm a', unit: chartXUnit },
            min: chartXMin, max: chartXMax,
            grid: { color: '#f0f4f8' },
            ticks: { color: '#7a90a8', maxTicksLimit: 10 },
          },
          y: {
            grid: { color: '#f0f4f8' },
            ticks: { color: '#7a90a8' },
            title: { display: !!unit, text: unit, color: '#7a90a8', font: { size: 11 } },
          },
        },
        plugins: {
          legend: { labels: { color: '#4a6080', boxWidth: 12 } },
          tooltip: {
            callbacks: {
              label: ctx => {
                const u = document.querySelector('.metric-btn.active')?.dataset.unit || '';
                return (ctx.dataset.label || '') + ': ' + ctx.parsed.y + (u ? ' ' + u : '');
              }
            }
          }
        }
      }
    });
  } else {
    poolChart.data.datasets = datasets;
    poolChart.options.scales.x.min  = chartXMin;
    poolChart.options.scales.x.max  = chartXMax;
    poolChart.options.scales.x.time.unit = chartXUnit;
    poolChart.options.scales.y.title.display = !!unit;
    poolChart.options.scales.y.title.text    = unit;
    poolChart.update();
  }
}

function updateDesc(btn) {
  const el = document.getElementById('metric-desc');
  const desc = btn ? btn.dataset.desc : '';
  if (desc) { el.textContent = desc; el.style.display = 'block'; }
  else { el.style.display = 'none'; }
}

document.getElementById('metric-btns').addEventListener('click', e => {
  const btn = e.target.closest('.metric-btn');
  if (!btn) return;
  document.querySelectorAll('.metric-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  updateDesc(btn);
  renderChart();
});
updateDesc(document.querySelector('.metric-btn.active'));

async function fetchZoneHistory() {
  const base = '/api/water-chemistry/history';
  const zp = '&zone=' + encodeURIComponent(ZONE);
  if (mode === 'recent') {
    const xMax = new Date(Date.now() + offsetMs);
    const xMin = new Date(xMax.getTime() - rangeDays * 86400000);
    return await fetchJSON(base + '?limit=100000&bucket_minutes=' + getBucket()
      + '&start=' + encodeURIComponent(localISO(xMin))
      + '&end='   + encodeURIComponent(localISO(xMax)) + zp);
  } else if (mode === 'month') {
    return await fetchJSON('/api/water-chemistry/history/month?month=' + activeMonth
      + '&bucket_minutes=' + getBucket() + zp);
  } else {
    return await fetchJSON('/api/water-chemistry/history/year?bucket_minutes=' + getBucket() + zp);
  }
}

async function loadHistoryForChart() {
  try {
    if (mode === 'recent') {
      const xMax = new Date(Date.now() + offsetMs);
      const xMin = new Date(xMax.getTime() - rangeDays * 86400000);
      chartXMin = xMin; chartXMax = xMax;
      chartXUnit = rangeDays <= 1 ? 'hour' : 'day';
    } else if (mode === 'month') {
      chartXMin = new Date(2000, activeMonth - 1, 1);
      chartXMax = new Date(2000, activeMonth, 0, 23, 59, 59);
      chartXUnit = 'day';
    } else {
      chartXMin = new Date(2000, 0, 1);
      chartXMax = new Date(2000, 11, 31, 23, 59, 59);
      chartXUnit = 'month';
    }

    if (!historyCache[ZONE]) {
      historyCache[ZONE] = await fetchZoneHistory();
    }
    const totalPts = historyCache[ZONE].length;
    renderChart();
    document.getElementById('resp-size').textContent = totalPts + ' pts';

    if (mode === 'recent') {
      const peek = await fetchJSON('/api/water-chemistry/history?end=' + encodeURIComponent(localISO(chartXMin))
        + '&limit=1&zone=' + encodeURIComponent(ZONE));
      document.getElementById('btn-prev').disabled = peek.length === 0;
      document.getElementById('btn-next').disabled = offsetMs >= 0;
    }
  } catch(e) { showError('Failed to load history: ' + e.message); }
}

const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

async function init() {
  try {
    const devices = await fetchJSON('/api/water-chemistry/current');
    const monitored = devices.some(d => d.current_zone === ZONE && !d.offline);
    const badge = document.getElementById('status-badge');
    if (monitored) {
      badge.innerHTML = '<span style="color:#2a9d6e;font-size:.8rem;font-weight:600;margin-left:.5rem">&#9679; Online</span>';
    } else {
      badge.innerHTML = '<span style="color:#aabbc8;font-size:.8rem;font-weight:600;margin-left:.5rem">&#9679; Offline</span>';
      document.getElementById('recent-group').style.display = 'none';
      const m = new Date().getMonth() + 1;
      mode = 'month';
      activeMonth = m;
      document.querySelectorAll('#month-btns button').forEach(b => {
        b.classList.toggle('active', b.textContent === MONTH_NAMES[m - 1]);
      });
    }
  } catch(e) { /* keep defaults on error */ }
  loadHistoryForChart();
}

init();
</script>
</body>
</html>"""


@app.get("/zones")
def zones_page():
    return Response(_ZONES_PAGE, mimetype="text/html")


@app.get("/water-chemistry")
def water_chemistry_page():
    return Response(_WATER_CHEM_PAGE, mimetype="text/html")


@app.get("/water-chemistry/<path:zone_name>")
def water_chemistry_zone_page(zone_name):
    import json as _json
    ZONE_COLORS = ['#2e7dd4', '#e07820', '#2a9d6e', '#7b4fb5', '#c0392b', '#16a085', '#d35400']
    UNZONED_COLOR = '#aabbc8'
    if zone_name == '__unzoned__':
        title = 'Unzoned'
        color = UNZONED_COLOR
        zone_type = None
    else:
        with _conn() as conn:
            rows = conn.execute("SELECT name, zone_type FROM wc_zones ORDER BY id ASC").fetchall()
        zone_row = next((r for r in rows if r["name"] == zone_name), None)
        names = [r[0] for r in rows]
        idx = names.index(zone_name) if zone_name in names else 0
        title = zone_name
        color = ZONE_COLORS[idx % len(ZONE_COLORS)]
        zone_type = zone_row["zone_type"] if zone_row else None
    if zone_type == 'running_water':
        html = (_RUNNING_WATER_ZONE_PAGE_TEMPLATE
                .replace('__ZONE_TITLE__', title)
                .replace('__ZONE_JSON__', _json.dumps(zone_name)))
    else:
        html = (_WATER_CHEM_ZONE_PAGE_TEMPLATE
                .replace('__ZONE_TITLE__', title)
                .replace('__ZONE_JSON__', _json.dumps(zone_name))
                .replace('__ZONE_COLOR_JSON__', _json.dumps(color)))
    return Response(html, mimetype="text/html")


@app.get("/pool")
def pool_page_redirect():
    from flask import redirect
    return redirect("/water-chemistry", code=301)


@app.get("/api/rssi")
def api_rssi():
    """Latest RSSI for every BLE device (sensors + presence devices)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT label, address, rssi, ts FROM ble_rssi ORDER BY label"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


_RSSI_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bluetooth Signal &mdash; Smart Home</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .4rem; color: #1a2535; letter-spacing: -.02em; }
    .nav { margin-bottom: 1.5rem; }
    .nav a { font-size: .85rem; color: #2e7dd4; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
    .card { background: #fff; border-radius: 12px; padding: 1.8rem 2rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); max-width: 480px; }
    .select-row { display: flex; align-items: center; gap: .8rem; margin-bottom: 2rem; }
    .select-row label { font-size: .8rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; white-space: nowrap; }
    select { background: #fff; color: #1a2535; border: 1px solid #d0dce8; border-radius: 8px; padding: .45rem .9rem; font-size: .95rem; font-weight: 500; cursor: pointer; flex: 1; }
    .rssi-display { text-align: center; padding: 2rem 0 1.5rem; }
    .rssi-value { font-size: 6rem; font-weight: 800; letter-spacing: -.04em; line-height: 1; transition: color .3s; }
    .rssi-unit { font-size: 1.5rem; font-weight: 500; color: #7a90a8; margin-top: .3rem; }
    .rssi-label { font-size: .8rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-top: 1.2rem; }
    .rssi-ts { font-size: .8rem; color: #aabbcc; margin-top: .4rem; }
    .signal-bar { display: flex; justify-content: center; gap: .35rem; margin-top: 1.5rem; }
    .signal-bar .seg { width: 24px; border-radius: 3px 3px 0 0; background: #e0e8f0; }
    .legend { display: flex; justify-content: center; gap: 1.2rem; margin-top: 1.2rem; font-size: .75rem; color: #7a90a8; }
    .legend span { display: flex; align-items: center; gap: .3rem; }
    .legend span::before { content: ''; display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
    .legend .good::before { background: #2a9d6e; }
    .legend .fair::before { background: #e07820; }
    .legend .poor::before { background: #c0392b; }
    #error-bar { display: none; background: #c0392b; color: #fff; padding: .6rem 1rem; border-radius: 8px; margin-bottom: 1rem; font-size: .88rem; }
    .no-device { text-align: center; color: #aabbcc; padding: 2rem 0; font-size: 1rem; }
  </style>
</head>
<body>
  <h1>Bluetooth Signal</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div id="error-bar"></div>
  <div class="card">
    <div class="select-row">
      <label for="device-select">Device</label>
      <select id="device-select">
        <option value="">-- select a device --</option>
      </select>
    </div>
    <div id="rssi-display" class="rssi-display">
      <div class="no-device">Select a device above</div>
    </div>
    <div class="legend">
      <span class="good">Good (&gt;&nbsp;&minus;60&nbsp;dBm)</span>
      <span class="fair">Fair (&minus;60 to &minus;80)</span>
      <span class="poor">Poor (&lt;&nbsp;&minus;80&nbsp;dBm)</span>
    </div>
  </div>
<script>
const sel = document.getElementById('device-select');
const display = document.getElementById('rssi-display');
const errorBar = document.getElementById('error-bar');
let latestData = {};
let intervalId = null;

function showError(msg) {
  errorBar.textContent = msg;
  errorBar.style.display = 'block';
}
function clearError() {
  errorBar.style.display = 'none';
}

function rssiColor(v) {
  if (v > -60) return '#2a9d6e';
  if (v > -80) return '#e07820';
  return '#c0392b';
}

function signalBars(v) {
  const heights = [14, 22, 32, 44, 58];
  let filled;
  if (v > -55) filled = 5;
  else if (v > -65) filled = 4;
  else if (v > -75) filled = 3;
  else if (v > -85) filled = 2;
  else filled = 1;
  const color = rssiColor(v);
  return heights.map((h, i) => {
    const active = i < filled;
    return `<div class="seg" style="height:${h}px;background:${active ? color : '#e0e8f0'};opacity:${active ? 1 : 0.4};"></div>`;
  }).join('');
}

function renderDisplay(label) {
  const d = latestData[label];
  if (!d || d.rssi == null) {
    display.innerHTML = '<div class="no-device">No RSSI data for this device</div>';
    return;
  }
  const color = rssiColor(d.rssi);
  const ts = d.ts ? new Date(d.ts.replace(' ', 'T')).toLocaleTimeString() : '';
  display.innerHTML = `
    <div class="rssi-value" style="color:${color}">${d.rssi}</div>
    <div class="rssi-unit">dBm</div>
    <div class="signal-bar">${signalBars(d.rssi)}</div>
    <div class="rssi-label">${label}</div>
    <div class="rssi-ts">Last updated ${ts}</div>
  `;
}

async function loadRssi() {
  try {
    const r = await fetch('/api/rssi');
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    const data = await r.json();
    clearError();
    latestData = {};
    data.forEach(d => { latestData[d.label] = d; });

    const labels = data.map(d => d.label).sort();
    const current = sel.value;
    const existing = [...sel.options].slice(1).map(o => o.value);
    if (JSON.stringify(existing) !== JSON.stringify(labels)) {
      while (sel.options.length > 1) sel.remove(1);
      labels.forEach(lbl => {
        const opt = document.createElement('option');
        opt.value = lbl;
        opt.textContent = lbl;
        sel.appendChild(opt);
      });
      if (current && labels.includes(current)) sel.value = current;
    }

    const chosen = sel.value;
    if (chosen) renderDisplay(chosen);
  } catch(e) {
    showError('Failed to load RSSI data: ' + e.message);
  }
}

sel.addEventListener('change', () => {
  const chosen = sel.value;
  if (chosen) renderDisplay(chosen);
  else display.innerHTML = '<div class="no-device">Select a device above</div>';
});

loadRssi();
setInterval(loadRssi, 3000);
</script>
</body>
</html>"""


@app.get("/rssi")
def rssi_page():
    return Response(_RSSI_PAGE, mimetype="text/html")


def run(db_path: str, host: str, port: int, debug: bool) -> None:
    global _db_path
    _db_path = db_path
    open_db(db_path).close()  # ensure schema exists
    app.run(host=host, port=port, debug=debug)
