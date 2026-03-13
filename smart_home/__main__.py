from __future__ import annotations
import asyncio
import csv
import datetime
import io
import zipfile
import os
import click
from bleak import BleakScanner
from smart_home.scanner import scan
from smart_home import labels as _labels
from smart_home.battery import dump_gatt
from smart_home.db import open_db, insert_reading, bulk_insert

DEFAULT_DB = os.path.expanduser("~/.local/share/smart-home/readings.db")


@click.group()
def main():
    """Monitor Govee H5074 temperature/humidity sensors via BLE."""


@main.command("label")
@click.option("--timeout", "-t", type=float, default=30.0,
              help="Seconds to scan for sensors (default: 30).")
def label_sensors(timeout):
    """Scan for sensors, then prompt for a label for each unlabeled one."""
    label_map = _labels.load()
    found: dict[str, str] = {}  # address -> govee name

    def on_reading(reading):
        if reading.address not in label_map and reading.address not in found:
            found[reading.address] = reading.name

    click.echo(f"Scanning for sensors ({int(timeout)}s)...")
    try:
        asyncio.run(scan(on_reading, duration=timeout))
    except KeyboardInterrupt:
        pass

    if not found:
        click.echo("No new (unlabeled) sensors found.")
        return

    click.echo(f"\nFound {len(found)} new sensor(s). Enter a label for each:\n")
    changed = False
    for addr, name in found.items():
        label = click.prompt(f"  {name} ({addr})").strip()
        if label:
            label_map[addr] = label
            changed = True

    if changed:
        _labels.save(label_map)
        click.echo("\nLabels saved.")


DEVICE_TYPES = {
    "1": ("Govee H5074",         ("Govee_H5074", "GVH5074")),
    "2": ("Xiaomi LYWSD03MMC",   ("LYWSD03MMC", "ATC_")),
}


@main.command("list-devices")
def list_devices():
    """Show all registered devices and their labels."""
    label_map = _labels.load()
    if not label_map:
        click.echo("No devices registered. Run 'smart-home add-device' to add one.")
        return
    for addr, label in sorted(label_map.items(), key=lambda x: x[1]):
        click.echo(f"  {label:<20} {addr}")


@main.command("sensor-history")
@click.option("--label", "-l", default=None, help="Filter by sensor label.")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of rows to show.")
@click.option("--db", default=DEFAULT_DB, show_default=True, help="SQLite database path.")
def sensor_history(label, limit, db):
    """Show recent sensor readings from the database."""
    conn = open_db(db)
    params = [limit]
    where = ""
    if label:
        where = "WHERE label = ? "
        params.insert(0, label)
    rows = conn.execute(
        f"SELECT ts, label, temp_f, humidity FROM readings {where}ORDER BY ts DESC LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        click.echo("No readings found.")
        return
    click.echo(f"  {'timestamp':<22} {'label':<20} {'temp (°F)':<12} {'humidity'}")
    click.echo("  " + "-" * 62)
    for ts, lbl, temp_f, humidity in rows:
        click.echo(f"  {ts:<22} {(lbl or ''):<20} {temp_f:<12.1f} {humidity:.1f}%")


@main.command("add-device")
@click.option("--timeout", "-t", type=float, default=15.0,
              help="Seconds to scan (default: 15).")
def add_device(timeout):
    """Scan for sensors and register them with a label.

    Prompts for device type, scans for matching BLE devices, then asks
    for a label for each new device found.
    """
    click.echo("What type of sensor do you want to add?\n")
    for key, (name, _) in DEVICE_TYPES.items():
        click.echo(f"  {key}. {name}")
    choice = click.prompt("\nEnter choice", type=click.Choice(list(DEVICE_TYPES)))
    type_label, name_prefixes = DEVICE_TYPES[choice]

    label_map = _labels.load()
    found: dict[str, str] = {}  # address -> device name

    def callback(device, adv):
        name = device.name or adv.local_name or ""
        if any(name.startswith(p) for p in name_prefixes):
            if device.address not in label_map and device.address not in found:
                found[device.address] = name

    async def _run():
        async with BleakScanner(detection_callback=callback):
            await asyncio.sleep(timeout)

    click.echo(f"\nScanning for {type_label} sensors ({int(timeout)}s)...")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

    if not found:
        click.echo(f"No new {type_label} devices found.")
        return

    click.echo(f"\nFound {len(found)} new sensor(s). Enter a label for each:\n")
    changed = False
    for addr, name in found.items():
        label = click.prompt(f"  {name} ({addr})").strip()
        if label:
            label_map[addr] = label
            changed = True

    if changed:
        _labels.save(label_map)
        click.echo("\nLabels saved.")


@main.command("import")
@click.argument("zipfile_path", metavar="ZIPFILE")
@click.option("--label", required=True, help="Label to assign to all imported readings.")
@click.option("--db", default=DEFAULT_DB, show_default=True,
              help="SQLite database path.")
def import_zip(zipfile_path, label, db):
    """Import temperature history from a Govee export zip file.

    Example: govee-monitor import inside.zip --label=Inside
    """
    rows = []
    with zipfile.ZipFile(zipfile_path) as zf:
        csv_names = sorted(n for n in zf.namelist() if n.endswith(".csv"))
        if not csv_names:
            click.echo("No CSV files found in zip.")
            return
        click.echo(f"Reading {len(csv_names)} CSV file(s)...")
        for name in csv_names:
            raw = zf.read(name).decode("utf-8-sig")  # strips BOM
            reader = csv.reader(io.StringIO(raw))
            next(reader)  # skip header
            for line in reader:
                if len(line) < 3:
                    continue
                ts = line[0].strip().replace(" ", "T")
                try:
                    temp_f = float(line[1].strip())
                    humidity = float(line[2].strip())
                except ValueError:
                    continue
                rows.append((ts, label, temp_f, humidity))

    if not rows:
        click.echo("No data rows found.")
        return

    conn = open_db(db)
    inserted = bulk_insert(conn, rows)
    click.echo(f"Imported {inserted} new rows ({len(rows)} total, {len(rows)-inserted} duplicates skipped).")


@main.command()
@click.option("--duration", "-d", type=float, default=None,
              help="How many seconds to scan (default: indefinitely).")
@click.option("--verbose", "-v", is_flag=True, help="Show raw advertisement data.")
@click.option("--db", default=DEFAULT_DB, show_default=True,
              help="SQLite database path for storing readings.")
@click.option("--no-db", is_flag=True, help="Disable database logging.")
def monitor(duration, verbose, db, no_db):
    """Continuously print readings from nearby H5074 sensors."""
    label_map = _labels.load()
    seen: set[str] = set()
    last_temp: dict[str, float] = {}      # address -> last recorded temp_f
    last_hum:  dict[str, float] = {}      # address -> last recorded humidity
    last_write: dict[str, datetime.datetime] = {}  # address -> last write time
    HEARTBEAT = datetime.timedelta(minutes=30)
    conn = None if no_db else open_db(db)
    if conn:
        click.echo(f"Logging to {db}")

    def on_reading(reading):
        reading.label = label_map.get(reading.address)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        click.echo(f"[{ts}] {reading}")
        seen.add(reading.address)
        if conn:
            now = datetime.datetime.now()
            temp_changed = reading.temp_f != last_temp.get(reading.address)
            hum_changed  = reading.humidity != last_hum.get(reading.address)
            overdue = (now - last_write.get(reading.address, datetime.datetime.min)) >= HEARTBEAT
            if temp_changed or hum_changed or overdue:
                insert_reading(conn, reading)
                last_temp[reading.address] = reading.temp_f
                last_hum[reading.address]  = reading.humidity
                last_write[reading.address] = now

    click.echo("Scanning for Govee H5074 sensors... (Ctrl+C to stop)")
    try:
        asyncio.run(scan(on_reading, duration=duration, verbose=verbose))
    except KeyboardInterrupt:
        click.echo(f"\nDone. Saw {len(seen)} device(s).")


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind address.")
@click.option("--port", default=5000, show_default=True, help="Port to listen on.")
@click.option("--db", default=DEFAULT_DB, show_default=True,
              help="SQLite database path.")
@click.option("--debug", is_flag=True, hidden=True)
def serve(host, port, db, debug):
    """Run the HTTP API server.

    Endpoints:\n
      GET /api/current           — latest reading per sensor\n
      GET /api/history           — historical readings\n
        ?label=inside            — filter by label\n
        ?start=2026-01-01        — earliest timestamp\n
        ?end=2026-03-12          — latest timestamp\n
        ?limit=1000              — max rows (default 1000, max 10000)
    """
    from smart_home.web import run
    click.echo(f"Serving on http://{host}:{port}  (db: {db})")
    run(db_path=db, host=host, port=port, debug=debug)


@main.command()
@click.option("--timeout", "-t", type=float, default=30.0,
              help="Seconds to scan (default: 30).")
@click.option("--verbose", "-v", is_flag=True, help="Show raw advertisement data.")
def scan_once(timeout, verbose):
    """Scan for a fixed duration and print all devices found."""
    label_map = _labels.load()
    readings: dict[str, object] = {}

    def on_reading(reading):
        reading.label = label_map.get(reading.address)
        readings[reading.address] = reading

    click.echo(f"Scanning for {timeout}s...")
    try:
        asyncio.run(scan(on_reading, duration=timeout, verbose=verbose))
    except KeyboardInterrupt:
        pass

    if not readings:
        click.echo("No Govee H5074 devices found.")
    else:
        click.echo(f"\nFound {len(readings)} device(s):")
        for r in readings.values():
            click.echo(f"  {r}")


@main.command("gatt-dump")
@click.argument("address")
def gatt_dump(address):
    """Connect to ADDRESS and dump all GATT services and readable characteristic values.

    Use this to find where battery info is stored. Example:\n
      govee-monitor gatt-dump A4:C1:38:C7:6E:35
    """
    click.echo(f"Connecting to {address}...")
    asyncio.run(dump_gatt(address))


@main.command("scan-all")
@click.option("--timeout", "-t", type=float, default=15.0,
              help="Seconds to scan (default: 15).")
def scan_all(timeout):
    """Scan for ALL nearby BLE devices and dump their raw advertisement data.

    Use this to diagnose what your sensors are actually advertising.
    """
    import asyncio
    from bleak import BleakScanner

    seen = {}

    def callback(device, adv):
        seen[device.address] = (device, adv)

    async def _run():
        async with BleakScanner(detection_callback=callback):
            await asyncio.sleep(timeout)

    click.echo(f"Scanning all BLE devices for {timeout}s...")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

    if not seen:
        click.echo("No BLE devices found. Check that bluetoothd is running and you have permission.")
        click.echo("Try: sudo govee-monitor scan-all")
        return

    click.echo(f"\nFound {len(seen)} device(s):\n")
    for addr, (device, adv) in sorted(seen.items()):
        name = device.name or adv.local_name or "(no name)"
        click.echo(f"  {addr}  name={name!r}  rssi={adv.rssi}")
        if adv.manufacturer_data:
            for cid, data in adv.manufacturer_data.items():
                click.echo(f"    manufacturer[0x{cid:04X}] = {data.hex()}")
        if adv.service_data:
            for uuid, data in adv.service_data.items():
                click.echo(f"    service_data[{uuid}] = {data.hex()}")
        if adv.service_uuids:
            click.echo(f"    service_uuids = {adv.service_uuids}")


if __name__ == "__main__":
    main()
