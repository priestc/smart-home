/*
 * smart-home BLE relay firmware for ESP32
 *
 * Active BLE scanner + GATT relay on request.
 *
 * Every 30 s (NTP-aligned to :00 or :30; multiple relays stagger their offsets):
 *   1. Active BLE scan for 15 s — collects advertisements + scan responses.
 *   2. POST batch to /api/ble-relay — server returns relay_offset + any pending tasks.
 *   3. On GATT cycle (:30 slot): connect to pool monitor, read characteristic, POST.
 *   4. If pair_mode received: advertise as "SmHome-{id}", wait for iPhone to bond.
 *
 * Offline resilience:
 *   - NTP-synced clock (UTC) stamps every scan batch.
 *   - Failed batches buffered in RAM (up to MAX_BUFFER entries); replayed
 *     one-per-cycle when the server is reachable again.
 *   - g_presence_last_seen tracks when each named device was last seen locally.
 *
 * Presence detection:
 *   - Active scanning causes iPhones to respond with their name in SCAN_RSP,
 *     but only after they have bonded with this relay.
 *   - Use `smart-home pair-relay <name> <label>` to trigger bonding.
 *
 * On first boot (or after RESET_CONFIG command on serial): waits for JSON
 * config over serial, stores in NVS, reboots.
 *
 * Provisioned via: smart-home add-relay
 * Build via:       cd relay_firmware && ./build.sh
 * Monitor via:     smart-home relay-log  (on the server)
 *
 * Requires: ArduinoJson >= 7.0, ESP32 Arduino core >= 2.0
 */

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEClient.h>
#include <BLEServer.h>
#include <BLESecurity.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <time.h>
#include <esp_system.h>
#include <map>
#include <string>
#include <vector>

#define FIRMWARE_VERSION      "1.7.53"
#define FIRMWARE_REV          60
#define BAUD_RATE              115200
#define SCAN_SECONDS           15
#define PROVISION_TIMEOUT_MS   60000UL
#define BOOT_PROBE_MS          3000UL
#define HTTP_TIMEOUT_MS        2000
#define MAX_BUFFER             30

// GATT UUIDs — full 128-bit form matches what Python pool.py uses and avoids
// 16-bit→128-bit expansion mismatches in the Bluedroid BLEUUID comparator.
static const char* YC01_SVC_UUID  = "0000ff00-0000-1000-8000-00805f9b34fb";
static const char* YC01_CHAR_UUID = "0000ff02-0000-1000-8000-00805f9b34fb";

static char g_ssid[64];
static char g_pass[64];
static char g_url[128];
static char g_token[64];
static char g_id[32];

// ── Tracked-device filter (populated from /api/relay-startup on boot) ─────────

static std::vector<std::string> g_tracked_macs;   // lowercase colon-separated
static std::vector<std::string> g_tracked_names;  // exact BLE advertised names

// Returns true if this device should be included in POST payloads.
// When both lists are empty (startup call not yet made / failed), pass everything.
static bool isTracked(const std::string& addr_lower, const String& name) {
    if (g_tracked_macs.empty() && g_tracked_names.empty()) return true;
    for (const auto& m : g_tracked_macs)
        if (m == addr_lower) return true;
    if (name.length() > 0)
        for (const auto& n : g_tracked_names)
            if (name == n.c_str()) return true;
    return false;
}

// ── Advertisement buffer ──────────────────────────────────────────────────────

struct DevInfo {
    String name;
    int8_t rssi;
    esp_ble_addr_type_t addr_type;
    bool has_mfr;
    uint16_t mfr_company;
    String mfr_hex;
    bool has_svc;
    String svc_uuid;
    String svc_hex;
};

static std::map<std::string, DevInfo> g_seen;

// ── Offline resilience state ──────────────────────────────────────────────────

static String g_scan_ts;
static std::map<std::string, String> g_presence_last_seen;
static std::vector<String> g_batch_queue;

// ── Persistent pool monitor state ─────────────────────────────────────────────

static String              g_pool_addr;    // configured address (from server/NVS)
static String              g_pool_label;
static BLEClient*          g_pool_client        = nullptr;
static int                 g_pool_fails         = 0;
static volatile bool       g_pool_seen_in_scan  = false;  // set by AdvCallback, cleared by main task
static unsigned long       g_pool_retry_after_ms = 0;     // millis() after which next connect is allowed
static String              g_pool_status;                  // last outcome, sent in POST for relay-log
static int                 g_relay_offset = 0;             // seconds offset within 30-s period (0–29)
static int                 g_poll_skip_cycles = 1;         // cycles to skip between pool reads when stable
#define POOL_RETRY_INTERVAL_MS 30000UL

// ── App watchdog state ────────────────────────────────────────────────────────

static String         g_current_op;
static String         g_pending_crash_reason;
static unsigned long  g_last_post_ok_ms   = 0;  // gate for maybeSendPendingCrash only
static unsigned long  g_last_cycle_start_ms = 0; // watchdog reference: set after each cycle begins
static unsigned long  g_startup_ms         = 0;
#define APP_WDT_MS (2UL * 60UL * 1000UL)

// ── Pair mode state ───────────────────────────────────────────────────────────

static bool   g_pair_mode   = false;  // set by server response; cleared when mode starts
static String g_pair_label;           // label to assign to the newly bonded device
static volatile bool g_bonded = false; // set by security callback on success

// ── Helpers ───────────────────────────────────────────────────────────────────

static String hexEncode(const uint8_t* data, size_t len) {
    String out;
    out.reserve(len * 2);
    char buf[3];
    for (size_t i = 0; i < len; i++) {
        sprintf(buf, "%02x", data[i]);
        out += buf;
    }
    return out;
}

static String getTimestamp() {
    struct tm ti;
    if (!getLocalTime(&ti)) return "";
    char buf[20];
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &ti);
    return String(buf);
}

// ── BLE advertisement callback ────────────────────────────────────────────────

class AdvCallback : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice dev) override {
        std::string addr = dev.getAddress().toString().c_str();
        DevInfo info;
        info.name      = dev.haveName() ? dev.getName().c_str() : "";
        info.rssi      = dev.getRSSI();
        info.addr_type = (esp_ble_addr_type_t)dev.getAddressType();
        info.has_mfr   = false;
        info.has_svc   = false;

        if (dev.haveManufacturerData()) {
            String mfr = dev.getManufacturerData();
            if (mfr.length() >= 2) {
                info.has_mfr     = true;
                info.mfr_company = (uint8_t)mfr[0] | ((uint8_t)mfr[1] << 8);
                info.mfr_hex     = hexEncode((const uint8_t*)mfr.c_str() + 2, mfr.length() - 2);
            }
        }
        if (dev.haveServiceData()) {
            info.has_svc = true;
            String uuid  = dev.getServiceDataUUID().toString().c_str();
            uuid.toLowerCase();
            info.svc_uuid = uuid;
            String svc   = dev.getServiceData();
            info.svc_hex = hexEncode((const uint8_t*)svc.c_str(), svc.length());
        }
        g_seen[addr] = info;

        if (info.name.length() && g_scan_ts.length())
            g_presence_last_seen[info.name.c_str()] = g_scan_ts;

        // Signal main task to stop the scan early so we connect immediately.
        // Only when we are allowed to attempt a connection (cooldown expired).
        // Do NOT call BLE APIs here — stop() from a GAP callback causes a panic.
        if (g_pool_addr.length() > 0 &&
            !(g_pool_client && g_pool_client->isConnected()) &&
            millis() >= g_pool_retry_after_ms &&
            (info.name.startsWith("BLE_YC01") || info.name.startsWith("BLE-YC01"))) {
            g_pool_seen_in_scan = true;
        }
    }
};

static AdvCallback g_cb;

// ── Config helpers ────────────────────────────────────────────────────────────

static void loadConfig() {
    Preferences p;
    p.begin("relay", true);
    strlcpy(g_ssid,  p.getString("ssid",  "").c_str(), sizeof(g_ssid));
    strlcpy(g_pass,  p.getString("pass",  "").c_str(), sizeof(g_pass));
    strlcpy(g_url,   p.getString("url",   "").c_str(), sizeof(g_url));
    strlcpy(g_token, p.getString("token", "").c_str(), sizeof(g_token));
    strlcpy(g_id,    p.getString("id", "esp32-relay").c_str(), sizeof(g_id));
    g_relay_offset = p.getInt("relay_offset", 0);
    p.end();
}

static void clearConfig() {
    Preferences p;
    p.begin("relay", false);
    p.clear();
    p.end();
}

static void loadPoolMonitor() {
    Preferences p;
    p.begin("relay", true);
    g_pool_addr        = p.getString("pool_addr",  "");
    g_pool_label       = p.getString("pool_label", "");
    g_poll_skip_cycles = p.getInt("poll_skip",  1);
    p.end();
}

static void savePoolMonitor(const String& addr, const String& label) {
    Preferences p;
    p.begin("relay", false);
    p.putString("pool_addr",  addr);
    p.putString("pool_label", label);
    p.end();
}

static void savePollSkipCycles(int cycles) {
    Preferences p;
    p.begin("relay", false);
    p.putInt("poll_skip", cycles);
    p.end();
}

// ── BLE bonding / pair mode ───────────────────────────────────────────────────

class PairServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
        Serial.println("Pair: device connected — requesting encryption...");
        // Peripheral-initiated security request triggers Just-Works SMP pairing
        // in the connecting central (nRF Connect, iOS, etc.) without needing a
        // protected characteristic to prompt it.
        esp_bd_addr_t bda;
        memcpy(bda, param->connect.remote_bda, sizeof(esp_bd_addr_t));
        esp_ble_set_encryption(bda, ESP_BLE_SEC_ENCRYPT_NO_MITM);
    }
    void onDisconnect(BLEServer*) override {
        Serial.println("Pair: device disconnected.");
    }
};

class PairSecCallbacks : public BLESecurityCallbacks {
    bool     onSecurityRequest()            override { return true; }
    uint32_t onPassKeyRequest()             override { return 0; }
    void     onPassKeyNotify(uint32_t pin)  override { Serial.printf("Pair PIN: %06u\n", pin); }
    bool     onConfirmPIN(uint32_t)         override { return true; }
    bool     onAuthorizationRequest(uint16_t, uint16_t, bool) override { return true; }
    void     onAuthenticationComplete(esp_ble_auth_cmpl_t cmpl) override {
        if (cmpl.success) {
            g_bonded = true;
            Serial.println("Pair: bonding successful!");
        } else {
            Serial.printf("Pair: bonding failed (reason %d)\n", (int)cmpl.fail_reason);
        }
    }
};

static PairServerCallbacks g_pair_srv_cb;
static PairSecCallbacks    g_pair_sec_cb;
static BLEServer*          g_ble_server = nullptr;

// Advertise as "SmHome-{relay_id}" and wait for the user's iPhone to bond.
// After bonding, iOS will respond to our active-scan SCAN_REQ with its name,
// enabling name-based presence detection.
static void pairModeStart(const String& label) {
    g_pair_mode = false;  // consumed
    g_bonded    = false;

    Serial.printf("=== Pair mode: label='%s' ===\n", label.c_str());
    Serial.println("Stopping scanner...");
    BLEDevice::getScan()->stop();
    delay(300);

    // Configure security: bonding, Just Works (no MITM), Secure Connections
    BLESecurity::setAuthenticationMode(true, false, true);
    BLESecurity::setCapability(ESP_IO_CAP_NONE);
    BLESecurity::setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
    BLESecurity::setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
    BLEDevice::setSecurityCallbacks(&g_pair_sec_cb);

    if (!g_ble_server) {
        g_ble_server = BLEDevice::createServer();
        g_ble_server->setCallbacks(&g_pair_srv_cb);
    }

    // Advertise using the device name set at init ("SmHome-{relay_id}").
    // Calling startAdvertising() with no custom data avoids the async race in
    // setAdvertisementData() / setScanResponseData() + start().
    BLEDevice::startAdvertising();

    Serial.printf("Advertising as 'SmHome-%s'\n", g_id);
    Serial.printf("On iPhone: Settings > Bluetooth > tap 'SmHome-%s' to pair\n", g_id);
    Serial.println("Waiting up to 60 seconds...");

    unsigned long deadline = millis() + 60000;
    while (millis() < deadline && !g_bonded) {
        delay(100);
    }

    BLEDevice::getAdvertising()->stop();
    delay(200);

    if (g_bonded) {
        Serial.printf("Pair complete! '%s' will now appear in presence detection.\n",
                      label.c_str());
    } else {
        Serial.println("Pair mode timed out (60 s) — no bonding occurred.");
    }
}

// ── Provisioning mode ─────────────────────────────────────────────────────────

static void provisionMode() {
    Serial.println("SMHOME_RELAY " FIRMWARE_VERSION);
    Serial.println("WAITING_FOR_CONFIG");
    Serial.flush();
    unsigned long deadline = millis() + PROVISION_TIMEOUT_MS;
    while (millis() < deadline) {
        if (!Serial.available()) { delay(50); continue; }
        String line = Serial.readStringUntil('\n');
        line.trim();
        JsonDocument doc;
        if (deserializeJson(doc, line) != DeserializationError::Ok) {
            Serial.println("ERR:BAD_JSON");
            Serial.flush();
            continue;
        }
        Preferences p;
        p.begin("relay", false);
        p.putString("ssid",  doc["ssid"]  | "");
        p.putString("pass",  doc["pass"]  | "");
        p.putString("url",   doc["url"]   | "");
        p.putString("token", doc["token"] | "");
        p.putString("id",    doc["id"]    | "esp32-relay");
        p.end();
        Serial.println("CONFIG_SAVED");
        Serial.flush();
        delay(500);
        ESP.restart();
    }
    Serial.println("ERR:PROVISION_TIMEOUT");
    Serial.flush();
}

// ── WiFi + NTP ────────────────────────────────────────────────────────────────

static void connectWiFi() {
    String hostname = String("smart-home-relay-") + g_id;
    hostname.replace("_", "-");
    WiFi.setHostname(hostname.c_str());
    WiFi.begin(g_ssid, g_pass);
    Serial.printf("Connecting to WiFi '%s'", g_ssid);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
        delay(500);
        Serial.print(".");
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi connect failed — will retry");
        return;
    }

    Serial.printf("\nWiFi OK: %s\n", WiFi.localIP().toString().c_str());
    // Modem sleep lets the coexistence manager interleave WiFi and BLE timeslots.
    WiFi.setSleep(true);
    MDNS.begin(WiFi.getHostname());
    Serial.printf("Server URL: %s\n", g_url);

    // NTP time sync (UTC, no DST offset)
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    Serial.print("NTP sync");
    struct tm ti;
    int ntpTries = 0;
    while (!getLocalTime(&ti) && ntpTries++ < 20) {
        delay(500);
        Serial.print(".");
    }
    if (getLocalTime(&ti)) {
        char buf[20];
        strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &ti);
        Serial.printf("\nNTP OK: %s UTC\n", buf);
    } else {
        Serial.println("\nNTP FAILED — batch timestamps will be omitted");
    }

    // DNS diagnostic
    String host = String(g_url);
    int schemeEnd = host.indexOf("://");
    if (schemeEnd >= 0) host = host.substring(schemeEnd + 3);
    int slashPos = host.indexOf('/');
    if (slashPos >= 0) host = host.substring(0, slashPos);
    int colonPos = host.indexOf(':');
    if (colonPos >= 0) host = host.substring(0, colonPos);
    IPAddress resolved;
    if (WiFi.hostByName(host.c_str(), resolved))
        Serial.printf("DNS OK: %s -> %s\n", host.c_str(), resolved.toString().c_str());
    else
        Serial.printf("DNS FAILED: could not resolve '%s'\n", host.c_str());
}

static void poolDisconnect();  // forward declaration

// ── Startup: fetch tracked-device list from server ───────────────────────────

static void fetchTrackedDevices() {
    if (WiFi.status() != WL_CONNECTED || strlen(g_url) == 0) return;
    HTTPClient http;
    http.begin(String(g_url) + "/api/relay-startup");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(10000);
    int code = http.GET();
    if (code == 200) {
        String resp = http.getString();
        JsonDocument doc;
        if (deserializeJson(doc, resp) == DeserializationError::Ok) {
            g_tracked_macs.clear();
            g_tracked_names.clear();
            for (JsonVariant v : doc["tracked_macs"].as<JsonArray>()) {
                std::string mac = v.as<String>().c_str();
                for (char& c : mac) c = tolower((unsigned char)c);
                g_tracked_macs.push_back(mac);
            }
            for (JsonVariant v : doc["tracked_names"].as<JsonArray>())
                g_tracked_names.push_back(v.as<String>().c_str());
            Serial.printf("Startup: tracking %u MACs, %u BLE names\n",
                          (unsigned)g_tracked_macs.size(),
                          (unsigned)g_tracked_names.size());
        }
    } else {
        Serial.printf("Startup: relay-startup returned %d — sending all devices\n", code);
    }
    http.end();
}

// ── Crash reporting ───────────────────────────────────────────────────────────

static void sendCrashReport(const String& reason) {
    if (WiFi.status() != WL_CONNECTED || strlen(g_url) == 0) return;
    JsonDocument doc;
    doc["relay_id"] = g_id;
    doc["reason"]   = reason;
    doc["uptime_s"] = millis() / 1000;
    if (g_current_op.length()) doc["op"] = g_current_op;
    String payload;
    serializeJson(doc, payload);
    HTTPClient http;
    http.begin(String(g_url) + "/api/ble-relay/crash");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(10000);
    int code = http.POST(payload);
    Serial.printf("Crash report POST -> %d\n", code);
    http.end();
}

static void checkAppWatchdog() {
    // Use cycle-start time so a server outage (failed POSTs, healthy cycles)
    // does not trigger a restart. Only fire when a cycle itself takes too long,
    // which indicates the relay is hung inside a BLE or HTTP operation.
    unsigned long ref = g_last_cycle_start_ms > 0 ? g_last_cycle_start_ms : g_startup_ms;
    if (ref == 0) return;
    unsigned long elapsed = millis() - ref;
    if (elapsed > APP_WDT_MS) {
        String msg = "cycle took " + String(elapsed / 1000) + "s";
        if (g_current_op.length()) msg += "; stuck in: " + g_current_op;
        Serial.println("App watchdog: " + msg);
        // Save to NVS so it's sent after reboot if sendCrashReport fails now
        {
            Preferences p;
            p.begin("relay", false);
            p.putString("crash_reason", msg);
            p.end();
        }
        sendCrashReport(msg);
        delay(3000);
        ESP.restart();
    }
}

static void maybeSendPendingCrash() {
    if (g_pending_crash_reason.length() == 0 || g_last_post_ok_ms == 0) return;
    sendCrashReport(g_pending_crash_reason);
    g_pending_crash_reason = "";
}

// ── HTTP POST to /api/ble-relay ───────────────────────────────────────────────

static bool httpPost(const String& payload, bool parse_gatt) {
    HTTPClient http;
    http.begin(String(g_url) + "/api/ble-relay");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(HTTP_TIMEOUT_MS);
    int code = http.POST(const_cast<String&>(payload));

    if (code == 200) {
        g_last_post_ok_ms = millis();
        if (parse_gatt) {
            String resp = http.getString();
            JsonDocument rdoc;
            if (deserializeJson(rdoc, resp) == DeserializationError::Ok) {
                // Check for pair mode instruction from server
                if (rdoc["pair_mode"].is<JsonObject>()) {
                    g_pair_label = rdoc["pair_mode"]["label"].as<String>();
                    g_pair_mode  = true;
                    Serial.printf("Pair mode requested for label: '%s'\n",
                                  g_pair_label.c_str());
                }
                // Check for pool monitor assignment change
                if (!rdoc["pool_monitor"].isNull()) {
                    String new_addr  = rdoc["pool_monitor"]["address"].as<String>();
                    String new_label = rdoc["pool_monitor"]["label"].as<String>();
                    if (rdoc["pool_monitor"]["poll_skip_cycles"].is<int>()) {
                        int new_skip = rdoc["pool_monitor"]["poll_skip_cycles"].as<int>();
                        if (new_skip != g_poll_skip_cycles) {
                            g_poll_skip_cycles = new_skip;
                            savePollSkipCycles(new_skip);
                            Serial.printf("Pool: poll rate updated — skip %d cycle(s) (%ds interval)\n",
                                          new_skip, (new_skip + 1) * 30);
                        }
                    }
                    if (new_addr != g_pool_addr) {
                        Serial.printf("Pool monitor assigned: %s (%s)\n",
                                      new_label.c_str(), new_addr.c_str());
                        savePoolMonitor(new_addr, new_label);
                        g_pool_addr  = new_addr;
                        g_pool_label = new_label;
                        poolDisconnect();
                    }
                } else if (g_pool_addr.length() > 0) {
                    Serial.println("Pool monitor cleared by server");
                    savePoolMonitor("", "");
                    g_pool_addr  = "";
                    g_pool_label = "";
                    poolDisconnect();
                }
                if (rdoc["relay_offset"].is<int>()) {
                    int new_offset = rdoc["relay_offset"].as<int>();
                    if (new_offset >= 0 && new_offset < 30 && new_offset != g_relay_offset) {
                        g_relay_offset = new_offset;
                        Preferences p;
                        p.begin("relay", false);
                        p.putInt("relay_offset", new_offset);
                        p.end();
                        Serial.printf("Relay offset updated to %ds\n", new_offset);
                    }
                }
                Serial.printf("POST 200 (%d inserted)\n", rdoc["inserted"].as<int>());
            }
        }
        http.end();
        return true;
    }

    const char* reason = (code == -1) ? "connection refused/no route" :
                         (code == -4) ? "not connected" :
                         (code == -11) ? "read timeout" : "error";
    Serial.printf("POST failed: %d (%s) url=%s\n", code, reason, g_url);
    http.end();
    return false;
}

// ── Batch payload builder ─────────────────────────────────────────────────────

static String buildPayload(bool include_presence, bool pool_offline = false,
                            const String& pool_hex = "", int8_t pool_rssi = 0,
                            bool pool_seen = false, bool pool_skip = false,
                            bool buffered = false,
                            const std::vector<String>* buffered_batches = nullptr) {
    JsonDocument doc;
    doc["relay_id"] = g_id;
    doc["rev"] = FIRMWARE_REV;
    if (g_scan_ts.length()) doc["batch_ts"] = g_scan_ts;
    if (buffered) doc["buffered"] = true;
    doc["buffer_size"] = (int)g_batch_queue.size();
    if (buffered_batches && !buffered_batches->empty()) {
        JsonArray ba = doc["buffered_batches"].to<JsonArray>();
        for (const auto& s : *buffered_batches)
            ba.add(s.c_str());  // stored as escaped JSON string; server parses with json.loads()
    }

    JsonArray arr = doc["advertisements"].to<JsonArray>();
    for (auto& kv : g_seen) {
        if (!isTracked(kv.first, kv.second.name)) continue;
        JsonObject obj = arr.add<JsonObject>();
        obj["address"] = kv.first.c_str();
        if (kv.second.name.length()) obj["name"] = kv.second.name;
        obj["rssi"] = kv.second.rssi;
        if (kv.second.has_mfr) {
            JsonObject mfr = obj["manufacturer_data"].to<JsonObject>();
            mfr[String(kv.second.mfr_company)] = kv.second.mfr_hex;
        }
        if (kv.second.has_svc) {
            JsonObject svc = obj["service_data"].to<JsonObject>();
            svc[kv.second.svc_uuid] = kv.second.svc_hex;
        }
    }

    if (include_presence && !g_presence_last_seen.empty()) {
        JsonObject pls = doc["presence_last_seen"].to<JsonObject>();
        for (auto& kv : g_presence_last_seen) {
            if (!g_tracked_names.empty()) {
                bool found = false;
                for (const auto& n : g_tracked_names)
                    if (kv.first == n) { found = true; break; }
                if (!found) continue;
            }
            pls[kv.first.c_str()] = kv.second;
        }
    }

    if (pool_skip) doc["pool_skip"] = true;
    if (pool_offline && !pool_skip) doc["pool_offline"] = true;
    if (pool_offline && pool_seen && !pool_skip) doc["pool_seen"] = true;
    if (g_pool_status.length() > 0) doc["pool_status"] = g_pool_status;
    if (pool_hex.length() > 0) {
        JsonObject pr = doc["pool_reading"].to<JsonObject>();
        pr["address"]    = g_pool_addr;
        pr["label"]      = g_pool_label;
        pr["result_hex"] = pool_hex;
        pr["rssi"]       = pool_rssi;
    }

    String payload;
    serializeJson(doc, payload);
    return payload;
}

// ── BLE advertisement batch POST with buffering ───────────────────────────────

static void bufferPush(const String& payload) {
    if (g_batch_queue.size() >= MAX_BUFFER) {
        g_batch_queue.erase(g_batch_queue.begin());
        Serial.println("Buffer full — dropped oldest batch");
    }
    g_batch_queue.push_back(payload);
}

// Set true after the first successful POST when the buffer is non-empty.
// The following cycle drains the entire buffer in one go before resuming
// normal operation. The one-cycle gap guarantees we only attempt the bulk
// drain once we know the connection is stable.
static bool s_drain_next_cycle = false;

static void postBatch(bool pool_offline = false, const String& pool_hex = "", int8_t pool_rssi = 0, bool pool_seen = false, bool pool_skip = false) {
    Serial.printf("Scan: %u devices  buffer: %u  fw=%s  rev=%d\n",
                  (unsigned)g_seen.size(), (unsigned)g_batch_queue.size(),
                  FIRMWARE_VERSION, FIRMWARE_REV);

    // Drain cycle: bundle all buffered entries into this POST alongside the current batch.
    if (s_drain_next_cycle) {
        s_drain_next_cycle = false;
        Serial.printf("Sending current batch bundled with %u buffered entries...\n",
                      (unsigned)g_batch_queue.size());
        if (httpPost(buildPayload(true, pool_offline, pool_hex, pool_rssi, pool_seen, pool_skip,
                                  false, &g_batch_queue), true)) {
            Serial.printf("Drain complete — %u buffered entries delivered.\n",
                          (unsigned)g_batch_queue.size());
            g_batch_queue.clear();
        } else {
            Serial.println("Drain POST failed — buffering current batch, will retry next reconnect.");
            bufferPush(buildPayload(false, pool_offline, pool_hex, pool_rssi, pool_seen, pool_skip, true));
        }
        return;
    }

    if (g_seen.empty() && !pool_offline && pool_hex.length() == 0 && !pool_skip) return;

    if (!httpPost(buildPayload(true, pool_offline, pool_hex, pool_rssi, pool_seen, pool_skip), true)) {
        bufferPush(buildPayload(false, pool_offline, pool_hex, pool_rssi, pool_seen, pool_skip, true));
    } else if (!g_batch_queue.empty()) {
        // First successful POST with a non-empty buffer — drain on the next cycle.
        s_drain_next_cycle = true;
        Serial.printf("Server reachable — will drain %u buffered batches next cycle.\n",
                      (unsigned)g_batch_queue.size());
    }
}

// ── Persistent pool monitor ───────────────────────────────────────────────────

static void poolDisconnect() {
    if (!g_pool_client) return;
    if (g_pool_client->isConnected()) {
        g_pool_client->disconnect();
        delay(300);
    }
    // Delete the client so the next connect always gets a fresh BLEClient with no
    // stale m_haveServices/m_servicesMap state. After a disconnect, m_haveServices
    // stays true, causing the next getServices() call to return cached characteristic
    // objects whose semaphores are in an undefined state — making readValue() hang.
    delete g_pool_client;
    g_pool_client = nullptr;
}

static void resetBLEStack() {
    // After a connect timeout the BLE controller stays in connection-initiation
    // mode and can no longer scan. Deinit + reinit restores a clean state.
    // We abandon g_pool_client without deleting: after deinit the GATT resources
    // are released by the stack, and calling the destructor on a dead interface
    // could crash.
    Serial.println("Pool: resetting BLE stack after connect timeout");
    g_pool_client = nullptr;
    String ble_name = String("SmHome-") + g_id;
    BLEDevice::deinit();
    delay(500);
    BLEDevice::init(ble_name.c_str());
    Serial.println("Pool: BLE stack reset complete");
}

// Sleep until the next 30-second clock boundary for this relay's assigned offset.
// Returns true when the fire time falls in the [30–59] range of the minute —
// that is the GATT cycle where we attempt the pool monitor connection.
// Falls back to a 3-second delay if NTP hasn't synced yet.
static bool sleepUntilNextSlot() {
    time_t now;
    time(&now);
    if (now < 1000000000L) {
        // NTP not ready — short fixed delay, no GATT
        delay(3000);
        return false;
    }
    int pos        = (int)(now % 30);
    int sleep_secs = ((g_relay_offset - pos) + 30) % 30;
    time_t fire_at = now + (time_t)sleep_secs;
    bool is_gatt   = (fire_at % 60) >= 30;
    Serial.printf("Slot: +%ds  offset=%d  gatt=%s  fire@%lld\n",
                  sleep_secs, g_relay_offset, is_gatt ? "yes" : "no", (long long)fire_at);
    if (sleep_secs > 0) delay((uint32_t)sleep_secs * 1000);
    return is_gatt;
}

static void doPoolMonitorCycle() {
    // Sleep at the START of each cycle so the POST lands on a clock boundary.
    sleepUntilNextSlot();

    // do_gatt is determined after the scan based on whether the device was seen.
    // s_pool_last_read_ok drives pool_skip: suppresses offline events when the pool
    // was recently readable but just isn't visible in the current scan.
    // s_pool_skip_counter counts down g_poll_skip_cycles skips between GATT reads.
    static bool s_pool_last_read_ok  = false;
    static int  s_pool_skip_counter  = 0;

    checkAppWatchdog();
    g_last_cycle_start_ms = millis();  // watchdog measures from here to next cycle start
    g_pool_status = "";  // clear each cycle so stale statuses don't persist

    static int s_pool_wifi_fails = 0;
    if (WiFi.status() != WL_CONNECTED) {
        s_pool_wifi_fails++;
        if (s_pool_wifi_fails % 4 == 0) {
            Serial.printf("Pool: WiFi lost (%d failures) — full reconnect\n", s_pool_wifi_fails);
            WiFi.disconnect();
            delay(1000);
            WiFi.begin(g_ssid, g_pass);
        } else {
            Serial.println("Pool: WiFi lost — reconnecting...");
            WiFi.reconnect();
        }
        delay(5000);
        return;
    }
    s_pool_wifi_fails = 0;

    // ── Step 1: BLE scan ──────────────────────────────────────────────────────
    // Scan first so we know the YC01's current address/type before connecting.
    // The v3.3.x library auto-stops the scanner before any connect attempt, so
    // there is no SCAN_REQ interference when we connect right after the scan.
    g_seen.clear();
    BLEScan* scan = BLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(&g_cb, false);
    scan->setActiveScan(true);
    scan->setInterval(160);
    scan->setWindow(80);
    g_current_op = "ble-scan";

    // Always run the full scan so iPhones get the complete active-scan window to
    // respond with their name in a scan response. The YC01 continuously advertises
    // so it will still be present after the scan; no need to cut short for it.
    g_pool_seen_in_scan = false;
    scan->start(SCAN_SECONDS, nullptr, false);
    unsigned long scan_deadline = millis() + (uint32_t)SCAN_SECONDS * 1000UL;
    while (millis() < scan_deadline) {
        if (!scan->isScanning()) break;
        delay(20);
    }
    if (scan->isScanning()) scan->stop();
    scan->clearResults();
    g_scan_ts = getTimestamp();  // stamp after scan so batch_ts reflects POST time

    // ── Step 2: Pool monitor connect + read ───────────────────────────────────
    // Mirror the Python _yc01_persistent_loop approach: find the device by name
    // in the scan just completed, then connect immediately while it is still
    // actively advertising. Handles RPA rotation (address changes, name doesn't).
    String pool_hex;
    int8_t pool_rssi = 0;
    bool pool_offline = true;
    bool pool_seen_now = false;
    bool do_gatt = false;  // set inside pool block; used by pool_skip below
    // Capture skip state before the read so pool_skip reflects this cycle,
    // not the counter value written for the NEXT cycle after a successful read.
    bool was_skip_cycle = (s_pool_skip_counter > 0) && (g_pool_addr.length() > 0);

    if (g_pool_addr.length() > 0) {
        // Find YC01 by name first; fall back to configured address.
        // Always do this — even on skipped cycles — so pool_seen_now is accurate.
        String cur_addr;
        uint8_t cur_type = BLE_ADDR_TYPE_PUBLIC;
        for (auto& kv : g_seen) {
            const DevInfo& di = kv.second;
            if (di.name.startsWith("BLE_YC01") || di.name.startsWith("BLE-YC01")) {
                cur_addr = String(kv.first.c_str());
                cur_type = (uint8_t)di.addr_type;
                Serial.printf("Pool: found '%s' at %s type=%d\n",
                              di.name.c_str(), cur_addr.c_str(), cur_type);
                break;
            }
        }
        if (cur_addr.length() == 0) {
            String addr_lc = g_pool_addr;
            addr_lc.toLowerCase();
            auto it = g_seen.find(addr_lc.c_str());
            if (it != g_seen.end()) {
                cur_addr = String(it->first.c_str());
                cur_type = (uint8_t)it->second.addr_type;
                Serial.printf("Pool: found by addr %s type=%d\n",
                              cur_addr.c_str(), cur_type);
            }
        }
        pool_seen_now = (cur_addr.length() > 0);

        // After a successful read, count down g_poll_skip_cycles cycles before
        // attempting another GATT read. Counter is cleared on any failure so we
        // retry immediately rather than waiting out the configured interval.
        do_gatt = pool_seen_now && (s_pool_skip_counter == 0);
        if (s_pool_skip_counter > 0) {
            Serial.printf("Pool: skip cycle (%d remaining)\n", s_pool_skip_counter);
            s_pool_skip_counter--;
        }
        if (do_gatt) {
            if (!g_pool_client) {
                g_pool_client = BLEDevice::createClient();
                g_pool_client->setMTU(23);
            }

            // Connect if not already connected, device seen, and cooldown expired.
            if (!g_pool_client->isConnected() && pool_seen_now &&
                millis() >= g_pool_retry_after_ms) {
                g_current_op = "pool-connect";
                unsigned long connect_start = millis();
                Serial.printf("Pool: connecting to %s (%s) type=%d...\n",
                              g_pool_label.c_str(), cur_addr.c_str(), cur_type);
                bool ok = g_pool_client->connect(
                    BLEAddress(cur_addr.c_str(), cur_type),
                    cur_type,
                    8000  // 8 s — fits within the 30 s cycle slot
                );
                unsigned long connect_ms = millis() - connect_start;
                if (!ok) {
                    g_pool_fails++;
                    g_pool_retry_after_ms = millis() + POOL_RETRY_INTERVAL_MS;
                    g_pool_status = connect_ms < 2000 ? "connect_fail_fast" : "connect_timeout";
                    s_pool_last_read_ok = false;
                    s_pool_skip_counter = 0;
                    Serial.printf("Pool: connect failed in %lums (fail #%d) [%s], retry in %lus\n",
                                  connect_ms, g_pool_fails, g_pool_status.c_str(),
                                  POOL_RETRY_INTERVAL_MS / 1000UL);
                    if (connect_ms >= 2000) {
                        // Any real timeout leaves the BLE controller stuck in connection-initiation
                        // mode and unable to scan. Reset the stack to recover.
                        // BLEDevice::deinit() can briefly disrupt WiFi coexistence; reconnect if needed.
                        resetBLEStack();
                        if (WiFi.status() != WL_CONNECTED) {
                            Serial.println("Pool: WiFi dropped after BLE reset — reconnecting");
                            WiFi.reconnect();
                            delay(3000);
                        }
                    } else {
                        poolDisconnect();
                    }
                } else {
                    g_pool_fails = 0;
                    g_pool_retry_after_ms = 0;
                    g_pool_status = "";
                    Serial.printf("Pool: connected to %s in %lums\n",
                                  g_pool_label.c_str(), connect_ms);
                }
            }

            // Read GATT if connected.
            if (g_pool_client && g_pool_client->isConnected()) {
                // getServices() triggers GATT service discovery and returns the populated map.
                // We iterate all services manually because Bluedroid's UUID comparator fails to
                // match both 16-bit and 128-bit forms reliably — mirrors Python's read_gatt_char
                // which finds the characteristic regardless of service UUID.
                BLERemoteCharacteristic* chr = nullptr;
                String svc_uuids;
                std::map<std::string, BLERemoteService*>* pSvcs = g_pool_client->getServices();
                if (pSvcs) {
                    for (auto& kv : *pSvcs) {
                        svc_uuids += String(kv.first.c_str()) + " ";
                        // Try 128-bit form first, then 16-bit — Bluedroid may store either.
                        BLERemoteCharacteristic* c = kv.second->getCharacteristic(BLEUUID(YC01_CHAR_UUID));
                        if (c && c->canRead()) { chr = c; break; }
                        c = kv.second->getCharacteristic(BLEUUID((uint16_t)0xFF02));
                        if (c && c->canRead()) { chr = c; break; }
                    }
                }
                Serial.printf("Pool: services found: [%s]\n", svc_uuids.c_str());

                if (chr) {
                    g_current_op = "pool-read";
                    String val = chr->readValue();
                    if (val.length() > 0) {
                        pool_hex  = hexEncode((const uint8_t*)val.c_str(), val.length());
                        pool_rssi = g_pool_client->getRssi();
                        pool_offline = false;
                        g_pool_fails = 0;
                        g_pool_status = "";
                        s_pool_last_read_ok = true;
                        s_pool_skip_counter = g_poll_skip_cycles;
                        Serial.printf("Pool: read %u bytes\n", (unsigned)val.length());
                        // Disconnect immediately after reading — mirrors Python's connect-read-disconnect
                        // pattern. Keeping the connection open risks hanging on the next readValue()
                        // or getServices() call if the YC01 goes out of range before we reconnect.
                        poolDisconnect();
                    } else {
                        g_pool_status = "empty_read";
                        s_pool_last_read_ok = false;
                        s_pool_skip_counter = 0;
                        Serial.println("Pool: empty GATT read");
                        poolDisconnect();
                        g_pool_fails++;
                        // Connection was healthy — no cooldown needed, retry next cycle.
                        g_pool_retry_after_ms = 0;
                    }
                } else {
                    g_pool_status = String("no_char svcs:") + (pSvcs ? String(pSvcs->size()) : "null");
                    s_pool_last_read_ok = false;
                    s_pool_skip_counter = 0;
                    Serial.printf("Pool: characteristic not found across %s\n", svc_uuids.c_str());
                    poolDisconnect();
                    g_pool_fails++;
                    // Connection was healthy — no cooldown needed, retry next cycle.
                    g_pool_retry_after_ms = 0;
                }
            }
        }
    }

    // If the cycle was a do_gatt attempt but pool is still offline with no status,
    // we were in the retry cooldown and skipped the connect entirely.
    if (do_gatt && pool_offline && pool_seen_now && g_pool_status.length() == 0) {
        unsigned long remaining_s = g_pool_retry_after_ms > millis()
            ? (g_pool_retry_after_ms - millis()) / 1000 : 0;
        g_pool_status = remaining_s > 0
            ? String("cooldown ") + remaining_s + "s" : "cooldown";
    }

    // ── Step 3: Single POST with pool + sensor data ────────────────────────────
    g_current_op = "http-post";
    // pool_skip: this cycle was an intentional wait between reads.
    // was_skip_cycle was captured before any read so a successful read this
    // cycle doesn't cause its own POST to be tagged as a skip.
    bool pool_skip = was_skip_cycle;
    postBatch(pool_offline, pool_hex, pool_rssi, pool_offline && pool_seen_now, pool_skip);
    g_current_op = "";
    maybeSendPendingCrash();
}

// ── Arduino entry points ──────────────────────────────────────────────────────

void setup() {
    Serial.begin(BAUD_RATE);
    delay(500);

    unsigned long probe_end = millis() + BOOT_PROBE_MS;
    while (millis() < probe_end) {
        if (Serial.available()) {
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd == "RESET_CONFIG") {
                clearConfig();
                Serial.println("CONFIG_CLEARED");
                Serial.flush();
                break;
            }
        }
        delay(50);
    }

    loadConfig();
    if (strlen(g_ssid) == 0) {
        provisionMode();
        while (true) delay(1000);
    }

    loadPoolMonitor();
    if (g_pool_addr.length() > 0) {
        Serial.printf("Pool monitor mode active: %s (%s)\n",
                      g_pool_label.c_str(), g_pool_addr.c_str());
    }

    connectWiFi();
    fetchTrackedDevices();
    g_startup_ms = millis();
    {
        // Check NVS for crash reason saved by app watchdog on previous boot
        Preferences p;
        p.begin("relay", true);
        String saved = p.getString("crash_reason", "");
        p.end();
        if (saved.length() > 0) {
            g_pending_crash_reason = saved;
            Preferences p2;
            p2.begin("relay", false);
            p2.remove("crash_reason");
            p2.end();
            Serial.println("Pending crash report (from NVS): " + saved);
        }

        // Check hardware reset reason (only set if no NVS reason already loaded)
        if (g_pending_crash_reason.length() == 0) {
            esp_reset_reason_t rst = esp_reset_reason();
            const char* rst_str = nullptr;
            if      (rst == ESP_RST_PANIC)    rst_str = "panic/exception";
            else if (rst == ESP_RST_INT_WDT)  rst_str = "interrupt watchdog";
            else if (rst == ESP_RST_TASK_WDT) rst_str = "task watchdog";
            else if (rst == ESP_RST_WDT)      rst_str = "other watchdog";
            else if (rst == ESP_RST_BROWNOUT) rst_str = "brownout";
            if (rst_str) {
                g_pending_crash_reason = String("hard reset: ") + rst_str;
                Serial.println("Crash detected: " + g_pending_crash_reason);
            }
        }
    }
    String ble_name = String("SmHome-") + g_id;
    BLEDevice::init(ble_name.c_str());
    Serial.printf("BLE relay ready  id=%s  ble=%s  fw=%s  rev=%d\n",
                  g_id, ble_name.c_str(), FIRMWARE_VERSION, FIRMWARE_REV);
}

void loop() {
    // If assigned as a pool monitor node, run the persistent GATT loop exclusively.
    if (g_pool_addr.length() > 0) {
        doPoolMonitorCycle();
        return;
    }

    // Sleep until this relay's assigned clock-aligned slot (offset within 30 s).
    // The return value indicates the GATT cycle (fire_at % 60 >= 30) — unused
    // in the non-pool-monitor path since there is no GATT work here.
    sleepUntilNextSlot();

    checkAppWatchdog();
    g_last_cycle_start_ms = millis();

    static int s_wifi_fail_count = 0;
    if (WiFi.status() != WL_CONNECTED) {
        s_wifi_fail_count++;
        if (s_wifi_fail_count % 4 == 0) {
            Serial.printf("WiFi lost (%d failures) — full reconnect\n", s_wifi_fail_count);
            WiFi.disconnect();
            delay(1000);
            WiFi.begin(g_ssid, g_pass);
        } else {
            Serial.println("WiFi lost — reconnecting...");
            WiFi.reconnect();
        }
        delay(5000);
        return;
    }
    s_wifi_fail_count = 0;

    g_current_op = "ble-scan";
    g_seen.clear();

    BLEScan* scan = BLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(&g_cb, false);
    scan->setActiveScan(true);   // needed to receive scan responses where iPhone advertises its name
    scan->setInterval(160);  // 100 ms — gives WiFi regular radio gaps
    scan->setWindow(80);     // 50 ms active per interval (50% duty cycle)
    scan->start(SCAN_SECONDS, false);
    scan->clearResults();
    g_scan_ts = getTimestamp();  // stamp after scan so batch_ts reflects POST time

    g_current_op = "http-post";
    postBatch();
    g_current_op = "";
    maybeSendPendingCrash();

    if (g_pair_mode)
        pairModeStart(g_pair_label);
}

