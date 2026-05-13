/*
 * smart-home BLE relay firmware for ESP32
 *
 * Passive BLE scanner + GATT relay on request.
 *
 * Every ~18 s:
 *   1. Passive BLE scan for 15 s — collects all advertisements.
 *   2. POST batch to /api/ble-relay — server returns any pending GATT tasks.
 *   3. For each GATT task: connect to device, read characteristic, POST result.
 *
 * On first boot (or after RESET_CONFIG command on serial): waits for JSON
 * config over serial, stores in NVS, reboots.
 *
 * Provisioned via: smart-home add-relay
 * Build via:       cd relay_firmware && ./build.sh
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
#include <Preferences.h>
#include <ArduinoJson.h>
#include <map>
#include <string>
#include <vector>

#define FIRMWARE_VERSION "1.1.0"
#define BAUD_RATE         115200
#define SCAN_SECONDS      15
#define POST_INTERVAL_MS  18000UL
#define PROVISION_TIMEOUT_MS 60000UL
#define BOOT_PROBE_MS     3000UL
#define HTTP_TIMEOUT_MS   10000
#define GATT_TIMEOUT_MS   12000

// GATT characteristic UUIDs (must match smart_home/pool.py)
static const uint16_t YC01_SVC_UUID16  = 0xFF00;
static const uint16_t YC01_CHAR_UUID16 = 0xFF02;

static char g_ssid[64];
static char g_pass[64];
static char g_url[128];
static char g_token[64];
static char g_id[32];

// ── Advertisement buffer ─────────────────────────────────────────────────────

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
                info.has_mfr    = true;
                info.mfr_company = (uint8_t)mfr[0] | ((uint8_t)mfr[1] << 8);
                info.mfr_hex    = hexEncode((const uint8_t*)mfr.c_str() + 2, mfr.length() - 2);
            }
        }
        if (dev.haveServiceData()) {
            info.has_svc = true;
            String uuid  = dev.getServiceDataUUID().toString().c_str();
            uuid.toLowerCase();
            info.svc_uuid = uuid;
            String svc = dev.getServiceData();
            info.svc_hex = hexEncode((const uint8_t*)svc.c_str(), svc.length());
        }
        if (info.name.length() || info.has_mfr || info.has_svc)
            g_seen[addr] = info;
    }
};

static AdvCallback g_cb;

// ── GATT task queue ───────────────────────────────────────────────────────────

struct GattTask {
    String task_id;
    String address;
    String device_type;
};

static std::vector<GattTask> g_gatt_tasks;

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
        if (line == "RESET_CONFIG") {
            clearConfig();
            Serial.println("CONFIG_CLEARED");
            Serial.flush();
            continue;
        }
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

// ── WiFi ──────────────────────────────────────────────────────────────────────

static void connectWiFi() {
    // Build hostname: "smart-home-relay-patio" (hyphens only — underscores are invalid in DNS)
    String hostname = String("smart-home-relay-") + g_id;
    hostname.replace("_", "-");
    WiFi.setHostname(hostname.c_str());
    WiFi.begin(g_ssid, g_pass);
    Serial.printf("Connecting to WiFi '%s'", g_ssid);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
        delay(500);
        Serial.print(".");
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\nWiFi OK: %s\n", WiFi.localIP().toString().c_str());
        MDNS.begin(WiFi.getHostname());
        Serial.printf("Server URL: %s\n", g_url);
    } else {
        Serial.println("\nWiFi connect failed — will retry");
    }
}

// ── BLE advertisement batch POST ──────────────────────────────────────────────

static void postBatch() {
    Serial.printf("Scan: %u devices\n", (unsigned)g_seen.size());
    if (g_seen.empty()) return;

    JsonDocument doc;
    doc["relay_id"] = g_id;
    JsonArray arr = doc["advertisements"].to<JsonArray>();

    for (auto& kv : g_seen) {
        JsonObject obj = arr.add<JsonObject>();
        obj["address"] = kv.first.c_str();
        if (kv.second.name.length())
            obj["name"] = kv.second.name;
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

    String payload;
    serializeJson(doc, payload);

    HTTPClient http;
    http.begin(String(g_url) + "/api/ble-relay");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + g_token);
    http.setTimeout(HTTP_TIMEOUT_MS);
    int code = http.POST(payload);

    if (code == 200) {
        // Parse response for pending GATT tasks
        String resp = http.getString();
        JsonDocument rdoc;
        if (deserializeJson(rdoc, resp) == DeserializationError::Ok) {
            JsonArray tasks = rdoc["gatt_tasks"];
            for (JsonObject t : tasks) {
                GattTask gt;
                gt.task_id    = t["id"].as<String>();
                gt.address    = t["address"].as<String>();
                gt.device_type = t["device_type"].as<String>();
                g_gatt_tasks.push_back(gt);
                Serial.printf("GATT task queued: %s  type=%s\n",
                              gt.address.c_str(), gt.device_type.c_str());
            }
        }
        Serial.printf("POST 200 (%d inserted)\n", rdoc["inserted"].as<int>());
    } else {
        const char* reason = (code == -1) ? "connection refused/no route" :
                             (code == -4) ? "not connected" :
                             (code == -11) ? "read timeout" : "error";
        Serial.printf("POST failed: %d (%s) url=%s\n", code, reason, g_url);
    }
    http.end();
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

        // Try to connect by address (works even without a prior scan)
        if (client->connect(BLEAddress(task.address.c_str(), BLE_ADDR_TYPE_PUBLIC),
                            BLE_ADDR_TYPE_PUBLIC, GATT_TIMEOUT_MS / 1000)) {

            BLERemoteService*        svc  = nullptr;
            BLERemoteCharacteristic* chr  = nullptr;

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

// ── Arduino entry points ──────────────────────────────────────────────────────

void setup() {
    Serial.begin(BAUD_RATE);
    delay(500);

    // Brief window to accept RESET_CONFIG from the provisioning tool
    unsigned long probe_end = millis() + BOOT_PROBE_MS;
    while (millis() < probe_end) {
        if (Serial.available()) {
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd == "RESET_CONFIG") {
                clearConfig();
                Serial.println("CONFIG_CLEARED");
                Serial.flush();
            }
        }
        delay(50);
    }

    loadConfig();
    if (strlen(g_ssid) == 0) {
        provisionMode();
        while (true) delay(1000);
    }

    connectWiFi();
    String ble_name = String("smart-home_relay_") + g_id;
    BLEDevice::init(ble_name.c_str());
    Serial.printf("BLE relay ready  id=%s  ble=%s  fw=%s\n", g_id, ble_name.c_str(), FIRMWARE_VERSION);
}

void loop() {
    unsigned long cycle_start = millis();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi lost — reconnecting...");
        WiFi.reconnect();
        delay(5000);
        return;
    }

    // ── Passive BLE scan ─────────────────────────────────────────────────────
    g_seen.clear();
    g_gatt_tasks.clear();

    BLEScan* scan = BLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(&g_cb, false);
    scan->setActiveScan(false);
    scan->setInterval(100);
    scan->setWindow(99);
    scan->start(SCAN_SECONDS, false);
    scan->clearResults();

    // ── Post advertisements; receive GATT tasks in response ──────────────────
    postBatch();

    // ── Execute any GATT tasks (scan is stopped, no conflict) ─────────────────
    if (!g_gatt_tasks.empty())
        processGattTasks();

    // ── Wait for next cycle ───────────────────────────────────────────────────
    unsigned long elapsed = millis() - cycle_start;
    const unsigned long MIN_DELAY = 3000UL;
    if (POST_INTERVAL_MS > elapsed + MIN_DELAY)
        delay(POST_INTERVAL_MS - elapsed);
    else
        delay(MIN_DELAY);
}
