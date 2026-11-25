import SwiftUI
import UIKit

// Shared by the Receipts and Products tabs: sends a photo to the server with
// progress, and keeps a copy on disk until the server has it so a failed
// upload (bad airport Wi-Fi, the app being closed) can be retried later.

struct PhotoUploadErrorResponse: Codable { let error: String }

enum PhotoUploadError: LocalizedError {
    case noServer, unreachable(String?), badImage, server(String)
    var errorDescription: String? {
        switch self {
        case .noServer:            return "No server URL configured. Set one in Settings."
        case .unreachable(let why):
            return "Could not reach the smart home server" + (why.map { " (\($0))" } ?? "") + "."
        case .badImage:            return "Could not encode the photo."
        case .server(let msg):     return msg
        }
    }
}

// MARK: - Progress

struct UploadProgress: Equatable {
    var sent: Int64 = 0
    var total: Int64 = 0
    var bytesPerSecond: Double = 0

    /// Still finding which server URL answers.
    var connecting: Bool { total == 0 }
    var fraction: Double { total > 0 ? min(1, Double(sent) / Double(total)) : 0 }

    var detail: String {
        if connecting { return "Connecting to server…" }
        if sent >= total { return "Sent \(Self.bytes(total)) · waiting for the server…" }
        var s = "\(Self.bytes(sent)) of \(Self.bytes(total))"
        if bytesPerSecond > 0 {
            s += " · \(Self.bytes(Int64(bytesPerSecond)))/s"
            let left = Double(total - sent) / bytesPerSecond
            s += left < 60 ? " · \(max(1, Int(left))) s left" : " · \(Int(left / 60)) min left"
        }
        return s
    }

    private static func bytes(_ n: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: n, countStyle: .file)
    }
}

/// Progress bar plus "412 KB of 1.8 MB · 52 KB/s · 27 s left".
struct UploadProgressBar: View {
    let progress: UploadProgress

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if progress.connecting {
                ProgressView().progressViewStyle(.linear)
            } else {
                ProgressView(value: progress.fraction)
            }
            Text(progress.detail)
                .font(.caption).monospacedDigit()
                .foregroundColor(.secondary)
        }
    }
}

/// Reports upload progress, throttled to a few updates a second, with a smoothed speed.
nonisolated private final class ProgressDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let onProgress: @Sendable (UploadProgress) -> Void
    // Only touched from URLSession's serial delegate queue.
    private var lastTime: Date?
    private var lastSent: Int64 = 0
    private var rate: Double = 0

    init(onProgress: @escaping @Sendable (UploadProgress) -> Void) { self.onProgress = onProgress }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        let now = Date()
        let done = totalBytesSent >= totalBytesExpectedToSend
        guard let last = lastTime else {
            lastTime = now
            lastSent = totalBytesSent
            onProgress(UploadProgress(sent: totalBytesSent, total: totalBytesExpectedToSend))
            return
        }
        let dt = now.timeIntervalSince(last)
        guard dt >= 0.25 || done else { return }
        let instant = Double(totalBytesSent - lastSent) / max(dt, 0.001)
        rate = rate == 0 ? instant : rate * 0.7 + instant * 0.3
        lastTime = now
        lastSent = totalBytesSent
        onProgress(UploadProgress(sent: totalBytesSent, total: totalBytesExpectedToSend, bytesPerSecond: rate))
    }
}

// MARK: - Sending

enum PhotoUploader {
    static func normalizeURL(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return nil }
        if !s.hasPrefix("http") { s = "http://" + s }
        if s.hasSuffix("/") { s = String(s.dropLast()) }
        return s
    }

    /// Asks every configured URL at once and returns the first that answers, so an
    /// unreachable home-network address doesn't stall the upload when away from home.
    static func firstReachable(_ candidates: [String]) async throws -> String {
        guard !candidates.isEmpty else { throw PhotoUploadError.noServer }
        return try await withThrowingTaskGroup(of: String?.self) { group in
            var lastError: String?
            for base in candidates {
                group.addTask {
                    guard let url = URL(string: base + "/api/receipts/config") else { return nil }
                    let (_, response) = try await URLSession.shared.data(for: URLRequest(url: url, timeoutInterval: 10))
                    return response is HTTPURLResponse ? base : nil
                }
            }
            while let next = await group.nextResult() {
                switch next {
                case .success(let base?):
                    group.cancelAll()
                    return base
                case .success(nil): continue
                case .failure(let error): lastError = error.localizedDescription
                }
            }
            throw PhotoUploadError.unreachable(lastError)
        }
    }

    /// Sends the JPEG to `path` on `base`, calling `onProgress` on the main actor as bytes go out.
    /// Asks iOS for extra time so switching apps mid-upload doesn't kill it.
    static func send(_ jpeg: Data, to base: String, path: String,
                     onProgress: @escaping @MainActor (UploadProgress) -> Void) async throws -> Data {
        guard let url = URL(string: base + path) else { throw PhotoUploadError.noServer }
        var req = URLRequest(url: url, timeoutInterval: 60)   // idle timeout, not a total limit
        req.httpMethod = "POST"
        req.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")

        let bgTask = UIApplication.shared.beginBackgroundTask(withName: "photo-upload")
        defer { if bgTask != .invalid { UIApplication.shared.endBackgroundTask(bgTask) } }

        let delegate = ProgressDelegate { p in Task { @MainActor in onProgress(p) } }
        let (data, response) = try await URLSession.shared.upload(for: req, from: jpeg, delegate: delegate)
        guard let http = response as? HTTPURLResponse else { throw PhotoUploadError.unreachable(nil) }
        guard (200..<300).contains(http.statusCode) else {
            let msg = (try? JSONDecoder().decode(PhotoUploadErrorResponse.self, from: data))?.error
            throw PhotoUploadError.server(msg ?? "Server returned HTTP \(http.statusCode)")
        }
        return data
    }
}

// MARK: - Saved until sent

/// Photos that haven't reached the server yet, kept in Application Support so
/// they survive the app being closed. Deleted once the server has them.
struct UnsentPhotos {
    let folder: String   // "receipts" or "product-photos"

    /// `info` is what the photo was taken for (e.g. which receipt item), kept beside it.
    struct Saved { let id: UUID; let date: Date; let jpeg: Data; let info: Data? }

    private var dir: URL {
        URL.applicationSupportDirectory.appending(path: "unsent-uploads/\(folder)", directoryHint: .isDirectory)
    }

    private func file(_ id: UUID) -> URL { dir.appending(path: id.uuidString + ".jpg") }
    private func infoFile(_ id: UUID) -> URL { dir.appending(path: id.uuidString + ".json") }

    func save(_ id: UUID, _ jpeg: Data, info: Data? = nil) throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // Written first, so a saved photo is never without what it was taken for
        if let info { try info.write(to: infoFile(id), options: .atomic) }
        try jpeg.write(to: file(id), options: .atomic)
    }

    func remove(_ id: UUID) {
        try? FileManager.default.removeItem(at: file(id))
        try? FileManager.default.removeItem(at: infoFile(id))
    }

    /// Oldest first.
    func load() -> [Saved] {
        let fm = FileManager.default
        guard let urls = try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.creationDateKey]) else {
            return []
        }
        return urls.compactMap { url -> Saved? in
            guard url.pathExtension == "jpg",
                  let id = UUID(uuidString: url.deletingPathExtension().lastPathComponent),
                  let data = try? Data(contentsOf: url) else { return nil }
            let date = (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
            return Saved(id: id, date: date, jpeg: data, info: try? Data(contentsOf: infoFile(id)))
        }
        .sorted { $0.date < $1.date }
    }
}

extension UIImage {
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
