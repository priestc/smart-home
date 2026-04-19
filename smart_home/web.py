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
    """Latest reading for each sensor label."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT label, temp_f, humidity, rssi, ts
            FROM readings
            WHERE id IN (
                SELECT MAX(id) FROM readings WHERE temp_f IS NOT NULL GROUP BY label
            )
            ORDER BY label
        """).fetchall()
    return jsonify([dict(r) for r in rows])


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


@app.get("/api/presence/history")
def presence_history_api():
    """Per-device presence stats (7d / 30d) and recent away periods."""
    import datetime
    from smart_home.presence import load_history, load_devices, load_state
    entries = load_history()
    devices = load_devices()
    state   = load_state()
    if not devices:
        return jsonify([])
    now = datetime.datetime.now()
    by_device: dict = {}
    for e in entries:
        by_device.setdefault(e["ble_name"], []).append(e)

    def _periods(dev_entries, window_start):
        pre    = [e for e in dev_entries if e["ts"] <  window_start.isoformat()]
        in_win = [e for e in dev_entries if e["ts"] >= window_start.isoformat()]
        initial = pre[-1]["status"] if pre else "unknown"
        trans = [(window_start, initial)]
        for e in in_win:
            trans.append((datetime.datetime.fromisoformat(e["ts"]), e["status"]))
        trans.append((now, None))
        out = []
        for i in range(len(trans) - 1):
            s, status = trans[i]
            e2 = trans[i + 1][0]
            if status and status != "unknown":
                out.append((s, e2, status))
        return out

    result = []
    for ble_name, label in sorted(devices.items(), key=lambda x: x[1]):
        s = state.get(ble_name, {})
        dev_entries = sorted(by_device.get(ble_name, []), key=lambda e: e["ts"])
        windows = {}
        for days in (7, 30):
            periods = _periods(dev_entries, now - datetime.timedelta(days=days))
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
        # recent away periods from last 90 days
        away_list = [
            {
                "start": s.isoformat(timespec="seconds"),
                "end":   e.isoformat(timespec="seconds"),
                "duration_secs": round((e - s).total_seconds()),
            }
            for s, e, st in _periods(dev_entries, now - datetime.timedelta(days=90))
            if st == "away"
        ]
        result.append({
            "name":        label,
            "ble_name":    ble_name,
            "status":      s.get("status", "unknown"),
            "last_seen":   s.get("last_seen"),
            "windows":     windows,
            "recent_away": list(reversed(away_list))[:25],
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


@app.get("/api/presence")
def presence():
    """Current presence status for all registered devices."""
    from smart_home.presence import load_state, load_devices
    devices = load_devices()
    state = load_state()
    result = []
    for ble_name, label in devices.items():
        s = state.get(ble_name, {})
        result.append({
            "ble_name": ble_name,
            "name": label,
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
            ORDER BY ts ASC LIMIT ?
        """
    else:
        sql = f"SELECT ts, label, temp_f, humidity, rssi, battery FROM readings{where_sql} ORDER BY ts ASC"

    if bucket > 1:
        params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
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
    .device-name { font-size: 1.1rem; font-weight: 700; }
    .device-sub  { font-size: .78rem; color: #7a90a8; margin-top: .1rem; }
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
  </style>
</head>
<body>
  <h1>Presence</h1>
  <div class="nav"><a href="/">&larr; Dashboard</a></div>
  <div id="content"><p class="empty">Loading&hellip;</p></div>

<script>
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
      <div><div class="device-name">${d.name}</div><div class="device-sub">${d.status} &middot; ${sub}</div></div>
    </div>
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
  const data = await fetch("/api/presence/history").then(r => r.json());
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
  const data = await fetch("/api/trends").then(r => r.json());
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
  const data = await fetch("/api/minmax-tod").then(r => r.json());
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
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b"];
const colorMap = {};
function labelColor(lbl) { return colorMap[lbl] ?? COLORS[0]; }
let rangeDays = 1;
function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function loadColors() {
  const data = await fetch("/api/current").then(r => r.json());
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
  const data = await fetch(`/api/history?start=${start}&limit=8000&bucket_minutes=${bucket}`).then(r => r.json());
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
  const data = await fetch("/api/current").then(r => r.json());
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
      fetch(`/api/history?${params}`).then(r => r.json()),
      fetch(`/api/events?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=200`).then(r => r.json()),
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
    const peek = await fetch(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`).then(r => r.json());
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    const data = await fetch(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`).then(r => r.json());
    chart.data.datasets = buildSensorDatasets(data, [], true);
    const xMin = new Date(2000, activeMonth - 1, 1);
    const xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "day";
  } else {
    const data = await fetch(`/api/history/year?bucket_minutes=${getBucket()}`).then(r => r.json());
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
  const data = await fetch("/api/current").then(r => r.json());
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
async function fetchJSON(url) {
  const r = await fetch(url);
  const cl = r.headers.get('content-length');
  const text = await r.text();
  const bytes = cl !== null ? parseInt(cl) : new TextEncoder().encode(text).length;
  return { data: JSON.parse(text), bytes };
}
async function loadChart() {
  let totalBytes = 0;
  if (mode === "recent") {
    const xMax = new Date(Date.now() + offsetMs);
    const xMin = new Date(xMax - rangeDays * 86400000);
    const params = `start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`;
    const [hist, evts] = await Promise.all([
      fetchJSON(`/api/history?${params}`),
      fetchJSON(`/api/events?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=200`),
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
    const peek = await fetch(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`).then(r => r.json());
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    const { data, bytes } = await fetchJSON(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`);
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
      fetchJSON(`/api/history?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=8000&bucket_minutes=${getBucket()}`),
      fetchJSON(`/api/events?start=${localISO(xMin)}&end=${localISO(xMax)}&limit=200`),
    ]);
    totalBytes = hist.bytes + evts.bytes;
    chart.data.datasets = buildSensorDatasets(hist.data, evts.data, false);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "hour";
    chart.options.scales.x.ticks.stepSize = 1;
  } else {
    const { data, bytes } = await fetchJSON(`/api/history/year?bucket_minutes=${getBucket()}`);
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
  const years = await fetch("/api/history/years").then(r => r.json());
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
  const data = await fetch("/api/current").then(r => r.json());
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
    data = await fetch(`/api/history?${params}`).then(r => r.json());
    batteryChart.options.scales.x.min = xMin;
    batteryChart.options.scales.x.max = xMax;
    batteryChart.options.scales.x.time.unit = rangeDays === 0.125 ? "minute" : rangeDays === 1 ? "hour" : "day";
    batteryChart.options.scales.x.ticks.stepSize = rangeDays === 0.125 ? 30 : 1;
    const peek = await fetch(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`).then(r => r.json());
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    data = await fetch(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`).then(r => r.json());
    const xMin = new Date(2000, activeMonth - 1, 1), xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    batteryChart.options.scales.x.min = xMin;
    batteryChart.options.scales.x.max = xMax;
    batteryChart.options.scales.x.time.unit = "day";
  } else {
    data = await fetch(`/api/history/year?bucket_minutes=${getBucket()}`).then(r => r.json());
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
  const data = await fetch("/api/current").then(r => r.json());
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
    data = await fetch(`/api/history?${params}`).then(r => r.json());
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = rangeDays === 0.125 ? "minute" : rangeDays === 1 ? "hour" : "day";
    chart.options.scales.x.ticks.stepSize = rangeDays === 0.125 ? 30 : 1;
    const peek = await fetch(`/api/history?end=${localISO(xMin)}&limit=1&bucket_minutes=${getBucket()}`).then(r => r.json());
    document.getElementById('btn-prev').disabled = peek.length === 0;
    document.getElementById('btn-next').disabled = offsetMs >= 0;
  } else if (mode === "month") {
    data = await fetch(`/api/history/month?month=${activeMonth}&bucket_minutes=${getBucket()}`).then(r => r.json());
    const xMin = new Date(2000, activeMonth - 1, 1), xMax = new Date(2000, activeMonth, 0, 23, 59, 59);
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = "day";
  } else {
    data = await fetch(`/api/history/year?bucket_minutes=${getBucket()}`).then(r => r.json());
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
const EVENT_LABELS = """ + str(EVENT_LABELS).replace("'", '"') + """;
async function load() {
  const data = await fetch("/api/events?limit=100").then(r => r.json());
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
    .chart-links { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
    .chart-link { display: flex; align-items: center; justify-content: space-between; gap: 1.5rem; background: #fff; border-radius: 12px; padding: 1rem 1.5rem; min-width: 220px; text-decoration: none; color: #1a2535; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); transition: box-shadow .15s, transform .15s; }
    .chart-link:hover { box-shadow: 0 2px 8px rgba(0,0,0,.12), 0 6px 18px rgba(0,0,0,.08); transform: translateY(-1px); }
    .chart-link .cl-title { font-size: .9rem; font-weight: 600; }
    .chart-link .cl-arrow { color: #aabbc8; font-size: 1.1rem; }
  </style>
</head>
<body>
  <h1>Smart Home &nbsp;<a href="/trends" style="font-size:.85rem;font-weight:500;color:#2e7dd4;text-decoration:none;">Trends &rarr;</a></h1>

  <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:2rem">
    <div class="cards" id="cards" style="margin-bottom:0;flex-wrap:wrap;display:flex;gap:1rem"></div>
    <div class="garage-cards" id="garage-cards" style="margin-bottom:0"></div>
  </div>
  <div class="presence-cards" id="presence-cards"></div>

  <div class="section-title">Charts</div>
  <div class="chart-links">
    <a href="/chart/temperature" class="chart-link"><span class="cl-title">Temperature</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/humidity"    class="chart-link"><span class="cl-title">Humidity</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/differential" class="chart-link"><span class="cl-title">Differentials</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/sensors"     class="chart-link"><span class="cl-title">Sensor Battery Life</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/signal"      class="chart-link"><span class="cl-title">Signal Strength</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/events"            class="chart-link"><span class="cl-title">Temperature Events</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/process-stats"     class="chart-link"><span class="cl-title">Process Stats</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/camera"            class="chart-link"><span class="cl-title">Cameras</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/garage"            class="chart-link"><span class="cl-title">Garage Door</span><span class="cl-arrow">&#8594;</span></a>
  </div>

  <div id="events-wrap" style="display:none">
    <div class="section-title">Events &nbsp;<a href="/events" style="font-size:.75rem;font-weight:500;color:#2e7dd4;text-decoration:none;">View all &rarr;</a></div>
    <div class="events-list" id="events-list"></div>
  </div>

<script>
async function loadCurrent() {
  const data = await fetch("/api/current").then(r => r.json());
  document.getElementById("cards").innerHTML = data.map(s => `
    <div class="card">
      <div class="label">${s.label || s.address}</div>
      <div class="temp">${s.temp_f.toFixed(1)}&deg;F</div>
      <div class="hum">${s.humidity.toFixed(1)}% RH</div>
      <div class="ts">${new Date(s.ts).toLocaleString()}</div>
    </div>`).join("");
}
async function loadPresence() {
  const data = await fetch("/api/presence").then(r => r.json());
  const el = document.getElementById("presence-cards");
  if (!data.length) { el.innerHTML = ""; return; }
  el.innerHTML = data.map(d => {
    const ago = d.last_seen ? timeSince(new Date(d.last_seen)) : "never";
    return `<a href="/presence" class="presence-card" style="text-decoration:none;color:inherit">
      <div class="presence-dot ${d.status}"></div>
      <div class="presence-info">
        <div class="name">${d.name}</div>
        <div class="status">${d.status} &middot; ${ago}</div>
      </div>
    </a>`;
  }).join("");
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
  const data = await fetch("/api/events?limit=15").then(r => r.json());
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
}
const garageOpenSince = {};  // name -> ms timestamp when last opened
function fmtDur(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sc = s % 60;
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
}
setInterval(tickGarageTimers, 1000);
async function loadGarage() {
  const garages = await fetch("/api/garage").then(r => r.json());
  if (!garages.length) return;
  const results = await Promise.all(garages.map(g =>
    fetch(`/api/garage/${encodeURIComponent(g.name)}/status`).then(r => r.json())
      .then(s => ({ name: g.name, ...s })).catch(() => ({ name: g.name, ok: false }))
  ));
  const el = document.getElementById("garage-cards");
  el.innerHTML = results.map(d => {
    let stateClass = "unknown", stateText = "?";
    if (d.ok) {
      if (d.door_closed === true)  { stateClass = "closed"; stateText = "CLOSED"; }
      else if (d.door_closed === false) { stateClass = "open"; stateText = "OPEN"; }
    } else { stateText = "⚠"; }
    if (d.door_closed === false && d.last_opened) {
      garageOpenSince[d.name] = new Date(d.last_opened.replace(" ", "T")).getTime();
    } else if (d.door_closed !== false) {
      delete garageOpenSince[d.name];
    }
    return `<a href="/garage" class="garage-card">
      <div class="label">${d.name}</div>
      <div class="gstate ${stateClass}">${stateText}</div>
      <div class="gtimer" id="gtimer-${d.name}"></div>
    </a>`;
  }).join("");
}
loadCurrent();
loadPresence();
loadEvents();
loadGarage();
setInterval(loadCurrent, 30000);
setInterval(loadPresence, 30000);
setInterval(loadEvents, 60000);
setInterval(loadGarage, 15000);
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
            rows = conn.execute(
                "SELECT ts, cpu_percent, mem_mb FROM process_stats WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (start, end),
            ).fetchall()
        else:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            rows = conn.execute(
                "SELECT ts, cpu_percent, mem_mb FROM process_stats WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
    return jsonify([{"ts": r[0], "cpu": r[1], "mem": r[2]} for r in rows])


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
let rangeDays = 1;

function makeChart(id, label, color, yLabel) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: [{ label, data: [], borderColor: color, backgroundColor: color + "22",
                         borderWidth: 1.5, pointRadius: 0, tension: 0, fill: true }] },
    options: {
      animation: false, parsing: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${ctx.raw.y != null ? ctx.raw.y.toFixed(1) : "—"} ${yLabel}` } } },
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" },
             ticks: { color: "#7a90a8", maxTicksLimit: 20 }, grid: { color: "#e8eef4" } },
        y: { min: 0, ticks: { color: "#7a90a8", callback: v => v + " " + yLabel }, grid: { color: "#e8eef4" } }
      }
    }
  });
}

const cpuChart = makeChart("cpu-chart", "CPU %",    "#e07820", "%");
const memChart = makeChart("mem-chart", "Memory MB", "#2e7dd4", "MB");

function setRange(days) {
  rangeDays = days;
  document.querySelectorAll(".range-btns button[data-days]").forEach(b =>
    b.classList.toggle("active", parseFloat(b.dataset.days) === days));
  load();
}

async function load() {
  const data = await fetch(`/api/process-stats?days=${rangeDays}`).then(r => r.json());
  const now = new Date();
  const xMin = new Date(now - rangeDays * 86400000);
  [cpuChart, memChart].forEach(c => { c.options.scales.x.min = xMin; c.options.scales.x.max = now;
    c.options.scales.x.time.unit = rangeDays <= 1 ? "hour" : "day"; });
  cpuChart.data.datasets[0].data = data.map(r => ({ x: new Date(r.ts), y: r.cpu }));
  memChart.data.datasets[0].data = data.map(r => ({ x: new Date(r.ts), y: r.mem }));
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
    return jsonify([{"name": c["name"], "zones": c.get("zones", [])} for c in cameras])


@app.get("/api/camera/events/<name>")
def api_camera_events(name):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, zone, pct, screenshot IS NOT NULL AS has_image FROM camera_events WHERE camera=? ORDER BY ts DESC LIMIT 100",
            (name,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/camera/temp/<name>")
def api_camera_temp(name):
    days = request.args.get("days", 1, type=float)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, temp_c FROM camera_temps WHERE camera=? AND ts >= datetime('now', ?) ORDER BY ts ASC",
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
      </div>
    </div>
    <div class="panel">
      <div class="section-title">Recent Motion Events</div>
      <div id="events-wrap"><p class="empty">Loading&hellip;</p></div>
    </div>
    <div class="panel" id="temp-panel" style="display:none">
      <div class="section-title" style="display:flex;justify-content:space-between;align-items:center">
        <span>Camera Temperature (&deg;C)</span>
        <span id="temp-range-btns" style="display:flex;gap:.4rem">
          <button onclick="loadTemp(0.125)" data-days="0.125" style="background:#fff;border:1px solid #d0dce8;border-radius:5px;padding:.2rem .6rem;cursor:pointer;font-size:.75rem;color:#4a6080">3h</button>
          <button onclick="loadTemp(1)" data-days="1" style="background:#2e7dd4;border:1px solid #2e7dd4;border-radius:5px;padding:.2rem .6rem;cursor:pointer;font-size:.75rem;color:#fff">24h</button>
          <button onclick="loadTemp(7)" data-days="7" style="background:#fff;border:1px solid #d0dce8;border-radius:5px;padding:.2rem .6rem;cursor:pointer;font-size:.75rem;color:#4a6080">7d</button>
        </span>
      </div>
      <canvas id="temp-chart" height="100" style="margin-top:.8rem"></canvas>
    </div>
  </div>

<script>
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

async function loadEvents() {
  if (!activeCam) return;
  const data = await fetch(`/api/camera/events/${encodeURIComponent(activeCam)}`).then(r => r.json());
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

let tempChart = null, tempRangeDays = 1;

async function loadTemp(days) {
  if (!activeCam) return;
  if (days !== undefined) {
    tempRangeDays = days;
    document.querySelectorAll("#temp-range-btns button").forEach(b => {
      const active = parseFloat(b.dataset.days) === days;
      b.style.background = active ? "#2e7dd4" : "#fff";
      b.style.color = active ? "#fff" : "#4a6080";
      b.style.borderColor = active ? "#2e7dd4" : "#d0dce8";
    });
  }
  const data = await fetch(`/api/camera/temp/${encodeURIComponent(activeCam)}?days=${tempRangeDays}`).then(r => r.json());
  const panel = document.getElementById("temp-panel");
  if (!data.length) { panel.style.display = "none"; return; }
  panel.style.display = "";
  const pts = data.map(r => ({ x: new Date(r.ts), y: r.temp_c }));
  if (tempChart) {
    tempChart.data.datasets[0].data = pts;
    tempChart.update();
  } else {
    tempChart = new Chart(document.getElementById("temp-chart"), {
      type: "line",
      data: { datasets: [{ data: pts, borderColor: "#e07820", backgroundColor: "transparent",
                           borderWidth: 1.5, pointRadius: 0, tension: 0 }] },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { type: "time", time: { tooltipFormat: "MMM d, h:mm a" },
               grid: { color: "#f0f4f8" }, ticks: { color: "#7a90a8", maxTicksLimit: 8 } },
          y: { grid: { color: "#f0f4f8" }, ticks: { color: "#7a90a8" } },
        },
      },
    });
  }
}

function switchCam(name) {
  activeCam = name;
  document.querySelectorAll(".cam-tab").forEach(b =>
    b.classList.toggle("active", b.dataset.cam === name));
  document.getElementById("zones-link").href = `/camera/zones?cam=${encodeURIComponent(name)}`;
  document.getElementById("main").style.display = "";
  live = true;
  document.querySelector(".feed-actions button").textContent = "Pause";
  startLive();
  loadEvents();
  loadTemp();
  setInterval(loadEvents, 30000);
  setInterval(loadTemp, 60000);
}

async function init() {
  const data = await fetch("/api/cameras").then(r => r.json());
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
  const data = await fetch(`/api/camera/zones/${encodeURIComponent(activeCam)}`).then(r => r.json());
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
  const data = await fetch("/api/cameras").then(r => r.json());
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
            row = conn.execute(
                "SELECT ts FROM garage_events WHERE name=? AND state='open' ORDER BY ts DESC LIMIT 1",
                (name,),
            ).fetchone()
        last_opened = row["ts"] if row else None
        return jsonify({
            "ok": True,
            "output": status.get("output", False),
            "door_closed": status.get("door_closed"),
            "last_opened": last_opened,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.get("/api/garage/<name>/events")
def api_garage_events(name):
    limit = min(int(request.args.get("limit", 200)), 1000)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, state FROM garage_events WHERE name=? ORDER BY ts DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    return jsonify([{"ts": r["ts"], "state": r["state"]} for r in rows])


@app.post("/api/garage/<name>/auto")
def api_garage_auto(name):
    from smart_home import garage as _garage
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("auto", False))
    _garage.set_auto(name, enabled)
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
                  cursor: pointer; user-select: none; }
    .auto-label input[type=checkbox] { width: 1rem; height: 1rem; cursor: pointer; accent-color: #2e7dd4; }
    #no-garages { color: #7a90a8; font-size: .9rem; }
    .history { margin-top: 2rem; }
    .history h2 { font-size: 1rem; font-weight: 700; color: #1a2535; margin-bottom: .8rem;
                  letter-spacing: -.01em; }
    .history-table { width: 100%; max-width: 540px; border-collapse: collapse; font-size: .85rem; }
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
      <thead><tr><th>Door</th><th>State</th><th>Time</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>
<script>
const openSince = {};  // name -> Date when door first seen open

function fmtDuration(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sc = s % 60;
  if (h > 0) return `${h}h ${m}m ${sc}s`;
  if (m > 0) return `${m}m ${sc}s`;
  return `${sc}s`;
}

function tickTimers() {
  const now = Date.now();
  for (const [name, since] of Object.entries(openSince)) {
    const el = document.getElementById(`timer-${name}`);
    if (el) el.textContent = "Open for " + fmtDuration(now - since);
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
  btn.textContent = "Trigger";
}

function applyStatus(name, data) {
  const stateEl = document.getElementById(`state-${name}`);
  const timerEl = document.getElementById(`timer-${name}`);
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
    timerEl.textContent = "";
    delete openSince[name];
  } else if (data.door_closed === false) {
    stateEl.textContent = "OPEN";
    stateEl.className = "door-state open";
    if (data.last_opened) {
      // Server timestamp is local time ("YYYY-MM-DD HH:MM:SS"); parse as local
      openSince[name] = new Date(data.last_opened.replace(" ", "T")).getTime();
    } else if (!openSince[name]) {
      openSince[name] = Date.now();
    }
  } else {
    stateEl.textContent = "UNKNOWN";
    stateEl.className = "door-state unknown";
    timerEl.textContent = "";
    delete openSince[name];
  }
}

async function refreshStatus(name) {
  const data = await fetch(`/api/garage/${encodeURIComponent(name)}/status`).then(r => r.json());
  applyStatus(name, data);
}

async function loadHistory(garages) {
  const allEvents = [];
  for (const g of garages) {
    const evts = await fetch(`/api/garage/${encodeURIComponent(g.name)}/events`).then(r => r.json());
    for (const e of evts) allEvents.push({name: g.name, ...e});
  }
  allEvents.sort((a, b) => b.ts.localeCompare(a.ts));
  if (!allEvents.length) return;
  document.getElementById("history").style.display = "";
  document.getElementById("history-body").innerHTML = allEvents.map(e => `
    <tr>
      <td>${e.name}</td>
      <td class="state-${e.state}">${e.state.toUpperCase()}</td>
      <td>${e.ts}</td>
    </tr>`).join("");
}

async function load() {
  const garages = await fetch("/api/garage").then(r => r.json());
  const el = document.getElementById("doors");
  if (!garages.length) {
    document.getElementById("no-garages").style.display = "";
    return;
  }
  el.innerHTML = garages.map(g => `
    <div class="door-card">
      <div class="door-name">${g.name}</div>
      <div class="door-state unknown" id="state-${g.name}">…</div>
      <div class="open-timer" id="timer-${g.name}"></div>
      <button class="trigger-btn" id="btn-${g.name}"
        onclick="trigger('${g.name}', this, document.getElementById('last-${g.name}'))">Trigger</button>
      <div class="last-triggered" id="last-${g.name}"></div>
      <label class="auto-label">
        <input type="checkbox" id="auto-${g.name}"
          onchange="setAuto('${g.name}', this.checked)">
        Automatically open/close
      </label>
    </div>`).join("");

  for (const g of garages) {
    const autoEl = document.getElementById(`auto-${g.name}`);
    if (autoEl) autoEl.checked = !!g.auto;
    refreshStatus(g.name);
  }
  loadHistory(garages);
}

load();
setInterval(() => fetch("/api/garage").then(r => r.json()).then(gs => gs.forEach(g => refreshStatus(g.name))), 10000);
</script>
</body>
</html>"""


@app.get("/garage")
def garage_page():
    return Response(_GARAGE_PAGE, mimetype="text/html")


def run(db_path: str, host: str, port: int, debug: bool) -> None:
    global _db_path
    _db_path = db_path
    open_db(db_path).close()  # ensure schema exists
    app.run(host=host, port=port, debug=debug)
