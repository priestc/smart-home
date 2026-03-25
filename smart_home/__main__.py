from __future__ import annotations
import asyncio
import csv
import datetime
import io
import shutil
import zipfile
import os
from pathlib import Path
import click
from bleak import BleakScanner
from smart_home.scanner import scan, is_xiaomi_lywsd03mmc, is_pvvx_lywsd03mmc, read_lywsd03mmc
from smart_home import labels as _labels
from smart_home import pvvx as _pvvx
from smart_home import presence as _presence
from smart_home import push as _push
from smart_home.battery import dump_gatt
from smart_home.db import open_db, insert_reading, bulk_insert, insert_no_reading

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


@main.command("unlabel")
@click.argument("sensor")
@click.option("--purge", is_flag=True, help="Also delete all DB readings for this sensor.")
@click.option("--db", default=DEFAULT_DB, show_default=True, help="SQLite database path.")
def unlabel(sensor, purge, db):
    """Remove a sensor by label name or MAC address.

    SENSOR can be a label (e.g. 'Outside') or a MAC address.
    """
    label_map = _labels.load()

    # Find the entry — match by address or label (case-insensitive)
    sensor_lower = sensor.lower()
    match_addr = next(
        (addr for addr, lbl in label_map.items()
         if addr.lower() == sensor_lower or lbl.lower() == sensor_lower),
        None,
    )

    if match_addr is None:
        click.echo(f"No sensor found matching {sensor!r}.")
        click.echo("Current labels:")
        for addr, lbl in label_map.items():
            click.echo(f"  {lbl}  ({addr})")
        return

    label_name = label_map[match_addr]
    click.echo(f"Removing sensor: {label_name} ({match_addr})")

    del label_map[match_addr]
    _labels.save(label_map)
    click.echo("Removed from labels.")

    if purge:
        conn = open_db(db)
        deleted = conn.execute(
            "DELETE FROM readings WHERE address = ? OR label = ?",
            (match_addr, label_name),
        ).rowcount
        conn.commit()
        conn.close()
        click.echo(f"Purged {deleted} readings from database.")
    else:
        click.echo("Tip: pass --purge to also delete its readings from the database.")


DEVICE_TYPES = {
    "1": ("Govee H5074",         ("Govee_H5074", "GVH5074")),
    "2": ("Xiaomi LYWSD03MMC",   ("LYWSD03MMC", "ATC_")),
}


@main.command("install-services")
def install_services():
    """Copy systemd service files to /etc/systemd/system/ and reload the daemon.

    Run with sudo:  sudo env PATH="$PATH" smart-home install-services
    """
    pkg_dir = Path(__file__).parent
    services = ["smart-home.service", "smart-home-api.service"]
    dest_dir = Path("/etc/systemd/system")
    for name in services:
        src = pkg_dir / name
        dst = dest_dir / name
        shutil.copy(src, dst)
        click.echo(f"Installed {dst}")
    os.system("systemctl daemon-reload")
    click.echo("\nDone. To enable and start:")
    click.echo("  sudo systemctl enable --now smart-home.service")
    click.echo("  sudo systemctl enable --now smart-home-api.service")


@main.command("list-devices")
def list_devices():
    """Show all registered devices and their labels."""
    label_map = _labels.load()
    if not label_map:
        click.echo("No devices registered. Run 'smart-home add-device' to add one.")
        return
    for addr, label in sorted(label_map.items(), key=lambda x: x[1]):
        click.echo(f"  {label:<20} {addr}")


@main.command("recent-readings")
@click.argument("label")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of readings to show.")
@click.option("--db", default=DEFAULT_DB, show_default=True, help="SQLite database path.")
def recent_readings(label, limit, db):
    """Show the most recent readings for a sensor label.

    Example: smart-home recent-readings inside
    """
    conn = open_db(db)
    rows = conn.execute(
        "SELECT ts, temp_f, humidity FROM readings WHERE label = ? ORDER BY ts DESC LIMIT ?",
        (label, limit),
    ).fetchall()
    if not rows:
        click.echo(f"No readings found for label '{label}'.")
        return
    rows = list(reversed(rows))  # show oldest first, most recent at bottom
    now = datetime.datetime.now()

    def ago(ts_str):
        try:
            dt = datetime.datetime.fromisoformat(ts_str)
        except ValueError:
            return ""
        secs = int((now - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        return f"{secs // 86400}d ago"

    click.echo(f"\n  Recent readings for: {label}\n")
    click.echo(f"  {'timestamp':<22} {'temp (°F)':<12} {'humidity':<12} {'when'}")
    click.echo("  " + "-" * 56)
    for ts, temp_f, humidity in rows:
        click.echo(f"  {ts:<22} {temp_f:<12.1f} {humidity:<12.1f} {ago(ts)}")


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
        temp_str = f"{temp_f:.1f}" if temp_f is not None else "—"
        hum_str  = f"{humidity:.1f}%" if humidity is not None else "—"
        click.echo(f"  {ts:<22} {(lbl or ''):<20} {temp_str:<12} {hum_str}")


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


@main.command("add-presence-device")
@click.option("--timeout", "-t", type=float, default=15.0,
              help="Seconds to scan (default: 15).")
def add_presence_device(timeout):
    """Scan for BLE devices and register one as a presence detector."""
    from bleak import BleakScanner

    found = {}  # ble_name -> rssi (only devices with a name)

    def callback(device, adv):
        name = device.name or adv.local_name or ""
        if name:
            found[name] = adv.rssi

    async def _run():
        async with BleakScanner(detection_callback=callback):
            await asyncio.sleep(timeout)

    click.echo(f"Scanning for BLE devices ({int(timeout)}s)...")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

    if not found:
        click.echo("No named devices found.")
        return

    devices_list = sorted(found.items(), key=lambda x: x[1], reverse=True)  # sort by rssi
    click.echo(f"\nFound {len(devices_list)} named device(s):\n")
    for i, (name, rssi) in enumerate(devices_list, 1):
        click.echo(f"  {i}. {name!r}  rssi={rssi}")

    choice = click.prompt("\nEnter number to register as presence device", type=int)
    if not 1 <= choice <= len(devices_list):
        click.echo("Invalid choice.")
        return

    ble_name, _ = devices_list[choice - 1]
    label = click.prompt("Display name for this device", default=ble_name).strip()

    devices = _presence.load_devices()
    devices[ble_name] = label
    _presence.save_devices(devices)
    click.echo(f"\nRegistered '{label}' (BLE name: {ble_name!r}) as a presence device.")


@main.command("list-presence-devices")
def list_presence_devices():
    """Show registered presence devices and their current status."""
    devices = _presence.load_devices()
    if not devices:
        click.echo("No presence devices registered. Run 'smart-home add-presence-device' to add one.")
        return

    state = _presence.load_state()
    now = datetime.datetime.now()

    def since(ts_str):
        if not ts_str:
            return "never"
        try:
            dt = datetime.datetime.fromisoformat(ts_str)
            secs = int((now - dt).total_seconds())
            if secs < 60:    return f"{secs}s ago"
            if secs < 3600:  return f"{secs // 60}m ago"
            if secs < 86400: return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
            return f"{secs // 86400}d ago"
        except ValueError:
            return ts_str

    click.echo(f"\n  {'label':<24} {'ble name':<24} {'status':<10} {'last seen'}")
    click.echo("  " + "-" * 76)
    for ble_name, label in sorted(devices.items(), key=lambda x: x[1]):
        s = state.get(ble_name, {})
        status = s.get("status", "unknown")
        last_seen = since(s.get("last_seen"))
        stale = status == "home" and s.get("last_seen") and \
            (now - datetime.datetime.fromisoformat(s["last_seen"])).total_seconds() > 300
        flag = "  (stale?)" if stale else ""
        click.echo(f"  {label:<24} {ble_name:<24} {status:<10} {last_seen}{flag}")


@main.command("configure-push")
def configure_push():
    """Set up Apple Push Notification (APNs) credentials.

    You'll need an APNs Auth Key (.p8 file) from the Apple Developer portal:
    Certificates, Identifiers & Profiles → Keys → Create a key with APNs enabled.
    """
    click.echo("\nAPNs Push Notification Setup\n")
    click.echo("You need an APNs Auth Key from developer.apple.com.")
    click.echo("Go to: Certificates, Identifiers & Profiles → Keys → + → Enable Apple Push Notifications\n")

    key_file = click.prompt("Path to .p8 key file").strip()
    if not Path(key_file).expanduser().exists():
        click.echo(f"File not found: {key_file}")
        return

    key_id   = click.prompt("Key ID (10-character string from the key page)").strip()
    team_id  = click.prompt("Team ID (10-character string from your account page)").strip()
    bundle_id = click.prompt("App Bundle ID (e.g. com.yourname.smarthomenotify)").strip()
    sandbox  = click.confirm("Use sandbox/development APNs? (Yes for dev builds, No for App Store)", default=True)

    creds = {
        "key_file": str(Path(key_file).expanduser()),
        "key_id": key_id,
        "team_id": team_id,
        "bundle_id": bundle_id,
        "sandbox": sandbox,
    }
    _push.save_credentials(creds)
    click.echo("\nCredentials saved. The monitor will now send push notifications when a presence device goes away.")
    click.echo("\nTo test, run:  smart-home test-push")


@main.command("test-push")
def test_push():
    """Send a test push notification to all registered devices."""
    tokens = _push.load_tokens()
    if not tokens:
        click.echo("No devices registered. Open the SmartHome iOS app and tap 'Register for Notifications'.")
        return
    creds = _push.load_credentials()
    if not creds:
        click.echo("Push not configured. Run 'smart-home configure-push' first.")
        return
    click.echo(f"Sending test notification to {len(tokens)} device(s)...")
    _push.send_notification(title="Smart Home", body="Test notification — push is working!")
    click.echo("Done.")


@main.command("presence-history")
@click.option("--days", "-d", default=7, show_default=True, help="How many days back to analyze.")
@click.option("--label", "-l", default=None, help="Filter by presence device label.")
def presence_history(days, label):
    """Show presence history: away count and time breakdown.

    Example: smart-home presence-history --days 30
    """
    entries = _presence.load_history()
    devices = _presence.load_devices()

    if not entries and not devices:
        click.echo("No presence devices registered.")
        return
    if not entries:
        click.echo("No presence history recorded yet. The monitor must run to build history.")
        return

    now = datetime.datetime.now()
    window_start = now - datetime.timedelta(days=days)

    def fmt_dur(secs):
        secs = int(secs)
        if secs < 60:    return f"{secs}s"
        if secs < 3600:  return f"{secs // 60}m"
        d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
        if d:  return f"{d}d {h}h" if h else f"{d}d"
        return f"{h}h {m}m" if m else f"{h}h"

    # Group entries by ble_name
    by_device: dict[str, list] = {}
    for e in entries:
        by_device.setdefault(e["ble_name"], []).append(e)

    # Filter by label if requested
    if label:
        by_device = {k: v for k, v in by_device.items()
                     if v[0]["label"].lower() == label.lower()}
        if not by_device:
            click.echo(f"No history found for label '{label}'.")
            return

    for ble_name, dev_entries in sorted(by_device.items(), key=lambda x: x[1][0]["label"]):
        dev_label = dev_entries[0]["label"]
        dev_entries.sort(key=lambda e: e["ts"])

        # Split into before/within the window to determine initial status
        pre = [e for e in dev_entries if e["ts"] < window_start.isoformat()]
        in_win = [e for e in dev_entries if e["ts"] >= window_start.isoformat()]

        initial_status = pre[-1]["status"] if pre else "unknown"

        # Build list of (datetime, status) transitions within the window
        transitions = [(window_start, initial_status)]
        for e in in_win:
            transitions.append((datetime.datetime.fromisoformat(e["ts"]), e["status"]))
        transitions.append((now, None))  # sentinel

        # Build periods: (start, end, status)
        periods = []
        for i in range(len(transitions) - 1):
            start_dt, status = transitions[i]
            end_dt = transitions[i + 1][0]
            if status and status != "unknown":
                periods.append((start_dt, end_dt, status))

        home_secs = sum((e - s).total_seconds() for s, e, st in periods if st == "home")
        away_secs = sum((e - s).total_seconds() for s, e, st in periods if st == "away")
        total_secs = home_secs + away_secs
        away_periods = [(s, e) for s, e, st in periods if st == "away"]

        click.echo(f"\n  {dev_label}  (last {days} day{'s' if days != 1 else ''})")
        click.echo(f"  {'─' * 50}")

        if total_secs == 0:
            click.echo("  No data in this window.")
            continue

        home_pct = 100 * home_secs / total_secs
        away_pct = 100 * away_secs / total_secs
        click.echo(f"  Away events : {len(away_periods)}")
        click.echo(f"  Time home   : {fmt_dur(home_secs):>10}  ({home_pct:.0f}%)")
        click.echo(f"  Time away   : {fmt_dur(away_secs):>10}  ({away_pct:.0f}%)")

        if away_periods:
            click.echo(f"\n  Away periods:")
            for s, e in away_periods:
                dur = fmt_dur((e - s).total_seconds())
                end_str = e.strftime("%m-%d %H:%M") if e != now else "now"
                click.echo(f"    {s.strftime('%m-%d %H:%M')} → {end_str}  ({dur})")


@main.command()
@click.option("--duration", "-d", type=float, default=None,
              help="How many seconds to scan (default: indefinitely).")
@click.option("--verbose", "-v", is_flag=True, help="Show raw advertisement data.")
@click.option("--db", default=DEFAULT_DB, show_default=True,
              help="SQLite database path for storing readings.")
@click.option("--no-db", is_flag=True, help="Disable database logging.")
def monitor(duration, verbose, db, no_db):
    """Scan all BLE devices: log sensor readings and track presence."""
    label_map = _labels.load()
    pvvx_addresses = _pvvx.load_addresses()
    seen: set[str] = set()
    last_seen: dict[str, datetime.datetime] = {}   # address -> last advertisement received
    last_no_reading: dict[str, datetime.datetime] = {}  # address -> last no_reading insert
    MISSING_THRESHOLD = datetime.timedelta(minutes=10)

    # Xiaomi devices — polled actively via GATT on each advertisement (with cooldown).
    # Key insight: BlueZ evicts devices from its cache shortly after their last advertisement.
    # We must connect as soon as the device is seen, not on a fixed timer.
    xiaomi_devices: dict[str, tuple] = {}  # address -> (BLEDevice, name, last_rssi)
    _poll_active: set[str] = set()         # addresses currently being polled (one at a time per device)
    _last_poll_ok: dict[str, datetime.datetime] = {}  # address -> last successful poll time
    POLL_COOLDOWN = datetime.timedelta(seconds=30)
    scanner_ref: list = []

    # presence tracking
    presence_devices = _presence.load_devices()
    presence_last_seen: dict[str, datetime.datetime] = {}
    presence_state = _presence.load_state()
    presence_addr_map: dict[str, str] = {}  # MAC address -> ble_name (for nameless adverts)
    PRESENCE_TIMEOUT = datetime.timedelta(minutes=5)

    def on_device(device, adv):
        ble_name = device.name or adv.local_name or ""
        now = datetime.datetime.now()

        # Match by name first; if matched, remember this MAC address
        if ble_name and ble_name in presence_devices:
            presence_addr_map[device.address] = ble_name
            presence_last_seen[ble_name] = now
            if verbose:
                click.echo(f"[presence] {ble_name!r} seen (by name, addr={device.address})")
            return

        # Match by previously-seen MAC address (handles nameless advertisements)
        matched_name = presence_addr_map.get(device.address)
        if matched_name:
            presence_last_seen[matched_name] = now
            if verbose:
                click.echo(f"[presence] {matched_name!r} seen (by addr={device.address})")
            return

        if is_xiaomi_lywsd03mmc(device, adv) and device.address.upper() not in pvvx_addresses and device.address in label_map:
            is_new = device.address not in xiaomi_devices
            xiaomi_devices[device.address] = (device, ble_name or "LYWSD03MMC", adv.rssi)
            if is_new:
                label = label_map.get(device.address) or ble_name or device.address
                click.echo(f"[{now.strftime('%H:%M:%S')}] Discovered Xiaomi sensor: {label} ({device.address})")
            # Trigger a poll on every advertisement if not already polling this device
            # and the cooldown has passed. Device is guaranteed fresh in BlueZ cache right now.
            addr = device.address
            last_ok = _last_poll_ok.get(addr, datetime.datetime.min)
            if addr not in _poll_active and (now - last_ok) >= POLL_COOLDOWN:
                _poll_active.add(addr)
                try:
                    asyncio.get_running_loop().create_task(_poll_xiaomi(addr))
                except RuntimeError:
                    _poll_active.discard(addr)

        if verbose and ble_name:
            click.echo(f"[presence] untracked: {ble_name!r} ({device.address})")
            if adv.manufacturer_data:
                for cid, data in adv.manufacturer_data.items():
                    click.echo(f"  manufacturer_data[0x{cid:04X}] = {data.hex()}")
            if adv.service_data:
                for uuid, data in adv.service_data.items():
                    click.echo(f"  service_data[{uuid}] = {data.hex()}")

    async def check_presence():
        while True:
            await asyncio.sleep(30)
            now = datetime.datetime.now()
            changed = False
            for ble_name, label in presence_devices.items():
                last = presence_last_seen.get(ble_name)
                new_status = "home" if last and (now - last) < PRESENCE_TIMEOUT else "away"
                old_status = presence_state.get(ble_name, {}).get("status")
                old_last_seen = presence_state.get(ble_name, {}).get("last_seen")
                new_last_seen = last.isoformat() if last else None
                if new_status != old_status:
                    ts = now.strftime("%H:%M:%S")
                    click.echo(f"[{ts}] Presence: {label} is {new_status}")
                    if new_status == "away":
                        _push.send_notification(
                            title="Left home",
                            body=f"{label} left home",
                        )
                    _presence.append_history({
                        "ts": now.isoformat(timespec="seconds"),
                        "ble_name": ble_name,
                        "label": label,
                        "status": new_status,
                    })
                if new_status != old_status or new_last_seen != old_last_seen:
                    presence_state[ble_name] = {
                        "name": label,
                        "status": new_status,
                        "last_seen": new_last_seen,
                    }
                    changed = True
            if changed:
                _presence.save_state(presence_state)

    conn = None if no_db else open_db(db)
    if conn:
        click.echo(f"Logging to {db}")
    if presence_devices:
        click.echo(f"Tracking presence for {len(presence_devices)} device(s).")

    # Load all-time per-label records from DB: {label: {"high": float, "low": float}}
    records: dict[str, dict] = {}
    if conn:
        rows = conn.execute(
            "SELECT label, MAX(temp_f), MIN(temp_f) FROM readings WHERE temp_f IS NOT NULL AND label IS NOT NULL GROUP BY label"
        ).fetchall()
        for label, hi, lo in rows:
            records[label] = {"high": hi, "low": lo}

    _last_record_notify: datetime.datetime = datetime.datetime.min
    RECORD_NOTIFY_COOLDOWN = datetime.timedelta(hours=1)

    async def _notify_record(kind: str, label: str, temp: float) -> None:
        nonlocal _last_record_notify
        now = datetime.datetime.now()
        if kind == "low" and now.hour < 8:
            # Sleep until 8 AM today
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            await asyncio.sleep((target - now).total_seconds())
            now = datetime.datetime.now()
        if (now - _last_record_notify) < RECORD_NOTIFY_COOLDOWN:
            return
        _last_record_notify = now
        _push.send_notification(
            title=f"New record {kind}: {label}",
            body=f"{label} hit a new all-time record {kind} of {temp:.1f}°F",
        )

    async def check_missing_sensors():
        while True:
            await asyncio.sleep(300)  # check every 5 minutes
            if not conn:
                continue
            now = datetime.datetime.now()
            for addr, label in label_map.items():
                if not label:
                    continue
                last = last_seen.get(addr)
                if last is None:
                    continue  # never seen this session, skip
                if (now - last) >= MISSING_THRESHOLD:
                    last_nr = last_no_reading.get(addr, datetime.datetime.min)
                    if (now - last_nr) >= MISSING_THRESHOLD:
                        insert_no_reading(conn, label, addr)
                        last_no_reading[addr] = now
                        ts = now.strftime("%H:%M:%S")
                        click.echo(f"[{ts}] No reading: {label} ({addr})")

    async def _poll_xiaomi(addr: str) -> None:
        """Poll one Xiaomi sensor immediately after it has been seen advertising.
        Uses _poll_active to ensure only one poll per device runs at a time.
        Concurrent connections across different devices are allowed — BlueZ handles
        serialization and returns 'Operation already in progress' if busy.
        """
        try:
            # Brief delay to let BlueZ fully register the device before connecting
            await asyncio.sleep(0.5)
            entry = xiaomi_devices.get(addr)
            if entry is None:
                return
            ble_device, name, last_rssi = entry
            label = label_map.get(addr) or name
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            click.echo(f"[{ts}] Polling {label} ({addr})...")
            reading, err = await read_lywsd03mmc(ble_device, name)
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            if reading is not None:
                _last_poll_ok[addr] = datetime.datetime.now()
                _, _, last_rssi = xiaomi_devices.get(addr, (None, None, last_rssi))
                reading.rssi = last_rssi
                click.echo(f"[{ts}] Poll OK: {label} temp={reading.temp_f:.1f}°F humidity={reading.humidity:.1f}%")
                on_reading(reading)
            else:
                click.echo(f"[{ts}] Poll FAILED: {label} ({addr}): {err}")
                if err and "not found" in err:
                    # BlueZ evicted the device; remove so next advertisement triggers a fresh poll
                    xiaomi_devices.pop(addr, None)
        finally:
            _poll_active.discard(addr)

    # latest reading per address, updated on every advertisement
    latest_reading: dict[str, object] = {}

    def on_reading(reading):
        db_label = label_map.get(reading.address)
        reading.label = db_label or reading.name or reading.address  # always show something in terminal
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        click.echo(f"[{ts}] {reading}")
        seen.add(reading.address)
        last_seen[reading.address] = datetime.datetime.now()
        if db_label:
            latest_reading[reading.address] = reading

    async def snapshot_loop():
        """Once per minute, write the latest reading for every sensor to the DB
        using a single shared timestamp so all readings align in the database."""
        while True:
            await asyncio.sleep(60)
            if not conn or not latest_reading:
                continue
            ts = datetime.datetime.now().replace(second=0, microsecond=0).isoformat(timespec="seconds")
            for addr, reading in list(latest_reading.items()):
                conn.execute(
                    "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity, rssi, battery, raw_reading) VALUES (?,?,?,?,?,?,?,?)",
                    (ts, reading.address, reading.label, reading.temp_f, reading.humidity, reading.rssi, reading.battery, reading.raw_reading),
                )
            conn.commit()
            n = len(latest_reading)
            click.echo(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Snapshot written: {n} sensor(s) at {ts}")

        # Check for all-time temperature records (only for explicitly labeled sensors)
        label = db_label
        if label and reading.temp_f is not None:
            if label not in records:
                # First time seeing this label — initialize without notifying
                records[label] = {"high": reading.temp_f, "low": reading.temp_f}
            else:
                rec = records[label]
                if reading.temp_f > rec["high"]:
                    rec["high"] = reading.temp_f
                    click.echo(f"[{ts}] Record high for {label}: {reading.temp_f:.1f}°F")
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(_notify_record("high", label, reading.temp_f))
                    except RuntimeError:
                        pass
                elif reading.temp_f < rec["low"]:
                    rec["low"] = reading.temp_f
                    click.echo(f"[{ts}] Record low for {label}: {reading.temp_f:.1f}°F")
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(_notify_record("low", label, reading.temp_f))
                    except RuntimeError:
                        pass

    click.echo("Monitoring BLE devices... (Ctrl+C to stop)")
    try:
        extra = [check_missing_sensors(), snapshot_loop()]
        if presence_devices:
            extra.append(check_presence())
        asyncio.run(scan(
            on_reading,
            duration=duration,
            verbose=verbose,
            on_device=on_device,
            extra_tasks=extra,
            scanner_ref=scanner_ref,
        ))
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
        return

    label_map = _labels.load()
    supported, unsupported = {}, {}
    for addr, (device, adv) in seen.items():
        name = device.name or adv.local_name or ""
        is_supported = any(name.startswith(p) for _, prefixes in DEVICE_TYPES.values() for p in prefixes)
        (supported if is_supported else unsupported)[addr] = (device, adv)

    def print_device(addr, device, adv, show_label=False):
        name = device.name or adv.local_name or "(no name)"
        label = label_map.get(addr)
        label_str = f"  [{label}]" if label else "  [no label]" if show_label else ""
        click.echo(f"  {addr}  name={name!r}  rssi={adv.rssi}{label_str}")
        if adv.manufacturer_data:
            for cid, data in adv.manufacturer_data.items():
                click.echo(f"    manufacturer[0x{cid:04X}] = {data.hex()}")
        if adv.service_data:
            for uuid, data in adv.service_data.items():
                click.echo(f"    service_data[{uuid}] = {data.hex()}")
        if adv.service_uuids:
            click.echo(f"    service_uuids = {adv.service_uuids}")

    click.echo(f"\n── Supported devices ({len(supported)}) ──────────────────────────")
    if supported:
        for addr, (device, adv) in sorted(supported.items()):
            print_device(addr, device, adv, show_label=True)
    else:
        click.echo("  (none found)")

    click.echo(f"\n── Unsupported devices ({len(unsupported)}) ─────────────────────")
    if unsupported:
        for addr, (device, adv) in sorted(unsupported.items()):
            print_device(addr, device, adv)


@main.command("mark-pvvx")
@click.argument("address")
def mark_pvvx(address):
    """Mark a sensor as already running PVVX firmware.

    Use this if a sensor was flashed before this tracking was added.
    ADDRESS is the MAC address (e.g. A4:C1:38:7C:6F:9D).
    """
    _pvvx.mark_address(address)
    click.echo(f"Marked {address.upper()} as PVVX.")
    click.echo(f"Known PVVX devices: {sorted(_pvvx.load_addresses())}")


@main.command("flash")
@click.argument("address", required=False)
@click.option(
    "--firmware", "-f", "firmware_path", default=None,
    help="Path to a .bin file to flash (default: download latest PVVX from GitHub).",
)
@click.option(
    "--timeout", "-t", type=float, default=40.0,
    help="Seconds to wait for sensor after power-cycle (default: 40).",
)
def flash_device(address, firmware_path, timeout):
    """Flash PVVX custom firmware onto a LYWSD03MMC temperature sensor.

    After flashing, the sensor broadcasts temperature/humidity passively
    (no GATT connection needed) and its BLE name changes to ATC_XXXXXX.
    Existing labels are preserved — the MAC address does not change.

    If ADDRESS is not given, scans for nearby LYWSD03MMC sensors first.
    """
    from smart_home import flasher as _flasher

    label_map = _labels.load()

    # ── 1. Identify the target sensor ────────────────────────────────────────
    if not address:
        click.echo("Scanning for LYWSD03MMC sensors (10s)...")
        found: dict[str, str] = {}

        def _cb(device, adv):
            name = device.name or adv.local_name or ""
            if name.startswith("LYWSD03MMC") and device.address not in found:
                found[device.address] = name

        async def _quick_scan():
            async with BleakScanner(detection_callback=_cb):
                await asyncio.sleep(10.0)

        try:
            asyncio.run(_quick_scan())
        except KeyboardInterrupt:
            pass

        if not found:
            click.echo(
                "No LYWSD03MMC sensors found.\n"
                "  • Make sure the sensor is powered on.\n"
                "  • If it was already flashed with PVVX it will show as ATC_XXXXXX.\n"
                "  • You can pass the MAC address directly: smart-home flash AA:BB:CC:DD:EE:FF"
            )
            return

        items = list(found.items())
        click.echo(f"\nFound {len(items)} sensor(s):\n")
        for i, (addr, name) in enumerate(items, 1):
            lbl = label_map.get(addr, "")
            click.echo(f"  {i}.  {addr}  {name}" + (f"  [{lbl}]" if lbl else ""))

        if len(items) == 1:
            address = items[0][0]
            click.echo(f"\nAuto-selected the only sensor.")
        else:
            idx = click.prompt("\nEnter number to flash", type=int)
            if not 1 <= idx <= len(items):
                click.echo("Invalid choice.")
                return
            address = items[idx - 1][0]

    address = address.upper()
    sensor_label = label_map.get(address, address)
    click.echo(f"\nSensor to flash: {sensor_label} ({address})")

    # ── 2. Load / download firmware ───────────────────────────────────────────
    if firmware_path:
        firmware_data = Path(firmware_path).read_bytes()
        click.echo(f"Firmware: {firmware_path} ({len(firmware_data):,} bytes)")
    else:
        click.echo("Downloading PVVX firmware...")
        try:
            firmware_data = _flasher.download_firmware()
        except Exception as e:
            click.echo(f"Download failed: {e}\nUse --firmware /path/to/ATC_v57.bin instead.")
            return
        click.echo(f"Firmware ready ({len(firmware_data):,} bytes)")

    try:
        total_blocks = _flasher.validate_firmware(firmware_data)
    except ValueError as e:
        click.echo(f"Invalid firmware: {e}")
        return

    # ── 3. Power-cycle instruction ────────────────────────────────────────────
    click.echo()
    click.echo("The sensor must be in connectable mode to accept the firmware.")
    click.echo("Steps:")
    click.echo("  1. Remove the battery from the sensor")
    click.echo("  2. Press Enter below")
    click.echo("  3. Immediately reinsert the battery")
    click.echo()
    click.prompt(
        "Press Enter, then quickly reinsert the battery",
        default="", prompt_suffix=" > ", show_default=False,
    )

    # ── 4. Scan for device, then flash immediately when seen ─────────────────
    click.echo(f"Waiting for {sensor_label} (up to {int(timeout)}s)...")
    target: list = [None]
    flash_error: list[str | None] = [None]

    async def _wait_and_flash():
        found_ev = asyncio.Event()

        def _det(device, adv):
            if device.address.upper() == address:
                target[0] = device
                found_ev.set()

        scanner = BleakScanner(detection_callback=_det)
        async with scanner:
            try:
                await asyncio.wait_for(found_ev.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                flash_error[0] = (
                    f"Sensor not found within {int(timeout)}s. "
                    "Try power-cycling again immediately before inserting the battery."
                )
                return

        click.echo(f"Found {sensor_label}! Connecting and flashing ({total_blocks} blocks)...")
        click.echo()

        last_pct: list[int] = [-1]
        bar_started: list[bool] = [False]

        def _progress(done: int, total: int, reconnecting: bool = False) -> None:
            if reconnecting:
                click.echo(f"\n  Connection dropped at block {done}/{total}, reconnecting and restarting from block 0...")
                last_pct[0] = -1  # force redraw after reconnect
                return
            pct = done * 100 // total
            if pct != last_pct[0]:
                last_pct[0] = pct
                filled = pct // 5
                bar = "#" * filled + "." * (20 - filled)
                click.echo(f"\r  [{bar}] {pct:3d}%  ({done}/{total} blocks)", nl=False)
                bar_started[0] = True

        # Print an initial empty bar so something is always visible while flashing.
        _progress(0, total_blocks)

        try:
            await _flasher.flash_firmware(target[0], firmware_data, progress=_progress)
        except Exception as e:
            flash_error[0] = str(e)
        finally:
            if bar_started[0]:
                click.echo()  # newline after progress bar

    try:
        asyncio.run(_wait_and_flash())
    except KeyboardInterrupt:
        click.echo("\nAborted.")
        return

    if flash_error[0]:
        click.echo(f"\nFlash failed: {flash_error[0]}")
        return

    # ── 5. Verify firmware installation ──────────────────────────────────────
    atc_name = "ATC_" + address.replace(":", "")[-6:]
    click.echo(f"\nFlash complete! The sensor is rebooting.")
    click.echo(f"Verifying firmware installation (waiting for {atc_name} advertisement)...")

    verify_timeout = 30.0
    verified: list[bool] = [False]

    old_name_seen: list[bool] = [False]

    async def _verify_firmware():
        found_ev = asyncio.Event()

        def _det(device, adv):
            name = device.name or adv.local_name or ""
            # If the MAC address is still advertising as LYWSD03MMC, the flash
            # did not take — record it and stop waiting.
            if device.address.upper() == address.upper() and name.upper().startswith("LYWSD03MMC"):
                old_name_seen[0] = True
                found_ev.set()
                return
            # Match by expected ATC name, or by MAC address advertising as PVVX
            if name.upper() == atc_name.upper() or (
                device.address.upper() == address.upper() and name.upper().startswith("ATC_")
            ):
                verified[0] = True
                found_ev.set()

        async with BleakScanner(detection_callback=_det):
            try:
                await asyncio.wait_for(found_ev.wait(), timeout=verify_timeout)
            except asyncio.TimeoutError:
                pass

    try:
        asyncio.run(_verify_firmware())
    except KeyboardInterrupt:
        click.echo("\nVerification skipped.")

    if old_name_seen[0]:
        click.echo(
            f"✗ Flash failed — {address} is still advertising as LYWSD03MMC.\n"
            "   The sensor did not accept the new firmware. Try again:\n"
            "   power the sensor off and back on, then re-run: smart-home flash"
        )
        return
    elif verified[0]:
        click.echo(f"✓ Firmware verified — {atc_name} is advertising successfully.")
    else:
        click.echo(
            f"⚠  Could not detect {atc_name} within {int(verify_timeout)}s.\n"
            "   The flash may still have succeeded — the sensor sometimes takes\n"
            "   longer to reboot. You can confirm manually with: smart-home scan"
        )

    # ── 6. Post-flash ─────────────────────────────────────────────────────────
    _pvvx.mark_address(address)

    click.echo(f"New BLE name: {atc_name}")
    click.echo()

    if address not in label_map:
        if click.confirm("Save a label for this sensor?", default=True):
            lbl = click.prompt("Label", default=atc_name).strip()
            if lbl:
                label_map[address] = lbl
                _labels.save(label_map)
                click.echo("Label saved.")
    else:
        click.echo(f"Existing label kept: {label_map[address]}")


if __name__ == "__main__":
    main()
