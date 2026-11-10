import WidgetKit
import SwiftUI

// MARK: - Model

struct SensorReading: Codable, Identifiable {
    var id: String { label ?? address ?? UUID().uuidString }
    let label: String?
    let address: String?
    let temp_f: Double?
    let humidity: Double?
    let ts: String?
}

// Matches the OFFLINE_MS threshold used by the web dashboard (smart_home/web.py).
private let offlineThreshold: TimeInterval = 10 * 60

// The API sends ISO 8601 with the server's UTC offset ("2026-11-10T06:55:00-07:00").
private let sensorTimestampFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter
}()

// Older servers sent naive local time ("2026-11-10 06:55:00").
private let legacySensorTimestampFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
    formatter.timeZone = .current
    return formatter
}()

private func parseSensorTimestamp(_ ts: String) -> Date? {
    sensorTimestampFormatter.date(from: ts) ?? legacySensorTimestampFormatter.date(from: ts)
}

private func isOnline(_ sensor: SensorReading) -> Bool {
    guard let ts = sensor.ts, let date = parseSensorTimestamp(ts) else {
        return false
    }
    return Date().timeIntervalSince(date) < offlineThreshold
}

// MARK: - Timeline Entry

struct SmartHomeEntry: TimelineEntry {
    let date: Date
    let sensors: [SensorReading]
    let error: String?
    let debugURL: String?
}

// MARK: - Provider

struct SmartHomeProvider: TimelineProvider {
    private let appGroup = "group.io.github.priestc.SmartHomeNotify"

    func placeholder(in context: Context) -> SmartHomeEntry {
        SmartHomeEntry(date: Date(), sensors: [
            SensorReading(label: "outside-shade", address: "", temp_f: 72.4, humidity: 45.1, ts: nil),
            SensorReading(label: "inside-office", address: "", temp_f: 75.8, humidity: 36.2, ts: nil),
        ], error: nil, debugURL: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (SmartHomeEntry) -> Void) {
        fetchEntry(completion: completion)
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<SmartHomeEntry>) -> Void) {
        fetchEntry { entry in
            let next = Calendar.current.date(byAdding: .minute, value: 5, to: Date())!
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    private func fetchEntry(completion: @escaping (SmartHomeEntry) -> Void) {
        let defaults = UserDefaults(suiteName: appGroup)
        let localURL     = defaults?.string(forKey: "localURL")     ?? ""
        let tailscaleURL = defaults?.string(forKey: "tailscaleURL") ?? ""

        let raw = localURL.isEmpty ? tailscaleURL : localURL
        let urlString = normalizeURL(raw) + "/api/current"
        guard !raw.isEmpty, let url = URL(string: urlString) else {
            completion(SmartHomeEntry(date: Date(), sensors: [], error: "No server URL configured.\nOpen Smart Home and enter your server address.", debugURL: "local='\(localURL)' tail='\(tailscaleURL)'"))
            return
        }

        let req = URLRequest(url: url, timeoutInterval: 10)
        URLSession.shared.dataTask(with: req) { data, response, error in
            if let data, let sensors = try? JSONDecoder().decode([SensorReading].self, from: data) {
                completion(SmartHomeEntry(date: Date(), sensors: sensors.filter(isOnline), error: nil, debugURL: nil))
            } else {
                let httpStatus = (response as? HTTPURLResponse).map { "HTTP \($0.statusCode)" } ?? "no response"
                let errMsg = error?.localizedDescription ?? "decode failed"
                completion(SmartHomeEntry(date: Date(), sensors: [], error: "Could not reach server", debugURL: "URL: \(urlString)\n\(httpStatus)\n\(errMsg)"))
            }
        }.resume()
    }

    private func normalizeURL(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespaces)
        if !s.hasPrefix("http") { s = "http://" + s }
        if s.hasSuffix("/") { s = String(s.dropLast()) }
        return s
    }
}

// MARK: - Widget View

struct SmartHomeWidgetView: View {
    var entry: SmartHomeEntry

    private let orange  = Color(red: 0.878, green: 0.471, blue: 0.125)
    private let blue    = Color(red: 0.180, green: 0.490, blue: 0.831)
    private let muted   = Color(red: 0.478, green: 0.565, blue: 0.659)
    private let text    = Color(red: 0.102, green: 0.145, blue: 0.208)
    private let bg      = Color(red: 0.941, green: 0.957, blue: 0.973)
    private let divider = Color(red: 0.878, green: 0.910, blue: 0.941)

    var body: some View {
        ZStack {
            bg.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .center) {
                    Text("Smart Home")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundColor(text)
                    Spacer()
                    Text(entry.date, style: .time)
                        .font(.system(size: 11))
                        .foregroundColor(muted)
                }
                .padding(.bottom, 10)

                if let error = entry.error {
                    Text(error)
                        .font(.system(size: 12))
                        .foregroundColor(.red)
                        .fixedSize(horizontal: false, vertical: true)
                    if let dbg = entry.debugURL {
                        Text(dbg)
                            .font(.system(size: 9))
                            .foregroundColor(muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                } else if entry.sensors.isEmpty {
                    Text("No sensors found")
                        .font(.system(size: 12))
                        .foregroundColor(muted)
                    Spacer()
                } else {
                    ForEach(Array(entry.sensors.enumerated()), id: \.element.id) { idx, sensor in
                        if idx > 0 {
                            divider.frame(height: 1).padding(.vertical, 6)
                        }
                        HStack(alignment: .center) {
                            Text((sensor.label ?? sensor.address ?? "?").uppercased())
                                .font(.system(size: 11, weight: .bold))
                                .foregroundColor(muted)
                                .lineLimit(1)
                            Spacer()
                            if let temp = sensor.temp_f {
                                Text(String(format: "%.1f°F", temp))
                                    .font(.system(size: 16, weight: .bold))
                                    .foregroundColor(orange)
                            }
                            if let hum = sensor.humidity {
                                Text(String(format: "%.1f%%", hum))
                                    .font(.system(size: 14))
                                    .foregroundColor(blue)
                                    .padding(.leading, 10)
                            }
                        }
                    }
                    Spacer()
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
        }
    }
}

// MARK: - Widget

struct SmartHomeWidget: Widget {
    let kind = "SmartHomeWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SmartHomeProvider()) { entry in
            SmartHomeWidgetView(entry: entry)
                .containerBackground(Color(red: 0.941, green: 0.957, blue: 0.973), for: .widget)
        }
        .configurationDisplayName("Smart Home")
        .description("Current temperature and humidity for all sensors.")
        .supportedFamilies([.systemMedium, .systemLarge])
    }
}

// MARK: - Preview

#Preview(as: .systemMedium) {
    SmartHomeWidget()
} timeline: {
    SmartHomeEntry(date: Date(), sensors: [
        SensorReading(label: "outside-shade", address: "", temp_f: 72.4, humidity: 45.1, ts: nil),
        SensorReading(label: "outside-sun",   address: "", temp_f: 80.2, humidity: 38.7, ts: nil),
        SensorReading(label: "inside-office", address: "", temp_f: 75.8, humidity: 36.2, ts: nil),
    ], error: nil, debugURL: nil)
}
