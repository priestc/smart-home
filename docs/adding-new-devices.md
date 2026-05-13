# Adding Support for New Devices

## Temperature & Humidity → Temperature Charts

Write a row to the **`readings`** table with a non-null `temp_f` value.
Use `insert_reading` from `smart_home/db.py`, or `INSERT OR IGNORE INTO readings` directly.

Required columns:
| Column | Type | Notes |
|--------|------|-------|
| `ts` | TEXT | `YYYY-MM-DD HH:MM:SS`, local time |
| `label` | TEXT | Human-readable name, must match the label in `labels.json` |
| `temp_f` | REAL | Fahrenheit. This is what the temperature charts query. |
| `humidity` | REAL | Optional percentage (0–100). |
| `battery` | INTEGER | Optional — see below. |
| `rssi` | INTEGER | Optional signal strength in dBm. |

Once a row exists with a non-null `temp_f` the sensor will automatically appear in:
- The main temperature history chart (`/chart/sensors`)
- The sensor battery life chart (if `battery` is also populated)
- The `/api/current` response
- Offline detection and alerts (the monitor loop tracks `last_seen` per address)

## Battery → Battery Life Chart

The battery chart pulls from `/api/history`, which UNIONs two tables:

| Table | When to use |
|-------|-------------|
| `readings` | Any sensor that also reports temperature — just populate the `battery` column on the same row. |
| `pool_readings` | Pool-chemistry sensors (BLE_YC01) — this table is already UNIONed in. |

### Rule of thumb

- **Sensor with temperature** (thermometer, combo sensor, etc.): write `battery` into the existing `readings` row. Nothing else needed.
- **Sensor without temperature** (a dedicated battery-only or chemistry sensor): create a new table and UNION it into `/api/history` and `/api/current` following the same pattern used for `pool_readings`.

### How to UNION a new table in

1. Add the table in `db.py → open_db()` (see `pool_readings` as a template).
2. In `web.py → history()` (the `/api/history` endpoint), add a `UNION ALL` branch that selects `ts, label, NULL AS temp_f, NULL AS humidity, rssi, battery` from the new table, with the same `where_sql` and bucketing logic as the `pool_readings` branch.
3. In `web.py → current()` (the `/api/current` endpoint), add a `UNION ALL` branch that selects `label, NULL AS temp_f, NULL AS humidity, rssi, ts` from the new table.

That's all — the battery chart's JS calls those two endpoints, so the new sensor's label and data will appear automatically without any frontend changes.

## Offline Alerts

Offline detection is driven by `last_seen[address]` in the monitor loop (`__main__.py`).
For BLE sensors discovered via passive scanning, this is updated automatically by `on_reading`.
For polled sensors (GATT, HTTP, etc.) you need to update `last_seen[address]` whenever a successful reading is obtained.

Pool monitors use a separate `pool_last_reading[label]` dict and a dedicated check in `snapshot_loop` — follow that pattern for any sensor polled outside the normal BLE advertisement flow.
