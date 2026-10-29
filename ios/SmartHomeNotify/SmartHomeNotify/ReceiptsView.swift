import SwiftUI
import UIKit
import PhotosUI
import Charts
import Combine

private let receiptsAppGroupDefaults = UserDefaults(suiteName: "group.io.github.priestc.SmartHomeNotify")!

// MARK: - Models

struct ReceiptsConfig: Codable {
    let model: String
    let categories: [String]
    let tax_category: String
}

struct Receipt: Codable, Identifiable {
    let id: Int
    let uploaded_at: String
    let status: String
    let error: String?
    let merchant: String?
    let purchase_date: String?
    let subtotal: Double?
    let tax: Double?
    let total: Double?
    let notes: String?
    let n_items: Int
    let mismatch: Double?
    var items: [ReceiptItem]?

    var title: String {
        switch status {
        case "processing": return "Reading receipt…"
        case "error":      return "Couldn't read receipt"
        default:           return merchant ?? "Unknown store"
        }
    }
    var dateText: String { purchase_date ?? String(uploaded_at.prefix(10)) }
}

struct ReceiptItem: Codable, Identifiable {
    let id: Int
    var description: String
    var quantity: Double?
    var total_price: Double
    var category: String
    /// The line as printed and its item number (e.g. Walmart's 12-digit code);
    /// nil for items added by hand.
    var raw_text: String? = nil
    var item_code: String? = nil

    var printedText: String? {
        let parts = [raw_text, item_code].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

private struct ReceiptsListResponse: Codable { let receipts: [Receipt] }
private struct ReceiptUploadResponse: Codable { let id: Int }
private struct ReceiptsErrorResponse: Codable { let error: String }

struct SpendingRow: Codable, Identifiable {
    let month: String
    let category: String
    let amount: Double
    var id: String { month + category }
}

struct SpendingTotal: Codable, Identifiable {
    let category: String
    let amount: Double
    var id: String { category }
}

struct SpendingSummary: Codable {
    let start: String
    let months: Int
    let n_receipts: Int
    let rows: [SpendingRow]
    let totals: [SpendingTotal]
    let grand_total: Double
}

enum SpendingPeriod: Int, CaseIterable, Identifiable {
    case month = 1, quarter = 3, half = 6, year = 12
    var id: Int { rawValue }
    var label: String {
        switch self {
        case .month: return "This Month"
        case .quarter: return "3 Mo"
        case .half: return "6 Mo"
        case .year: return "12 Mo"
        }
    }
}

private func fmtMoney(_ v: Double) -> String {
    v.formatted(.currency(code: "USD"))
}

private let monthParser: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM"
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()

// MARK: - Store

@MainActor
final class ReceiptsStore: ObservableObject {
    @Published var config: ReceiptsConfig?
    @Published var receipts: [Receipt] = []
    @Published var summary: SpendingSummary?
    @Published var period: SpendingPeriod = .month
    @Published var errorMessage: String?
    @Published var isLoading = false
    @Published var isUploading = false

    private var pollTask: Task<Void, Never>?

    enum ReceiptsError: LocalizedError {
        case noServer, unreachable, badImage, server(String)
        var errorDescription: String? {
            switch self {
            case .noServer:        return "No server URL configured. Set one in Settings."
            case .unreachable:     return "Could not reach the smart home server."
            case .badImage:        return "Could not encode the photo."
            case .server(let msg): return msg
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
    private func request(path: String, method: String = "GET", json: [String: Any]? = nil,
                         body: Data? = nil, contentType: String? = nil,
                         timeout: TimeInterval = 8) async throws -> Data {
        let candidates = [receiptsAppGroupDefaults.string(forKey: "localURL") ?? "",
                          receiptsAppGroupDefaults.string(forKey: "tailscaleURL") ?? ""].compactMap(normalizeURL)
        guard !candidates.isEmpty else { throw ReceiptsError.noServer }

        var lastError: Error = ReceiptsError.unreachable
        for base in candidates {
            guard let url = URL(string: base + path) else { continue }
            var req = URLRequest(url: url, timeoutInterval: timeout)
            req.httpMethod = method
            if let json {
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try? JSONSerialization.data(withJSONObject: json)
            } else if let body {
                req.setValue(contentType, forHTTPHeaderField: "Content-Type")
                req.httpBody = body
            }
            let data: Data, response: URLResponse
            do {
                (data, response) = try await URLSession.shared.data(for: req)
            } catch {
                lastError = error
                continue
            }
            guard let http = response as? HTTPURLResponse else { lastError = ReceiptsError.unreachable; continue }
            guard (200..<300).contains(http.statusCode) else {
                let msg = (try? JSONDecoder().decode(ReceiptsErrorResponse.self, from: data))?.error
                throw ReceiptsError.server(msg ?? "Server returned HTTP \(http.statusCode)")
            }
            return data
        }
        throw lastError
    }

    func loadAll() async {
        isLoading = true
        defer { isLoading = false }
        do {
            async let c = request(path: "/api/receipts/config")
            async let r = request(path: "/api/receipts")
            async let s = request(path: "/api/receipts/summary?months=\(period.rawValue)")
            config = try JSONDecoder().decode(ReceiptsConfig.self, from: try await c)
            receipts = try JSONDecoder().decode(ReceiptsListResponse.self, from: try await r).receipts
            summary = try JSONDecoder().decode(SpendingSummary.self, from: try await s)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        pollIfProcessing()
    }

    func changePeriod(_ p: SpendingPeriod) async {
        period = p
        do {
            summary = try JSONDecoder().decode(SpendingSummary.self,
                                               from: try await request(path: "/api/receipts/summary?months=\(p.rawValue)"))
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// While any receipt is still being read, refresh every few seconds so it flips to done/error on its own.
    private func pollIfProcessing() {
        guard receipts.contains(where: { $0.status == "processing" }), pollTask == nil else { return }
        pollTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard let self else { return }
            self.pollTask = nil
            await self.loadAll()
        }
    }

    func upload(_ image: UIImage) async {
        isUploading = true
        defer { isUploading = false }
        guard let jpeg = image.resizedForUpload(maxDimension: 2400).jpegData(compressionQuality: 0.8) else {
            errorMessage = ReceiptsError.badImage.localizedDescription
            return
        }
        do {
            _ = try await request(path: "/api/receipts", method: "POST", body: jpeg,
                                  contentType: "image/jpeg", timeout: 60)
            await loadAll()
        } catch {
            errorMessage = "Upload failed: \(error.localizedDescription)"
        }
    }

    func detail(_ id: Int) async throws -> Receipt {
        try JSONDecoder().decode(Receipt.self, from: try await request(path: "/api/receipts/\(id)"))
    }

    func image(_ id: Int) async throws -> UIImage? {
        UIImage(data: try await request(path: "/api/receipts/\(id)/image", timeout: 30))
    }

    func retry(_ id: Int) async {
        do {
            _ = try await request(path: "/api/receipts/\(id)/retry", method: "POST")
            await loadAll()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func delete(_ id: Int) async throws {
        _ = try await request(path: "/api/receipts/\(id)", method: "DELETE")
        await loadAll()
    }

    func updateReceipt(_ id: Int, fields: [String: Any]) async throws {
        _ = try await request(path: "/api/receipts/\(id)", method: "PATCH", json: fields)
        await loadAll()
    }

    /// Creates the item when `itemId` is nil, otherwise updates it.
    func saveItem(receiptId: Int, itemId: Int?, fields: [String: Any]) async throws {
        if let itemId {
            _ = try await request(path: "/api/receipts/items/\(itemId)", method: "PATCH", json: fields)
        } else {
            _ = try await request(path: "/api/receipts/\(receiptId)/items", method: "POST", json: fields)
        }
        await loadAll()
    }

    func deleteItem(_ itemId: Int) async throws {
        _ = try await request(path: "/api/receipts/items/\(itemId)", method: "DELETE")
        await loadAll()
    }
}

private extension UIImage {
    func resizedForUpload(maxDimension: CGFloat) -> UIImage {
        let longest = max(size.width, size.height)
        guard longest > maxDimension else { return self }
        let scale = maxDimension / longest
        let newSize = CGSize(width: size.width * scale, height: size.height * scale)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: newSize, format: format).image { _ in
            draw(in: CGRect(origin: .zero, size: newSize))
        }
    }
}

// MARK: - Tab root

struct ReceiptsView: View {
    @StateObject private var store = ReceiptsStore()
    @State private var showCamera = false
    @State private var libraryItem: PhotosPickerItem?
    @State private var showLibrary = false

    var body: some View {
        NavigationStack {
            List {
                if let err = store.errorMessage {
                    Section { Text(err).font(.footnote).foregroundColor(.red) }
                }

                Section {
                    Picker("Period", selection: Binding(get: { store.period },
                                                        set: { p in Task { await store.changePeriod(p) } })) {
                        ForEach(SpendingPeriod.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    if let s = store.summary {
                        SpendingSummaryView(summary: s)
                    }
                } header: {
                    Text("Spending")
                }

                Section("Receipts") {
                    if store.receipts.isEmpty && !store.isLoading {
                        Text("No receipts yet. Tap the camera to scan one.")
                            .foregroundColor(.secondary)
                    }
                    ForEach(store.receipts) { r in
                        NavigationLink {
                            ReceiptDetailView(store: store, receiptId: r.id)
                        } label: {
                            ReceiptRow(receipt: r)
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await store.loadAll() }
            .navigationTitle("Receipts")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    if store.isUploading {
                        ProgressView()
                    } else {
                        Menu {
                            if UIImagePickerController.isSourceTypeAvailable(.camera) {
                                Button { showCamera = true } label: { Label("Take Photo", systemImage: "camera") }
                            }
                            Button { showLibrary = true } label: { Label("Choose from Library", systemImage: "photo") }
                        } label: {
                            Image(systemName: "camera")
                        } primaryAction: {
                            if UIImagePickerController.isSourceTypeAvailable(.camera) { showCamera = true } else { showLibrary = true }
                        }
                        .accessibilityLabel("Scan receipt")
                    }
                }
            }
            .fullScreenCover(isPresented: $showCamera) {
                CameraPicker { image in
                    showCamera = false
                    if let image { Task { await store.upload(image) } }
                }
                .ignoresSafeArea()
            }
            .photosPicker(isPresented: $showLibrary, selection: $libraryItem, matching: .images)
            .onChange(of: libraryItem) { _, item in
                guard let item else { return }
                libraryItem = nil
                Task {
                    do {
                        guard let data = try await item.loadTransferable(type: Data.self),
                              let image = UIImage(data: data) else {
                            store.errorMessage = "Could not load that photo."
                            return
                        }
                        await store.upload(image)
                    } catch {
                        store.errorMessage = "Could not load that photo: \(error.localizedDescription)"
                    }
                }
            }
        }
        .task { await store.loadAll() }
    }
}

private struct ReceiptRow: View {
    let receipt: Receipt

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(receipt.title)
                    .foregroundColor(receipt.status == "error" ? .red : .primary)
                Text(receipt.status == "error" ? (receipt.error ?? "") : "\(receipt.dateText) · \(receipt.n_items) items")
                    .font(.caption).foregroundColor(.secondary).lineLimit(2)
            }
            Spacer()
            if receipt.status == "processing" {
                ProgressView()
            } else if let t = receipt.total {
                if receipt.mismatch != nil {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.orange)
                        .accessibilityLabel("Items don't add up to the total")
                }
                Text(fmtMoney(t)).font(.body.bold()).monospacedDigit()
            }
        }
    }
}

// MARK: - Summary

private struct SpendingSummaryView: View {
    let summary: SpendingSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(fmtMoney(summary.grand_total)).font(.largeTitle.bold()).monospacedDigit()
            Text("\(summary.n_receipts) receipt\(summary.n_receipts == 1 ? "" : "s")")
                .font(.caption).foregroundColor(.secondary)
        }
        .padding(.vertical, 4)

        if summary.months > 1 && !summary.rows.isEmpty {
            Chart(summary.rows) { r in
                BarMark(x: .value("Month", monthParser.date(from: r.month) ?? .distantPast, unit: .month),
                        y: .value("Spent", r.amount))
                    .foregroundStyle(by: .value("Category", r.category))
            }
            .chartYAxis { AxisMarks { v in
                AxisGridLine()
                AxisValueLabel { if let d = v.as(Double.self) { Text(d, format: .currency(code: "USD").precision(.fractionLength(0))) } }
            } }
            .chartLegend(position: .bottom, alignment: .leading)
            .frame(height: 220)
        }

        ForEach(summary.totals) { t in
            HStack {
                Text(t.category)
                Spacer()
                if summary.grand_total > 0 {
                    Text("\(Int((t.amount / summary.grand_total * 100).rounded()))%")
                        .font(.caption).foregroundColor(.secondary)
                }
                Text(fmtMoney(t.amount)).monospacedDigit().frame(minWidth: 80, alignment: .trailing)
            }
        }
    }
}

// MARK: - Detail

private let isoDayFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()

private func parseMoney(_ s: String) -> Double? {
    Double(s.replacingOccurrences(of: "$", with: "").replacingOccurrences(of: ",", with: "")
            .trimmingCharacters(in: .whitespaces))
}

private func moneyText(_ v: Double?) -> String {
    v.map { String(format: "%.2f", $0) } ?? ""
}

/// Which editor sheet is open: the receipt header, an existing item, or a new item.
private enum ReceiptEditor: Identifiable {
    case header, item(ReceiptItem), newItem
    var id: String {
        switch self {
        case .header: return "header"
        case .item(let i): return "item-\(i.id)"
        case .newItem: return "new"
        }
    }
}

struct ReceiptDetailView: View {
    @ObservedObject var store: ReceiptsStore
    let receiptId: Int
    @Environment(\.dismiss) private var dismiss

    @State private var receipt: Receipt?
    @State private var image: UIImage?
    @State private var errorMessage: String?
    @State private var showImage = false
    @State private var confirmDelete = false
    @State private var editor: ReceiptEditor?

    var body: some View {
        List {
            if let err = errorMessage {
                Section { Text(err).font(.footnote).foregroundColor(.red) }
            }
            if let r = receipt {
                if let diff = r.mismatch, let total = r.total {
                    Section {
                        Label {
                            Text("Items + tax come to \(fmtMoney(total + diff)), but the receipt total is \(fmtMoney(total)). A line was probably misread; check the items against the photo.")
                                .font(.footnote)
                        } icon: {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.orange)
                        }
                    }
                }

                Section {
                    LabeledContent("Store", value: r.merchant ?? "—")
                    LabeledContent("Date", value: r.dateText)
                    if let s = r.subtotal { LabeledContent("Subtotal", value: fmtMoney(s)) }
                    LabeledContent("Tax", value: r.tax.map(fmtMoney) ?? "—")
                    LabeledContent("Total", value: r.total.map(fmtMoney) ?? "—").bold()
                    if r.status == "error" {
                        Text(r.error ?? "Unknown error").foregroundColor(.red)
                        Button("Retry") { Task { await store.retry(receiptId); await load() } }
                    }
                    if let n = r.notes { Text(n).font(.footnote).foregroundColor(.secondary) }
                } header: {
                    HStack {
                        Text("Receipt")
                        Spacer()
                        if r.status == "done" { Button("Edit") { editor = .header }.font(.subheadline) }
                    }
                }

                if r.status == "done" {
                    Section {
                        ForEach(r.items ?? []) { item in
                            Button { editor = .item(item) } label: { itemRow(item) }
                                .foregroundColor(.primary)
                        }
                        .onDelete { offsets in
                            let ids = offsets.compactMap { r.items?[$0].id }
                            Task { await deleteItems(ids) }
                        }
                        Button { editor = .newItem } label: { Label("Add Item", systemImage: "plus") }
                    } header: {
                        Text("Items")
                    } footer: {
                        Text("Tap an item to correct it. Swipe left to delete.")
                    }
                }
            }

            if let image {
                Section("Photo") {
                    Image(uiImage: image)
                        .resizable().scaledToFit()
                        .frame(maxHeight: 300)
                        .onTapGesture { showImage = true }
                }
            }

            Section {
                Button("Delete Receipt", role: .destructive) { confirmDelete = true }
            }
        }
        .navigationTitle(receipt?.title ?? "Receipt")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .confirmationDialog("Delete this receipt?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                Task {
                    do { try await store.delete(receiptId); dismiss() }
                    catch { errorMessage = error.localizedDescription }
                }
            }
        }
        .sheet(isPresented: $showImage) {
            if let image {
                NavigationStack {
                    ScrollView([.vertical, .horizontal]) {
                        Image(uiImage: image).resizable().scaledToFit()
                    }
                    .navigationTitle("Photo")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) { Button("Done") { showImage = false } }
                    }
                }
            }
        }
        .sheet(item: $editor) { which in
            NavigationStack {
                switch which {
                case .header:
                    if let r = receipt {
                        ReceiptHeaderEditor(receipt: r, image: image) { fields in
                            try await store.updateReceipt(receiptId, fields: fields)
                            await load()
                        }
                    }
                case .item(let item):
                    ReceiptItemEditor(item: item, categories: store.config?.categories ?? [], image: image) { fields in
                        try await store.saveItem(receiptId: receiptId, itemId: item.id, fields: fields)
                        await load()
                    }
                case .newItem:
                    ReceiptItemEditor(item: nil, categories: store.config?.categories ?? [], image: image) { fields in
                        try await store.saveItem(receiptId: receiptId, itemId: nil, fields: fields)
                        await load()
                    }
                }
            }
        }
        .task { await load() }
    }

    private func itemRow(_ item: ReceiptItem) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text(item.description)
                if let printed = item.printedText, printed != item.description {
                    Text(printed).font(.caption.monospaced()).foregroundColor(.secondary)
                }
                HStack(spacing: 6) {
                    Text(item.category).font(.caption)
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(Capsule().fill(Color.accentColor.opacity(0.15)))
                    if let q = item.quantity, q != 1 {
                        Text("× \(q.formatted())").font(.caption).foregroundColor(.secondary)
                    }
                }
            }
            Spacer()
            Text(fmtMoney(item.total_price)).monospacedDigit()
                .foregroundColor(item.total_price < 0 ? .green : .primary)
        }
    }

    private func deleteItems(_ ids: [Int]) async {
        do {
            for id in ids { try await store.deleteItem(id) }
        } catch {
            errorMessage = error.localizedDescription
        }
        await load()
    }

    private func load() async {
        do {
            receipt = try await store.detail(receiptId)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        if image == nil {
            do { image = try await store.image(receiptId) }
            catch { errorMessage = "Could not load photo: \(error.localizedDescription)" }
        }
    }
}

/// Small zoomable copy of the receipt photo shown above the editors, so lines can be checked while typing.
private struct ReceiptPhotoReference: View {
    let image: UIImage?

    var body: some View {
        if let image {
            Section {
                ScrollView([.vertical, .horizontal]) {
                    Image(uiImage: image).resizable().scaledToFit().frame(width: 600)
                }
                .frame(height: 220)
            }
        }
    }
}

private struct ReceiptHeaderEditor: View {
    let receipt: Receipt
    let image: UIImage?
    let onSave: ([String: Any]) async throws -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var merchant = ""
    @State private var hasDate = true
    @State private var date = Date()
    @State private var subtotal = ""
    @State private var tax = ""
    @State private var total = ""
    @State private var errorMessage: String?
    @State private var saving = false

    var body: some View {
        Form {
            ReceiptPhotoReference(image: image)
            if let err = errorMessage {
                Section { Text(err).font(.footnote).foregroundColor(.red) }
            }
            Section {
                TextField("Store", text: $merchant)
                Toggle("Date on receipt", isOn: $hasDate)
                if hasDate { DatePicker("Date", selection: $date, displayedComponents: .date) }
            }
            Section {
                moneyField("Subtotal", $subtotal)
                moneyField("Tax", $tax)
                moneyField("Total", $total)
            } footer: {
                Text("Leave a field empty if it isn't on the receipt.")
            }
        }
        .navigationTitle("Edit Receipt")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                if saving { ProgressView() } else { Button("Save") { Task { await save() } } }
            }
        }
        .onAppear {
            merchant = receipt.merchant ?? ""
            if let d = receipt.purchase_date.flatMap(isoDayFormatter.date(from:)) { date = d } else { hasDate = false }
            subtotal = moneyText(receipt.subtotal)
            tax = moneyText(receipt.tax)
            total = moneyText(receipt.total)
        }
    }

    private func moneyField(_ label: String, _ text: Binding<String>) -> some View {
        LabeledContent(label) {
            TextField("0.00", text: text).keyboardType(.decimalPad).multilineTextAlignment(.trailing)
        }
    }

    private func save() async {
        var fields: [String: Any] = [
            "merchant": merchant,
            "purchase_date": hasDate ? isoDayFormatter.string(from: date) : NSNull(),
        ]
        for (key, text) in [("subtotal", subtotal), ("tax", tax), ("total", total)] {
            if text.trimmingCharacters(in: .whitespaces).isEmpty {
                fields[key] = NSNull()
            } else if let v = parseMoney(text) {
                fields[key] = v
            } else {
                errorMessage = "\(key.capitalized) isn't a valid amount."
                return
            }
        }
        saving = true
        defer { saving = false }
        do {
            try await onSave(fields)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct ReceiptItemEditor: View {
    let item: ReceiptItem?
    let categories: [String]
    let image: UIImage?
    let onSave: ([String: Any]) async throws -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var description = ""
    @State private var price = ""
    @State private var quantity = ""
    @State private var category = "Other"
    @State private var errorMessage: String?
    @State private var saving = false

    var body: some View {
        Form {
            ReceiptPhotoReference(image: image)
            if let err = errorMessage {
                Section { Text(err).font(.footnote).foregroundColor(.red) }
            }
            Section {
                if let printed = item?.printedText {
                    LabeledContent("On receipt") { Text(printed).font(.callout.monospaced()) }
                }
                TextField("Description", text: $description)
                LabeledContent("Price") {
                    TextField("0.00", text: $price).keyboardType(.numbersAndPunctuation).multilineTextAlignment(.trailing)
                }
                LabeledContent("Quantity") {
                    TextField("optional", text: $quantity).keyboardType(.decimalPad).multilineTextAlignment(.trailing)
                }
                Picker("Category", selection: $category) {
                    ForEach(categories, id: \.self) { Text($0).tag($0) }
                }
            } footer: {
                Text("Enter discounts and coupons as a negative price, e.g. -1.50.")
            }
        }
        .navigationTitle(item == nil ? "Add Item" : "Edit Item")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                if saving { ProgressView() } else { Button("Save") { Task { await save() } } }
            }
        }
        .onAppear {
            guard let item else { return }
            description = item.description
            price = moneyText(item.total_price)
            quantity = item.quantity.map { $0.formatted() } ?? ""
            category = item.category
        }
    }

    private func save() async {
        guard !description.trimmingCharacters(in: .whitespaces).isEmpty else {
            errorMessage = "Enter a description."
            return
        }
        guard let p = parseMoney(price) else {
            errorMessage = "Enter a valid price."
            return
        }
        var fields: [String: Any] = ["description": description, "total_price": p, "category": category]
        if quantity.trimmingCharacters(in: .whitespaces).isEmpty {
            fields["quantity"] = NSNull()
        } else if let q = Double(quantity) {
            fields["quantity"] = q
        } else {
            errorMessage = "Quantity isn't a number."
            return
        }
        saving = true
        defer { saving = false }
        do {
            try await onSave(fields)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Camera

struct CameraPicker: UIViewControllerRepresentable {
    let onFinish: (UIImage?) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onFinish: (UIImage?) -> Void
        init(onFinish: @escaping (UIImage?) -> Void) { self.onFinish = onFinish }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            onFinish(info[.originalImage] as? UIImage)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            onFinish(nil)
        }
    }
}
