from __future__ import annotations
import datetime
import sqlite3


def _is_indoor(label: str) -> bool:
    return label.lower().startswith(("indoor-", "inside-"))


def _ts_to_epoch(ts: str) -> float:
    return datetime.datetime.strptime(ts.replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()


def _epoch_to_ts(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _interpolate_crossing(
    t1: float, a1: float, b1: float,
    t2: float, a2: float, b2: float,
) -> tuple[float, float] | None:
    """Find where series a and series b cross in [t1, t2].

    Returns (crossing_epoch, crossing_value) or None.
    """
    diff1 = a1 - b1
    diff2 = a2 - b2
    if diff1 * diff2 > 0:
        return None  # same sign — no crossing
    denom = diff1 - diff2  # == (a1-b1) - (a2-b2)
    if abs(denom) < 1e-9:
        # Lines are parallel and already overlapping
        if abs(diff1) < 0.05:
            return (t1 + t2) / 2, (a1 + b1) / 2
        return None
    frac = diff1 / denom  # fraction of interval at crossing
    t_cross = t1 + frac * (t2 - t1)
    val = a1 + frac * (a2 - a1)
    return t_cross, val


def _recent_readings(conn: sqlite3.Connection, label: str, n: int = 120) -> list[tuple[str, float]]:
    """Return last n (ts, temp_f) rows for label, oldest first."""
    rows = conn.execute(
        "SELECT ts, temp_f FROM readings WHERE label=? AND temp_f IS NOT NULL ORDER BY ts DESC LIMIT ?",
        (label, n),
    ).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


def _insert_event(conn: sqlite3.Connection, ts: str, event_type: str, value: float, details: str) -> bool:
    """Insert event, ignoring duplicates. Returns True if inserted."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO temperature_events (ts, event_type, value, details) VALUES (?,?,?,?)",
        (ts, event_type, round(value, 2), details),
    )
    conn.commit()
    return cur.rowcount > 0


def _check_two_label_crossing(
    conn: sqlite3.Connection, label_a: str, label_b: str, event_type: str
) -> int:
    """Detect a crossing between two single labels. Returns number of events inserted."""
    rows_a = _recent_readings(conn, label_a)
    rows_b = _recent_readings(conn, label_b)
    if len(rows_a) < 2 or len(rows_b) < 2:
        return 0

    # Align by timestamp — the snapshot_loop writes all sensors at the same ts,
    # so most readings will share timestamps.
    by_ts: dict[str, dict] = {}
    for ts, val in rows_a:
        by_ts.setdefault(ts, {})["a"] = val
    for ts, val in rows_b:
        by_ts.setdefault(ts, {})["b"] = val

    common = sorted(
        [(ts, d["a"], d["b"]) for ts, d in by_ts.items() if "a" in d and "b" in d]
    )
    if len(common) < 2:
        return 0

    inserted = 0
    for i in range(len(common) - 1):
        ts1, a1, b1 = common[i]
        ts2, a2, b2 = common[i + 1]
        result = _interpolate_crossing(_ts_to_epoch(ts1), a1, b1, _ts_to_epoch(ts2), a2, b2)
        if result is None:
            continue
        t_cross, val = result
        details = f"{label_a}={a2:.1f}°F, {label_b}={b2:.1f}°F"
        if _insert_event(conn, _epoch_to_ts(t_cross), event_type, val, details):
            inserted += 1
    return inserted


def _check_indoor_outside_crossing(
    conn: sqlite3.Connection, indoor_labels: list[str], event_type: str
) -> int:
    """Detect a crossing between indoor average and outside-shade. Returns events inserted."""
    rows_shade = _recent_readings(conn, "outside-shade")
    if len(rows_shade) < 2:
        return 0

    indoor_by_ts: dict[str, list[float]] = {}
    for label in indoor_labels:
        for ts, val in _recent_readings(conn, label):
            indoor_by_ts.setdefault(ts, []).append(val)

    avg_by_ts = {ts: sum(vals) / len(vals) for ts, vals in indoor_by_ts.items()}
    shade_by_ts = {ts: val for ts, val in rows_shade}

    common = sorted(
        [(ts, avg_by_ts[ts], shade_by_ts[ts])
         for ts in avg_by_ts if ts in shade_by_ts]
    )
    if len(common) < 2:
        return 0

    inserted = 0
    for i in range(len(common) - 1):
        ts1, a1, b1 = common[i]
        ts2, a2, b2 = common[i + 1]
        result = _interpolate_crossing(_ts_to_epoch(ts1), a1, b1, _ts_to_epoch(ts2), a2, b2)
        if result is None:
            continue
        t_cross, val = result
        details = f"indoor_avg={a2:.1f}°F, outside_shade={b2:.1f}°F ({len(indoor_labels)} sensors)"
        if _insert_event(conn, _epoch_to_ts(t_cross), event_type, val, details):
            inserted += 1
    return inserted


def detect_and_insert_events(conn: sqlite3.Connection) -> int:
    """Check for parity events and write any new ones to the DB.

    Returns the number of new events inserted.
    """
    label_rows = conn.execute(
        "SELECT DISTINCT label FROM readings WHERE temp_f IS NOT NULL AND label IS NOT NULL"
    ).fetchall()
    all_labels = {r[0] for r in label_rows}

    indoor_labels = [l for l in all_labels if _is_indoor(l)]
    inserted = 0

    if "outside-sun" in all_labels and "outside-shade" in all_labels:
        inserted += _check_two_label_crossing(conn, "outside-sun", "outside-shade", "sun_shade_parity")

    if indoor_labels and "outside-shade" in all_labels:
        inserted += _check_indoor_outside_crossing(conn, indoor_labels, "inside_outside_parity")

    return inserted


def get_recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Return recent temperature events, newest first."""
    rows = conn.execute(
        "SELECT id, ts, event_type, value, details FROM temperature_events ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "event_type": r[2], "value": r[3], "details": r[4]} for r in rows]
