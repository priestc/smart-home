# smart-home

A Linux-based smart home monitoring system. Currently supports BLE temperature/humidity sensors with a web dashboard and HTTP API.

## Supported Sensors

- **Govee H5074** — passive BLE, no pairing required
- **Xiaomi LYWSD03MMC** — requires [ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer) custom firmware for open BLE broadcasting

---

## Requirements

- Linux with Bluetooth (BlueZ)
- Python 3.9+
- `pipx`

### Bluetooth permissions

BLE scanning requires membership in the `bluetooth` group. Run this once, then log out and back in:

```bash
sudo usermod -a -G bluetooth $USER
```

---

## Installation

```bash
pipx install git+https://github.com/priestc/smart-home.git@master
```

To upgrade later:

```bash
pipx install git+https://github.com/priestc/smart-home.git@master --force
```

---

## Registering a Device

Run the `add-device` command. It will ask for the sensor type, scan for nearby devices, and prompt you to assign a label to each one found.

```
smart-home add-device
```

Example session:

```
What type of sensor do you want to add?

  1. Govee H5074
  2. Xiaomi LYWSD03MMC

Enter choice: 1

Scanning for Govee H5074 sensors (15s)...

Found 2 new sensor(s). Enter a label for each:

  Govee_H5074_6E35 (A4:C1:38:C7:6E:35): inside
  Govee_H5074_AB12 (A4:C1:38:D2:AB:12): outside

Labels saved.
```

Labels are stored in `~/.config/smart-home/labels.json` and used by the monitor and web dashboard.

---

## Running the Monitor

Scan and print readings continuously:

```bash
smart-home monitor
```

To run without writing to a database:

```bash
smart-home monitor --no-db
```

Readings are only written to the database when the temperature or humidity changes, or every 30 minutes as a heartbeat.

---

## Running as a System Service

Two systemd service files are included: one for the background monitor and one for the HTTP API.

### Install the services

```bash
sudo cp ~/smart-home/smart-home.service /etc/systemd/system/
sudo cp ~/smart-home/smart-home-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-home.service
sudo systemctl enable --now smart-home-api.service
```

### Check status / logs

```bash
systemctl status smart-home.service
journalctl -u smart-home.service -f
```

---

## Web Dashboard

Once the API service is running, open a browser to:

```
http://<your-machine-ip>:5000
```

The dashboard shows:
- Current temperature and humidity per sensor
- Temperature and humidity graphs with selectable time ranges (3h / 24h / 3d / 7d / 30d)
- Auto-refreshes current readings every 30 seconds

---

## HTTP API

| Endpoint | Description |
|---|---|
| `GET /api/current` | Latest reading per sensor |
| `GET /api/history` | Historical readings |

Query parameters for `/api/history`:

| Parameter | Description |
|---|---|
| `label` | Filter by sensor label |
| `start` | Earliest timestamp (ISO format, e.g. `2026-01-01`) |
| `end` | Latest timestamp |
| `limit` | Max rows returned (default 1000, max 200000) |

Example:

```
GET /api/history?label=inside&start=2026-03-01&limit=5000
```

---

## Importing Historical Data

Govee stores history in the app which can be exported as a zip file containing CSVs.

```bash
smart-home import ~/inside.zip --label=inside
smart-home import ~/outside.zip --label=outside
```

---

## Diagnostics

Scan all nearby BLE devices (useful for troubleshooting):

```bash
smart-home scan-all
```

Scan for a fixed duration and show decoded readings:

```bash
smart-home scan-once
```
