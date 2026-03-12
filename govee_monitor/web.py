from __future__ import annotations
import sqlite3
from flask import Flask, jsonify, request, Response

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
        limit = min(int(request.args.get("limit", 1000)), 10000)
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
    body { font-family: system-ui, sans-serif; background: #111; color: #eee; padding: 1.5rem; }
    h1 { font-size: 1.4rem; margin-bottom: 1.5rem; color: #fff; }
    .cards { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .card { background: #1e1e1e; border-radius: 8px; padding: 1rem 1.5rem; min-width: 180px; }
    .card .label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: .05em; }
    .card .temp  { font-size: 2.2rem; font-weight: 600; color: #f0a040; margin: .25rem 0; }
    .card .hum   { font-size: 1rem; color: #60b0f0; }
    .card .ts    { font-size: 0.75rem; color: #555; margin-top: .4rem; }
    .chart-wrap  { background: #1e1e1e; border-radius: 8px; padding: 1.2rem; margin-bottom: 1.5rem; }
    .chart-wrap h2 { font-size: 1rem; color: #aaa; margin-bottom: 1rem; }
    .range-btns  { margin-bottom: 1.5rem; display: flex; gap: .5rem; flex-wrap: wrap; }
    .range-btns button {
      background: #2a2a2a; color: #ccc; border: 1px solid #333;
      border-radius: 5px; padding: .35rem .9rem; cursor: pointer; font-size: .85rem;
    }
    .range-btns button.active { background: #f0a040; color: #111; border-color: #f0a040; }
  </style>
</head>
<body>
  <h1>Govee Monitor</h1>

  <div class="cards" id="cards"></div>

  <div class="range-btns">
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
const COLORS = ["#f0a040","#60b0f0","#80e080","#e060e0","#e08060"];
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
      plugins: { legend: { labels: { color: "#ccc" } } },
      scales: {
        x: {
          type: "time",
          time: { tooltipFormat: "MMM d, h:mm a" },
          ticks: { color: "#777", maxTicksLimit: 8 },
          grid:  { color: "#2a2a2a" }
        },
        y: {
          ticks: { color: "#777" },
          grid:  { color: "#2a2a2a" },
          title: { display: false }
        }
      }
    }
  });
}

function setRange(days) {
  rangeDays = days;
  document.querySelectorAll(".range-btns button").forEach((b,i) => {
    b.classList.toggle("active", [1,3,7,30][i] === days);
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
      <div class="ts">${s.ts}</div>
    </div>`).join("");
}

async function loadCharts() {
  const start = new Date(Date.now() - rangeDays * 86400000).toISOString().slice(0,19);
  const data  = await fetch(`/api/history?start=${start}&limit=10000`).then(r => r.json());

  // group by label, sort ascending
  const byLabel = {};
  for (const row of data) {
    (byLabel[row.label] ??= []).push({ x: new Date(row.ts), y: row.temp_f, h: row.humidity });
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
    app.run(host=host, port=port, debug=debug)
