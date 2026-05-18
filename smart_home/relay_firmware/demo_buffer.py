"""
Demo of the relay offline buffer algorithm.

Simulates an outage of various lengths and prints the timestamps stored
in the buffer when the server comes back online.

Run with:  python3 smart_home/relay_firmware/demo_buffer.py
"""

import sys
from datetime import datetime, timedelta

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_buffer import Buffer, make_payload, BASE

MAX_BUFFER = 10   # slots — change to match firmware MAX_BUFFER if desired

OUTAGES = [
    ("30 minutes",  30 * 60),
    ("1 hour",       1 * 60 * 60),
    ("6 hours",      6 * 60 * 60),
    ("1 day",       24 * 60 * 60),
    ("1 week",       7 * 24 * 60 * 60),
    ("1 month",     30 * 24 * 60 * 60),
]


def fmt_gap(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    seconds = abs(int(seconds))
    if seconds < 3600:
        return f"{sign}{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        return f"{sign}{h}h {rem // 60:02d}m"
    d, rem = divmod(seconds, 86400)
    return f"{sign}{d}d {rem // 3600:02d}h"


for label, secs in OUTAGES:
    buf = Buffer(max_buffer=MAX_BUFFER)
    outage_start = BASE
    outage_end   = BASE + timedelta(seconds=secs)

    for i in range(secs // 30):
        buf.push(make_payload(BASE + timedelta(seconds=i * 30)))

    print(f"\nOutage: {outage_start.strftime('%Y-%m-%d %H:%M:%S')}"
          f"  →  {outage_end.strftime('%Y-%m-%d %H:%M:%S')}"
          f"  ({label})")

    dts = [datetime.strptime(t, "%Y-%m-%d %H:%M:%S") for t in buf.timestamps()]
    gaps = [None] + [(dts[i] - dts[i - 1]).total_seconds() for i in range(1, len(dts))]
    changes = [None, None] + [gaps[i] - gaps[i - 1] for i in range(2, len(gaps))]

    for i, dt in enumerate(dts):
        gap_col    = f"  {fmt_gap(gaps[i]):<12}" if gaps[i] is not None else ""
        change_col = f"  {fmt_gap(changes[i])}" if changes[i] is not None else ""
        print(f"  {dt.strftime('%Y-%m-%d %H:%M:%S')}{gap_col}{change_col}")
