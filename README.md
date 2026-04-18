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

To remove a temperature sensor:

```bash
smart-home unlabel <label-or-mac>
# Add --purge to also delete its history from the database
```

---

## Removing Devices

Each device type has a dedicated remove command:

| Command | What it removes |
|---|---|
| `smart-home unlabel <label-or-mac>` | Temperature/humidity sensor |
| `smart-home remove-presence-device <name>` | Presence detection device |
| `smart-home remove-garage <name>` | Garage door |
| `smart-home remove-camera <name>` | IP camera |

Example:

```bash
smart-home remove-camera "front door"
```

After removing a device, restart the monitor service for the change to take effect:

```bash
sudo systemctl restart smart-home.service
```

---

## Adding a Garage Door (Shelly Gen3)

### Setup

Wire the Shelly to your garage door button terminals, connect it to WiFi, then register it:

```bash
smart-home configure-garage
```

The command auto-scans the network for Shelly devices and walks you through setup.

### Changing the IP address

The garage door configuration lives on the server at:

```
~/.config/smart-home/garages.json
```

Edit that file directly to update the IP:

```json
[
  {
    "name": "garage",
    "ip": "192.168.1.50",
    "pulse_seconds": 0.5
  }
]
```

Or re-run `smart-home configure-garage` — it will auto-discover the new IP and overwrite the entry. No service restart is needed after changing the IP.

---

## Adding a Camera (XIAO ESP32-S3 Sense)

### 1. Flash the firmware

Open `firmware/camera/camera.ino` in Arduino IDE.

Before flashing, edit the two lines at the top of the file:

```cpp
const char* WIFI_SSID = "YOUR_SSID";
const char* WIFI_PASS = "YOUR_PASSWORD";
```

Board settings:
- **Board:** XIAO_ESP32S3
- **PSRAM:** OPI PSRAM
- **Partition Scheme:** Huge APP (3MB No OTA/1MB SPIFFS)

Flash, then open the Serial Monitor at 115200 baud. The camera will print its IP address once connected to WiFi. Verify it works by opening `http://<ip>/snapshot` in a browser.

### 2. Register the camera

```bash
smart-home configure-camera
```

Enter the camera name and its IP address. The server will test connectivity by grabbing a snapshot.

### 3. Define motion zones

Open the web UI at `http://<your-server>:5000/camera`, select your camera, and draw zones on the live frame. Motion in a zone triggers a push notification.

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
sudo env PATH="$PATH" smart-home install-services
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

## iPhone Home Screen Widget

The `scriptable/SmartHomeWidget.js` file provides a home screen widget using the free [Scriptable](https://apps.apple.com/us/app/scriptable/id1405459188) app.

### Setup

1. Install **Scriptable** from the App Store
2. Open the script file on your phone (AirDrop, iCloud, or copy/paste) and add it to Scriptable
3. Edit the top of the script and set `SERVER_URL` to your server's IP address:
   ```js
   const SERVER_URL = "http://192.168.1.100:5000"
   ```
4. Long-press your home screen → tap **+** → search for **Scriptable**
5. Choose the **Medium** widget size and select **SmartHomeWidget**

> **Note:** Your iPhone must be on the same WiFi network as the server for the widget to reach it.

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
