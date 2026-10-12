import SwiftUI
import MapKit
import Charts
import Combine

private let gasAppGroupDefaults = UserDefaults(suiteName: "group.io.github.priestc.SmartHomeNotify")!
private let metersPerMile = 1609.344

// MARK: - Models

struct GasArea: Codable, Equatable {
    var lat: Double
    var lon: Double
    var radius_m: Double

    var center: CLLocationCoordinate2D { .init(latitude: lat, longitude: lon) }
}

struct GasHome: Codable {
    let lat: Double
    let lon: Double
}

struct GasRun: Codable {
    let ts: String
    let ok: Int?
    let n_stations: Int?
    let error: String?
}

struct GasConfig: Codable {
    let area: GasArea?
    let home: GasHome?
    let fuel_types: [String]
    let running: Bool
    let last_run: GasRun?
    let last_success: GasRun?
}

struct GasStation: Codable, Identifiable {
    var id: String { place_id }
    let place_id: String
    let name: String
    let address: String?
    let city: String?
    let lat: Double
    let lon: Double
    let price: Double
    let prev_price: Double?
    let price_updated: String?
    let distance_m: Double

    var coordinate: CLLocationCoordinate2D { .init(latitude: lat, longitude: lon) }
}

private struct GasStationsResponse: Codable {
    let stations: [GasStation]
}

struct GasAreaDay: Codable, Identifiable {
    var id: String { date }
    let date: String
    let min: Double
    let avg: Double
    let max: Double
    let n: Int
}

private struct GasAreaHistoryResponse: Codable {
    let days: [GasAreaDay]
}

struct GasPricePoint: Codable, Identifiable {
    var id: String { date }
    let date: String
    let price: Double
}

private struct GasStationHistoryResponse: Codable {
    let history: [String: [GasPricePoint]]
}

private struct GasErrorResponse: Codable {
    let error: String?
}

enum FuelType: String, CaseIterable, Identifiable {
    case regular = "REGULAR_UNLEADED", midgrade = "MIDGRADE", premium = "PREMIUM", diesel = "DIESEL"
    var id: String { rawValue }
    var label: String {
        switch self {
        case .regular: return "Regular"
        case .midgrade: return "Midgrade"
        case .premium: return "Premium"
        case .diesel: return "Diesel"
        }
    }
    static func label(for raw: String) -> String { FuelType(rawValue: raw)?.label ?? raw }
}

private let dayParser: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()

private func parseDay(_ s: String) -> Date { dayParser.date(from: s) ?? .distantPast }

private func fmtPrice(_ p: Double) -> String { String(format: "$%.2f", p) }

/// Green (cheapest) → red (most expensive).
private func priceColor(_ price: Double, lo: Double, hi: Double) -> Color {
    let t = hi > lo ? (price - lo) / (hi - lo) : 0
    return Color(hue: (130 - 130 * t) / 360, saturation: 0.7, brightness: 0.7)
}

// MARK: - Store

@MainActor
final class GasPricesStore: ObservableObject {
    @Published var config: GasConfig?
    @Published var stations: [GasStation] = []
    @Published var areaHistory: [GasAreaDay] = []
    @Published var fuel: FuelType = .regular
    @Published var errorMessage: String?
    @Published var isLoading = false
    @Published var isFetching = false

    enum GasError: LocalizedError {
        case noServer, unreachable, server(String)
        var errorDescription: String? {
            switch self {
            case .noServer:          return "No server URL configured. Set one in Settings."
            case .unreachable:       return "Could not reach the smart home server."
            case .server(let msg):   return msg
            }
        }
    }

    private func normalizeURL(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return nil }
        if !s.hasPrefix("http") { s = "http://" + s }
        if s.hasSuffix("/") { s = String(s.dropLast()) }
        return s
    }

    /// Tries each configured server URL in order (local first, then Tailscale) until one responds.
    /// A reachable server that returns an error status is reported as-is rather than retried.
    private func request(path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        let candidates = [gasAppGroupDefaults.string(forKey: "localURL") ?? "",
                          gasAppGroupDefaults.string(forKey: "tailscaleURL") ?? ""].compactMap(normalizeURL)
        guard !candidates.isEmpty else { throw GasError.noServer }

        var lastError: Error = GasError.unreachable
        for base in candidates {
            guard let url = URL(string: base + path) else { continue }
            var req = URLRequest(url: url, timeoutInterval: 8)
            req.httpMethod = method
            if let body {
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            }
            let data: Data, response: URLResponse
            do {
                (data, response) = try await URLSession.shared.data(for: req)
            } catch {
                lastError = error
                continue
            }
            guard let http = response as? HTTPURLResponse else { lastError = GasError.unreachable; continue }
            guard (200..<300).contains(http.statusCode) else {
                let msg = (try? JSONDecoder().decode(GasErrorResponse.self, from: data))?.error
                throw GasError.server(msg ?? "Server returned HTTP \(http.statusCode)")
            }
            return data
        }
        throw lastError
    }

    func loadAll() async {
        isLoading = true
        defer { isLoading = false }
        do {
            config = try JSONDecoder().decode(GasConfig.self, from: try await request(path: "/api/gas/config"))
            isFetching = config?.running ?? false
            try await loadPrices()
            if let run = config?.last_run, run.ok == 0 {
                errorMessage = "Last price fetch failed: \(run.error ?? "unknown error")"
            } else {
                errorMessage = nil
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func loadPrices() async throws {
        let q = "?fuel=\(fuel.rawValue)"
        async let s = request(path: "/api/gas/stations" + q)
        async let h = request(path: "/api/gas/area-history" + q)
        stations = try JSONDecoder().decode(GasStationsResponse.self, from: try await s).stations
        areaHistory = try JSONDecoder().decode(GasAreaHistoryResponse.self, from: try await h).days
    }

    func changeFuel(_ f: FuelType) async {
        fuel = f
        do { try await loadPrices() } catch { errorMessage = error.localizedDescription }
    }

    func saveArea(_ area: GasArea) async -> Bool {
        do {
            _ = try await request(path: "/api/gas/area", method: "POST",
                                  body: ["lat": area.lat, "lon": area.lon, "radius_m": area.radius_m.rounded()])
            await loadAll()
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    func fetchNow() async {
        do {
            _ = try await request(path: "/api/gas/fetch", method: "POST")
            isFetching = true
            while true {
                try await Task.sleep(nanoseconds: 2_000_000_000)
                let cfg = try JSONDecoder().decode(GasConfig.self, from: try await request(path: "/api/gas/config"))
                if !cfg.running { break }
            }
            isFetching = false
            await loadAll()
        } catch {
            isFetching = false
            errorMessage = error.localizedDescription
        }
    }

    func stationHistory(_ placeId: String) async throws -> [String: [GasPricePoint]] {
        let id = placeId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? placeId
        return try JSONDecoder().decode(GasStationHistoryResponse.self,
                                        from: try await request(path: "/api/gas/station/\(id)/history")).history
    }

    var statusText: String {
        guard let config else { return "" }
        if isFetching { return "Fetching prices…" }
        if config.area == nil { return "Draw your gas area on the map to start tracking prices." }
        if let s = config.last_success { return "Last fetched \(s.ts) · \(s.n_stations ?? 0) stations" }
        return "No prices fetched yet."
    }
}

// MARK: - Tab root

struct GasPricesView: View {
    @StateObject private var store = GasPricesStore()

    var body: some View {
        NavigationStack {
            List {
                if let err = store.errorMessage {
                    Section { Text(err).font(.footnote).foregroundColor(.red) }
                }

                Section {
                    Picker("Fuel", selection: Binding(get: { store.fuel },
                                                      set: { f in Task { await store.changeFuel(f) } })) {
                        ForEach(FuelType.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented)

                    NavigationLink {
                        GasMapView(store: store)
                    } label: {
                        Label(store.config?.area == nil ? "Open Map & Draw Gas Area" : "Open Map", systemImage: "map")
                    }
                } footer: {
                    Text(store.statusText)
                }

                if !store.stations.isEmpty {
                    Section("Area Prices") {
                        GasSummaryRow(stations: store.stations)
                        if store.areaHistory.count > 1 {
                            GasAreaChart(days: store.areaHistory).frame(height: 180)
                        }
                    }
                    Section("Stations (cheapest first)") {
                        ForEach(store.stations) { st in
                            NavigationLink {
                                GasStationDetailView(store: store, station: st)
                            } label: {
                                GasStationRow(station: st)
                            }
                        }
                    }
                } else if store.config?.area != nil && !store.isLoading {
                    Section {
                        Text("No \(store.fuel.label) prices for this area yet.")
                            .foregroundColor(.secondary)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await store.loadAll() }
            .navigationTitle("Gas Prices")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    if store.isFetching || store.isLoading {
                        ProgressView()
                    } else {
                        Button {
                            Task { await store.fetchNow() }
                        } label: {
                            Image(systemName: "arrow.down.circle")
                        }
                        .disabled(store.config?.area == nil)
                        .accessibilityLabel("Fetch prices now")
                    }
                }
            }
        }
        .task { await store.loadAll() }
    }
}

private struct GasSummaryRow: View {
    let stations: [GasStation]

    var body: some View {
        let prices = stations.map(\.price)
        let avg = prices.reduce(0, +) / Double(prices.count)
        HStack {
            stat(fmtPrice(prices.min() ?? 0), "Cheapest", .green)
            Spacer()
            stat(fmtPrice(avg), "Average", .primary)
            Spacer()
            stat(fmtPrice(prices.max() ?? 0), "Highest", .red)
            Spacer()
            stat("\(stations.count)", "Stations", .primary)
        }
        .padding(.vertical, 4)
    }

    private func stat(_ v: String, _ l: String, _ c: Color) -> some View {
        VStack(spacing: 2) {
            Text(v).font(.title3.bold()).foregroundColor(c)
            Text(l).font(.caption2).foregroundColor(.secondary)
        }
    }
}

private struct GasStationRow: View {
    let station: GasStation

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(station.name).font(.body)
                Text("\(station.city ?? "") · \(String(format: "%.1f mi", station.distance_m / metersPerMile))")
                    .font(.caption).foregroundColor(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(fmtPrice(station.price)).font(.body.bold()).monospacedDigit()
                if let prev = station.prev_price, abs(station.price - prev) >= 0.005 {
                    let d = station.price - prev
                    Text("\(d > 0 ? "▲" : "▼") \(String(format: "%.2f", abs(d)))")
                        .font(.caption).foregroundColor(d > 0 ? .red : .green)
                }
            }
        }
    }
}

private struct GasAreaChart: View {
    let days: [GasAreaDay]

    var body: some View {
        Chart {
            ForEach(days) { d in
                AreaMark(x: .value("Date", parseDay(d.date)),
                         yStart: .value("Min", d.min), yEnd: .value("Max", d.max))
                    .foregroundStyle(.blue.opacity(0.12))
                LineMark(x: .value("Date", parseDay(d.date)), y: .value("Average", d.avg))
                    .foregroundStyle(.blue)
            }
        }
        .chartYScale(domain: .automatic(includesZero: false))
        .chartYAxis { AxisMarks { v in
            AxisGridLine()
            AxisValueLabel { if let p = v.as(Double.self) { Text(fmtPrice(p)) } }
        } }
    }
}

// MARK: - Map

struct GasMapView: View {
    @ObservedObject var store: GasPricesStore

    @State private var camera: MapCameraPosition = .automatic
    @State private var drawing = false
    @State private var draft: GasArea?
    @State private var dragStart: CLLocationCoordinate2D?
    @State private var saving = false
    @State private var selected: GasStation?

    private var shownArea: GasArea? { draft ?? store.config?.area }

    var body: some View {
        let prices = store.stations.map(\.price)
        let lo = prices.min() ?? 0, hi = prices.max() ?? 0

        MapReader { proxy in
            Map(position: $camera) {
                if let a = shownArea {
                    MapCircle(center: a.center, radius: a.radius_m)
                        .foregroundStyle(.blue.opacity(0.08))
                        .stroke(.blue, lineWidth: 2)
                }
                if let h = store.config?.home {
                    Annotation("Home", coordinate: .init(latitude: h.lat, longitude: h.lon)) {
                        Image(systemName: "house.fill")
                            .padding(6).background(.white, in: Circle()).foregroundColor(.blue)
                    }
                }
                if !drawing {
                    ForEach(store.stations) { st in
                        Annotation(st.name, coordinate: st.coordinate) {
                            Text(String(format: "%.2f", st.price))
                                .font(.caption.bold()).monospacedDigit()
                                .padding(.horizontal, 5).padding(.vertical, 2)
                                .background(priceColor(st.price, lo: lo, hi: hi), in: RoundedRectangle(cornerRadius: 5))
                                .foregroundColor(.white)
                                .overlay(RoundedRectangle(cornerRadius: 5)
                                    .stroke(selected?.id == st.id ? Color.primary : .white.opacity(0.7),
                                            lineWidth: selected?.id == st.id ? 2 : 1))
                                .onTapGesture { selected = st }
                        }
                        .annotationTitles(.hidden)
                    }
                }
            }
            .mapControls { MapCompass(); MapScaleView() }
            .overlay {
                // While drawing, a transparent layer captures the drag so the map doesn't pan:
                // touch down = circle center, drag distance = radius.
                if drawing {
                    Color.clear.contentShape(Rectangle())
                        .gesture(DragGesture(minimumDistance: 0).onChanged { g in
                            guard let start = dragStart ?? proxy.convert(g.startLocation, from: .local),
                                  let cur = proxy.convert(g.location, from: .local) else { return }
                            dragStart = start
                            let r = CLLocation(latitude: start.latitude, longitude: start.longitude)
                                .distance(from: CLLocation(latitude: cur.latitude, longitude: cur.longitude))
                            draft = GasArea(lat: start.latitude, lon: start.longitude, radius_m: max(200, r))
                        }.onEnded { _ in dragStart = nil })
                }
            }
        }
        .safeAreaInset(edge: .bottom) { bottomBar }
        .navigationTitle(drawing ? "Draw Gas Area" : "Gas Map")
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(drawing)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                if drawing {
                    Button("Save") { Task { await save() } }
                        .disabled(draft == nil || saving)
                } else {
                    Button(store.config?.area == nil ? "Draw Area" : "Redraw") {
                        draft = nil
                        drawing = true
                    }
                }
            }
            if drawing {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button("Cancel") { draft = nil; drawing = false }
                }
            }
        }
        .onAppear { recenter() }
    }

    @ViewBuilder private var bottomBar: some View {
        VStack(spacing: 6) {
            if let err = store.errorMessage {
                Text(err).font(.footnote).foregroundColor(.red)
            }
            if drawing {
                if let d = draft {
                    Text(String(format: "Radius %.1f mi — drag again to redo", d.radius_m / metersPerMile))
                } else {
                    Text("Touch the center of your gas area and drag outward")
                }
            } else if let st = selected {
                NavigationLink {
                    GasStationDetailView(store: store, station: st)
                } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(st.name).font(.headline)
                            Text(st.address ?? st.city ?? "").font(.caption).foregroundColor(.secondary)
                        }
                        Spacer()
                        Text(fmtPrice(st.price)).font(.title3.bold())
                        Image(systemName: "chevron.right").foregroundColor(.secondary)
                    }
                }
                .buttonStyle(.plain)
            } else if store.config?.area == nil {
                Text("Tap “Draw Area” to set your local gas area")
            } else {
                Text("\(store.stations.count) stations · \(store.fuel.label) · tap a price for history")
            }
        }
        .font(.subheadline)
        .padding()
        .frame(maxWidth: .infinity)
        .background(.regularMaterial)
    }

    private func recenter() {
        let center: CLLocationCoordinate2D?
        var radius = 8000.0
        if let a = store.config?.area {
            center = a.center
            radius = a.radius_m
        } else if let h = store.config?.home {
            center = .init(latitude: h.lat, longitude: h.lon)
        } else {
            center = nil
        }
        if let center {
            camera = .region(MKCoordinateRegion(center: center, latitudinalMeters: radius * 2.4,
                                                longitudinalMeters: radius * 2.4))
        }
    }

    private func save() async {
        guard let d = draft else { return }
        saving = true
        defer { saving = false }
        if await store.saveArea(d) {
            draft = nil
            drawing = false
            recenter()
        }
    }
}

// MARK: - Station detail

struct GasStationDetailView: View {
    @ObservedObject var store: GasPricesStore
    let station: GasStation

    @State private var history: [String: [GasPricePoint]] = [:]
    @State private var errorMessage: String?
    @State private var loaded = false

    var body: some View {
        List {
            if let errorMessage {
                Section { Text(errorMessage).font(.footnote).foregroundColor(.red) }
            }
            Section {
                VStack(alignment: .leading, spacing: 4) {
                    Text(station.address ?? station.city ?? "").font(.subheadline).foregroundColor(.secondary)
                    Text(fmtPrice(station.price)).font(.largeTitle.bold())
                    Text(store.fuel.label).font(.caption).foregroundColor(.secondary)
                }
            }
            Section("Price History") {
                if !loaded {
                    ProgressView()
                } else if history.isEmpty {
                    Text("No history yet.").foregroundColor(.secondary)
                } else {
                    Chart {
                        ForEach(history.keys.sorted(), id: \.self) { ft in
                            ForEach(history[ft] ?? []) { p in
                                LineMark(x: .value("Date", parseDay(p.date)), y: .value("Price", p.price))
                                    .interpolationMethod(.stepEnd)
                                    .foregroundStyle(by: .value("Fuel", FuelType.label(for: ft)))
                                    .lineStyle(StrokeStyle(lineWidth: ft == store.fuel.rawValue ? 3 : 1.5))
                            }
                        }
                    }
                    .chartYScale(domain: .automatic(includesZero: false))
                    .frame(height: 220)
                }
            }
            if let pts = history[store.fuel.rawValue], !pts.isEmpty {
                Section("Daily \(store.fuel.label)") {
                    ForEach(pts.reversed()) { p in
                        HStack {
                            Text(p.date)
                            Spacer()
                            Text(fmtPrice(p.price)).monospacedDigit()
                        }
                    }
                }
            }
        }
        .navigationTitle(station.name)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            do {
                history = try await store.stationHistory(station.place_id)
                errorMessage = nil
            } catch {
                errorMessage = error.localizedDescription
            }
            loaded = true
        }
    }
}
