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
from smart_home import camera as _camera
from smart_home import garage as _garage
from smart_home import smart_plug as _smart_plug
from smart_home.battery import dump_gatt
from smart_home.db import open_db, insert_reading, bulk_insert, insert_no_reading, insert_plug_reading

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


def _remove_ble_sensor(name, purge, db):
    label_map = _labels.load()
    name_lower = name.lower()
    match_addr = next(
        (addr for addr, lbl in label_map.items()
         if addr.lower() == name_lower or lbl.lower() == name_lower),
        None,
    )
    if match_addr is None:
        return False
    lbl = label_map.pop(match_addr)
    _labels.save(label_map)
    click.echo(f"Removed BLE sensor '{lbl}' ({match_addr}).")
    if purge:
        conn = open_db(db)
        deleted = conn.execute(
            "DELETE FROM readings WHERE address = ? OR label = ?",
            (match_addr, lbl),
        ).rowcount
        conn.commit()
        conn.close()
        click.echo(f"Purged {deleted} readings from database.")
    return True


DEVICE_CATEGORIES = {
    "1": "BLE Sensor (temperature/humidity)",
    "2": "Thermostat",
    "3": "Smart Plug",
    "4": "Presence Device",
}

DEVICE_TYPES = {
    "1": ("Govee H5074",         ("Govee_H5074", "GVH5074")),
    "2": ("Xiaomi LYWSD03MMC",   ("LYWSD03MMC", "ATC_")),
}

THERMOSTAT_TYPES = {
    "1": "Ecobee",
    "2": "Home Assistant (via local API)",
}

SMART_PLUG_TYPES = {
    "1": "SONOFF S31",
}


def _add_smart_plug():
    click.echo("What type of smart plug do you want to add?\n")
    for key, name in SMART_PLUG_TYPES.items():
        click.echo(f"  {key}. {name}")
    choice = click.prompt("\nEnter choice", type=click.Choice(list(SMART_PLUG_TYPES)))
    plug_type = SMART_PLUG_TYPES[choice]

    click.echo("\nScanning local network for Tasmota devices...")
    found = _smart_plug.discover()

    if not found:
        click.echo("No Tasmota devices found automatically.")
        ip = click.prompt("Enter the IP address of the plug manually").strip()
    elif len(found) == 1:
        d = found[0]
        found_label = d["friendly_name"] or d["topic"] or d["ip"]
        click.echo(f"Found: {found_label} ({d['ip']})")
        if not click.confirm("Is this the plug you want to add?", default=True):
            ip = click.prompt("Enter the IP address manually").strip()
        else:
            ip = d["ip"]
    else:
        click.echo(f"\nFound {len(found)} Tasmota device(s):\n")
        for i, d in enumerate(found, 1):
            found_label = d["friendly_name"] or d["topic"] or d["ip"]
            click.echo(f"  {i}. {found_label} ({d['ip']})")
        idx = click.prompt("\nWhich one is the plug you want to add?",
                           type=click.IntRange(1, len(found))) - 1
        ip = found[idx]["ip"]

    name = click.prompt("\nWhat do you want to name this plug?").strip()
    device = click.prompt("What device is plugged into it (what you want to record the power draw of)?").strip()

    plugs = _smart_plug.load_config()
    plugs.append({
        "type": plug_type,
        "name": name,
        "device": device,
        "ip": ip,
    })
    _smart_plug.save_config(plugs)

    click.echo(f"\nSaved. Plug '{name}' monitoring '{device}' ({plug_type} at {ip}) has been registered.")
    click.echo("Run 'smart-home monitor' to start polling this plug.")


def _add_thermostat():
    click.echo("What type of thermostat do you want to add?\n")
    for key, name in THERMOSTAT_TYPES.items():
        click.echo(f"  {key}. {name}")
    choice = click.prompt("\nEnter choice", type=click.Choice(list(THERMOSTAT_TYPES)))

    if choice == "1":
        _add_thermostat_ecobee()
    elif choice == "2":
        _add_thermostat_homeassistant()


def _add_thermostat_ecobee():
    from smart_home import ecobee as _ecobee

    click.echo("\nYou need an Ecobee developer API key.")
    click.echo("Create one at ecobee.com: My Apps → Add Application → select 'ecobeePin'.\n")
    api_key = click.prompt("Ecobee API key").strip()

    click.echo("\nRequesting PIN from Ecobee...")
    try:
        pin_data = _ecobee.request_pin(api_key)
    except Exception as e:
        click.echo(f"Failed: {e}")
        return

    pin = pin_data["ecobeePin"]
    code = pin_data["code"]
    expires_min = pin_data.get("expires_in", 900) // 60
    click.echo(f"\n  PIN: {pin}\n")
    click.echo("Go to ecobee.com → My Account → My Apps → Add Application")
    click.echo(f"and enter the PIN above. You have {expires_min} minutes.\n")
    click.prompt("Press Enter when done", default="", prompt_suffix="", show_default=False)

    click.echo("Authorizing...")
    try:
        token_data = _ecobee.authorize(api_key, code)
    except Exception as e:
        click.echo(f"Authorization failed: {e}")
        return

    cfg = {
        "api_key": api_key,
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "label": "",
    }

    click.echo("Connected! Fetching thermostat info...")
    try:
        thermostats = _ecobee.get_thermostats(cfg)
    except Exception as e:
        click.echo(f"Failed to fetch thermostat data: {e}")
        return

    if not thermostats:
        click.echo("No thermostats found on this account.")
        return

    if len(thermostats) == 1:
        t = thermostats[0]
        click.echo(f'Found: "{t["name"]}" (serial: {t["identifier"]})')
    else:
        click.echo(f"\nFound {len(thermostats)} thermostat(s):\n")
        for i, t in enumerate(thermostats, 1):
            click.echo(f"  {i}. {t['name']} ({t['identifier']})")
        idx = click.prompt("Which one?", type=click.IntRange(1, len(thermostats))) - 1
        t = thermostats[idx]

    room = click.prompt("\nWhat room is this thermostat in?").strip().lower().replace(" ", "-")
    label = f"indoor-{room}"
    click.echo(f"Label: {label}")

    cfg["label"] = label
    cfg["identifier"] = t["identifier"]
    _ecobee.save_config(cfg)
    click.echo("\nSaved. Run 'smart-home monitor' to start polling this thermostat.")


def _add_thermostat_homeassistant():
    from smart_home import homeassistant as _ha

    click.echo("\nYou'll need a long-lived access token from Home Assistant.")
    click.echo("Go to your HA profile page → Long-Lived Access Tokens → Create Token.\n")
    url = click.prompt("Home Assistant URL (e.g. http://homeassistant.local:8123)").strip().rstrip("/")
    token = click.prompt("Long-lived access token").strip()

    cfg = {"url": url, "token": token, "entity_id": "", "label": ""}

    click.echo("\nConnecting...")
    try:
        _ha.test_connection(cfg)
    except Exception as e:
        click.echo(f"Connection failed: {e}")
        return

    click.echo("Connected! Fetching climate entities...")
    try:
        entities = _ha.get_climate_entities(cfg)
    except Exception as e:
        click.echo(f"Failed to fetch entities: {e}")
        return

    if not entities:
        click.echo("No climate entities with temperature data found in Home Assistant.")
        return

    click.echo(f"\nFound {len(entities)} thermostat(s):\n")
    for i, e in enumerate(entities, 1):
        temp = e["current_temperature"]
        hum = f"  humidity={e['current_humidity']}%" if e["current_humidity"] is not None else ""
        click.echo(f"  {i}. {e['name']} ({e['entity_id']})  temp={temp}°F{hum}")

    if len(entities) == 1:
        entity = entities[0]
    else:
        idx = click.prompt("\nWhich one?", type=click.IntRange(1, len(entities))) - 1
        entity = entities[idx]

    room = click.prompt("\nWhat room is this thermostat in?").strip().lower().replace(" ", "-")
    label = f"indoor-{room}"
    click.echo(f"Label: {label}")

    cfg["entity_id"] = entity["entity_id"]
    cfg["label"] = label
    _ha.save_config(cfg)
    click.echo("\nSaved. Run 'smart-home monitor' to start polling this thermostat.")


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
    """Show all registered devices of all types."""
    from smart_home import ecobee as _ecobee
    from smart_home import homeassistant as _ha

    any_found = False

    label_map = _labels.load()
    if label_map:
        any_found = True
        click.echo("\n  BLE Sensors:")
        click.echo("  " + "-" * 50)
        for addr, lbl in sorted(label_map.items(), key=lambda x: x[1]):
            click.echo(f"  {lbl:<24} {addr}")

    ecobee_cfg = _ecobee.load_config()
    ha_cfg = _ha.load_config()
    if ecobee_cfg or ha_cfg:
        any_found = True
        click.echo("\n  Thermostats:")
        click.echo("  " + "-" * 50)
        if ecobee_cfg:
            lbl = ecobee_cfg.get("label", "(no label)")
            ident = ecobee_cfg.get("identifier", "")
            click.echo(f"  {lbl:<24} Ecobee ({ident})")
        if ha_cfg:
            lbl = ha_cfg.get("label", "(no label)")
            entity = ha_cfg.get("entity_id", "")
            click.echo(f"  {lbl:<24} Home Assistant ({entity})")

    plugs = _smart_plug.load_config()
    if plugs:
        any_found = True
        click.echo("\n  Smart Plugs:")
        click.echo("  " + "-" * 50)
        for p in plugs:
            click.echo(f"  {p.get('name', ''):<24} {p.get('type', '')} → {p.get('device', '')} ({p.get('ip', '')})")

    presence = _presence.load_devices()
    if presence:
        any_found = True
        state = _presence.load_state()
        now = datetime.datetime.now()

        def _since(ts_str):
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

        click.echo("\n  Presence Devices:")
        click.echo("  " + "-" * 50)
        for ble_name, lbl in sorted(presence.items(), key=lambda x: x[1]):
            s = state.get(ble_name, {})
            status = s.get("status", "unknown")
            last_seen = _since(s.get("last_seen"))
            stale = (status == "home" and s.get("last_seen") and
                     (now - datetime.datetime.fromisoformat(s["last_seen"])).total_seconds() > 300)
            flag = "  (stale?)" if stale else ""
            click.echo(f"  {lbl:<24} {ble_name:<24} {status:<10} {last_seen}{flag}")

    if not any_found:
        click.echo("No devices registered. Run 'smart-home add-device' to add one.")


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
              help="Seconds to scan for BLE devices (default: 15).")
def add_device(timeout):
    """Register a new device of any type."""
    click.echo("What type of device do you want to add?\n")
    for key, name in DEVICE_CATEGORIES.items():
        click.echo(f"  {key}. {name}")
    choice = click.prompt("\nEnter choice", type=click.Choice(list(DEVICE_CATEGORIES)))

    if choice == "1":
        _add_ble_sensor(timeout)
    elif choice == "2":
        _add_thermostat()
    elif choice == "3":
        _add_smart_plug()
    elif choice == "4":
        _add_presence_device(timeout)


def _add_ble_sensor(timeout):
    click.echo("\nWhat type of BLE sensor do you want to add?\n")
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
        lbl = click.prompt(f"  {name} ({addr})").strip()
        if lbl:
            label_map[addr] = lbl
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
                ts = line[0].strip()
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


def _add_presence_device(timeout):
    found = {}  # ble_name -> rssi (only devices with a name)

    def callback(device, adv):
        name = device.name or adv.local_name or ""
        if name:
            found[name] = adv.rssi

    async def _run():
        async with BleakScanner(detection_callback=callback):
            await asyncio.sleep(timeout)

    click.echo(f"\nScanning for BLE devices ({int(timeout)}s)...")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

    if not found:
        click.echo("No named devices found.")
        return

    devices_list = sorted(found.items(), key=lambda x: x[1], reverse=True)
    click.echo(f"\nFound {len(devices_list)} named device(s):\n")
    for i, (name, rssi) in enumerate(devices_list, 1):
        click.echo(f"  {i}. {name!r}  rssi={rssi}")

    choice = click.prompt("\nEnter number to register as presence device", type=int)
    if not 1 <= choice <= len(devices_list):
        click.echo("Invalid choice.")
        return

    ble_name, _ = devices_list[choice - 1]
    lbl = click.prompt("Display name for this device", default=ble_name).strip()

    devices = _presence.load_devices()
    devices[ble_name] = lbl
    _presence.save_devices(devices)
    click.echo(f"\nRegistered '{lbl}' (BLE name: {ble_name!r}) as a presence device.")


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


@main.command("configure-camera")
def configure_camera():
    """Add or update an XIAO ESP32-S3 camera.

    Stores the camera's IP in ~/.config/smart-home/cameras.json.
    Use the web UI at /camera to define motion zones on the live frame.
    """
    import httpx

    click.echo("\nCamera Setup\n")
    name = click.prompt("Camera name (e.g. 'front-door')").strip()
    ip   = click.prompt("Camera IP address (e.g. 192.168.1.100)").strip()
    url  = f"http://{ip}"

    import httpx

    SNAPSHOT_PATHS = ["/snapshot", "/capture", "/jpg", "/image.jpg"]
    click.echo("Probing snapshot endpoint...")
    snapshot_path = None
    for path in SNAPSHOT_PATHS:
        try:
            r = httpx.get(f"{url}{path}", timeout=5.0)
            if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                snapshot_path = path
                click.echo(f"✓ Connected via {path} ({len(r.content)//1024} KB snapshot).")
                break
        except Exception:
            pass

    if snapshot_path is None:
        snapshot_path = click.prompt(
            "Could not auto-detect snapshot path. Enter it manually (e.g. /capture)",
            default="/snapshot",
        ).strip()

    cameras = _camera.load_config()
    existing = next((c for c in cameras if c["name"] == name), None)
    if existing:
        existing["url"] = url
        existing["snapshot_path"] = snapshot_path
        click.echo(f"Updated existing camera '{name}'.")
    else:
        cameras.append({"name": name, "url": url, "snapshot_path": snapshot_path, "zones": []})
        click.echo(f"Added camera '{name}'.")

    _camera.save_config(cameras)
    click.echo("\nDone. Define motion zones at: http://<your-server>:5000/camera")


@main.command("discover-shelly")
@click.option("--subnet", default=None, help="Override subnet to scan (e.g. 192.168.0). Default: auto-detect.")
def discover_shelly(subnet):
    """Scan the local network for Shelly devices and print their IPs and models."""
    detected = _garage.local_subnet() if subnet is None else subnet
    click.echo(f"Scanning {detected}.1-254 for Shelly devices...")
    found = _garage.discover(detected)
    if not found:
        click.echo("No Shelly devices found.")
        return
    click.echo(f"Found {len(found)} device(s):\n")
    for d in found:
        name  = d.get("name") or d.get("id") or "unknown"
        model = d.get("app") or d.get("type") or "?"
        gen   = d.get("gen", "?")
        mac   = d.get("mac", "?")
        click.echo(f"  {d['ip']:16s}  {name}  (model: {model}, gen: {gen}, mac: {mac})")


@main.command("configure-garage")
def configure_garage():
    """Add or update a Shelly Gen3 garage door switch.

    The Shelly should already be wired to the garage door button terminals.
    Runs Shelly discovery automatically to help you find the IP.
    """
    click.echo("\nGarage Door Setup\n")
    name = click.prompt("Name (e.g. 'garage', 'left-bay')").strip()

    click.echo(f"Scanning network for Shelly devices...")
    found = _garage.discover()
    if found:
        click.echo(f"Found {len(found)} Shelly device(s):\n")
        for i, d in enumerate(found):
            label = d.get("name") or d.get("id") or "unknown"
            model = d.get("app") or d.get("type") or "?"
            click.echo(f"  [{i+1}] {d['ip']:16s}  {label}  ({model})")
        click.echo(f"  [0] Enter IP manually\n")
        choice = click.prompt("Select device number (or 0 to type manually)", default=1, type=int)
        if 1 <= choice <= len(found):
            ip = found[choice - 1]["ip"]
            click.echo(f"Using {ip}")
        else:
            ip = click.prompt("Shelly IP address").strip()
    else:
        click.echo("No Shelly devices found on the network.")
        ip = click.prompt("Shelly IP address").strip()

    pulse = click.prompt("Pulse duration in seconds", default=0.5, type=float)

    click.echo(f"Testing connection to http://{ip}/...")
    try:
        status = _garage.get_status(ip)
        state = "ON" if status.get("output") else "OFF"
        click.echo(f"✓ Connected. Switch is currently {state}.")
    except Exception as e:
        click.echo(f"⚠️  Could not reach Shelly: {e}")
        click.echo("   Saved anyway — check the IP and try again.")

    garages = _garage.load_config()
    existing = next((g for g in garages if g["name"] == name), None)
    if existing:
        existing["ip"] = ip
        existing["pulse_seconds"] = pulse
        click.echo(f"Updated '{name}'.")
    else:
        garages.append({"name": name, "ip": ip, "pulse_seconds": pulse})
        click.echo(f"Added '{name}'.")

    _garage.save_config(garages)
    click.echo(f"\nDone. Control it at: http://<your-server>:5000/garage")


@main.command("remove-device")
@click.argument("name")
@click.option("--purge", is_flag=True, help="Also delete DB readings (BLE sensors only).")
@click.option("--db", default=DEFAULT_DB, show_default=True, help="SQLite database path.")
def remove_device(name, purge, db):
    """Remove a registered device by name. Works for all device types."""
    from smart_home import ecobee as _ecobee
    from smart_home import homeassistant as _ha

    name_lower = name.lower()
    removed = []

    if _remove_ble_sensor(name, purge, db):
        removed.append("BLE sensor")

    ecobee_cfg = _ecobee.load_config()
    if ecobee_cfg and ecobee_cfg.get("label", "").lower() == name_lower:
        _ecobee.CONFIG_PATH.unlink(missing_ok=True)
        click.echo(f"Removed Ecobee thermostat '{ecobee_cfg['label']}'.")
        removed.append("thermostat")

    ha_cfg = _ha.load_config()
    if ha_cfg and ha_cfg.get("label", "").lower() == name_lower:
        _ha.CONFIG_PATH.unlink(missing_ok=True)
        click.echo(f"Removed Home Assistant thermostat '{ha_cfg['label']}'.")
        removed.append("thermostat")

    plugs = _smart_plug.load_config()
    new_plugs = [p for p in plugs if p.get("name", "").lower() != name_lower]
    if len(new_plugs) < len(plugs):
        _smart_plug.save_config(new_plugs)
        click.echo(f"Removed smart plug '{name}'.")
        removed.append("smart plug")

    cameras = _camera.load_config()
    new_cameras = [c for c in cameras if c.get("name", "").lower() != name_lower]
    if len(new_cameras) < len(cameras):
        _camera.save_config(new_cameras)
        click.echo(f"Removed camera '{name}'.")
        removed.append("camera")

    garages = _garage.load_config()
    new_garages = [g for g in garages if g.get("name", "").lower() != name_lower]
    if len(new_garages) < len(garages):
        _garage.save_config(new_garages)
        click.echo(f"Removed garage '{name}'.")
        removed.append("garage")

    presence = _presence.load_devices()
    match = next(
        (ble for ble, lbl in presence.items()
         if ble.lower() == name_lower or lbl.lower() == name_lower),
        None,
    )
    if match is not None:
        lbl = presence.pop(match)
        _presence.save_devices(presence)
        click.echo(f"Removed presence device '{lbl}' ({match}).")
        removed.append("presence device")

    if not removed:
        click.echo(f"No device found matching {name!r}.")
        click.echo("Run 'smart-home list-devices' to see all registered devices.")


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
    sensor_offline_alerted: set[str] = set()  # addresses for which offline alert was sent this episode
    battery_low_alerted: set[str] = set()     # labels for which battery-low alert was sent
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
    PRESENCE_TIMEOUT = datetime.timedelta(seconds=30)

    # Tracks which presence devices have already had their auto-open fired this
    # "home" episode. Reset to empty when the device goes "away" again.
    _auto_open_done: set[str] = set()

    # Tracks which garage door names were auto-closed on departure so that only
    # those doors are re-opened on arrival (not doors the user left open manually).
    _auto_closed_doors: set[str] = set()

    async def _auto_open_on_arrival(ble_name: str, label: str) -> None:
        """Triggered immediately when a presence device is first seen after being away."""
        loop = asyncio.get_running_loop()
        log_ts = datetime.datetime.now().strftime("%H:%M:%S")
        click.echo(f"[{log_ts}] Presence: {label} first seen — checking auto-open garages")
        for g in _garage.load_config():
            if not g.get("auto"):
                continue
            name, ip, pulse = g["name"], g["ip"], g.get("pulse_seconds", 0.5)
            if name not in _auto_closed_doors:
                click.echo(f"[{log_ts}] Skipping '{name}' — not auto-closed on departure")
                continue
            try:
                status = await loop.run_in_executor(None, _garage.get_status, ip)
                if status.get("door_closed") is True:
                    await loop.run_in_executor(None, _garage.trigger, ip, pulse)
                    _auto_closed_doors.discard(name)
                    log_ts = datetime.datetime.now().strftime("%H:%M:%S")
                    click.echo(f"[{log_ts}] Auto-opened garage '{name}' ({label} arrived)")
                    _push.send_notification(
                        title="Garage opening",
                        body=f"Auto-opening '{name}' — {label} arrived",
                    )
            except Exception as e:
                click.echo(f"[{log_ts}] Auto-open failed for '{name}': {e}")

    def _fire_auto_open(ble_name: str) -> None:
        """Fire the auto-open task once per arrival episode."""
        if presence_state.get(ble_name, {}).get("status") != "away":
            return
        if ble_name in _auto_open_done:
            return
        _auto_open_done.add(ble_name)
        label = presence_devices.get(ble_name, ble_name)
        try:
            asyncio.get_running_loop().create_task(_auto_open_on_arrival(ble_name, label))
        except RuntimeError:
            _auto_open_done.discard(ble_name)

    def on_device(device, adv):
        ble_name = device.name or adv.local_name or ""
        now = datetime.datetime.now()

        # Match by name first; if matched, remember this MAC address
        if ble_name and ble_name in presence_devices:
            presence_addr_map[device.address] = ble_name
            presence_last_seen[ble_name] = now
            _fire_auto_open(ble_name)
            if verbose:
                click.echo(f"[presence] {ble_name!r} seen (by name, addr={device.address})")
            return

        # Match by previously-seen MAC address (handles nameless advertisements)
        matched_name = presence_addr_map.get(device.address)
        if matched_name:
            presence_last_seen[matched_name] = now
            _fire_auto_open(matched_name)
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
                        _auto_open_done.discard(ble_name)
                        _auto_closed_doors.clear()
                        _push.send_notification(
                            title="Left home",
                            body=f"{label} left home",
                        )
                        for g in _garage.load_config():
                            if g.get("auto"):
                                try:
                                    door_status = _garage.get_status(g["ip"])
                                    if door_status.get("door_closed") is False:
                                        _garage.trigger(g["ip"], g.get("pulse_seconds", 0.5))
                                        _auto_closed_doors.add(g["name"])
                                        click.echo(f"[{ts}] Auto-closed garage '{g['name']}' ({label} left)")
                                        _push.send_notification(
                                            title="Garage closing",
                                            body=f"{g['name']} garage door closing",
                                        )
                                except Exception as e:
                                    click.echo(f"[{ts}] Auto-close failed for '{g['name']}': {e}")
                    _presence.append_history({
                        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
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

    # Load hourly records from DB:
    # {label_key: {hour_of_day: {temp_max, temp_min, humi_max, humi_min}}}
    # Special keys: "__inside_avg__" and "__in_out_diff__"
    # Only sensors with >= 1 year of data are eligible for record notifications.
    ONE_YEAR = datetime.timedelta(days=365)
    hourly_records: dict[str, dict[int, dict]] = {}
    # labels_with_enough_data: set of label keys eligible for record checks
    labels_with_enough_data: set[str] = set()
    if conn:
        # Determine which per-sensor labels have >= 1 year of data
        age_rows = conn.execute("""
            SELECT label,
                   julianday('now') - julianday(MIN(ts)) AS age_days
            FROM readings
            WHERE label IS NOT NULL AND temp_f IS NOT NULL
            GROUP BY label
        """).fetchall()
        for lbl, age_days in age_rows:
            if age_days >= 365:
                labels_with_enough_data.add(lbl)

        # Check if inside sensors collectively have >= 1 year of data
        inside_age = conn.execute("""
            SELECT julianday('now') - julianday(MIN(ts))
            FROM readings
            WHERE label LIKE '%inside%' AND temp_f IS NOT NULL
        """).fetchone()[0] or 0
        if inside_age >= 365:
            labels_with_enough_data.add("__inside_avg__")

        # Check if both inside AND outside sensors have >= 1 year of data
        outside_age = conn.execute("""
            SELECT julianday('now') - julianday(MIN(ts))
            FROM readings
            WHERE label LIKE '%outside%' AND temp_f IS NOT NULL
        """).fetchone()[0] or 0
        if inside_age >= 365 and outside_age >= 365:
            labels_with_enough_data.add("__in_out_diff__")

        if labels_with_enough_data:
            click.echo(f"Hourly records enabled for: {', '.join(sorted(labels_with_enough_data))}")
        else:
            click.echo("Hourly records disabled — no sensor has 1 year of data yet.")

        rows = conn.execute("""
            SELECT label,
                   CAST(strftime('%H', ts) AS INTEGER) as hour,
                   MAX(temp_f), MIN(temp_f),
                   MAX(humidity), MIN(humidity)
            FROM readings
            WHERE label IS NOT NULL AND temp_f IS NOT NULL
            GROUP BY label, hour
        """).fetchall()
        for lbl, hour, mx_t, mn_t, mx_h, mn_h in rows:
            hourly_records.setdefault(lbl, {})[hour] = {
                "temp_max": mx_t, "temp_min": mn_t,
                "humi_max": mx_h, "humi_min": mn_h,
            }

        inside_avg_rows = conn.execute("""
            SELECT CAST(strftime('%H', ts) AS INTEGER) as hour,
                   MAX(avg_t), MIN(avg_t), MAX(avg_h), MIN(avg_h)
            FROM (
                SELECT ts, AVG(temp_f) as avg_t, AVG(humidity) as avg_h
                FROM readings
                WHERE label LIKE '%inside%' AND temp_f IS NOT NULL
                GROUP BY ts
            )
            GROUP BY hour
        """).fetchall()
        for hour, mx_t, mn_t, mx_h, mn_h in inside_avg_rows:
            hourly_records.setdefault("__inside_avg__", {})[hour] = {
                "temp_max": mx_t, "temp_min": mn_t,
                "humi_max": mx_h, "humi_min": mn_h,
            }

        diff_rows = conn.execute("""
            SELECT CAST(strftime('%H', i.ts) AS INTEGER) as hour,
                   MAX(i.avg_t - o.avg_t), MIN(i.avg_t - o.avg_t)
            FROM (
                SELECT ts, AVG(temp_f) as avg_t
                FROM readings
                WHERE label LIKE '%inside%' AND temp_f IS NOT NULL
                GROUP BY ts
            ) i
            JOIN (
                SELECT ts, AVG(temp_f) as avg_t
                FROM readings
                WHERE label LIKE '%outside%' AND temp_f IS NOT NULL
                GROUP BY ts
            ) o ON i.ts = o.ts
            GROUP BY hour
        """).fetchall()
        for hour, mx_d, mn_d in diff_rows:
            hourly_records.setdefault("__in_out_diff__", {})[hour] = {
                "temp_max": mx_d, "temp_min": mn_d,
                "humi_max": None, "humi_min": None,
            }

    # Tracks which hour-of-day the record check last ran; -1 forces a run at startup
    _last_record_check_hour = -1

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
    # high-res buffer: label -> [(epoch_time, temp_f), ...] kept for ~60 seconds
    high_res_buffer: dict[str, list] = {}

    def on_reading(reading):
        db_label = label_map.get(reading.address)
        reading.label = db_label or reading.name or reading.address  # always show something in terminal
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        click.echo(f"[{ts}] {reading}")
        seen.add(reading.address)
        now = datetime.datetime.now()
        last_seen[reading.address] = now
        if db_label:
            latest_reading[reading.address] = reading
            if reading.temp_f is not None:
                high_res_buffer.setdefault(db_label, []).append(
                    (now.timestamp(), reading.temp_f)
                )

    async def _backfill_pvvx(
        db_conn,
        address: str,
        label: str,
        offline_start: datetime.datetime,
        online_at: datetime.datetime,
    ) -> None:
        """Fetch PVVX sensor history and backfill the DB for the offline period."""
        log_ts = datetime.datetime.now().strftime("%H:%M:%S")
        click.echo(f"[{log_ts}] Backfilling PVVX history for {label} ({address})...")
        scanner = scanner_ref[0] if scanner_ref else None
        if scanner:
            await scanner.stop()
        try:
            records = await _pvvx.read_pvvx_history(address)
        finally:
            if scanner:
                await scanner.start()
        if not records:
            click.echo(f"[{log_ts}] No PVVX history returned for {label}")
            return

        # Filter to records within the offline window
        start_ts = offline_start.strftime("%Y-%m-%d %H:%M:%S")
        end_ts   = online_at.strftime("%Y-%m-%d %H:%M:%S")
        in_window = [r for r in records if start_ts <= r["ts"] <= end_ts]
        if not in_window:
            click.echo(f"[{log_ts}] PVVX history: no records in offline window for {label}")
            return

        # Delete the null placeholder rows that were inserted during the outage
        db_conn.execute(
            "DELETE FROM readings WHERE label=? AND ts>=? AND ts<=? AND temp_f IS NULL",
            (label, start_ts, end_ts),
        )

        # Insert history records (convert temp_c to temp_f)
        inserted = 0
        for r in in_window:
            temp_f = round(r["temp_c"] * 9 / 5 + 32, 2)
            cur = db_conn.execute(
                "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity) VALUES (?,?,?,?,?)",
                (r["ts"], address, label, temp_f, r["humidity"]),
            )
            inserted += cur.rowcount
        db_conn.commit()
        log_ts = datetime.datetime.now().strftime("%H:%M:%S")
        click.echo(f"[{log_ts}] PVVX backfill for {label}: {inserted} rows inserted ({len(in_window)} in window, {len(records)} total)")

    async def process_stats_loop():
        """Once per minute, record this process's CPU and memory usage."""
        import psutil
        proc = psutil.Process()
        proc.cpu_percent()  # first call establishes baseline; returns 0
        while True:
            await asyncio.sleep(60)
            if not conn:
                continue
            ts = datetime.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            cpu = proc.cpu_percent()  # % since last call, non-blocking
            mem = proc.memory_info().rss / 1024 / 1024
            conn.execute(
                "INSERT OR REPLACE INTO process_stats (ts, cpu_percent, mem_mb) VALUES (?,?,?)",
                (ts, round(cpu, 1), round(mem, 1)),
            )
            conn.commit()

    async def snapshot_loop():
        """Once per minute, write the latest reading for every sensor to the DB
        using a single shared timestamp so all readings align in the database."""
        nonlocal _last_record_check_hour
        while True:
            await asyncio.sleep(60)
            if not conn or not latest_reading:
                continue
            ts = datetime.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            for addr, reading in list(latest_reading.items()):
                conn.execute(
                    "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity, rssi, battery, raw_reading) VALUES (?,?,?,?,?,?,?,?)",
                    (ts, reading.address, reading.label, reading.temp_f, reading.humidity, reading.rssi, reading.battery, reading.raw_reading),
                )
            # Null readings for labeled sensors that didn't report this cycle
            for addr, label in label_map.items():
                if label and addr not in latest_reading:
                    conn.execute(
                        "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity) VALUES (?,?,?,NULL,NULL)",
                        (ts, addr, label),
                    )
            # Evaluate sensor events for every labeled sensor
            now_dt = datetime.datetime.now()
            log_ts = now_dt.strftime("%H:%M:%S")
            for addr, label in label_map.items():
                if not label:
                    continue
                last = last_seen.get(addr)
                recently_seen = last is not None and (now_dt - last).total_seconds() < 70

                if recently_seen:
                    # Sensor came back online after being offline
                    if addr in sensor_offline_alerted:
                        sensor_offline_alerted.discard(addr)
                        offline_row = conn.execute(
                            "SELECT ts FROM temperature_events WHERE event_type='sensor_offline' AND details=? ORDER BY ts DESC LIMIT 1",
                            (label,),
                        ).fetchone()
                        if offline_row:
                            offline_dt = datetime.datetime.strptime(offline_row[0], "%Y-%m-%d %H:%M:%S")
                            secs = int((now_dt - offline_dt).total_seconds())
                            hrs, rem = divmod(secs, 3600)
                            m = rem // 60
                            duration = f"{hrs}h {m}m" if hrs else f"{m}m"
                            online_details = f"{label} — offline for {duration}"
                        else:
                            online_details = label
                        conn.execute(
                            "INSERT OR IGNORE INTO temperature_events (ts, event_type, value, details) VALUES (?,?,?,?)",
                            (ts, "sensor_online", None, online_details),
                        )
                        _push.send_notification(title="Sensor Online", body=f"{label} is back online")
                        click.echo(f"[{log_ts}] Sensor back online: {label}")
                        # Backfill PVVX history for the offline period
                        if addr in pvvx_addresses and offline_row:
                            asyncio.get_running_loop().create_task(
                                _backfill_pvvx(conn, addr, label, offline_dt, now_dt)
                            )
                    # Battery check
                    reading = latest_reading.get(addr)
                    if reading and reading.battery is not None:
                        if reading.battery < 20 and label not in battery_low_alerted:
                            battery_low_alerted.add(label)
                            conn.execute(
                                "INSERT OR IGNORE INTO temperature_events (ts, event_type, value, details) VALUES (?,?,?,?)",
                                (ts, "battery_low", reading.battery, f"{label} battery at {reading.battery}%"),
                            )
                            _push.send_notification(title="Battery Low", body=f"{label} battery is at {reading.battery}%")
                            click.echo(f"[{log_ts}] Battery low: {label} at {reading.battery}%")
                        elif reading.battery >= 30:
                            battery_low_alerted.discard(label)
                else:
                    # Sensor offline — fire once per episode, but only after 5 minutes missing
                    if last is not None and addr not in sensor_offline_alerted:
                        if (now_dt - last).total_seconds() >= 300:
                            sensor_offline_alerted.add(addr)
                            conn.execute(
                                "INSERT OR IGNORE INTO temperature_events (ts, event_type, value, details) VALUES (?,?,?,?)",
                                (ts, "sensor_offline", None, label),
                            )
                            _push.send_notification(title="Sensor Offline", body=f"{label} has stopped responding")
                            click.echo(f"[{log_ts}] Sensor offline: {label}")

            conn.commit()
            n = len(latest_reading)
            click.echo(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Snapshot written: {n} sensor(s) at {ts}")

            # Check for hourly temperature/humidity records — once per hour only
            now_hour = datetime.datetime.now().hour
            if now_hour == _last_record_check_hour:
                continue
            _last_record_check_hour = now_hour
            h_str = f"{now_hour % 12 or 12}{'AM' if now_hour < 12 else 'PM'}"

            def _check_record(label_key: str, temp: float | None, humi: float | None) -> None:
                if label_key not in labels_with_enough_data:
                    return
                display = {
                    "__inside_avg__": "Inside Average",
                    "__in_out_diff__": "In/Out Diff",
                }.get(label_key, label_key)
                rec = hourly_records.setdefault(label_key, {}).setdefault(now_hour, {
                    "temp_max": None, "temp_min": None,
                    "humi_max": None, "humi_min": None,
                })
                if temp is not None:
                    if rec["temp_max"] is None or temp > rec["temp_max"]:
                        old = rec["temp_max"]
                        rec["temp_max"] = temp
                        if old is not None:
                            _push.send_notification(
                                title=f"Hourly record: {display}",
                                body=f"New {h_str} high temp: {temp:.1f}°F (was {old:.1f}°F)",
                            )
                            click.echo(f"[{log_ts}] Record {h_str} high temp for {label_key}: {temp:.1f}°F")
                    if rec["temp_min"] is None or temp < rec["temp_min"]:
                        old = rec["temp_min"]
                        rec["temp_min"] = temp
                        if old is not None:
                            _push.send_notification(
                                title=f"Hourly record: {display}",
                                body=f"New {h_str} low temp: {temp:.1f}°F (was {old:.1f}°F)",
                            )
                            click.echo(f"[{log_ts}] Record {h_str} low temp for {label_key}: {temp:.1f}°F")
                if humi is not None:
                    if rec["humi_max"] is None or humi > rec["humi_max"]:
                        old = rec["humi_max"]
                        rec["humi_max"] = humi
                        if old is not None:
                            _push.send_notification(
                                title=f"Hourly record: {display}",
                                body=f"New {h_str} high humidity: {humi:.1f}% (was {old:.1f}%)",
                            )
                            click.echo(f"[{log_ts}] Record {h_str} high humidity for {label_key}: {humi:.1f}%")
                    if rec["humi_min"] is None or humi < rec["humi_min"]:
                        old = rec["humi_min"]
                        rec["humi_min"] = humi
                        if old is not None:
                            _push.send_notification(
                                title=f"Hourly record: {display}",
                                body=f"New {h_str} low humidity: {humi:.1f}% (was {old:.1f}%)",
                            )
                            click.echo(f"[{log_ts}] Record {h_str} low humidity for {label_key}: {humi:.1f}%")

            for addr, reading in list(latest_reading.items()):
                lbl = label_map.get(addr)
                if lbl:
                    _check_record(lbl, reading.temp_f, reading.humidity)

            inside_temps = [r.temp_f for r in latest_reading.values()
                            if r.label and "inside" in r.label and r.temp_f is not None]
            inside_humis = [r.humidity for r in latest_reading.values()
                            if r.label and "inside" in r.label and r.humidity is not None]
            if inside_temps:
                _check_record(
                    "__inside_avg__",
                    sum(inside_temps) / len(inside_temps),
                    sum(inside_humis) / len(inside_humis) if inside_humis else None,
                )

            outside_temps = [r.temp_f for r in latest_reading.values()
                             if r.label and "outside" in r.label and r.temp_f is not None]
            if inside_temps and outside_temps:
                diff = sum(inside_temps) / len(inside_temps) - sum(outside_temps) / len(outside_temps)
                _check_record("__in_out_diff__", diff, None)

    from smart_home import ecobee as _ecobee
    from smart_home import homeassistant as _ha
    ecobee_cfg = _ecobee.load_config()
    ha_cfg = _ha.load_config()

    async def poll_homeassistant_loop():
        label = ha_cfg.get("label", "home-assistant")
        click.echo(f"Home Assistant thermostat configured: {label}")
        POLL_INTERVAL = 180
        while True:
            try:
                reading = _ha.fetch_reading(ha_cfg)
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                click.echo(f"[{ts}] {reading}")
                latest_reading[reading.address] = reading
            except Exception as e:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                click.echo(f"[{ts}] Home Assistant poll failed: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def poll_ecobee_loop():
        nonlocal ecobee_cfg
        label = ecobee_cfg.get("label", "ecobee")
        click.echo(f"Ecobee thermostat configured: {label}")
        POLL_INTERVAL = 180  # 3 minutes — matches Ecobee's runtime update frequency
        while True:
            try:
                reading, ecobee_cfg = _ecobee.fetch_reading(ecobee_cfg)
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                click.echo(f"[{ts}] {reading}")
                latest_reading[reading.address] = reading
            except Exception as e:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                click.echo(f"[{ts}] Ecobee poll failed: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    from smart_home.events import detect_and_insert_events

    async def check_events_loop():
        """After each snapshot cycle, scan for parity crossing events."""
        await asyncio.sleep(65)  # let the first snapshot settle
        while True:
            if conn:
                try:
                    # Prune buffer entries older than 60 seconds
                    cutoff = datetime.datetime.now().timestamp() - 60.0
                    for lbl in list(high_res_buffer):
                        high_res_buffer[lbl] = [
                            (t, v) for t, v in high_res_buffer[lbl] if t >= cutoff
                        ]
                    n = detect_and_insert_events(conn, high_res_buffer)
                    if n:
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        click.echo(f"[{ts}] Temperature event(s) detected: {n}")
                except Exception as e:
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    click.echo(f"[{ts}] Event check failed: {e}")
            await asyncio.sleep(60)

    garages_cfg = _garage.load_config()
    _door_states: dict[str, bool | None] = {}  # name -> door_closed (True=closed, False=open)

    async def garage_door_loop():
        """Poll Shelly garage doors every 15s; log open/closed transitions to garage_events."""
        if not garages_cfg or not conn:
            return
        while True:
            now = datetime.datetime.now()
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            log_ts = now.strftime("%H:%M:%S")
            for g in garages_cfg:
                name = g["name"]
                try:
                    status = _garage.get_status(g["ip"])
                    door_closed = status.get("door_closed")
                    if door_closed is None:
                        continue
                    prev = _door_states.get(name)
                    if prev != door_closed:
                        _door_states[name] = door_closed
                        state_str = "closed" if door_closed else "open"
                        conn.execute(
                            "INSERT INTO garage_events (ts, name, state) VALUES (?,?,?)",
                            (ts, name, state_str),
                        )
                        conn.commit()
                        click.echo(f"[{log_ts}] Garage '{name}': {state_str}")
                except Exception as e:
                    click.echo(f"[{log_ts}] Garage poll failed for '{name}': {e}")
            await asyncio.sleep(15)

    cameras_cfg = _camera.load_config()
    camera_watchers: list[_camera.CameraWatcher] = []
    for cam in cameras_cfg:
        w = _camera.CameraWatcher(cam)
        w.start()
        camera_watchers.append(w)
        click.echo(f"Camera watcher started: {cam['name']} ({len(cam.get('zones', []))} zone(s))")
        if cam.get("flipped"):
            import httpx as _httpx_cam
            base = cam["url"].rstrip("/")
            try:
                _httpx_cam.get(f"{base}/control?var=vflip&val=1", timeout=3)
                _httpx_cam.get(f"{base}/control?var=hmirror&val=1", timeout=3)
                click.echo(f"  Applied flip to {cam['name']}")
            except Exception as e:
                click.echo(f"  Could not apply flip to {cam['name']}: {e}")

    _camera_notify_times: dict[tuple, datetime.datetime] = {}
    CAMERA_COOLDOWN = datetime.timedelta(minutes=5)

    async def camera_vitals_loop():
        """Poll each camera's /vitals endpoint every 60s and store in camera_vitals."""
        import httpx as _httpx
        await asyncio.sleep(5)  # let things settle on startup
        while True:
            for cam in cameras_cfg:
                url = cam.get("url", "").rstrip("/") + "/vitals"
                try:
                    def _fetch(u=url):
                        r = _httpx.get(u, timeout=5.0)
                        r.raise_for_status()
                        return r.json()
                    data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
                    temp_c      = data.get("temperature_c")
                    wifi_rssi   = data.get("wifi_rssi_dbm")
                    free_heap   = data.get("free_heap_kb")
                    uptime_s    = data.get("uptime_s")
                    psram_total = data.get("psram_total_kb")
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_ts = datetime.datetime.now().strftime("%H:%M:%S")
                    click.echo(f"[{log_ts}] Camera vitals {cam['name']}: {temp_c}°C  RSSI={wifi_rssi}dBm  heap={free_heap}KB  uptime={uptime_s}s  psram={psram_total}KB")
                    if conn:
                        conn.execute(
                            "INSERT INTO camera_vitals (ts, camera, temp_c, wifi_rssi, free_heap_kb, uptime_s, psram_total_kb) VALUES (?,?,?,?,?,?,?)",
                            (ts, cam["name"], temp_c, wifi_rssi, free_heap, uptime_s, psram_total),
                        )
                        conn.commit()
                except Exception as e:
                    log_ts = datetime.datetime.now().strftime("%H:%M:%S")
                    click.echo(f"[{log_ts}] Camera vitals {cam['name']} error: {e}")
            await asyncio.sleep(60)

    async def camera_watch_loop():
        while True:
            await asyncio.sleep(1)
            for w in camera_watchers:
                while not w.events.empty():
                    try:
                        event = w.events.get_nowait()
                    except Exception:
                        break
                    if event[0] == "error":
                        click.echo(f"[camera:{w.name}] {event[1]}")
                    elif event[0] == "motion":
                        zone_name, pct = event[1], event[2]
                        screenshot = event[3] if len(event) > 3 else None
                        key = (w.name, zone_name)
                        now = datetime.datetime.now()
                        ts = now.strftime("%Y-%m-%d %H:%M:%S")
                        if conn:
                            conn.execute(
                                "INSERT INTO camera_events (ts, camera, zone, pct, screenshot) VALUES (?,?,?,?,?)",
                                (ts, w.name, zone_name, pct, screenshot),
                            )
                            conn.commit()
                        if now - _camera_notify_times.get(key, datetime.datetime.min) >= CAMERA_COOLDOWN:
                            _camera_notify_times[key] = now
                            _push.send_notification(
                                title=f"Motion: {w.name}",
                                body=f"Movement detected in '{zone_name}' ({pct}% changed)",
                            )
                            click.echo(f"[{now.strftime('%H:%M:%S')}] Motion in {w.name}/{zone_name} ({pct}%)")

    plugs_cfg = _smart_plug.load_config()

    async def poll_smart_plugs_loop():
        POLL_INTERVAL = 30
        for p in plugs_cfg:
            click.echo(f"Smart plug configured: {p['name']} ({p['device']}) at {p['ip']}")
        while True:
            for p in plugs_cfg:
                try:
                    loop = asyncio.get_running_loop()
                    reading = await loop.run_in_executor(None, _smart_plug.fetch_reading, p["ip"])
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    click.echo(
                        f"[{ts}] Plug '{p['name']}' ({p['device']}): "
                        f"{reading['watts']}W  {reading['volts']}V  {reading['amps']}A  "
                        f"pf={reading['power_factor']}  total={reading['energy_wh']}Wh"
                    )
                    if conn:
                        insert_plug_reading(
                            conn, p["device"], p.get("ip"),
                            reading["watts"], reading["volts"], reading["amps"],
                            reading["energy_wh"], reading["power_factor"], reading["is_on"],
                            reading["today_kwh"], reading["yesterday_kwh"],
                        )
                except Exception as e:
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    click.echo(f"[{ts}] Plug poll failed for '{p['name']}': {e}")
            await asyncio.sleep(POLL_INTERVAL)

    click.echo("Monitoring BLE devices... (Ctrl+C to stop)")
    try:
        extra = [snapshot_loop(), check_events_loop(), process_stats_loop(), garage_door_loop()]
        if cameras_cfg:
            extra.append(camera_watch_loop())
            extra.append(camera_vitals_loop())
        if presence_devices:
            extra.append(check_presence())
        if ecobee_cfg:
            extra.append(poll_ecobee_loop())
        if ha_cfg:
            extra.append(poll_homeassistant_loop())
        if plugs_cfg:
            extra.append(poll_smart_plugs_loop())
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
@click.argument("sensor", required=False)
@click.option("--mac-address", "-m", default=None, help="Connect by MAC address directly.")
def gatt_dump(sensor, mac_address):
    """Dump all GATT services for a sensor.

    SENSOR can be a label name (e.g. 'outside-sun') or a MAC address.
    Use --mac-address to force MAC address lookup.\n
    Examples:\n
      smart-home gatt-dump outside-sun\n
      smart-home gatt-dump --mac-address A4:C1:38:F6:DD:80
    """
    if mac_address:
        address = mac_address
    elif sensor:
        label_map = _labels.load()
        # Check if it looks like a MAC address
        if len(sensor) == 17 and sensor.count(":") == 5:
            address = sensor
        else:
            # Look up by label
            match = next(
                (addr for addr, lbl in label_map.items() if lbl.lower() == sensor.lower()),
                None,
            )
            if match is None:
                click.echo(f"No sensor found with label {sensor!r}.")
                click.echo("Known labels: " + ", ".join(sorted(label_map.values())))
                return
            address = match
            click.echo(f"Resolved {sensor!r} → {address}")
    else:
        click.echo("Provide a sensor label or --mac-address. Known labels:")
        label_map = _labels.load()
        for addr, lbl in sorted(label_map.items(), key=lambda x: x[1]):
            click.echo(f"  {lbl}  ({addr})")
        return
    asyncio.run(dump_gatt(address))


@main.command("pvvx-history")
@click.argument("sensor")
@click.option("--count", "-n", type=int, default=20, help="Number of most-recent records to show (default: 20). 0 = all.")
@click.option("--verbose", "-v", is_flag=True, help="Show raw BLE communication details.")
def pvvx_history(sensor, count, verbose):
    """Read and display PVVX internal history for a sensor.

    SENSOR can be a label name (e.g. 'outside-sun') or a MAC address.
    Shows timestamps and the interval between records so you can verify
    the sensor's snapshot frequency.
    """
    label_map = _labels.load()
    if len(sensor) == 17 and sensor.count(":") == 5:
        address = sensor.upper()
        label = label_map.get(address, address)
    else:
        match = next(
            (addr for addr, lbl in label_map.items() if lbl.lower() == sensor.lower()),
            None,
        )
        if match is None:
            click.echo(f"No sensor found with label {sensor!r}.")
            click.echo("Known labels: " + ", ".join(sorted(label_map.values())))
            return
        address = match
        label = sensor
        click.echo(f"Resolved {sensor!r} → {address}")

    import subprocess as _sp

    # Stop the monitor service if it's running (it holds the BLE adapter)
    svc_was_running = _sp.run(
        ["sudo", "-n", "systemctl", "is-active", "--quiet", "smart-home.service"]
    ).returncode == 0
    if svc_was_running:
        click.echo("Stopping smart-home.service...")
        _sp.run(["sudo", "-n", "systemctl", "stop", "smart-home.service"], check=True)

    try:
        click.echo(f"Reading PVVX history for {label} ({address})...")
        records = asyncio.run(_pvvx.read_pvvx_history(address, verbose=verbose))
    finally:
        if svc_was_running:
            click.echo("Restarting smart-home.service...")
            _sp.run(["sudo", "-n", "systemctl", "start", "smart-home.service"])

    if not records:
        click.echo("No history records returned (sensor not found or not PVVX firmware).")
        return

    # Sort by timestamp
    records.sort(key=lambda r: r["ts"])

    click.echo(f"Total records in sensor: {len(records)}")
    click.echo(f"Oldest: {records[0]['ts']}   Newest: {records[-1]['ts']}")
    click.echo()

    # Show the requested tail (most recent)
    subset = records if count == 0 else records[-count:]
    import datetime as _dt
    prev_ts = None
    click.echo(f"{'Timestamp':<22}  {'Temp':>7}  {'Hum':>6}  {'Vbat':>7}  {'Interval':>10}")
    click.echo("-" * 63)
    for r in subset:
        ts_dt = _dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
        if prev_ts is not None:
            gap = int((ts_dt - prev_ts).total_seconds())
            interval = f"{gap}s"
        else:
            interval = "—"
        temp_f = r["temp_c"] * 9 / 5 + 32
        click.echo(f"{r['ts']:<22}  {temp_f:>6.1f}F  {r['humidity']:>5.1f}%  {r['vbat_mv']:>5}mV  {interval:>10}")
        prev_ts = ts_dt


@main.command("backfill")
@click.option("--start", default=None, help="Gap start timestamp (e.g. '2026-04-17 17:45:00'). Auto-detected if omitted.")
@click.option("--end",   default=None, help="Gap end timestamp. Auto-detected if omitted.")
@click.option("--db", default=DEFAULT_DB, show_default=True, help="SQLite database path.")
def backfill(start, end, db):
    """Pull stored history from PVVX sensors and fill a gap in the database.

    Automatically detects the largest gap in readings for each PVVX sensor
    and fetches its on-device history to fill it in. Govee sensors (H5074)
    do not store history on-device; use 'smart-home import' with a Govee
    app export for those.

    The monitor service is stopped during the BLE connection and restarted
    afterwards.
    """
    import subprocess as _sp

    conn = open_db(db)
    label_map  = _labels.load()
    pvvx_addrs = _pvvx.load_addresses()

    # Build list of PVVX sensors that have a label
    pvvx_sensors = [
        (addr, lbl)
        for addr, lbl in label_map.items()
        if addr.upper() in pvvx_addrs
    ]

    if not pvvx_sensors:
        click.echo("No PVVX sensors registered. Run 'smart-home mark-pvvx <address>' first.")
        conn.close()
        return

    # Auto-detect gap per sensor (largest contiguous run of missing minutes)
    def detect_gap(label):
        rows = conn.execute(
            "SELECT ts FROM readings WHERE label=? ORDER BY ts DESC LIMIT 2000",
            (label,),
        ).fetchall()
        if len(rows) < 2:
            return None, None
        # Walk from newest backwards looking for a jump > 5 minutes
        for i in range(len(rows) - 1):
            newer = datetime.datetime.strptime(rows[i][0],   "%Y-%m-%d %H:%M:%S")
            older = datetime.datetime.strptime(rows[i+1][0], "%Y-%m-%d %H:%M:%S")
            gap_minutes = (newer - older).total_seconds() / 60
            if gap_minutes > 5:
                return older, newer
        return None, None

    svc_was_running = _sp.run(
        ["sudo", "-n", "systemctl", "is-active", "--quiet", "smart-home.service"]
    ).returncode == 0
    if svc_was_running:
        click.echo("Stopping smart-home.service for BLE access...")
        _sp.run(["sudo", "-n", "systemctl", "stop", "smart-home.service"], check=True)

    total_inserted = 0
    try:
        for addr, label in pvvx_sensors:
            # Determine gap window
            if start and end:
                gap_start = datetime.datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
                gap_end   = datetime.datetime.strptime(end,   "%Y-%m-%d %H:%M:%S")
            else:
                gap_start, gap_end = detect_gap(label)
                if gap_start is None:
                    click.echo(f"{label}: no gap detected, skipping.")
                    continue
                click.echo(f"{label}: detected gap {gap_start} → {gap_end} ({int((gap_end-gap_start).total_seconds()/60)} min)")

            click.echo(f"{label} ({addr}): fetching on-device history...")
            records = asyncio.run(_pvvx.read_pvvx_history(addr, count=255, verbose=False))

            if not records:
                click.echo(f"  No records returned — sensor not in range or not PVVX firmware.")
                continue

            records.sort(key=lambda r: r["ts"])
            click.echo(f"  Sensor has {len(records)} records ({records[0]['ts']} → {records[-1]['ts']})")

            start_str = gap_start.strftime("%Y-%m-%d %H:%M:%S")
            end_str   = gap_end.strftime("%Y-%m-%d %H:%M:%S")
            in_window = [r for r in records if start_str <= r["ts"] <= end_str]

            if not in_window:
                click.echo(f"  No records fall within the gap window — sensor may not store that far back.")
                continue

            inserted = 0
            for r in in_window:
                temp_f = round(r["temp_c"] * 9 / 5 + 32, 4)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO readings (ts, address, label, temp_f, humidity) VALUES (?,?,?,?,?)",
                    (r["ts"], addr, label, temp_f, r["humidity"]),
                )
                inserted += cur.rowcount
            conn.commit()
            total_inserted += inserted
            click.echo(f"  Inserted {inserted} rows ({len(in_window)} in window).")

    finally:
        conn.close()
        if svc_was_running:
            click.echo("Restarting smart-home.service...")
            _sp.run(["sudo", "-n", "systemctl", "start", "smart-home.service"])

    click.echo(f"\nDone. Total rows inserted: {total_inserted}")
    if any(lbl for addr, lbl in label_map.items() if addr.upper() not in pvvx_addrs):
        govee = [lbl for addr, lbl in label_map.items() if addr.upper() not in pvvx_addrs]
        click.echo(f"\nNote: {', '.join(govee)} are Govee sensors with no on-device history.")
        click.echo("      Export from the Govee app and use 'smart-home import' to fill their gap.")


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


@main.command("see-monitor")
def see_monitor():
    """Stream the smart-home monitor service log (journalctl -u smart-home.service)."""
    os.execvp("journalctl", ["journalctl", "-u", "smart-home.service"])


if __name__ == "__main__":
    main()
