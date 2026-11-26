import SwiftUI
import UniformTypeIdentifiers

/// Moving a password, key or token to or from an agent (backend/secret_pipe.py). Beacon keeps none of
/// it, and neither does the app: a value lives in memory while this sheet is in front, and nowhere else.
/// Both directions are CL4, so each is signed after Face ID.
nonisolated enum Secrets {
    /// Beacon's rule for a secret's name (secret_pipe.NAME_RE), checked here so a typo is caught before Face ID.
    static func isName(_ name: String) -> Bool {
        name.wholeMatch(of: /[A-Za-z0-9][A-Za-z0-9._\-]{0,63}/) != nil
    }

    /// How long a copied value stays on the pasteboard.
    static let clipboardSeconds: TimeInterval = 90

    struct Names: Decodable, Sendable { let names: [String] }
    struct Revealed: Decodable, Sendable { let name: String; let value: String }
    struct Saved: Decodable, Sendable { let path: String }

    struct Paste: Encodable, Sendable {
        let name: String
        let value: String
        let confirm = true  // the person confirmed here; asking Beacon first would cost a second Face ID
    }
}

struct SecretsView: View {
    @Environment(Session.self) private var session
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var phase
    let agent: String

    @State private var names: [String]?
    @State private var revealed: Secrets.Revealed?
    @State private var asking: String?
    @State private var name = ""
    @State private var value = ""
    @State private var working = false
    @State private var saved: String?
    @State private var problem: String?
    @State private var confirmClose = false

    var body: some View {
        NavigationStack {
            Form {
                if let revealed {
                    Section {
                        Text(verbatim: revealed.value).font(.body.monospaced()).privacySensitive()
                        Button("Copy", systemImage: "doc.on.doc") { copy(revealed.value) }
                        Button("Discard", role: .destructive) { self.revealed = nil }
                    } header: {
                        Text(verbatim: revealed.name)
                    } footer: {
                        Text("Shown once: it is no longer on the agent's host, and it is gone when you leave the app. "
                             + "Copy it first; a copy stays on this device for \(Int(Secrets.clipboardSeconds)) seconds.")
                    }
                }
                Section("Left for you") {
                    if let names {
                        if names.isEmpty { Text("Nothing waiting").foregroundStyle(.secondary) }
                        ForEach(names, id: \.self) { waiting in
                            Button { asking = waiting } label: {
                                LabeledContent { Text("Reveal…") } label: { Text(verbatim: waiting) }
                            }
                        }
                    } else if problem == nil {
                        ProgressView()
                    }
                }
                .disabled(working || revealed != nil)  // one at a time: a second would replace the first, for good
                Section {
                    TextField("Name, for example api-key", text: $name)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    // Not a SecureField: that takes one line, and keys have many.
                    TextField("Value", text: $value, axis: .vertical).lineLimit(1...6)
                        .font(.body.monospaced()).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .privacySensitive()
                    PasteButton(payloadType: String.self) { value = $0.first ?? value }
                    Button("Give to \(agent) with \(Biometry.name)", action: put)
                        .disabled(working || !Secrets.isName(name) || value.isEmpty)
                } header: {
                    Text("Give a secret")
                } footer: {
                    if !name.isEmpty && !Secrets.isName(name) {
                        Text("A name is up to 64 letters, digits, dots, dashes or underscores, starting with a letter or digit.")
                    } else {
                        Text("It goes into a file on the agent's host. Beacon does not keep it.")
                    }
                }
                if let saved { Section { Label("Saved as \(saved)", systemImage: "checkmark.circle").foregroundStyle(.green) } }
                if let problem { Section { Text(verbatim: problem).foregroundStyle(.red) } }
            }
            .navigationTitle("Secrets of \(agent)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { if revealed == nil { dismiss() } else { confirmClose = true } }
                }
            }
            .confirmationDialog("Close and lose this secret?", isPresented: $confirmClose, titleVisibility: .visible) {
                Button("Close", role: .destructive) { dismiss() }
            } message: {
                Text("It is no longer on the agent's host, so it cannot be shown again.")
            }
            .confirmationDialog("Reveal this secret?", isPresented: Binding(get: { asking != nil }, set: { if !$0 { asking = nil } }),
                                titleVisibility: .visible, presenting: asking) { waiting in
                Button("Reveal with \(Biometry.name)") { reveal(waiting) }
            } message: { waiting in
                Text(verbatim: "“\(waiting)” is then removed from the agent's host. It cannot be shown again.")
            }
            .task { await load() }
        }
        // A swipe must not throw away a value that cannot be fetched again.
        .interactiveDismissDisabled(revealed != nil)
        // Nothing of this in the app switcher; nothing kept once the app is in the background.
        .overlay { if phase != .active { Rectangle().fill(.background).ignoresSafeArea() } }
        .onChange(of: phase) {
            if phase == .background {
                revealed = nil
                value = ""
            }
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            let found: Secrets.Names = try await client.get("/api/agents/\(agent)/secrets")
            names = found.names
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            names = []
            problem = error.localizedDescription
        }
    }

    private func reveal(_ waiting: String) {
        guard Secrets.isName(waiting) else {
            problem = "That is not a name Beacon would accept."
            return
        }
        signed { client, key in
            revealed = try await client.send("POST", "/api/agents/\(agent)/secrets/\(waiting)/reveal?confirm=true", body: nil,
                                             signedBy: key, reason: "Reveal “\(waiting)” of \(agent)")
            names?.removeAll { $0 == waiting }
        }
    }

    private func put() {
        let paste = Secrets.Paste(name: name, value: value)
        signed { client, key in
            let done: Secrets.Saved = try await client.send("POST", "/api/agents/\(agent)/secrets", json: paste,
                                                            signedBy: key, reason: "Give “\(paste.name)” to \(agent)")
            saved = done.path
            name = ""
            value = ""
        }
    }

    private func signed(_ work: @escaping (Client, DeviceKey) async throws -> Void) {
        guard let client = session.client else { return }
        guard let key = DeviceKey.load() else {
            problem = "This device has no signing key any more. Log out and log in again."
            return
        }
        working = true
        problem = nil
        saved = nil
        Task {
            defer { working = false }
            do {
                try await work(client, key)
            } catch APIError.loggedOut {
                session.forget()
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    /// Not to other devices (Universal Clipboard), and not for long.
    private func copy(_ text: String) {
        UIPasteboard.general.setItems([[UTType.utf8PlainText.identifier: text]],
                                      options: [.localOnly: true, .expirationDate: Date().addingTimeInterval(Secrets.clipboardSeconds)])
    }
}
