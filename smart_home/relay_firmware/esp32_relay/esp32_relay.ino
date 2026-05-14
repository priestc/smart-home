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
 * Offline resilience:
 *   - NTP-synced clock (UTC) stamps every scan batch.
 *   - Failed batches buffered in RAM (up to MAX_BUFFER entries); replayed
 *     one-per-cycle when the server is reachable again.
 *   - g_presence_last_seen tracks when each named device was last seen locally.
 *
 * Web log:
 *   - HTTP server on port 80 serves a live activity log (last LOG_LINES lines).
 *   - Access at http://<relay-ip>/ from any browser on the local network.
 *   - Log is in RAM only; does not persist across reboots.
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
#include <WebServer.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <time.h>
#include <stdarg.h>
#include <map>
#include <string>
#include <vector>

#define FIRMWARE_VERSION      "1.3.0"
#define BAUD_RATE              115200
#define SCAN_SECONDS           15
#define POST_INTERVAL_MS       18000UL
#define PROVISION_TIMEOUT_MS   60000UL
#define BOOT_PROBE_MS          3000UL
#define HTTP_TIMEOUT_MS        10000
#define GATT_TIMEOUT_MS        12000
#define MAX_BUFFER             30     // max buffered batches before dropping oldest
#define LOG_LINES              100    // ring buffer depth
#define LOG_LINE_MAX           140    // max chars per log line

// GATT characteristic UUIDs (must match smart_home/pool.py)
static const uint16_t YC01_SVC_UUID16  = 0xFF00;
static const uint16_t YC01_CHAR_UUID16 = 0xFF02;

static char g_ssid[64];
static char g_pass[64];
static char g_url[128];
static char g_token[64];
static char g_id[32];

// ── In-RAM activity log ───────────────────────────────────────────────────────

static char g_log_buf[LOG_LINES][LOG_LINE_MAX];
static int  g_log_head  = 0;
static int  g_log_count = 0;

static void logf(const char* fmt, ...) {
    char msg[LOG_LINE_MAX];
    va_list args;
    va_start(args, fmt);
    vsnprintf(msg, sizeof(msg), fmt, args);
    va_end(args);

    struct tm ti;
    char ts[10] = "--:--:--";
    if (getLocalTime(&ti))
        strftime(ts, sizeof(ts), "%H:%M:%S", &ti);

    snprintf(g_log_buf[g_log_head], LOG_LINE_MAX, "[%s] %s", ts, msg);
    Serial.println(g_log_buf[g_log_head]);
    g_log_head = (g_log_head + 1) % LOG_LINES;
    if (g_log_count < LOG_LINES) g_log_count++;
}

// ── Web server ────────────────────────────────────────────────────────────────

static WebServer g_web(80);

static void handleRoot() {
    String html;
    html.reserve(1024);
    html = "<!DOCTYPE html><html><head>"
           "<meta charset='UTF-8'>"
           "<title>smart-home relay: ";
    html += g_id;
    html += "</title>"
            "<style>"
            "body{font-family:monospace;background:#111;color:#cfc;padding:1em;margin:0}"
            "h2{color:#8f8;margin:0 0 .3em}"
            "p{color:#777;margin:0 0 1em;font-size:.85em}"
            "pre{margin:0;white-space:pre-wrap;word-break:break-all;line-height:1.4}"
            "</style></head><body>"
            "<h2>smart-home relay: ";
    html += g_id;
    html += "</h2>"
            "<p>fw " FIRMWARE_VERSION " &nbsp;&middot;&nbsp; updates every 2&thinsp;s</p>"
            "<pre id='log'>Loading…</pre>"
            "<script>"
            "async function r(){"
            "try{"
            "const t=await fetch('/log');"
            "if(t.ok)document.getElementById('log').textContent=await t.text();"
            "}catch(e){}"
            "}"
            "setInterval(r,2000);r();"
            "</script>"
            "</body></html>";
    g_web.send(200, "text/html; charset=utf-8", html);
}

static void handleLog() {
    String out;
    out.reserve(g_log_count * 90);
    for (int i = 0; i < g_log_count; i++) {
        int idx = (g_log_head - g_log_count + i + LOG_LINES) % LOG_LINES;
        out += g_log_buf[idx];
        out += '\n';
    }
    g_web.send(200, "text/plain; charset=utf-8", out);
}

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

// Busy-wait for `ms` milliseconds while keeping the web server alive.
static void delayServing(unsigned long ms) {
    unsigned long until = millis() + ms;
    while (millis() < until) {
        g_web.handleClient();
        delay(5);
    }
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
        Serial.println();
        logf("WiFi connect failed — will retry");
        return;
    }

    Serial.println();
    logf("WiFi OK: %s", WiFi.localIP().toString().c_str());
    // Modem sleep lets the coexistence manager interleave WiFi and BLE timeslots.
    WiFi.setSleep(true);
    MDNS.begin(WiFi.getHostname());

    // NTP time sync (UTC, no DST offset)
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    Serial.print("NTP sync");
    struct tm ti;
    int ntpTries = 0;
    while (!getLocalTime(&ti) && ntpTries++ < 20) {
        delay(500);
        Serial.print(".");
    }
    Serial.println();
    if (getLocalTime(&ti)) {
        char buf[20];
        strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &ti);
        logf("NTP OK: %s UTC", buf);
    } else {
        logf("NTP FAILED — batch timestamps will be omitted");
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
        logf("DNS OK: %s -> %s", host.c_str(), resolved.toString().c_str());
    else
        logf("DNS FAILED: could not resolve '%s'", host.c_str());

    logf("Server URL: %s", g_url);
}

// ── HTTP POST to /api/ble-relay ───────────────────────────────────────────────

// parse_gatt: if true, parse GATT tasks from the response into g_gatt_tasks.
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
                    logf("GATT task queued: %s  type=%s",
                         gt.address.c_str(), gt.device_type.c_str());
                }
                logf("POST 200 (%d inserted)", rdoc["inserted"].as<int>());
            }
        }
        http.end();
        return true;
    }

    const char* reason = (code == -1) ? "connection refused/no route" :
                         (code == -4) ? "not connected" :
                         (code == -11) ? "read timeout" : "error";
    logf("POST failed: %d (%s)", code, reason);
    http.end();
    return false;
}

// ── Batch payload builder ─────────────────────────────────────────────────────

static String buildPayload(bool include_presence) {
    JsonDocument doc;
    doc["relay_id"] = g_id;
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
        logf("Buffer full — dropped oldest batch");
    }
    g_batch_queue.push_back(payload);
}

static void postBatch() {
    logf("Scan: %u devices  buffer: %u", (unsigned)g_seen.size(), (unsigned)g_batch_queue.size());

    if (!g_batch_queue.empty()) {
        if (httpPost(g_batch_queue.front(), false)) {
            g_batch_queue.erase(g_batch_queue.begin());
            logf("Buffered batch sent, %u remaining", (unsigned)g_batch_queue.size());
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
    logf("GATT result POST -> %d  task=%s  ok=%d",
         code, task_id.c_str(), (int)success);
    http.end();
}

static void processGattTasks() {
    for (auto& task : g_gatt_tasks) {
        logf("GATT: connecting %s  type=%s", task.address.c_str(), task.device_type.c_str());

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
                logf("GATT read OK: %u bytes", (unsigned)val.length());
            } else {
                error_msg = svc ? "characteristic not found" : "service not found";
                logf("GATT error: %s", error_msg.c_str());
            }
            client->disconnect();
        } else {
            error_msg = "connection failed";
            logf("GATT connect failed: %s", task.address.c_str());
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

    connectWiFi();

    // Start web log server
    g_web.on("/",    handleRoot);
    g_web.on("/log", handleLog);
    g_web.begin();
    logf("Web log: http://%s/", WiFi.localIP().toString().c_str());

    String ble_name = String("smart-home_relay_") + g_id;
    BLEDevice::init(ble_name.c_str());
    logf("BLE relay ready  id=%s  fw=" FIRMWARE_VERSION, g_id);
}

void loop() {
    unsigned long cycle_start = millis();

    if (WiFi.status() != WL_CONNECTED) {
        logf("WiFi lost — reconnecting...");
        WiFi.reconnect();
        delayServing(5000);
        return;
    }

    g_seen.clear();
    g_gatt_tasks.clear();
    g_scan_ts = getTimestamp();

    BLEScan* scan = BLEDevice::getScan();
    scan->setAdvertisedDeviceCallbacks(&g_cb, false);
    scan->setActiveScan(false);
    scan->setInterval(160);  // 100 ms — gives WiFi regular radio gaps
    scan->setWindow(80);     // 50 ms active per interval (50% duty cycle)
    scan->start(SCAN_SECONDS, false);
    scan->clearResults();

    postBatch();

    if (!g_gatt_tasks.empty())
        processGattTasks();

    // Wait out the rest of the cycle while serving web requests
    unsigned long elapsed = millis() - cycle_start;
    unsigned long wait = (POST_INTERVAL_MS > elapsed + 3000UL)
                         ? POST_INTERVAL_MS - elapsed
                         : 3000UL;
    delayServing(wait);
}
