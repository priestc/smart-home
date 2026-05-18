"""
Animation — adaptive-rate relay buffer, dual-relay interlacing.

Both relays observe the same temperature source. They fill their buffers
at 30 s/reading (Phase 1). Once full, each relay computes the ideal next
read time from its own buffer state and only wakes up then (Phase 2):

  W = (newest_ts - oldest_ts) / (MAX_BUFFER - 1)

  Relay 1 (teal):   next reading in W seconds     — exact ideal interval
  Relay 2 (orange): next reading in W/2 seconds   — halfway to the ideal

Because relay 2 reads at the midpoints of relay 1's intervals, the two
buffers naturally drift apart and their combined 20 readings cover the
day with roughly half the worst-case gap of either relay alone.

The eviction algorithm (min-merged-gap) is identical for both relays.

Run with:  python3 smart_home/relay_firmware/animate_buffer.py
           python3 smart_home/relay_firmware/animate_buffer.py --interp cubic
Save MP4:  python3 smart_home/relay_firmware/animate_buffer.py --save
"""

import sys
import os
import csv
import argparse
from datetime import datetime, timedelta

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from scipy.interpolate import CubicSpline, PchipInterpolator, Akima1DInterpolator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_buffer import Buffer

# ── CLI ───────────────────────────────────────────────────────────────────────

INTERP_CHOICES = ("pchip", "cubic", "akima", "cosine")

parser = argparse.ArgumentParser(description="Adaptive-rate dual-relay animation")
parser.add_argument("--interp", choices=INTERP_CHOICES, default="cubic")
parser.add_argument("--save", action="store_true")
args = parser.parse_args()

# ── Temperature dataset ───────────────────────────────────────────────────────

DATASET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "outside_shade_2026_05_16.csv")

BASE_DT = datetime(2026, 5, 16)
_ref_secs, _ref_temps = [], []
with open(DATASET) as f:
    for row in csv.DictReader(f):
        dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
        _ref_secs.append((dt - BASE_DT).total_seconds())
        _ref_temps.append(float(row["temperature_f"]))

ref_secs_arr  = np.array(_ref_secs)
ref_temps_arr = np.array(_ref_temps)
ref_hours     = [s / 3600 for s in _ref_secs]

def lookup_temp(t_secs: float) -> float:
    return float(np.interp(t_secs, ref_secs_arr, ref_temps_arr))

def format_ts(t_secs: float) -> str:
    return (BASE_DT + timedelta(seconds=round(t_secs))).strftime("%Y-%m-%d %H:%M:%S")

def ts_to_hour(ts: str) -> float:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return (dt - BASE_DT).total_seconds() / 3600

# ── Adaptive simulation ───────────────────────────────────────────────────────

MAX_BUFFER = 60
DAY_SECS   = 86400.0

def _payload(ts: str) -> str:
    return '{"batch_ts":"' + ts + '","data":1}'

buf1 = Buffer(max_buffer=MAX_BUFFER, parity=0)   # relay 1
buf2 = Buffer(max_buffer=MAX_BUFFER, parity=0)   # relay 2 (same eviction algo)

states:      list[dict] = []
ts_to_temp:  dict[str, float] = {}
ts_to_hour_: dict[str, float] = {}

def _record(relay: int, t: float, ts: str) -> None:
    temp = lookup_temp(t)
    ts_to_temp[ts]  = temp
    ts_to_hour_[ts] = t / 3600
    states.append({"relay": relay, "t": t, "ts": ts, "temp": temp,
                   "buf1": list(buf1.timestamps()), "buf2": list(buf2.timestamps())})

# ── Phase 1: sequential fill at 30 s — both relays see identical readings ──────
t = 0.0
while len(buf1.queue) < MAX_BUFFER:
    ts = format_ts(t)
    p  = _payload(ts)
    buf1.push(p)
    buf2.push(p)
    _record(1, t, ts)
    t += 30.0

# ── Phase 2: relay 1 fires every W; relay 2 fires at midpoints of relay 1 ──────
#
# After relay 1 fires at t_A and computes its next firing t_A_next, relay 2
# schedules exactly at (t_A + t_A_next) / 2.  Relay 2 uses relay 1's interval
# (derived from relay 1's buffer) so the two schedules never drift together.

t_A      = t - 30.0                        # relay 1's last Phase 1 reading
t_A_next = t_A + buf1.next_read_delay()    # relay 1's first Phase 2 firing
t_B_next = (t_A + t_A_next) / 2           # relay 2's first firing = midpoint

while t_A_next <= DAY_SECS or t_B_next <= DAY_SECS:
    if t_B_next < t_A_next and t_B_next <= DAY_SECS:
        # Relay 2 fires at the midpoint
        ts = format_ts(t_B_next)
        buf2.push(_payload(ts))
        _record(2, t_B_next, ts)
        t_B_next = DAY_SECS + 1            # wait; relay 1 will set next midpoint

    elif t_A_next <= DAY_SECS:
        # Relay 1 fires
        ts = format_ts(t_A_next)
        buf1.push(_payload(ts))
        _record(1, t_A_next, ts)
        t_A      = t_A_next
        t_A_next = t_A + buf1.next_read_delay()   # relay 1's next interval
        t_B_next = (t_A + t_A_next) / 2           # relay 2's next midpoint

    else:
        break

# Print final analysis
final = states[-1]
combined = sorted(set(final["buf1"]) | set(final["buf2"]), key=ts_to_hour_.__getitem__)
gaps_h = [ts_to_hour_[combined[i+1]] - ts_to_hour_[combined[i]]
          for i in range(len(combined)-1)]
r1_count = sum(1 for s in states if s["relay"] == 1)
r2_count = sum(1 for s in states if s["relay"] == 2)
print(f"Relay 1: {r1_count} readings   Relay 2: {r2_count} readings   "
      f"Total frames: {len(states)}")
print(f"Combined {len(combined)} pts — max gap {max(gaps_h):.2f}h  "
      f"min gap {min(gaps_h)*3600:.0f}s")

TOTAL         = len(states)
FPS           = 8
FREEZE_FRAMES = 3 * FPS
TOTAL_FRAMES  = TOTAL + FREEZE_FRAMES

# ── Interpolation ─────────────────────────────────────────────────────────────

def _cosine_interp(xs: np.ndarray, ys: np.ndarray) -> tuple:
    out_x, out_y = [], []
    for i in range(len(xs) - 1):
        t  = np.linspace(0, 1, 60, endpoint=(i == len(xs) - 2))
        mu = (1 - np.cos(t * np.pi)) / 2
        out_x.append(xs[i] + (xs[i + 1] - xs[i]) * t)
        out_y.append(ys[i] * (1 - mu) + ys[i + 1] * mu)
    return np.concatenate(out_x), np.concatenate(out_y)

INTERP_LABEL = {
    "pchip":  "PCHIP / Monotone Cubic",
    "cubic":  "Natural Cubic Spline",
    "akima":  "Akima",
    "cosine": "Cosine (piecewise)",
}

def make_curve(xs: np.ndarray, ys: np.ndarray) -> tuple:
    if len(xs) < 2:
        return np.array([]), np.array([])
    xd = np.linspace(xs[0], xs[-1], 600)
    m = args.interp
    if m == "cosine": return _cosine_interp(xs, ys)
    if m == "cubic":  return xd, CubicSpline(xs, ys)(xd)
    if m == "pchip":  return xd, PchipInterpolator(xs, ys)(xd)
    if m == "akima":  return xd, Akima1DInterpolator(xs, ys)(xd)
    raise ValueError(m)

# ── Figure ────────────────────────────────────────────────────────────────────

BG          = "#1a1a2e"
PANEL       = "#16213e"
GRID        = "#2a2a4a"
TRACE       = "#6060a8"
LINE_CMB    = "#00dd88"    # combined — bright green
LINE_R1     = "#2a9d8f"    # relay 1 dashed — dim teal
LINE_R2     = "#c85a00"    # relay 2 dashed — dim orange
DOT_R1      = "#4ecdc4"    # relay 1 dots — teal
DOT_R2      = "#ff8c42"    # relay 2 dots — orange
DOT_NEW     = "#ffd93d"    # newest arrival — gold
VLINE_COL   = "#ff6b6b"
TEXT        = "#e0e0e0"

fig, ax = plt.subplots(figsize=(16, 7))
fig.patch.set_facecolor(BG)
ax.set_facecolor(PANEL)
for spine in ax.spines.values():
    spine.set_color(GRID)
ax.tick_params(colors=TEXT)
ax.grid(axis="y", color=GRID, lw=0.5, zorder=0)

ax.plot(ref_hours, _ref_temps, color=TRACE, lw=0.8, alpha=0.45, zorder=1)

ax.set_xlim(-0.3, 24.3)
ax.set_ylim(min(_ref_temps) - 4, max(_ref_temps) + 4)
ax.set_xlabel("Time of Day", color=TEXT, labelpad=8)
ax.set_ylabel("Outside Air Temperature (°F)", color=TEXT, labelpad=8)
ax.set_xticks(range(0, 25, 2))
ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 2)], color=TEXT)
ax.tick_params(axis="y", labelcolor=TEXT)

vline = ax.axvline(x=0, color=VLINE_COL, lw=1.0, alpha=0.5, zorder=2)

# Individual relay curves (dashed, dim) and combined (solid, bright)
line_r1, = ax.plot([], [], color=LINE_R1, lw=1.2, zorder=3,
                   linestyle="--", alpha=0.6, label="Relay 1 curve")
line_r2, = ax.plot([], [], color=LINE_R2, lw=1.2, zorder=3,
                   linestyle="--", alpha=0.6, label="Relay 2 curve")
line_cmb, = ax.plot([], [], color=LINE_CMB, lw=2.2, zorder=3,
                    label="Combined (R1 + R2)")

# Scatter plots
r1_sc = ax.scatter([], [], s=65, color=DOT_R1, zorder=4,
                   edgecolors="white", linewidths=0.5,
                   label=f"Relay 1  (W intervals, {MAX_BUFFER} slots)")
r2_sc = ax.scatter([], [], s=65, color=DOT_R2, zorder=4,
                   edgecolors="white", linewidths=0.5,
                   label=f"Relay 2  (W/2 intervals, {MAX_BUFFER} slots)")
new_sc = ax.scatter([], [], s=180, color=DOT_NEW, zorder=5,
                    edgecolors="white", linewidths=1.2,
                    label="Current reading")

stats_text = ax.text(0.01, 0.97, "", transform=ax.transAxes,
                     color=TEXT, fontsize=9, va="top", fontfamily="monospace")

ax.legend(loc="upper right", facecolor=BG, labelcolor=TEXT,
          framealpha=0.9, edgecolor=GRID, fontsize=9)

title = fig.suptitle("", color=TEXT, fontsize=11, y=0.97)
fig.text(0.99, 0.01, f"interp: {INTERP_LABEL[args.interp]}",
         color="#7070a0", fontsize=9, ha="right", va="bottom")

# ── Animation helpers ─────────────────────────────────────────────────────────

def _set_scatter(sc, tss: list) -> None:
    if tss:
        sc.set_offsets(np.column_stack(
            [[ts_to_hour_[ts] for ts in tss],
             [ts_to_temp[ts]  for ts in tss]]))
    else:
        sc.set_offsets(np.empty((0, 2)))

def _set_line(line, tss: list) -> None:
    if len(tss) >= 2:
        hrs = np.array([ts_to_hour_[ts] for ts in tss])
        tmp = np.array([ts_to_temp[ts]  for ts in tss])
        idx = np.argsort(hrs)
        lx, ly = make_curve(hrs[idx], tmp[idx])
        line.set_data(lx, ly)
    else:
        line.set_data([], [])

def _max_gap_h(tss: list) -> float:
    if len(tss) < 2:
        return 0.0
    hrs = sorted(ts_to_hour_[ts] for ts in tss)
    return max(hrs[i + 1] - hrs[i] for i in range(len(hrs) - 1))

# ── Animation update ──────────────────────────────────────────────────────────

def update(frame: int) -> None:
    state = states[min(frame, TOTAL - 1)]
    b1, b2 = state["buf1"], state["buf2"]

    _set_scatter(r1_sc, b1)
    _set_scatter(r2_sc, b2)
    _set_line(line_r1, b1)
    _set_line(line_r2, b2)

    comb = sorted(set(b1) | set(b2), key=ts_to_hour_.__getitem__)
    _set_line(line_cmb, comb)

    max_h = _max_gap_h(comb)
    stats_text.set_text(
        f"Max gap: {max_h:.2f}h   combined: {len(comb)} pts"
    )

    new_sc.set_offsets([[state["t"] / 3600, state["temp"]]])
    vline.set_xdata([state["t"] / 3600] * 2)

    relay_lbl = f"Relay {state['relay']}"
    title.set_text(
        f"{relay_lbl}  ·  {state['ts']}"
        f"  ·  R1: {len(b1)}/{MAX_BUFFER}  R2: {len(b2)}/{MAX_BUFFER}"
        f"  ·  combined: {len(comb)} pts"
    )


# ── Run ───────────────────────────────────────────────────────────────────────

ani = animation.FuncAnimation(
    fig, update,
    frames=TOTAL_FRAMES,
    interval=1000 // FPS,
    repeat=False,
    blit=False,
)

plt.tight_layout(rect=[0, 0, 1, 0.95])

if args.save:
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"buffer_adaptive_{args.interp}.mp4")
    writer = animation.FFMpegWriter(
        fps=FPS, codec="h264", bitrate=4000,
        extra_args=["-pix_fmt", "yuv420p"],
    )
    print(f"Rendering {TOTAL_FRAMES} frames …")
    ani.save(out, writer=writer, dpi=120)
    print(f"Saved {out}")
else:
    plt.show()
