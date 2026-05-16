import SwiftUI
import UIKit
import WidgetKit

private let appGroupDefaults = UserDefaults(suiteName: "group.io.github.priestc.SmartHomeNotify")!

struct ContentView: View {
    @AppStorage("localURL",     store: appGroupDefaults) private var localURL     = ""
    @AppStorage("tailscaleURL", store: appGroupDefaults) private var tailscaleURL = ""
    @AppStorage("presenceName") private var presenceName = UIDevice.current.name
    @AppStorage("presenceRegistered") private var presenceRegistered = false

    @State private var pushStatus: String? = nil
    @State private var isRegistering = false
    @State private var presenceStatus: String? = nil
    @State private var isRegisteringPresence = false

    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationView {
            Form {
                Section(header: Text("Server"), footer: Text("Local is used when on home WiFi. Tailscale is used when away. Registration is attempted on both.")) {
                    TextField("192.168.1.231:5000", text: $localURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .listRowSeparator(.visible)
                    TextField("100.x.x.x:5000  (Tailscale IP)", text: $tailscaleURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section {
                    Button(action: registerForPush) {
                        if isRegistering {
                            HStack {
                                ProgressView()
                                Text("Registering…").padding(.leading, 8)
                            }
                        } else {
                            Text("Register for Notifications")
                        }
                    }
                    .disabled((localURL.isEmpty && tailscaleURL.isEmpty) || isRegistering)
                }

                if let pushStatus {
                    Section {
                        Text(pushStatus)
                            .font(.footnote)
                            .foregroundColor(pushStatus.hasPrefix("✓") ? .green : .red)
                    }
                }

                Section(
                    header: Text("Presence Detection"),
                    footer: Text("Heartbeats are sent to the local URL only — the server marks you away after 15 minutes without one.")
                ) {
                    TextField("Device name", text: $presenceName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button(action: registerPresenceDevice) {
                        if isRegisteringPresence {
                            HStack {
                                ProgressView()
                                Text("Registering…").padding(.leading, 8)
                            }
                        } else {
                            Text(presenceRegistered ? "Re-register as Presence Device" : "Register as Presence Device")
                        }
                    }
                    .disabled(localURL.isEmpty || presenceName.isEmpty || isRegisteringPresence)
                }

                if let presenceStatus {
                    Section {
                        Text(presenceStatus)
                            .font(.footnote)
                            .foregroundColor(presenceStatus.hasPrefix("✓") ? .green : .red)
                    }
                }

                Section(header: Text("About")) {
                    Text("You'll receive a push notification when the smart home server detects you've left home.")
                        .font(.footnote)
                        .foregroundColor(.secondary)
                }
            }
            .navigationTitle("Smart Home")
        }
        .onReceive(NotificationCenter.default.publisher(for: .apnsTokenReceived)) { _ in
            if !localURL.isEmpty || !tailscaleURL.isEmpty {
                registerForPush()
            }
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active {
                sendHeartbeat()
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

    private func registerForPush() {
        guard let token = UserDefaults.standard.string(forKey: "apnsDeviceToken"), !token.isEmpty else {
            pushStatus = "No device token yet — make sure notifications are allowed in Settings."
            return
        }

        let candidates = [localURL, tailscaleURL].compactMap { normalizeURL($0) }
        guard !candidates.isEmpty else {
            pushStatus = "Enter at least one server URL."
            return
        }

        isRegistering = true
        pushStatus = nil

        let body = try? JSONSerialization.data(withJSONObject: ["token": token])
        let group = DispatchGroup()
        var successes: [String] = []
        var failures:  [String] = []
        let lock = NSLock()

        for urlStr in candidates {
            guard let url = URL(string: "\(urlStr)/api/register-push-token") else {
                lock.lock(); failures.append(urlStr); lock.unlock()
                continue
            }
            var request = URLRequest(url: url, timeoutInterval: 10)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body

            group.enter()
            URLSession.shared.dataTask(with: request) { _, response, error in
                lock.lock()
                if error == nil, let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    successes.append(urlStr)
                } else {
                    failures.append(urlStr)
                }
                lock.unlock()
                group.leave()
            }.resume()
        }

        group.notify(queue: .main) {
            isRegistering = false
            if successes.isEmpty {
                pushStatus = "Could not reach any server. Check URLs and try again."
            } else if successes.count == candidates.count {
                pushStatus = "✓ Registered on all \(successes.count) server URL(s)."
            } else {
                pushStatus = "✓ Registered on \(successes.count) of \(candidates.count) URLs (local may be unreachable when away)."
            }
            WidgetCenter.shared.reloadAllTimelines()
        }
    }

    private func registerPresenceDevice() {
        guard let urlStr = normalizeURL(localURL),
              let url = URL(string: "\(urlStr)/api/register-presence-device") else {
            presenceStatus = "Enter a local server URL first."
            return
        }
        isRegisteringPresence = true
        presenceStatus = nil
        let body = try? JSONSerialization.data(withJSONObject: ["name": presenceName])
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                isRegisteringPresence = false
                if error == nil, let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    presenceStatus = "✓ Registered. Heartbeats will be sent automatically."
                    presenceRegistered = true
                    sendHeartbeat()
                } else {
                    presenceStatus = "Registration failed. Is the local URL reachable?"
                }
            }
        }.resume()
    }

    func sendHeartbeat() {
        guard presenceRegistered, !presenceName.isEmpty,
              let urlStr = normalizeURL(localURL),
              let url = URL(string: "\(urlStr)/api/presence-heartbeat") else { return }
        let body = try? JSONSerialization.data(withJSONObject: ["name": presenceName])
        var request = URLRequest(url: url, timeoutInterval: 5)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { _, _, _ in }.resume()
    }
}
