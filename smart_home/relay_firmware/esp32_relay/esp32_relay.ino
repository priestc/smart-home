/*
 * smart-home BLE relay firmware for ESP32
 *
 * Active BLE scanner + GATT relay on request.
 *
 * Every ~18 s:
 *   1. Active BLE scan for 15 s — collects advertisements + scan responses.
 *   2. POST batch to /api/ble-relay — server returns any pending GATT/pair tasks.
 *   3. For each GATT task: connect to device, read characteristic, POST result.
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
#include <map>
#include <string>
#include <vector>

#define FIRMWARE_VERSION      "1.5.0"
#define FIRMWARE_REV          5
#define BAUD_RATE              115200
#define SCAN_SECONDS           15
#define POST_INTERVAL_MS       18000UL
#define PROVISION_TIMEOUT_MS   60000UL
#define BOOT_PROBE_MS          3000UL
#define HTTP_TIMEOUT_MS        10000
#define GATT_TIMEOUT_MS        12000
#define MAX_BUFFER             30

// GATT characteristic UUIDs (must match smart_home/pool.py)
static const uint16_t YC01_SVC_UUID16  = 0xFF00;
static const uint16_t YC01_CHAR_UUID16 = 0xFF02;

static char g_ssid[64];
static char g_pass[64];
static char g_url[128];
static char g_token[64];
static char g_id[32];

// ── Advertisement buffer ──────────────────────────────────────────────────────

struct DevInfo {
    String name;
    int8_t rssi;
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

// ── GATT task queue ───────────────────────────────────────────────────────────

struct GattTask {
    String task_id;
    String address;
    String device_type;
};

static std::vector<GattTask> g_gatt_tasks;

// ── Persistent pool monitor state ─────────────────────────────────────────────

static String     g_pool_addr;
static String     g_pool_label;
static BLEClient* g_pool_client  = nullptr;
static int        g_pool_fails   = 0;
#define POOL_OFFLINE_THRESHOLD 2

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
        info.name    = dev.haveName() ? dev.getName().c_str() : "";
        info.rssi    = dev.getRSSI();
        info.has_mfr = false;
        info.has_svc = false;

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
        if (info.name.length() || info.has_mfr || info.has_svc)
            g_seen[addr] = info;

        if (info.name.length() && g_scan_ts.length())
            g_presence_last_seen[info.name.c_str()] = g_scan_ts;
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
    g_pool_addr  = p.getString("pool_addr",  "");
    g_pool_label = p.getString("pool_label", "");
    p.end();
}

static void savePoolMonitor(const String& addr, const String& label) {
    Preferences p;
    p.begin("relay", false);
    p.putString("pool_addr",  addr);
    p.putString("pool_label", label);
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

// ── HTTP POST to /api/ble-relay ───────────────────────────────────────────────

static bool httpPost(const String& payload, bool parse_gatt) {
    HTTPClient http;
    http.begin(String(g_url) + "/api/ble-relay");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(HTTP_TIMEOUT_MS);
    int code = http.POST(const_cast<String&>(payload));

    if (code == 200) {
        if (parse_gatt) {
            String resp = http.getString();
            JsonDocument rdoc;
            if (deserializeJson(rdoc, resp) == DeserializationError::Ok) {
                JsonArray tasks = rdoc["gatt_tasks"];
                for (JsonObject t : tasks) {
                    GattTask gt;
                    gt.task_id     = t["id"].as<String>();
                    gt.address     = t["address"].as<String>();
                    gt.device_type = t["device_type"].as<String>();
                    g_gatt_tasks.push_back(gt);
                    Serial.printf("GATT task queued: %s  type=%s\n",
                                  gt.address.c_str(), gt.device_type.c_str());
                }
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

static String buildPayload(bool include_presence) {
    JsonDocument doc;
    doc["relay_id"] = g_id;
    doc["rev"] = FIRMWARE_REV;
    if (g_scan_ts.length()) doc["batch_ts"] = g_scan_ts;

    JsonArray arr = doc["advertisements"].to<JsonArray>();
    for (auto& kv : g_seen) {
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
        for (auto& kv : g_presence_last_seen)
            pls[kv.first.c_str()] = kv.second;
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

static void postBatch() {
    Serial.printf("Scan: %u devices  buffer: %u\n",
                  (unsigned)g_seen.size(), (unsigned)g_batch_queue.size());

    if (!g_batch_queue.empty()) {
        if (httpPost(g_batch_queue.front(), false)) {
            g_batch_queue.erase(g_batch_queue.begin());
            Serial.printf("Buffered batch sent, %u remaining\n", (unsigned)g_batch_queue.size());
        } else {
            if (!g_seen.empty()) bufferPush(buildPayload(false));
            return;
        }
    }

    if (g_seen.empty()) return;

    if (!httpPost(buildPayload(true), true))
        bufferPush(buildPayload(false));
}

// ── GATT helpers ──────────────────────────────────────────────────────────────

static void postGattResult(const String& task_id, bool success,
                            const String& result_hex, const String& error_msg) {
    JsonDocument doc;
    doc["task_id"] = task_id;
    doc["success"] = success;
    if (success)
        doc["result_hex"] = result_hex;
    else
        doc["error"] = error_msg;

    String payload;
    serializeJson(doc, payload);

    HTTPClient http;
    http.begin(String(g_url) + "/api/ble-relay/gatt-result");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(HTTP_TIMEOUT_MS);
    int code = http.POST(payload);
    Serial.printf("GATT result POST -> %d  task=%s  ok=%d\n",
                  code, task_id.c_str(), (int)success);
    http.end();
}

static void processGattTasks() {
    for (auto& task : g_gatt_tasks) {
        Serial.printf("Processing GATT task: %s  type=%s\n",
                      task.address.c_str(), task.device_type.c_str());

        bool success = false;
        String result_hex;
        String error_msg;

        BLEClient* client = BLEDevice::createClient();
        client->setMTU(23);

        if (client->connect(BLEAddress(task.address.c_str(), BLE_ADDR_TYPE_PUBLIC),
                            BLE_ADDR_TYPE_PUBLIC, GATT_TIMEOUT_MS / 1000)) {

            BLERemoteService*        svc = nullptr;
            BLERemoteCharacteristic* chr = nullptr;

            if (task.device_type == "yc01") {
                svc = client->getService(BLEUUID(YC01_SVC_UUID16));
                if (svc) chr = svc->getCharacteristic(BLEUUID(YC01_CHAR_UUID16));
            }

            if (chr && chr->canRead()) {
                String val = chr->readValue();
                result_hex = hexEncode((const uint8_t*)val.c_str(), val.length());
                success    = true;
                Serial.printf("  GATT read OK: %u bytes\n", (unsigned)val.length());
            } else {
                error_msg = svc ? "characteristic not found" : "service not found";
                Serial.println("  " + error_msg);
            }
            client->disconnect();
        } else {
            error_msg = "connection failed";
            Serial.println("  GATT connect failed");
        }

        delete client;
        postGattResult(task.task_id, success, result_hex, error_msg);
    }
    g_gatt_tasks.clear();
}

// ── Persistent pool monitor ───────────────────────────────────────────────────

static void poolDisconnect() {
    if (g_pool_client && g_pool_client->isConnected()) {
        g_pool_client->disconnect();
        delay(300);
    }
}

// POST a pool reading and return false if the server has cleared the assignment.
static bool postPoolReading(const String& hex) {
    JsonDocument doc;
    doc["relay_id"]   = g_id;
    doc["address"]    = g_pool_addr;
    doc["label"]      = g_pool_label;
    doc["result_hex"] = hex;
    if (g_pool_client && g_pool_client->isConnected()) {
        doc["rssi"] = g_pool_client->getRssi();
    }
    String payload;
    serializeJson(doc, payload);

    HTTPClient http;
    http.begin(String(g_url) + "/api/pool/relay-reading");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(HTTP_TIMEOUT_MS);
    int code = http.POST(payload);

    bool still_assigned = true;
    if (code == 200) {
        String resp = http.getString();
        JsonDocument rdoc;
        if (deserializeJson(rdoc, resp) == DeserializationError::Ok) {
            if (rdoc["pool_monitor"].isNull()) {
                Serial.println("Pool: server cleared assignment — returning to normal mode");
                savePoolMonitor("", "");
                g_pool_addr  = "";
                g_pool_label = "";
                still_assigned = false;
            }
        }
    } else {
        Serial.printf("Pool: reading POST failed: %d\n", code);
    }
    http.end();
    return still_assigned;
}

static void postOfflineStatus() {
    JsonDocument doc;
    doc["relay_id"] = g_id;
    doc["address"]  = g_pool_addr;
    doc["label"]    = g_pool_label;
    doc["offline"]  = true;
    String payload;
    serializeJson(doc, payload);

    HTTPClient http;
    http.begin(String(g_url) + "/api/pool/relay-reading");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(HTTP_TIMEOUT_MS);
    int code = http.POST(payload);
    if (code == 200) {
        String resp = http.getString();
        JsonDocument rdoc;
        if (deserializeJson(rdoc, resp) == DeserializationError::Ok && rdoc["pool_monitor"].isNull()) {
            Serial.println("Pool: server cleared assignment while offline");
            savePoolMonitor("", "");
            g_pool_addr  = "";
            g_pool_label = "";
        }
    }
    http.end();
}

static void doPoolMonitorCycle() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Pool: WiFi lost — reconnecting...");
        WiFi.reconnect();
        delay(5000);
        return;
    }

    // ── Offline/scanning mode ────────────────────────────────────────────────
    if (g_pool_fails >= POOL_OFFLINE_THRESHOLD) {
        Serial.printf("Pool: offline (%d failures) — scanning for device...\n", g_pool_fails);
        g_seen.clear();
        g_scan_ts = getTimestamp();
        BLEScan* scan = BLEDevice::getScan();
        scan->setAdvertisedDeviceCallbacks(&g_cb, false);
        scan->setActiveScan(true);
        scan->setInterval(160);
        scan->setWindow(80);
        scan->start(10, false);
        scan->clearResults();

        // Check if our pool monitor MAC appeared in the scan.
        // g_seen keys are lowercase; g_pool_addr is uppercase from server.
        String addr_lc = g_pool_addr;
        addr_lc.toLowerCase();
        if (g_seen.count(addr_lc.c_str())) {
            Serial.println("Pool: device found advertising — connecting now");
            g_pool_fails = 0;
            // Fall through immediately to connect while it's still in the advertising window.
        } else {
            postOfflineStatus();
            return;
        }
    }

    // ── Normal connected mode ────────────────────────────────────────────────
    if (!g_pool_client) {
        g_pool_client = BLEDevice::createClient();
        g_pool_client->setMTU(23);
    }

    if (!g_pool_client->isConnected()) {
        Serial.printf("Pool: connecting to %s (%s)...\n",
                      g_pool_label.c_str(), g_pool_addr.c_str());
        bool ok = g_pool_client->connect(
            BLEAddress(g_pool_addr.c_str(), BLE_ADDR_TYPE_PUBLIC),
            BLE_ADDR_TYPE_PUBLIC,
            GATT_TIMEOUT_MS / 1000
        );
        if (!ok) {
            g_pool_fails++;
            Serial.printf("Pool: connect failed (%d/%d)\n", g_pool_fails, POOL_OFFLINE_THRESHOLD);
            delay(5000);
            return;
        }
        Serial.printf("Pool: connected to %s\n", g_pool_label.c_str());
        g_pool_fails = 0;
    }

    BLERemoteService* svc = g_pool_client->getService(BLEUUID(YC01_SVC_UUID16));
    if (!svc) {
        Serial.println("Pool: service not found");
        poolDisconnect();
        g_pool_fails++;
        delay(5000);
        return;
    }
    BLERemoteCharacteristic* chr = svc->getCharacteristic(BLEUUID(YC01_CHAR_UUID16));
    if (!chr || !chr->canRead()) {
        Serial.println("Pool: characteristic not found");
        poolDisconnect();
        g_pool_fails++;
        delay(5000);
        return;
    }

    String val = chr->readValue();
    if (val.length() == 0) {
        Serial.println("Pool: empty GATT read — reconnecting");
        poolDisconnect();
        g_pool_fails++;
        delay(5000);
        return;
    }

    g_pool_fails = 0;
    String hex = hexEncode((const uint8_t*)val.c_str(), val.length());
    Serial.printf("Pool: read %u bytes  label=%s\n",
                  (unsigned)val.length(), g_pool_label.c_str());

    bool still_assigned = postPoolReading(hex);
    if (still_assigned) {
        delay(30000);
    }
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

    unsigned long cycle_start = millis();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi lost — reconnecting...");
        WiFi.reconnect();
        delay(5000);
        return;
    }

    g_seen.clear();
    g_gatt_tasks.clear();
    g_scan_ts = getTimestamp();

    BLEScan* scan = BLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(&g_cb, false);
    scan->setActiveScan(true);   // needed to receive scan responses where iPhone advertises its name
    scan->setInterval(160);  // 100 ms — gives WiFi regular radio gaps
    scan->setWindow(80);     // 50 ms active per interval (50% duty cycle)
    scan->start(SCAN_SECONDS, false);
    scan->clearResults();

    postBatch();

    if (!g_gatt_tasks.empty())
        processGattTasks();

    if (g_pair_mode)
        pairModeStart(g_pair_label);

    unsigned long elapsed = millis() - cycle_start;
    const unsigned long MIN_DELAY = 3000UL;
    if (POST_INTERVAL_MS > elapsed + MIN_DELAY)
        delay(POST_INTERVAL_MS - elapsed);
    else
        delay(MIN_DELAY);
}
