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
    start = request.args.get("start")
    end   = request.args.get("end")
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
                strftime('%Y-%m-%dT%H:%M:%S', CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs} * {bucket_secs}, 'unixepoch') AS ts,
                label,
                ROUND(AVG(temp_f), 2)  AS temp_f,
                ROUND(AVG(humidity), 2) AS humidity,
                ROUND(AVG(rssi), 0)    AS rssi
            FROM readings{where_sql}
            GROUP BY CAST(strftime('%s', ts) AS INTEGER) / {bucket_secs}, label
            ORDER BY ts DESC LIMIT ?
        """
    else:
        sql = f"SELECT ts, label, temp_f, humidity, rssi FROM readings{where_sql} ORDER BY ts DESC"

    if bucket > 1:
        params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


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
          ticks: { color: "#7a90a8", callback: v => v + "°F" },
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


@app.get("/")
def index():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Govee Monitor</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4f8; color: #1a2535; padding: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; color: #1a2535; letter-spacing: -.02em; }
    .cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .card {
      background: #fff; border-radius: 12px; padding: 1.1rem 1.5rem; min-width: 190px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05);
    }
    .card .label { font-size: 0.75rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }
    .card .temp  { font-size: 2.4rem; font-weight: 700; color: #e07820; margin: .2rem 0 .1rem; line-height: 1; }
    .card .hum   { font-size: 1rem; color: #2e7dd4; font-weight: 500; }
    .card .ts    { font-size: 0.72rem; color: #aabbc8; margin-top: .5rem; }
    .chart-wrap  {
      background: #fff; border-radius: 12px; padding: 1.4rem 1.4rem 1rem;
      margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05);
    }
    .chart-wrap h2 { font-size: 0.85rem; color: #7a90a8; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 1rem; }
    .presence-cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .presence-card {
      background: #fff; border-radius: 12px; padding: 1rem 1.5rem; min-width: 160px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08), 0 4px 12px rgba(0,0,0,.05);
      display: flex; align-items: center; gap: .9rem;
    }
    .presence-dot { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
    .presence-dot.home { background: #2a9d6e; }
    .presence-dot.away { background: #c0392b; }
    .presence-dot.unknown { background: #aabbc8; }
    .presence-info .name { font-size: .85rem; font-weight: 600; color: #1a2535; }
    .presence-info .status { font-size: .75rem; color: #7a90a8; margin-top: .15rem; }
    .range-btns  { margin-bottom: 1.5rem; display: flex; gap: .4rem; flex-wrap: wrap; }
    .range-btns button {
      background: #fff; color: #4a6080; border: 1px solid #d0dce8;
      border-radius: 6px; padding: .35rem 1rem; cursor: pointer; font-size: .85rem;
      font-weight: 500; transition: all .15s;
    }
    .range-btns button:hover { background: #f0f4f8; border-color: #aabbc8; }
    .range-btns button.active { background: #e07820; color: #fff; border-color: #e07820; }
  </style>
</head>
<body>
  <h1>Smart Home &nbsp;<a href="/trends" style="font-size:.85rem;font-weight:500;color:#2e7dd4;text-decoration:none;">Trends &rarr;</a></h1>

  <div class="cards" id="cards"></div>

  <div class="presence-cards" id="presence-cards"></div>

  <div class="range-btns">
    <button onclick="setRange(0.125)" >3h</button>
    <button onclick="setRange(1)"  class="active">24h</button>
    <button onclick="setRange(3)"  >3d</button>
    <button onclick="setRange(7)"  >7d</button>
    <button onclick="setRange(30)" >30d</button>
  </div>

  <div class="chart-wrap">
    <h2>Temperature (°F)</h2>
    <canvas id="tempChart" height="90"></canvas>
  </div>

  <div class="chart-wrap">
    <h2>Humidity (%)</h2>
    <canvas id="humChart" height="90"></canvas>
  </div>

  <div class="chart-wrap" id="diffWrap" style="display:none">
    <h2>Inside / Outside Differential (°F)</h2>
    <canvas id="diffChart" height="90"></canvas>
  </div>

<script>
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b"];
const colorMap = {};

function labelColor(lbl) {
  return colorMap[lbl] ?? COLORS[0];
}
let tempChart, humChart, diffChart, rangeDays = 1;

const tempCtx = document.getElementById("tempChart").getContext("2d");
const humCtx  = document.getElementById("humChart").getContext("2d");
const diffCtx = document.getElementById("diffChart").getContext("2d");

function makeChart(ctx, label, yLabel) {
  return new Chart(ctx, {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      parsing: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#4a6080" } } },
      scales: {
        x: {
          type: "time",
          time: { tooltipFormat: "MMM d, h:mm a" },
          ticks: { color: "#7a90a8", maxTicksLimit: 8 },
          grid:  { color: "#e8eef4" }
        },
        y: {
          ticks: { color: "#7a90a8" },
          grid:  { color: "#e8eef4" },
          title: { display: false }
        }
      }
    }
  });
}

function setRange(days) {
  rangeDays = days;
  document.querySelectorAll(".range-btns button").forEach((b,i) => {
    b.classList.toggle("active", [0.125,1,3,7,30][i] === days);
  });
  loadCharts();
}

async function loadCurrent() {
  const data = await fetch("/api/current").then(r => r.json());
  // assign colors by sorted label position so they're consistent across all charts
  data.map(s => s.label).filter(Boolean).sort()
    .forEach((lbl, i) => { colorMap[lbl] = COLORS[i % COLORS.length]; });
  const el = document.getElementById("cards");
  el.innerHTML = data.map(s => `
    <div class="card">
      <div class="label">${s.label || s.address}</div>
      <div class="temp">${s.temp_f.toFixed(1)}°F</div>
      <div class="hum">${s.humidity.toFixed(1)}% RH</div>
      <div class="ts">${new Date(s.ts).toLocaleString()}</div>
    </div>`).join("");
}

function localISO(date) {
  const p = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth()+1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
}

async function loadCharts() {
  const start = localISO(new Date(Date.now() - rangeDays * 86400000));
  const bucketMap = { 0.125: 1, 1: 2, 3: 10, 7: 20, 30: 60 };
  const bucket = bucketMap[rangeDays] || 1;
  const data  = await fetch(`/api/history?start=${start}&limit=8000&bucket_minutes=${bucket}`).then(r => r.json());

  // group by label, sort ascending
  const byLabel = {};
  for (const row of data) {
    (byLabel[row.label] ??= []).push({ x: new Date(row.ts), y: row.temp_f, h: row.humidity });
  }
  for (const pts of Object.values(byLabel)) pts.sort((a,b) => a.x - b.x);

  const labels = Object.keys(byLabel).sort();

  tempChart.data.datasets = labels.map(lbl => ({
    label: lbl,
    data: byLabel[lbl].map(p => ({ x: p.x, y: p.y })),
    borderColor: labelColor(lbl),
    backgroundColor: "transparent",
    borderWidth: 1.5,
    pointRadius: 0,
    tension: 0,
  }));

  humChart.data.datasets = labels.map(lbl => ({
    label: lbl,
    data: byLabel[lbl].map(p => ({ x: p.x, y: p.h })),
    borderColor: labelColor(lbl),
    backgroundColor: "transparent",
    borderWidth: 1.5,
    pointRadius: 0,
    tension: 0,
  }));

  const xMin = new Date(Date.now() - rangeDays * 86400000);
  const xMax = new Date();
  const timeUnit = rangeDays >= 3 ? "day" : "hour";
  for (const chart of [tempChart, humChart]) {
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.options.scales.x.time.unit = timeUnit;
  }

  // Inside / Outside differential
  const insideLabel  = Object.keys(byLabel).find(l => l.toLowerCase() === "inside");
  const outsideLabel = Object.keys(byLabel).find(l => l.toLowerCase() === "outside");
  const diffWrap = document.getElementById("diffWrap");
  if (insideLabel && outsideLabel) {
    const outsideMap = new Map();
    for (const row of data) {
      if (row.label === outsideLabel && row.temp_f != null) outsideMap.set(row.ts, row.temp_f);
    }
    const diffData = [];
    for (const row of data) {
      if (row.label === insideLabel && row.temp_f != null) {
        const outTemp = outsideMap.get(row.ts);
        if (outTemp != null) diffData.push({ x: new Date(row.ts), y: Math.round((row.temp_f - outTemp) * 10) / 10 });
      }
    }
    diffData.sort((a, b) => a.x - b.x);
    diffChart.data.datasets = [{
      label: "Inside − Outside",
      data: diffData,
      borderColor: "#9b4dca",
      backgroundColor: "transparent",
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0,
    }];
    diffChart.options.scales.x.min = xMin;
    diffChart.options.scales.x.max = xMax;
    diffChart.options.scales.x.time.unit = timeUnit;
    diffChart.update();
    diffWrap.style.display = "";
  } else {
    diffWrap.style.display = "none";
  }

  tempChart.update();
  humChart.update();
}

tempChart = makeChart(tempCtx, "Temperature (°F)");
humChart  = makeChart(humCtx,  "Humidity (%)");
diffChart = new Chart(diffCtx, {
  type: "line",
  data: { datasets: [] },
  options: {
    animation: false,
    parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false } },
    scales: {
      x: {
        type: "time",
        time: { tooltipFormat: "MMM d, h:mm a" },
        ticks: { color: "#7a90a8", maxTicksLimit: 8 },
        grid:  { color: "#e8eef4" }
      },
      y: {
        ticks: { color: "#7a90a8", callback: v => v + "°F" },
        grid:  { color: ctx => ctx.tick.value === 0 ? "#aabbc8" : "#e8eef4" }
      }
    }
  }
});

async function loadPresence() {
  const data = await fetch("/api/presence").then(r => r.json());
  const el = document.getElementById("presence-cards");
  if (!data.length) { el.innerHTML = ""; return; }
  el.innerHTML = data.map(d => {
    const ago = d.last_seen ? timeSince(new Date(d.last_seen)) : "never";
    return `<div class="presence-card">
      <div class="presence-dot ${d.status}"></div>
      <div class="presence-info">
        <div class="name">${d.name}</div>
        <div class="status">${d.status} &middot; ${ago}</div>
      </div>
    </div>`;
  }).join("");
}

function timeSince(date) {
  const s = Math.floor((Date.now() - date) / 1000);
  if (s < 60)   return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ago`;
  return `${Math.floor(s/86400)}d ago`;
}

loadCurrent().then(loadCharts);
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
