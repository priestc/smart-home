import SwiftUI
import UIKit
import PhotosUI
import Combine

// The app only captures receipts: take a photo, upload it, and show whether the
// server could read it. Browsing, spending summaries and corrections live in the
// web app at /receipts.

private let receiptsAppGroupDefaults = UserDefaults(suiteName: "group.io.github.priestc.SmartHomeNotify")!

private struct ReceiptStatus: Codable {
    let status: String
    let error: String?
    let merchant: String?
    let total: Double?
    let n_items: Int
    let mismatch: Double?
}

private struct ReceiptUploadResponse: Codable { let id: Int }
private struct ReceiptsErrorResponse: Codable { let error: String }

/// One photo sent from this device. Photos the server hasn't got yet are also
/// saved on the phone (see UnsentPhotos), so they come back after a restart.
struct ReceiptUpload: Identifiable {
    let id: UUID
    let thumbnail: UIImage
    let jpeg: Data
    var receiptId: Int?
    var state: State = .uploading(UploadProgress())

    enum State {
        /// Sending the photo.
        case uploading(UploadProgress)
        /// The server never got it; it can be retried.
        case notSent(String)
        /// The server has it and is reading the items.
        case reading
        case read(String)
        case failed(String)
    }
}

// MARK: - Store

@MainActor
final class ReceiptsStore: ObservableObject {
    @Published var uploads: [ReceiptUpload] = []
    /// The server URL that last answered, so "Open in Browser" goes to one that works.
    @Published var reachableBase: String?
    /// Set when a photo couldn't be saved on the phone (it then can't be retried after a restart).
    @Published var saveError: String?

    private let unsent = UnsentPhotos(folder: "receipts")

    init() {
        uploads = unsent.load().reversed().map { saved in
            ReceiptUpload(id: saved.id,
                          thumbnail: UIImage(data: saved.jpeg)?.resizedForUpload(maxDimension: 200) ?? UIImage(),
                          jpeg: saved.jpeg,
                          state: .notSent("Not sent yet. Tap Retry to send it."))
        }
    }

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

    private var candidates: [String] {
        [receiptsAppGroupDefaults.string(forKey: "localURL") ?? "",
         receiptsAppGroupDefaults.string(forKey: "tailscaleURL") ?? ""].compactMap(PhotoUploader.normalizeURL)
    }

    var webURL: URL? {
        (reachableBase ?? candidates.first).flatMap { URL(string: $0 + "/receipts") }
    }

    /// Tries each configured server URL in order (local first, then Tailscale) until one responds.
    /// A reachable server that returns an error status is reported as-is rather than retried.
    private func request(path: String, timeout: TimeInterval = 8) async throws -> Data {
        guard !candidates.isEmpty else { throw ReceiptsError.noServer }

        var lastError: Error = ReceiptsError.unreachable
        for base in candidates {
            guard let url = URL(string: base + path) else { continue }
            let req = URLRequest(url: url, timeoutInterval: timeout)
            let data: Data, response: URLResponse
            do {
                (data, response) = try await URLSession.shared.data(for: req)
            } catch {
                lastError = error
                continue
            }
            guard let http = response as? HTTPURLResponse else { lastError = ReceiptsError.unreachable; continue }
            reachableBase = base
            guard (200..<300).contains(http.statusCode) else {
                let msg = (try? JSONDecoder().decode(ReceiptsErrorResponse.self, from: data))?.error
                throw ReceiptsError.server(msg ?? "Server returned HTTP \(http.statusCode)")
            }
            return data
        }
        throw lastError
    }

    private func setState(_ uploadId: UUID, _ state: ReceiptUpload.State, receiptId: Int? = nil) {
        guard let i = uploads.firstIndex(where: { $0.id == uploadId }) else { return }
        uploads[i].state = state
        if let receiptId { uploads[i].receiptId = receiptId }
    }

    func upload(_ image: UIImage) async {
        let thumb = image.resizedForUpload(maxDimension: 200)
        guard let jpeg = image.resizedForUpload(maxDimension: 2400).jpegData(compressionQuality: 0.8) else {
            uploads.insert(ReceiptUpload(id: UUID(), thumbnail: thumb, jpeg: Data(),
                                         state: .failed(ReceiptsError.badImage.localizedDescription)), at: 0)
            return
        }
        let item = ReceiptUpload(id: UUID(), thumbnail: thumb, jpeg: jpeg)
        uploads.insert(item, at: 0)
        do {
            try unsent.save(item.id, jpeg)
            saveError = nil
        } catch {
            saveError = "Couldn't save a copy of the photo on the phone, so if it fails to send it can only be retried until the app closes: \(error.localizedDescription)"
        }
        await send(item.id)
    }

    func retry(_ uploadId: UUID) async {
        guard let u = uploads.first(where: { $0.id == uploadId }), case .notSent = u.state else { return }
        await send(uploadId)
    }

    /// Throws away a photo that was never sent.
    func discard(_ uploadId: UUID) {
        unsent.remove(uploadId)
        uploads.removeAll { $0.id == uploadId }
    }

    private func send(_ uploadId: UUID) async {
        guard let jpeg = uploads.first(where: { $0.id == uploadId })?.jpeg else { return }
        setState(uploadId, .uploading(UploadProgress()))
        let data: Data
        do {
            let base = try await PhotoUploader.firstReachable(candidates)
            reachableBase = base
            data = try await PhotoUploader.send(jpeg, to: base, path: "/api/receipts") { [weak self] p in
                self?.showProgress(uploadId, p)
            }
        } catch {
            setState(uploadId, .notSent("Upload failed: \(error.localizedDescription)"))
            return
        }
        // The server has it now, so there's nothing left to retry.
        unsent.remove(uploadId)
        guard let rid = try? JSONDecoder().decode(ReceiptUploadResponse.self, from: data).id else {
            setState(uploadId, .failed("Uploaded, but the server's reply didn't include a receipt id. Check the web app."))
            return
        }
        setState(uploadId, .reading, receiptId: rid)
        await waitUntilRead(uploadId, rid)
    }

    private func showProgress(_ uploadId: UUID, _ p: UploadProgress) {
        guard let i = uploads.firstIndex(where: { $0.id == uploadId }),
              case .uploading = uploads[i].state else { return }
        uploads[i].state = .uploading(p)
    }

    /// Polls the receipt until the server has finished reading it (usually 20–40 s).
    private func waitUntilRead(_ uploadId: UUID, _ rid: Int) async {
        var failures = 0
        while true {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            do {
                let s = try JSONDecoder().decode(ReceiptStatus.self, from: try await request(path: "/api/receipts/\(rid)"))
                failures = 0
                switch s.status {
                case "processing":
                    continue
                case "done":
                    var text = s.merchant ?? "Unknown store"
                    if let t = s.total { text += " · " + t.formatted(.currency(code: "USD")) }
                    text += " · \(s.n_items) item\(s.n_items == 1 ? "" : "s")"
                    if s.mismatch != nil { text += " · doesn't add up, check it on the web" }
                    setState(uploadId, .read(text))
                default:
                    setState(uploadId, .failed(s.error ?? "The server couldn't read this receipt"))
                }
                return
            } catch {
                failures += 1
                if failures >= 5 {
                    setState(uploadId, .failed("Uploaded, but couldn't check its status: \(error.localizedDescription)"))
                    return
                }
            }
        }
    }
}

// MARK: - Tab root

struct ReceiptsView: View {
    @StateObject private var store = ReceiptsStore()
    @State private var showCamera = false
    @State private var libraryItem: PhotosPickerItem?
    @State private var showLibrary = false
    @State private var errorMessage: String?

    private var hasCamera: Bool { UIImagePickerController.isSourceTypeAvailable(.camera) }

    var body: some View {
        NavigationStack {
            List {
                if let err = errorMessage ?? store.saveError {
                    Section { Text(err).font(.footnote).foregroundColor(.red) }
                }

                Section {
                    if hasCamera {
                        Button { showCamera = true } label: {
                            Label("Take Photo of Receipt", systemImage: "camera.fill")
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
                    Text("The photo is sent to the server, which reads the items. Review and correct them in the web app.")
                }

                if !store.uploads.isEmpty {
                    Section("Sent") {
                        ForEach(store.uploads) { u in
                            UploadRow(upload: u,
                                      onRetry: { Task { await store.retry(u.id) } },
                                      onDiscard: { store.discard(u.id) })
                        }
                    }
                }

                if let url = store.webURL {
                    Section {
                        Link(destination: url) {
                            Label("Open Receipts in Browser", systemImage: "safari")
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Receipts")
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

private struct UploadRow: View {
    let upload: ReceiptUpload
    let onRetry: () -> Void
    let onDiscard: () -> Void
    @State private var confirmDiscard = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(uiImage: upload.thumbnail)
                .resizable().scaledToFill()
                .frame(width: 44, height: 44)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 4) {
                switch upload.state {
                case .uploading(let progress):
                    Text("Uploading…")
                    UploadProgressBar(progress: progress)
                case .notSent(let msg):
                    Label("Not sent", systemImage: "exclamationmark.triangle.fill").foregroundColor(.red)
                    Text(msg).font(.caption).foregroundColor(.red)
                    HStack {
                        Button(action: onRetry) {
                            Label("Retry", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(.borderedProminent)
                        Button("Discard", role: .destructive) { confirmDiscard = true }
                            .buttonStyle(.bordered)
                    }
                    .font(.caption)
                    .padding(.top, 2)
                case .reading:
                    Text("Reading receipt…")
                    Text("Usually takes 20–40 seconds").font(.caption).foregroundColor(.secondary)
                case .read(let summary):
                    Label("Read", systemImage: "checkmark.circle.fill").foregroundColor(.green)
                    Text(summary).font(.caption).foregroundColor(.secondary)
                case .failed(let msg):
                    Label("Failed", systemImage: "exclamationmark.triangle.fill").foregroundColor(.red)
                    Text(msg).font(.caption).foregroundColor(.red)
                }
            }
            Spacer(minLength: 0)
            if case .reading = upload.state { ProgressView() }
        }
        .confirmationDialog("Discard this receipt photo? It was never sent to the server.",
                            isPresented: $confirmDiscard, titleVisibility: .visible) {
            Button("Discard Photo", role: .destructive, action: onDiscard)
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
