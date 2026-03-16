import SwiftUI

struct ContentView: View {
    @AppStorage("serverURL") private var serverURL = ""
    @State private var status: String? = nil
    @State private var isRegistering = false

    var body: some View {
        NavigationView {
            Form {
                Section(header: Text("Server")) {
                    TextField("http://192.168.1.231:5000", text: $serverURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section {
                    Button(action: registerDevice) {
                        if isRegistering {
                            HStack {
                                ProgressView()
                                Text("Registering…").padding(.leading, 8)
                            }
                        } else {
                            Text("Register for Notifications")
                        }
                    }
                    .disabled(serverURL.isEmpty || isRegistering)
                }

                if let status {
                    Section {
                        Text(status)
                            .font(.footnote)
                            .foregroundColor(status.hasPrefix("✓") ? .green : .red)
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
        // Auto-register when a fresh token arrives (e.g. first launch)
        .onReceive(NotificationCenter.default.publisher(for: .apnsTokenReceived)) { _ in
            if !serverURL.isEmpty {
                registerDevice()
            }
        }
    }

    private func registerDevice() {
        guard let token = UserDefaults.standard.string(forKey: "apnsDeviceToken"), !token.isEmpty else {
            status = "No device token yet — make sure notifications are allowed in Settings."
            return
        }

        var urlStr = serverURL.trimmingCharacters(in: .whitespaces)
        if !urlStr.hasPrefix("http") { urlStr = "http://" + urlStr }
        if urlStr.hasSuffix("/") { urlStr = String(urlStr.dropLast()) }

        guard let url = URL(string: "\(urlStr)/api/register-push-token") else {
            status = "Invalid server URL."
            return
        }

        isRegistering = true
        status = nil

        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["token": token])

        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                isRegistering = false
                if let error {
                    status = "Error: \(error.localizedDescription)"
                } else if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    status = "✓ Registered — you'll be notified when you leave home."
                } else {
                    status = "Server error. Check the URL and try again."
                }
            }
        }.resume()
    }
}
