import SwiftUI
import UserNotifications

/// Who is logged in where, this person's devices, and logging out.
struct SettingsView: View {
    @Environment(Session.self) private var session
    @Environment(\.dismiss) private var dismiss
    /// Shown from the gear on a phone, which then needs a way to close it.
    var asSheet = false
    @State private var devices: [Device] = []
    @State private var version: Version?
    @State private var problem: String?
    @State private var notifications: UNAuthorizationStatus?
    @Environment(\.scenePhase) private var phase
    @Environment(\.openURL) private var openURL

    var body: some View {
        NavigationStack {
            Form {
                Section("Logged in") {
                    LabeledContent("Beacon", value: session.server?.host() ?? "-")
                    LabeledContent("Account", value: session.email ?? "-")
                    LabeledContent("Role", value: session.role ?? "-")
                    if let version { LabeledContent("Beacon version", value: version.version) }
                    LabeledContent("App version", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "-")
                }
                Section {
                    ForEach(devices) { device in
                        LabeledContent(device.current ? "\(device.name) (this one)" : device.name,
                                       value: Date(timeIntervalSince1970: device.lastSeenAt).formatted(.relative(presentation: .named)))
                    }
                } header: {
                    Text("Devices")
                } footer: {
                    Text("A device stays logged in until it has not been used for 30 days. Log out a lost one from the web page.")
                }
                Section {
                    LabeledContent("Notifications", value: Self.describe(notifications))
                    if notifications == .denied, let url = URL(string: UIApplication.openNotificationSettingsURLString) {
                        Button("Open notification settings") { openURL(url) }
                    }
                    if let pushProblem = session.pushProblem { Text(pushProblem).foregroundStyle(.red) }
                } footer: {
                    if notifications == .denied {
                        Text("Without them this device hears nothing about requests that wait for you.")
                    }
                }
                Section {
                    Button("Log out", role: .destructive, action: logOut)
                }
                if let problem { Section { Text(problem).foregroundStyle(.red) } }
            }
            .navigationTitle("Settings")
            .toolbar {
                if asSheet { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            }
            .task(id: phase) { notifications = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus }
            .task {
                guard let client = session.client else { return }
                version = try? await client.get("/api/version")
                let all: [Device] = (try? await client.get("/api/app/devices")) ?? []
                devices = all.filter { $0.email == session.email }
            }
        }
    }

    static func describe(_ status: UNAuthorizationStatus?) -> String {
        switch status {
        case .authorized, .provisional, .ephemeral: "On"
        case .denied: "Off"
        case .notDetermined: "Not asked yet"
        default: "-"
        }
    }

    /// Deletes this device on Beacon, then forgets it here. If Beacon cannot be reached, the device is
    /// forgotten here anyway; the token left on Beacon expires after 30 days, or an operator removes it.
    private func logOut() {
        Task {
            if let client = session.client, let id = session.deviceId {
                do {
                    let _: Empty = try await client.send("DELETE", "/api/app/devices/\(id)", body: nil)
                } catch {
                    problem = error.localizedDescription
                }
            }
            session.forget()
        }
    }
}
