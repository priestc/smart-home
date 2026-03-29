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


def _interp_at(buf: list[tuple[float, float]], t: float) -> float | None:
    """Linearly interpolate a sorted (epoch, value) buffer at time t."""
    if not buf:
        return None
    if t <= buf[0][0]:
        return buf[0][1]
    if t >= buf[-1][0]:
        return buf[-1][1]
    for i in range(len(buf) - 1):
        t1, v1 = buf[i]
        t2, v2 = buf[i + 1]
        if t1 <= t <= t2:
            frac = (t - t1) / (t2 - t1) if t2 != t1 else 0.0
            return v1 + frac * (v2 - v1)
    return None


def _refine_crossing_with_buffer(
    buf_a: list[tuple[float, float]],
    buf_b: list[tuple[float, float]],
    t_coarse_start: float,
    t_coarse_end: float,
) -> tuple[float, float, float, float, float, float, float, float] | None:
    """Find the most precise crossing between series a and b using high-res buffer data.

    Filters both buffers to the coarse window, builds a joint timeline from all
    timestamps in either buffer, then finds the tightest consecutive pair that
    straddles the crossing.

    Returns (t_cross, val, t1, a1, b1, t2, a2, b2) or None.
    """
    margin = 5.0
    seg_a = [(t, v) for t, v in buf_a if t_coarse_start - margin <= t <= t_coarse_end + margin]
    seg_b = [(t, v) for t, v in buf_b if t_coarse_start - margin <= t <= t_coarse_end + margin]
    if not seg_a or not seg_b:
        return None

    all_times = sorted({t for t, _ in seg_a} | {t for t, _ in seg_b})
    joint = []
    for t in all_times:
        va = _interp_at(seg_a, t)
        vb = _interp_at(seg_b, t)
        if va is not None and vb is not None:
            joint.append((t, va, vb))

    if len(joint) < 2:
        return None

    best: tuple | None = None
    best_gap = float("inf")
    for i in range(len(joint) - 1):
        t1, a1, b1 = joint[i]
        t2, a2, b2 = joint[i + 1]
        if (a1 - b1) * (a2 - b2) >= 0:
            continue
        gap = t2 - t1
        if gap < best_gap:
            result = _interpolate_crossing(t1, a1, b1, t2, a2, b2)
            if result:
                best_gap = gap
                t_cross, val = result
                best = (t_cross, val, t1, a1, b1, t2, a2, b2)

    return best


def _refine_indoor_outdoor_crossing(
    high_res_buffer: dict[str, list[tuple[float, float]]],
    indoor_labels: list[str],
    t_coarse_start: float,
    t_coarse_end: float,
) -> tuple[float, float, float, float, float, float, float, float] | None:
    """Like _refine_crossing_with_buffer but for indoor_avg vs outside-shade."""
    margin = 5.0
    shade_buf = [(t, v) for t, v in high_res_buffer.get("outside-shade", [])
                 if t_coarse_start - margin <= t <= t_coarse_end + margin]
    indoor_bufs = {
        lbl: [(t, v) for t, v in high_res_buffer.get(lbl, [])
              if t_coarse_start - margin <= t <= t_coarse_end + margin]
        for lbl in indoor_labels
        if high_res_buffer.get(lbl)
    }
    if not shade_buf or not indoor_bufs:
        return None

    all_times = sorted(
        {t for b in indoor_bufs.values() for t, _ in b} | {t for t, _ in shade_buf}
    )
    joint = []
    for t in all_times:
        indoor_vals = [v for v in (_interp_at(b, t) for b in indoor_bufs.values()) if v is not None]
        shade_val = _interp_at(shade_buf, t)
        if indoor_vals and shade_val is not None:
            joint.append((t, sum(indoor_vals) / len(indoor_vals), shade_val))

    if len(joint) < 2:
        return None

    best: tuple | None = None
    best_gap = float("inf")
    for i in range(len(joint) - 1):
        t1, a1, b1 = joint[i]
        t2, a2, b2 = joint[i + 1]
        if (a1 - b1) * (a2 - b2) >= 0:
            continue
        gap = t2 - t1
        if gap < best_gap:
            result = _interpolate_crossing(t1, a1, b1, t2, a2, b2)
            if result:
                best_gap = gap
                t_cross, val = result
                best = (t_cross, val, t1, a1, b1, t2, a2, b2)

    return best


def _check_two_label_crossing(
    conn: sqlite3.Connection, label_a: str, label_b: str, event_type: str,
    high_res_buffer: dict[str, list[tuple[float, float]]] | None = None,
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
        t1_ep = _ts_to_epoch(ts1)
        t2_ep = _ts_to_epoch(ts2)
        coarse = _interpolate_crossing(t1_ep, a1, b1, t2_ep, a2, b2)
        if coarse is None:
            continue
        t_cross, val = coarse

        # Try to refine using high-res buffer
        d_t1, d_a1, d_b1 = ts1, a1, b1
        d_t2, d_a2, d_b2 = ts2, a2, b2
        if high_res_buffer and label_a in high_res_buffer and label_b in high_res_buffer:
            refined = _refine_crossing_with_buffer(
                high_res_buffer[label_a], high_res_buffer[label_b], t1_ep, t2_ep
            )
            if refined is not None:
                t_cross, val, r1, ra1, rb1, r2, ra2, rb2 = refined
                d_t1, d_a1, d_b1 = _epoch_to_ts(r1), ra1, rb1
                d_t2, d_a2, d_b2 = _epoch_to_ts(r2), ra2, rb2

        if high_res_buffer and high_res_buffer.get(label_a) and high_res_buffer.get(label_b):
            ta, va = min(high_res_buffer[label_a], key=lambda x: abs(x[0] - t_cross))
            tb, vb = min(high_res_buffer[label_b], key=lambda x: abs(x[0] - t_cross))
            details = (
                f"{label_a}={va:.1f}°F ({_epoch_to_ts(ta)[11:]}), "
                f"{label_b}={vb:.1f}°F ({_epoch_to_ts(tb)[11:]})"
            )
        else:
            details = f"{label_a}={d_a2:.1f}°F ({d_t2[11:]}), {label_b}={d_b2:.1f}°F ({d_t2[11:]})"
        if _insert_event(conn, _epoch_to_ts(t_cross), event_type, val, details):
            inserted += 1
    return inserted


def _check_indoor_outside_crossing(
    conn: sqlite3.Connection, indoor_labels: list[str], event_type: str,
    high_res_buffer: dict[str, list[tuple[float, float]]] | None = None,
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
        t1_ep = _ts_to_epoch(ts1)
        t2_ep = _ts_to_epoch(ts2)
        coarse = _interpolate_crossing(t1_ep, a1, b1, t2_ep, a2, b2)
        if coarse is None:
            continue
        t_cross, val = coarse

        d_t1, d_a1, d_b1 = ts1, a1, b1
        d_t2, d_a2, d_b2 = ts2, a2, b2
        if high_res_buffer:
            refined = _refine_indoor_outdoor_crossing(
                high_res_buffer, indoor_labels, t1_ep, t2_ep
            )
            if refined is not None:
                t_cross, val, r1, ra1, rb1, r2, ra2, rb2 = refined
                d_t1, d_a1, d_b1 = _epoch_to_ts(r1), ra1, rb1
                d_t2, d_a2, d_b2 = _epoch_to_ts(r2), ra2, rb2

        if high_res_buffer and high_res_buffer.get("outside-shade"):
            shade_t, shade_v = min(high_res_buffer["outside-shade"], key=lambda x: abs(x[0] - t_cross))
            indoor_near = [
                min(high_res_buffer[lbl], key=lambda x: abs(x[0] - t_cross))
                for lbl in indoor_labels if high_res_buffer.get(lbl)
            ]
            if indoor_near:
                avg_v = sum(v for _, v in indoor_near) / len(indoor_near)
                avg_t = sum(t for t, _ in indoor_near) / len(indoor_near)
                details = (
                    f"indoor_avg={avg_v:.1f}°F ({_epoch_to_ts(avg_t)[11:]}), "
                    f"outside_shade={shade_v:.1f}°F ({_epoch_to_ts(shade_t)[11:]})"
                )
            else:
                details = f"indoor_avg={d_a2:.1f}°F ({d_t2[11:]}), outside_shade={d_b2:.1f}°F ({d_t2[11:]})"
        else:
            details = f"indoor_avg={d_a2:.1f}°F ({d_t2[11:]}), outside_shade={d_b2:.1f}°F ({d_t2[11:]})"
        if _insert_event(conn, _epoch_to_ts(t_cross), event_type, val, details):
            inserted += 1
    return inserted


def detect_and_insert_events(
    conn: sqlite3.Connection,
    high_res_buffer: dict[str, list[tuple[float, float]]] | None = None,
) -> int:
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
        inserted += _check_two_label_crossing(
            conn, "outside-sun", "outside-shade", "sun_shade_parity", high_res_buffer
        )

    if indoor_labels and "outside-shade" in all_labels:
        inserted += _check_indoor_outside_crossing(
            conn, indoor_labels, "inside_outside_parity", high_res_buffer
        )

    return inserted


def get_recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Return recent temperature events, newest first."""
    rows = conn.execute(
        "SELECT id, ts, event_type, value, details FROM temperature_events ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "event_type": r[2], "value": r[3], "details": r[4]} for r in rows]
