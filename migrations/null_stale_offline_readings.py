#!/usr/bin/env python3
"""One-time migration: null out readings that were written after a sensor went offline.

When a BLE sensor's battery dies, the old reading stays in latest_reading and gets
re-written every minute with a fresh timestamp, creating a flat stale line on the graph.
This script finds sensor_offline events and nulls out readings recorded after each
offline event (up to either the next sensor_online event or the current time).
"""

import sqlite3
import os
import sys

DB_PATH = os.path.expanduser("~/.local/share/smart-home/readings.db")
if len(sys.argv) > 1:
    DB_PATH = sys.argv[1]

conn = sqlite3.connect(DB_PATH)

offline_events = conn.execute("""
    SELECT details AS label, ts AS offline_ts
    FROM temperature_events
    WHERE event_type = 'sensor_offline'
    ORDER BY ts
""").fetchall()

if not offline_events:
    print("No sensor_offline events found.")
    conn.close()
    sys.exit(0)

updates = []
for label, offline_ts in offline_events:
    online_row = conn.execute("""
        SELECT ts FROM temperature_events
        WHERE event_type = 'sensor_online'
          AND details LIKE ?
          AND ts > ?
        ORDER BY ts ASC LIMIT 1
    """, (f"{label}%", offline_ts)).fetchone()
    end_ts = online_row[0] if online_row else "9999-12-31 23:59:59"

    count = conn.execute("""
        SELECT COUNT(*) FROM readings
        WHERE label = ? AND ts > ? AND ts < ? AND temp_f IS NOT NULL
    """, (label, offline_ts, end_ts)).fetchone()[0]

    if count:
        updates.append((label, offline_ts, end_ts, count))

if not updates:
    print("No stale readings found to null out.")
    conn.close()
    sys.exit(0)

print("Stale readings to null out:")
for label, offline_ts, end_ts, count in updates:
    end_display = end_ts if end_ts != "9999-12-31 23:59:59" else "(now)"
    print(f"  {label}: {count} rows from {offline_ts} to {end_display}")

confirm = input("\nNull out these readings? [y/N] ").strip().lower()
if confirm != "y":
    print("Aborted.")
    conn.close()
    sys.exit(1)

total = 0
for label, offline_ts, end_ts, _ in updates:
    cur = conn.execute("""
        UPDATE readings SET temp_f = NULL, humidity = NULL
        WHERE label = ? AND ts > ? AND ts < ? AND temp_f IS NOT NULL
    """, (label, offline_ts, end_ts))
    total += cur.rowcount

conn.commit()
print(f"Nulled {total} row(s).")
conn.close()
