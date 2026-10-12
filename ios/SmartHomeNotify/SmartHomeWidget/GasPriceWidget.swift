import WidgetKit
import SwiftUI

// MARK: - Model

/// Mirrors /api/gas/summary (smart_home/web.py).
struct GasSummary: Codable {
    let fuel_label: String
    let date: String
    let current_avg: Double
    let current_min: Double
    let cheapest_name: String
    let n_stations: Int
    let avg_30d: Double
    let avg_30d_days: Int
    let diff: Double
    let trend: String          // "cheaper" | "pricier" | "same"
    let last_run_error: String?
}

private struct GasSummaryError: Codable {
    let error: String?
}

struct GasPriceEntry: TimelineEntry {
    let date: Date
    let summary: GasSummary?
    let error: String?
}

// MARK: - Provider

struct GasPriceProvider: TimelineProvider {
    private let appGroup = "group.io.github.priestc.SmartHomeNotify"

    func placeholder(in context: Context) -> GasPriceEntry {
        GasPriceEntry(date: Date(), summary: GasSummary(
            fuel_label: "Regular", date: "", current_avg: 4.39, current_min: 4.21,
            cheapest_name: "Eagle Qwik Mart", n_stations: 25, avg_30d: 4.47, avg_30d_days: 30,
            diff: -0.08, trend: "cheaper", last_run_error: nil), error: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (GasPriceEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await fetchEntry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<GasPriceEntry>) -> Void) {
        Task {
            let entry = await fetchEntry()
            // Prices change once a day; retry sooner if the fetch failed.
            let minutes = entry.error == nil ? 60 : 15
            let next = Calendar.current.date(byAdding: .minute, value: minutes, to: Date())!
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    private func normalizeURL(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return nil }
        if !s.hasPrefix("http") { s = "http://" + s }
        if s.hasSuffix("/") { s = String(s.dropLast()) }
        return s
    }

    /// Tries the local URL first, then Tailscale, so the widget works both at home and away.
    private func fetchEntry() async -> GasPriceEntry {
        let defaults = UserDefaults(suiteName: appGroup)
        let candidates = [defaults?.string(forKey: "localURL") ?? "",
                          defaults?.string(forKey: "tailscaleURL") ?? ""].compactMap(normalizeURL)
        guard !candidates.isEmpty else {
            return GasPriceEntry(date: Date(), summary: nil,
                                 error: "No server URL configured. Open Smart Home and enter your server address.")
        }

        var lastError = "Could not reach server"
        for base in candidates {
            guard let url = URL(string: base + "/api/gas/summary?fuel=REGULAR_UNLEADED") else { continue }
            do {
                let (data, response) = try await URLSession.shared.data(for: URLRequest(url: url, timeoutInterval: 8))
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                if status == 200 {
                    let summary = try JSONDecoder().decode(GasSummary.self, from: data)
                    return GasPriceEntry(date: Date(), summary: summary, error: nil)
                }
                // Server reachable but returned an error (e.g. no gas area yet) — report it, don't retry.
                let msg = (try? JSONDecoder().decode(GasSummaryError.self, from: data))?.error
                return GasPriceEntry(date: Date(), summary: nil, error: msg ?? "Server returned HTTP \(status)")
            } catch {
                lastError = "Could not reach server: \(error.localizedDescription)"
            }
        }
        return GasPriceEntry(date: Date(), summary: nil, error: lastError)
    }
}

// MARK: - View

struct GasPriceWidgetView: View {
    @Environment(\.widgetFamily) private var family
    var entry: GasPriceEntry

    private let green = Color(red: 0.165, green: 0.616, blue: 0.431)
    private let red   = Color(red: 0.753, green: 0.224, blue: 0.169)
    private let muted = Color(red: 0.478, green: 0.565, blue: 0.659)

    var body: some View {
        Group {
            if let s = entry.summary {
                content(s)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    header("Regular")
                    Text(entry.error ?? "No data")
                        .font(.system(size: 12))
                        .foregroundColor(.red)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .containerBackground(for: .widget) { Color(.systemBackground) }
    }

    private func color(_ s: GasSummary) -> Color {
        switch s.trend {
        case "cheaper": return green
        case "pricier": return red
        default:        return muted
        }
    }

    private func header(_ fuel: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: "fuelpump.fill")
            Text("Gas · \(fuel)")
        }
        .font(.system(size: 12, weight: .semibold))
        .foregroundColor(muted)
    }

    @ViewBuilder
    private func content(_ s: GasSummary) -> some View {
        let c = color(s)
        let arrow = s.trend == "cheaper" ? "▼" : s.trend == "pricier" ? "▲" : "▬"
        VStack(alignment: .leading, spacing: 2) {
            header(s.fuel_label)
            Text(String(format: "$%.2f", s.current_avg))
                .font(.system(size: family == .systemSmall ? 34 : 40, weight: .bold))
                .monospacedDigit()
                .foregroundColor(c)
                .minimumScaleFactor(0.6)
                .lineLimit(1)
            Text("\(arrow) \(String(format: "$%.2f", abs(s.diff))) vs 30-day")
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(c)
            Text(String(format: "30-day avg $%.2f", s.avg_30d) + (s.avg_30d_days < 30 ? " (\(s.avg_30d_days)d)" : ""))
                .font(.system(size: 11))
                .foregroundColor(muted)
            Spacer(minLength: 0)
            if family != .systemSmall {
                Text(String(format: "Cheapest $%.2f · ", s.current_min) + s.cheapest_name)
                    .font(.system(size: 12))
                    .lineLimit(1)
            }
            if let err = s.last_run_error {
                Text("⚠ \(err)")
                    .font(.system(size: 10))
                    .foregroundColor(.red)
                    .lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Widget

struct GasPriceWidget: Widget {
    let kind = "GasPriceWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: GasPriceProvider()) { entry in
            GasPriceWidgetView(entry: entry)
        }
        .configurationDisplayName("Gas Prices")
        .description("Today's average regular gas price in your area vs. the 30-day average.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
