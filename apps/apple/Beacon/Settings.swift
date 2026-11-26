import SwiftUI

/// Who is logged in where, this person's devices, and logging out.
struct SettingsView: View {
    @Environment(Session.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var devices: [Device] = []
    @State private var version: Version?
    @State private var problem: String?

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
                    Button("Log out", role: .destructive, action: logOut)
                }
                if let problem { Section { Text(problem).foregroundStyle(.red) } }
            }
            .navigationTitle("Settings")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task {
                guard let client = session.client else { return }
                version = try? await client.get("/api/version")
                let all: [Device] = (try? await client.get("/api/app/devices")) ?? []
                devices = all.filter { $0.email == session.email }
            }
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
            dismiss()
        }
    }
}
