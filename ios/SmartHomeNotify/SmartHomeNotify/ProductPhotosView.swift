import SwiftUI
import UIKit
import PhotosUI
import Combine

// Product pictures: take a photo of an item and the server's vision model
// matches it to one of the products (from receipts) that has no picture yet.
// If it found no match, or the wrong one, "Change" picks the product by hand.

private let photosAppGroupDefaults = UserDefaults(suiteName: "group.io.github.priestc.SmartHomeNotify")!

struct ProductPhoto: Codable, Identifiable {
    let id: Int
    let uploaded_at: String
    let status: String          // processing, matched, unmatched, error
    let error: String?
    let product_id: Int?
    let product_name: String?
    let match_source: String?   // model or manual
    let model_seen: String?
    let model_notes: String?
}

struct ProductSummary: Codable, Identifiable {
    let id: Int
    let name: String
    let merchants: String?
    let printed: String?
    let photo_id: Int?
    let times_bought: Int
}

private struct PhotoListResponse: Codable { let photos: [ProductPhoto] }
private struct ProductListResponse: Codable { let products: [ProductSummary] }
private struct PhotoUploadResponse: Codable { let id: Int }
private struct PhotoErrorResponse: Codable { let error: String }

/// A photo still being sent from this device.
struct PendingPhotoUpload: Identifiable {
    let id = UUID()
    let thumbnail: UIImage
    var error: String?
}

// MARK: - Store

@MainActor
final class ProductPhotosStore: ObservableObject {
    @Published var photos: [ProductPhoto] = []
    @Published var pending: [PendingPhotoUpload] = []
    @Published var loadError: String?
    /// The server URL that last answered; images are loaded from it.
    @Published var reachableBase: String?
    private var pollTask: Task<Void, Never>?

    enum PhotosError: LocalizedError {
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

    private var candidates: [String] {
        [photosAppGroupDefaults.string(forKey: "localURL") ?? "",
         photosAppGroupDefaults.string(forKey: "tailscaleURL") ?? ""].compactMap(normalizeURL)
    }

    var webURL: URL? {
        (reachableBase ?? candidates.first).flatMap { URL(string: $0 + "/receipts/items") }
    }

    func imageURL(photoId: Int) -> URL? {
        (reachableBase ?? candidates.first).flatMap { URL(string: $0 + "/api/product-photos/\(photoId)/image") }
    }

    /// Tries each configured server URL in order (local first, then Tailscale) until one responds.
    /// A reachable server that returns an error status is reported as-is rather than retried.
    func request(path: String, method: String = "GET", body: Data? = nil,
                 contentType: String? = nil, timeout: TimeInterval = 8) async throws -> Data {
        guard !candidates.isEmpty else { throw PhotosError.noServer }
        var lastError: Error = PhotosError.unreachable
        for base in candidates {
            guard let url = URL(string: base + path) else { continue }
            var req = URLRequest(url: url, timeoutInterval: timeout)
            req.httpMethod = method
            if let body {
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
            guard let http = response as? HTTPURLResponse else { lastError = PhotosError.unreachable; continue }
            reachableBase = base
            guard (200..<300).contains(http.statusCode) else {
                let msg = (try? JSONDecoder().decode(PhotoErrorResponse.self, from: data))?.error
                throw PhotosError.server(msg ?? "Server returned HTTP \(http.statusCode)")
            }
            return data
        }
        throw lastError
    }

    func refresh() async {
        do {
            let data = try await request(path: "/api/product-photos?limit=50")
            photos = try JSONDecoder().decode(PhotoListResponse.self, from: data).photos
            loadError = nil
        } catch {
            loadError = "Could not load photos: \(error.localizedDescription)"
        }
        schedulePollIfNeeded()
    }

    /// While the model is matching a photo (usually 20–40 s), check back every few seconds.
    private func schedulePollIfNeeded() {
        pollTask?.cancel()
        guard photos.contains(where: { $0.status == "processing" }) else { return }
        pollTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }

    func upload(_ image: UIImage) async {
        let item = PendingPhotoUpload(thumbnail: image.photoResized(maxDimension: 200))
        pending.insert(item, at: 0)
        func fail(_ msg: String) {
            if let i = pending.firstIndex(where: { $0.id == item.id }) { pending[i].error = msg }
        }
        guard let jpeg = image.photoResized(maxDimension: 2400).jpegData(compressionQuality: 0.8) else {
            fail(PhotosError.badImage.localizedDescription)
            return
        }
        do {
            let data = try await request(path: "/api/product-photos", method: "POST", body: jpeg,
                                         contentType: "image/jpeg", timeout: 60)
            _ = try JSONDecoder().decode(PhotoUploadResponse.self, from: data)
        } catch {
            fail("Upload failed: \(error.localizedDescription)")
            return
        }
        pending.removeAll { $0.id == item.id }
        await refresh()
    }

    func dismissPending(_ id: UUID) { pending.removeAll { $0.id == id } }

    func products() async throws -> [ProductSummary] {
        try JSONDecoder().decode(ProductListResponse.self, from: try await request(path: "/api/products")).products
    }

    /// Point a photo at another product, or at none (productId nil).
    func setProduct(photoId: Int, productId: Int?) async throws {
        let value: Any = productId.map { $0 as Any } ?? NSNull()
        let body = try JSONSerialization.data(withJSONObject: ["product_id": value])
        let data = try await request(path: "/api/product-photos/\(photoId)/product", method: "POST",
                                     body: body, contentType: "application/json")
        let updated = try JSONDecoder().decode(ProductPhoto.self, from: data)
        if let i = photos.firstIndex(where: { $0.id == photoId }) { photos[i] = updated }
    }

    func retry(photoId: Int) async throws {
        _ = try await request(path: "/api/product-photos/\(photoId)/retry", method: "POST")
        await refresh()
    }

    func delete(photoId: Int) async throws {
        _ = try await request(path: "/api/product-photos/\(photoId)", method: "DELETE")
        photos.removeAll { $0.id == photoId }
    }
}

private extension UIImage {
    func photoResized(maxDimension: CGFloat) -> UIImage {
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

struct ProductPhotosView: View {
    @StateObject private var store = ProductPhotosStore()
    @State private var showCamera = false
    @State private var libraryItem: PhotosPickerItem?
    @State private var showLibrary = false
    @State private var errorMessage: String?
    @State private var changing: ProductPhoto?

    private var hasCamera: Bool { UIImagePickerController.isSourceTypeAvailable(.camera) }

    var body: some View {
        NavigationStack {
            List {
                if let err = errorMessage ?? store.loadError {
                    Section { Text(err).font(.footnote).foregroundColor(.red) }
                }

                Section {
                    if hasCamera {
                        Button { showCamera = true } label: {
                            Label("Take Photo of Product", systemImage: "camera.fill")
                                .font(.headline)
                                .frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .buttonStyle(.borderedProminent)
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                    }
                    Button { showLibrary = true } label: {
                        Label("Choose from Library", systemImage: "photo")
                            .frame(maxWidth: .infinity, minHeight: 36)
                    }
                    .buttonStyle(.bordered)
                    .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                } footer: {
                    Text("The server matches the photo to a product from your receipts that has no picture yet. Tap Change if it picked the wrong one or found none.")
                }

                if !store.pending.isEmpty {
                    Section("Sending") {
                        ForEach(store.pending) { p in
                            PendingRow(upload: p) { store.dismissPending(p.id) }
                        }
                    }
                }

                if !store.photos.isEmpty {
                    Section("Photos") {
                        ForEach(store.photos) { photo in
                            PhotoRow(photo: photo, imageURL: store.imageURL(photoId: photo.id)) {
                                changing = photo
                            }
                            .swipeActions {
                                Button(role: .destructive) {
                                    Task {
                                        do { try await store.delete(photoId: photo.id) }
                                        catch { errorMessage = "Could not delete photo: \(error.localizedDescription)" }
                                    }
                                } label: { Label("Delete", systemImage: "trash") }
                                if photo.status != "processing" {
                                    Button {
                                        Task {
                                            do { try await store.retry(photoId: photo.id) }
                                            catch { errorMessage = "Could not retry: \(error.localizedDescription)" }
                                        }
                                    } label: { Label("Match Again", systemImage: "arrow.clockwise") }
                                    .tint(.blue)
                                }
                            }
                        }
                    }
                }

                if let url = store.webURL {
                    Section {
                        Link(destination: url) {
                            Label("Open Products in Browser", systemImage: "safari")
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Products")
            .refreshable { await store.refresh() }
            .task { await store.refresh() }
            .sheet(item: $changing) { photo in
                ProductPickerSheet(store: store, photo: photo) { changing = nil }
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
                            errorMessage = "Could not load that photo."
                            return
                        }
                        errorMessage = nil
                        await store.upload(image)
                    } catch {
                        errorMessage = "Could not load that photo: \(error.localizedDescription)"
                    }
                }
            }
        }
    }
}

private struct PendingRow: View {
    let upload: PendingPhotoUpload
    let onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(uiImage: upload.thumbnail)
                .resizable().scaledToFill()
                .frame(width: 52, height: 52)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            if let err = upload.error {
                VStack(alignment: .leading, spacing: 2) {
                    Label("Failed", systemImage: "exclamationmark.triangle.fill").foregroundColor(.red)
                    Text(err).font(.caption).foregroundColor(.red)
                }
                Spacer()
                Button("Dismiss", action: onDismiss).font(.caption)
            } else {
                Text("Uploading…")
                Spacer()
                ProgressView()
            }
        }
    }
}

private struct PhotoThumb: View {
    let url: URL?
    var size: CGFloat = 52

    var body: some View {
        AsyncImage(url: url) { phase in
            switch phase {
            case .success(let img): img.resizable().scaledToFill()
            case .failure: Image(systemName: "photo").foregroundColor(.secondary)
            default: ProgressView()
            }
        }
        .frame(width: size, height: size)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct PhotoRow: View {
    let photo: ProductPhoto
    let imageURL: URL?
    let onChange: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            PhotoThumb(url: imageURL)
            VStack(alignment: .leading, spacing: 2) {
                switch photo.status {
                case "processing":
                    Text("Matching…")
                    Text("Usually takes 20–40 seconds").font(.caption).foregroundColor(.secondary)
                case "matched":
                    Text(photo.product_name ?? "Product #\(photo.product_id ?? 0)").font(.body)
                    Text(photo.match_source == "manual" ? "Picked by hand" : "Matched by the model")
                        .font(.caption).foregroundColor(.secondary)
                case "unmatched":
                    Label("No match", systemImage: "questionmark.circle").foregroundColor(.orange)
                    if let seen = photo.model_seen {
                        Text("Looks like: \(seen)").font(.caption).foregroundColor(.secondary)
                    }
                default:
                    Label("Failed", systemImage: "exclamationmark.triangle.fill").foregroundColor(.red)
                    Text(photo.error ?? "Matching failed").font(.caption).foregroundColor(.red)
                }
            }
            Spacer()
            if photo.status == "processing" {
                ProgressView()
            } else {
                Button(photo.status == "matched" ? "Change" : "Pick", action: onChange)
                    .buttonStyle(.bordered)
                    .font(.caption)
            }
        }
    }
}

// MARK: - Picking the product by hand

private struct ProductPickerSheet: View {
    @ObservedObject var store: ProductPhotosStore
    let photo: ProductPhoto
    let onDone: () -> Void

    @State private var products: [ProductSummary] = []
    @State private var loading = true
    @State private var error: String?
    @State private var search = ""
    @State private var saving = false
    @State private var missingOnly = true

    private var shown: [ProductSummary] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return products.filter { p in
            if missingOnly && p.photo_id != nil && p.id != photo.product_id { return false }
            guard !q.isEmpty else { return true }
            return [p.name, p.merchants ?? "", p.printed ?? ""].contains { $0.lowercased().contains(q) }
        }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 12) {
                        PhotoThumb(url: store.imageURL(photoId: photo.id), size: 64)
                        VStack(alignment: .leading, spacing: 2) {
                            if let seen = photo.model_seen {
                                Text("Model saw: \(seen)").font(.caption)
                            }
                            Text("Currently: \(photo.product_name ?? "no product")")
                                .font(.caption).foregroundColor(.secondary)
                        }
                    }
                    Toggle("Only products without a picture", isOn: $missingOnly)
                }
                if let error {
                    Section { Text(error).font(.footnote).foregroundColor(.red) }
                }
                if loading {
                    Section { HStack { Spacer(); ProgressView(); Spacer() } }
                } else {
                    Section {
                        ForEach(shown) { p in
                            Button { choose(p.id) } label: {
                                HStack(spacing: 12) {
                                    if let pid = p.photo_id {
                                        PhotoThumb(url: store.imageURL(photoId: pid), size: 40)
                                    }
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(p.name).foregroundColor(.primary)
                                        let meta = [p.merchants, p.printed].compactMap { $0 }.joined(separator: " · ")
                                        if !meta.isEmpty {
                                            Text(meta).font(.caption).foregroundColor(.secondary).lineLimit(2)
                                        }
                                    }
                                    Spacer()
                                    if p.id == photo.product_id {
                                        Image(systemName: "checkmark").foregroundColor(.accentColor)
                                    }
                                }
                            }
                            .disabled(saving)
                        }
                        if shown.isEmpty {
                            Text(products.isEmpty ? "No products yet. Scan a receipt first." : "No products match.")
                                .foregroundColor(.secondary)
                        }
                    }
                    if photo.product_id != nil {
                        Section {
                            Button("Not any of these", role: .destructive) { choose(nil) }
                                .disabled(saving)
                        }
                    }
                }
            }
            .searchable(text: $search, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "Name, store or printed text")
            .navigationTitle("Which product?")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: onDone) }
                if saving { ToolbarItem(placement: .confirmationAction) { ProgressView() } }
            }
            .task {
                do {
                    products = try await store.products()
                    error = nil
                } catch {
                    self.error = "Could not load products: \(error.localizedDescription)"
                }
                loading = false
            }
        }
    }

    private func choose(_ productId: Int?) {
        saving = true
        Task {
            do {
                try await store.setProduct(photoId: photo.id, productId: productId)
                onDone()
            } catch {
                self.error = "Could not save: \(error.localizedDescription)"
            }
            saving = false
        }
    }
}
