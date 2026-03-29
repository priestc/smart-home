from __future__ import annotations
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
            "status":      s.get("status", "unknown"),
            "last_seen":   s.get("last_seen"),
            "windows":     windows,
            "recent_away": list(reversed(away_list))[:25],
        })
    return jsonify(result)


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
                ROUND(AVG(temp_f), 2)  AS temp_f,
                ROUND(AVG(humidity), 2) AS humidity,
                ROUND(AVG(rssi), 0)    AS rssi
            FROM readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, label
            ORDER BY ts ASC LIMIT ?
        """
    else:
        sql = f"SELECT ts, label, temp_f, humidity, rssi FROM readings{where_sql} ORDER BY ts ASC"

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
    .away-row { display: flex; justify-content: space-between; align-items: baseline; font-size: .85rem; padding: .4rem .6rem; border-radius: 6px; background: #f8f9fb; gap: 1rem; }
    .away-row .ar-time { color: #4a6080; }
    .away-row .ar-dur  { color: #7a90a8; font-size: .78rem; white-space: nowrap; }
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
        `<div class="away-row">
          <span class="ar-time">${fmtDt(a.start)} &rarr; ${fmtDt(a.end)}</span>
          <span class="ar-dur">${fmtDur(a.duration_secs)}</span>
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
        data.filter(r => r.label === shadeLbl && r.temp_f != null).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        const indoorMap = {};
        data.filter(r => indoorLabels.includes(r.label) && r.temp_f != null)
          .forEach(r => { (indoorMap[r.ts] ??= []).push(r.temp_f); });
        const allPts = Object.entries(indoorMap)
          .filter(([ts]) => shadeMap[ts] != null)
          .map(([ts, vals]) => ({ x: new Date(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length - shadeMap[ts] }))
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
        data.filter(r => r.label === shadeLbl && r.temp_f != null).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        data.filter(r => r.label === sunLbl   && r.temp_f != null).forEach(r => { sunMap[r.ts]   = r.temp_f; });
        const allPts = Object.keys(sunMap)
          .filter(ts => shadeMap[ts] != null)
          .map(ts => ({ x: new Date(ts), y: sunMap[ts] - shadeMap[ts] }))
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
  <div class="chart-wrap"><h2>Temperature (&deg;F)</h2><canvas id="chart" height="120"></canvas></div>
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
        data.filter(r => r.label === shadeLbl && r.temp_f != null).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        const indoorMap = {};
        data.filter(r => indoorLabels.includes(r.label) && r.temp_f != null)
          .forEach(r => { (indoorMap[r.ts] ??= []).push(r.temp_f); });
        const allPts = Object.entries(indoorMap)
          .filter(([ts]) => shadeMap[ts] != null)
          .map(([ts, vals]) => ({ x: new Date(ts), y: vals.reduce((a,b)=>a+b,0)/vals.length - shadeMap[ts] }))
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
        data.filter(r => r.label === shadeLbl && r.temp_f != null).forEach(r => { shadeMap[r.ts] = r.temp_f; });
        data.filter(r => r.label === sunLbl   && r.temp_f != null).forEach(r => { sunMap[r.ts]   = r.temp_f; });
        const allPts = Object.keys(sunMap)
          .filter(ts => shadeMap[ts] != null)
          .map(ts => ({ x: new Date(ts), y: sunMap[ts] - shadeMap[ts] }))
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
    });
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


@app.get("/api/events")
def events_api():
    """Recent temperature parity events."""
    limit = min(int(request.args.get("limit", 50)), 200)
    start = request.args.get("start", "").replace("T", " ") or None
    end   = request.args.get("end",   "").replace("T", " ") or None
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
    const badgeClass = e.event_type === "sun_shade_parity" ? "badge-sun" : "badge-io";
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
    .chart-links { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
    .chart-link { display: flex; align-items: center; justify-content: space-between; gap: 1.5rem; background: #fff; border-radius: 12px; padding: 1rem 1.5rem; min-width: 220px; text-decoration: none; color: #1a2535; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05); transition: box-shadow .15s, transform .15s; }
    .chart-link:hover { box-shadow: 0 2px 8px rgba(0,0,0,.12), 0 6px 18px rgba(0,0,0,.08); transform: translateY(-1px); }
    .chart-link .cl-title { font-size: .9rem; font-weight: 600; }
    .chart-link .cl-arrow { color: #aabbc8; font-size: 1.1rem; }
  </style>
</head>
<body>
  <h1>Smart Home &nbsp;<a href="/trends" style="font-size:.85rem;font-weight:500;color:#2e7dd4;text-decoration:none;">Trends &rarr;</a></h1>

  <div class="cards" id="cards"></div>
  <div class="presence-cards" id="presence-cards"></div>

  <div class="section-title">Charts</div>
  <div class="chart-links">
    <a href="/chart/temperature" class="chart-link"><span class="cl-title">Temperature</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/humidity"    class="chart-link"><span class="cl-title">Humidity</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/chart/differential" class="chart-link"><span class="cl-title">Differentials</span><span class="cl-arrow">&#8594;</span></a>
    <a href="/events"            class="chart-link"><span class="cl-title">Temperature Events</span><span class="cl-arrow">&#8594;</span></a>
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
loadCurrent();
loadPresence();
setInterval(loadCurrent, 30000);
setInterval(loadPresence, 30000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


def run(db_path: str, host: str, port: int, debug: bool) -> None:
    global _db_path
    _db_path = db_path
    open_db(db_path).close()  # ensure schema exists
    app.run(host=host, port=port, debug=debug)
