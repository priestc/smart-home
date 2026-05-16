import UIKit
import UserNotifications
import BackgroundTasks

private let heartbeatTaskID = "io.github.priestc.SmartHomeNotify.heartbeat"
private let appGroupDefaults = UserDefaults(suiteName: "group.io.github.priestc.SmartHomeNotify")!

class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            guard granted else { return }
            DispatchQueue.main.async {
                UIApplication.shared.registerForRemoteNotifications()
            }
        }

        BGTaskScheduler.shared.register(forTaskWithIdentifier: heartbeatTaskID, using: nil) { task in
            self.handleHeartbeatTask(task as! BGAppRefreshTask)
        }
        scheduleHeartbeatTask()

        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        UserDefaults.standard.set(token, forKey: "apnsDeviceToken")
        NotificationCenter.default.post(name: .apnsTokenReceived, object: token)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("APNs registration failed: \(error)")
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func scheduleHeartbeatTask() {
        let request = BGAppRefreshTaskRequest(identifier: heartbeatTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private func handleHeartbeatTask(_ task: BGAppRefreshTask) {
        scheduleHeartbeatTask()

        let presenceName = UserDefaults.standard.string(forKey: "presenceName") ?? ""
        let presenceRegistered = UserDefaults.standard.bool(forKey: "presenceRegistered")
        let rawLocalURL = appGroupDefaults.string(forKey: "localURL") ?? ""

        guard presenceRegistered, !presenceName.isEmpty, !rawLocalURL.isEmpty else {
            task.setTaskCompleted(success: true)
            return
        }

        var localURL = rawLocalURL.trimmingCharacters(in: .whitespaces)
        if !localURL.hasPrefix("http") { localURL = "http://" + localURL }
        if localURL.hasSuffix("/") { localURL = String(localURL.dropLast()) }

        guard let url = URL(string: "\(localURL)/api/presence-heartbeat") else {
            task.setTaskCompleted(success: true)
            return
        }

        let body = try? JSONSerialization.data(withJSONObject: ["name": presenceName])
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body

        let dataTask = URLSession.shared.dataTask(with: request) { _, _, _ in
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { dataTask.cancel() }
        dataTask.resume()
    }
}

extension Notification.Name {
    static let apnsTokenReceived = Notification.Name("apnsTokenReceived")
}
