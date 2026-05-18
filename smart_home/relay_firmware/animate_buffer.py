"""
Animation of the min-merged-gap buffer eviction algorithm.

Loads temperature_dataset.csv (2880 readings, midnight → midnight, 30 s apart)
and pushes them one by one into the 10-slot buffer. Each animation frame is one
push. Blue dots are readings currently held in the buffer. The gold dot marks
the newest arrival. When a reading is evicted its dot disappears.

Run with:  python3 smart_home/relay_firmware/animate_buffer.py
Save MP4:  python3 smart_home/relay_firmware/animate_buffer.py --save
"""

import sys
import os
import csv
import argparse
from datetime import datetime

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.animation as animation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_buffer import Buffer, batch_ts

# ── Dataset ───────────────────────────────────────────────────────────────────

DATASET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "temperature_dataset.csv")

timestamps, temperatures = [], []
with open(DATASET) as f:
    for row in csv.DictReader(f):
        timestamps.append(row["timestamp"])
        temperatures.append(float(row["temperature_f"]))

temp_by_ts  = dict(zip(timestamps, temperatures))
hour_by_ts  = {
    ts: (lambda dt: dt.hour + dt.minute / 60 + dt.second / 3600)
        (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    for ts in timestamps
}
t_hours = [hour_by_ts[ts] for ts in timestamps]

# ── Precompute buffer states ───────────────────────────────────────────────────

MAX_BUFFER = 10

def _make_payload(ts_str: str) -> str:
    return f'{{"batch_ts":"{ts_str}","data":1}}'

buf = Buffer(max_buffer=MAX_BUFFER)
states = []
prev_set: set = set()

for ts in timestamps:
    buf.push(_make_payload(ts))
    curr_set = set(buf.timestamps())
    diff = prev_set - curr_set
    states.append({
        "new_ts":     ts,
        "buffer":     list(buf.timestamps()),
        "evicted_ts": diff.pop() if diff else None,
    })
    prev_set = curr_set.copy()

TOTAL = len(states)

# ── Figure ────────────────────────────────────────────────────────────────────

BG      = "#1a1a2e"
PANEL   = "#16213e"
GRID    = "#2a2a4a"
TRACE   = "#3a3a6a"
DOT_BUF = "#4ecdc4"
DOT_NEW = "#ffd93d"
VLINE   = "#ff6b6b"
TEXT    = "#e0e0e0"

fig, ax = plt.subplots(figsize=(16, 7))
fig.patch.set_facecolor(BG)
ax.set_facecolor(PANEL)
for spine in ax.spines.values():
    spine.set_color(GRID)
ax.tick_params(colors=TEXT)
ax.grid(axis="y", color=GRID, lw=0.5, zorder=0)

# Full day trace (background reference)
ax.plot(t_hours, temperatures, color=TRACE, lw=0.7, zorder=1)

ax.set_xlim(-0.3, 24.3)
ax.set_ylim(min(temperatures) - 4, max(temperatures) + 4)
ax.set_xlabel("Time of Day", color=TEXT, labelpad=8)
ax.set_ylabel("Outside Air Temperature (°F)", color=TEXT, labelpad=8)
ax.set_xticks(range(0, 25, 2))
ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 2)], color=TEXT)
ax.tick_params(axis="y", labelcolor=TEXT)

# Vertical current-time indicator
vline = ax.axvline(x=0, color=VLINE, lw=1.2, alpha=0.6, zorder=2)

# Buffer dots (teal) — all slots except newest
buf_sc = ax.scatter([], [], s=110, color=DOT_BUF, zorder=4,
                    edgecolors="white", linewidths=0.6,
                    label=f"In buffer (max {MAX_BUFFER})")

# Newest arrival (gold)
new_sc = ax.scatter([], [], s=180, color=DOT_NEW, zorder=5,
                    edgecolors="white", linewidths=1.5,
                    label="New arrival")

legend = ax.legend(
    loc="upper left",
    facecolor=BG, labelcolor=TEXT,
    framealpha=0.9, edgecolor=GRID,
)

title = fig.suptitle("", color=TEXT, fontsize=11, y=0.97)

# ── Animation update ──────────────────────────────────────────────────────────

def update(frame: int):
    state   = states[frame]
    buf_ts  = state["buffer"]
    new_ts  = state["new_ts"]
    evicted = state["evicted_ts"]

    # Buffer dots (exclude newest — drawn separately)
    others = [ts for ts in buf_ts if ts != new_ts]
    if others:
        xs = [hour_by_ts[ts] for ts in others]
        ys = [temp_by_ts[ts] for ts in others]
        buf_sc.set_offsets(np.column_stack([xs, ys]))
    else:
        buf_sc.set_offsets(np.empty((0, 2)))

    # Newest arrival
    nx = hour_by_ts[new_ts]
    ny = temp_by_ts[new_ts]
    new_sc.set_offsets([[nx, ny]])

    # Current-time marker
    vline.set_xdata([nx, nx])

    # Title
    n = len(buf_ts)
    evict_info = f"  ·  evicted {evicted}" if evicted else ""
    title.set_text(
        f"Step {frame + 1}/{TOTAL}  ·  {new_ts}  ·  Buffer {n}/{MAX_BUFFER}{evict_info}"
    )

    return buf_sc, new_sc, vline, title


# ── Run ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--save", action="store_true", help="Save to animate_buffer.mp4")
args = parser.parse_args()

ani = animation.FuncAnimation(
    fig, update,
    frames=TOTAL,
    interval=20,   # ms per frame ≈ 50 fps → ~58 s total
    blit=True,
)

plt.tight_layout(rect=[0, 0, 1, 0.95])

if args.save:
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "animate_buffer.mp4")
    ani.save(out, writer="ffmpeg", fps=50, dpi=120,
             metadata={"title": "Buffer eviction algorithm"})
    print(f"Saved {out}")
else:
    plt.show()
