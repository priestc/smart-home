from __future__ import annotations
import sqlite3
from flask import Flask, jsonify, request, Response
from smart_home.db import open_db

app = Flask(__name__)
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
                SELECT MAX(id) FROM readings GROUP BY label
            )
            ORDER BY label
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/history")
def history():
    """Historical readings. Query params:
      label  - filter by sensor label (optional)
      start  - ISO datetime lower bound (optional)
      end    - ISO datetime upper bound (optional)
      limit  - max rows returned (default 1000, max 10000)
    """
    label = request.args.get("label")
    start = request.args.get("start")
    end   = request.args.get("end")
    try:
        limit = min(int(request.args.get("limit", 1000)), 200000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

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

    sql = "SELECT ts, label, temp_f, humidity, rssi FROM readings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


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
  <h1>Govee Monitor</h1>

  <div class="cards" id="cards"></div>

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

<script>
const COLORS = ["#e07820","#2e7dd4","#2a9d6e","#9b4dca","#c0392b"];
let tempChart, humChart, rangeDays = 1;

const tempCtx = document.getElementById("tempChart").getContext("2d");
const humCtx  = document.getElementById("humChart").getContext("2d");

function makeChart(ctx, label, yLabel) {
  return new Chart(ctx, {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      parsing: false,
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
  const el = document.getElementById("cards");
  el.innerHTML = data.map(s => `
    <div class="card">
      <div class="label">${s.label || s.address}</div>
      <div class="temp">${s.temp_f.toFixed(1)}°F</div>
      <div class="hum">${s.humidity.toFixed(1)}% RH</div>
      <div class="ts">${new Date((s.ts.endsWith('Z') ? s.ts : s.ts + 'Z')).toLocaleString()}</div>
    </div>`).join("");
}

async function loadCharts() {
  const start = new Date(Date.now() - rangeDays * 86400000).toISOString().slice(0,19);
  const limit = Math.max(2000, rangeDays * 24 * 60 * 10);
  const data  = await fetch(`/api/history?start=${start}&limit=${limit}`).then(r => r.json());

  // group by label, sort ascending
  const byLabel = {};
  for (const row of data) {
    const ts = row.ts.endsWith('Z') ? row.ts : row.ts + 'Z';
    (byLabel[row.label] ??= []).push({ x: new Date(ts), y: row.temp_f, h: row.humidity });
  }
  for (const pts of Object.values(byLabel)) pts.sort((a,b) => a.x - b.x);

  const labels = Object.keys(byLabel).sort();

  tempChart.data.datasets = labels.map((lbl, i) => ({
    label: lbl,
    data: byLabel[lbl].map(p => ({ x: p.x, y: p.y })),
    borderColor: COLORS[i % COLORS.length],
    backgroundColor: "transparent",
    borderWidth: 1.5,
    pointRadius: 0,
    tension: 0.3,
  }));

  humChart.data.datasets = labels.map((lbl, i) => ({
    label: lbl,
    data: byLabel[lbl].map(p => ({ x: p.x, y: p.h })),
    borderColor: COLORS[i % COLORS.length],
    backgroundColor: "transparent",
    borderWidth: 1.5,
    pointRadius: 0,
    tension: 0.3,
  }));

  tempChart.update();
  humChart.update();
}

tempChart = makeChart(tempCtx, "Temperature (°F)");
humChart  = makeChart(humCtx,  "Humidity (%)");

loadCurrent();
loadCharts();
setInterval(loadCurrent, 30000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


def run(db_path: str, host: str, port: int, debug: bool) -> None:
    global _db_path
    _db_path = db_path
    open_db(db_path).close()  # ensure schema exists
    app.run(host=host, port=port, debug=debug)
