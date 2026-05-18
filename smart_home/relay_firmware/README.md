# Relay Firmware — Buffer Algorithm Visualisations

This directory contains the ESP32 relay firmware (`esp32_relay/`) and a set of
Python scripts that visualise the offline-buffer algorithm used by the relay.

---

## Background

Each relay POSTs a BLE scan batch to the server every 30 seconds.  When WiFi
is unavailable the batch is stored in RAM.  Because the ESP32 has limited heap
(~150–200 KB available at runtime with the BLE and WiFi stacks loaded), the
buffer is capped at **60 entries** (`MAX_BUFFER` in `esp32_relay.ino`).

The challenge: a 60-entry cap at one reading per 30 s covers only **30 minutes**
of full-resolution data.  For longer outages the firmware needs to decide which
readings to keep so that, when WiFi returns, the server receives the best
possible picture of what happened during the gap.

### The eviction algorithm — min-merged-gap

When the buffer is full and a new reading arrives:

1. Insert the new reading in sorted timestamp order.
2. Find the interior reading `i` (never the oldest or newest) whose removal
   would create the smallest merged gap: `gap = times[i+1] − times[i−1]`.
3. Evict that reading.

This keeps the retained readings as evenly spaced as possible across the whole
outage duration, regardless of how long the outage lasts.

### Adaptive read rate (Phase 2)

Once the buffer is full the relay stops reading every 30 s and instead computes
the **ideal next read time**:

```
W = (newest_ts − oldest_ts) / (MAX_BUFFER − 1)
next_read = last_read + W
```

This rate slows naturally as the outage grows — a 1-hour outage produces reads
every ~1 minute; a 24-hour outage produces reads every ~24 minutes — always
keeping the buffer at the ideal even spacing.

### Two-relay interlacing

With two relays observing the same data source, the second relay can perfectly
interlace its readings between the first relay's readings without any
communication:

- **Relay 0** fires at the exact computed time `t_A_next`.
- **Relay k** (index `k` of `N` total) fires at `t_A + k × W / N`.

Because both relays share NTP time and identical Phase 1 buffer contents,
relay k can predict relay 0's exact schedule and place its own readings in
the gaps.  With N relays the worst-case combined gap shrinks to `W / N`.
Adding more relays requires no extra per-device computation — each relay only
needs to know its index `k` and the total count `N`, both provisioned at
setup time.

---

## Files

| File | Description |
|------|-------------|
| `esp32_relay/esp32_relay.ino` | ESP32 firmware source |
| `test_buffer.py` | Python reimplementation of the buffer algorithm + unit tests |
| `demo_buffer.py` | Text demo — prints buffered timestamps for various outage lengths |
| `animate_buffer.py` | Matplotlib animation of the dual-relay adaptive algorithm |
| `temperature_dataset.csv` | Synthetic 24-hour outdoor temperature dataset (30 s intervals) |
| `outside_shade_2026_05_16.csv` | Live outside-shade sensor data, 2026-05-16 (1 min intervals) |
| `buffer_adaptive_cubic.mp4` | Pre-rendered animation video (see below) |

---

## Prerequisites

```bash
pip install matplotlib scipy numpy
# For saving MP4 videos:
brew install ffmpeg        # macOS
# or: sudo apt install ffmpeg
```

---

## Running the animations

### Interactive (live window)

```bash
# From the repo root:
python3 smart_home/relay_firmware/animate_buffer.py
```

Switch interpolation method:

```bash
python3 smart_home/relay_firmware/animate_buffer.py --interp pchip    # default
python3 smart_home/relay_firmware/animate_buffer.py --interp cubic
python3 smart_home/relay_firmware/animate_buffer.py --interp akima
python3 smart_home/relay_firmware/animate_buffer.py --interp cosine
```

### Save to MP4

```bash
python3 smart_home/relay_firmware/animate_buffer.py --save
# Output: smart_home/relay_firmware/buffer_adaptive_cubic.mp4
```

### Text demo (no dependencies beyond Python stdlib)

```bash
python3 smart_home/relay_firmware/demo_buffer.py
```

### Unit tests

```bash
python3 smart_home/relay_firmware/test_buffer.py
```

---

## What the animation shows

<video src="https://github.com/priestc/smart-home/releases/download/buffer-animation-v1/buffer_adaptive_cubic.mp4" controls width="100%"></video>

The animation plays through a full 24-hour outage using live **outside-shade**
sensor data from 2026-05-16 (range: 69.8 °F → 91.6 °F).

**What you're watching:**

- **Faint background trace** — the full day of raw temperature readings (reference).
- **Teal dots** — the 60 readings currently held in relay 1's buffer.
- **Orange dots** — the 60 readings currently held in relay 2's buffer.
- **Gold dot** — the reading just taken (the current event).
- **Dashed teal/orange curves** — spline through each relay's individual buffer.
- **Solid green curve** — spline through all combined readings from both relays.
- **Vertical red line** — current time in the outage.
- **Top-left stat** — worst-case gap in the combined buffer at this moment.

**Phases visible in the animation:**

1. **Phase 1 — sequential fill (first ~30 minutes):** Both relays read every
   30 s.  The teal and orange dots are identical and overlap perfectly — the
   buffers fill in unison.

2. **Phase 2 — adaptive rate:** Once the buffer is full the vertical line
   starts making increasingly large time jumps.  Relay 1 fires at the computed
   ideal interval W; relay 2 fires at the midpoint of each of relay 1's
   intervals.  The teal and orange dots visibly separate and alternate across
   the day.

3. **End state:** 120 readings (60 per relay) cover the full 24-hour outage
   with a worst-case gap of ~30 minutes — compared to ~4 hours if both relays
   used fixed 30-second sampling with no coordination.

---

## Algorithm comparison (final buffer state)

| Approach | Max gap |
|----------|---------|
| Fixed 30 s, no coordination (both relays cluster) | ~4.1 h |
| Adaptive rate, B computes from own buffer (broken) | ~2.9 h |
| Adaptive rate, B uses A's schedule for midpoints ✓ | ~0.5 h |
| N relays, each at offset k/N | W / N |
