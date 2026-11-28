import AVFoundation
import SwiftUI

// Which tone this device plays for which kind of notice. iOS gives apps no access to the system's
// list of tones, so these are the app's own files (Sounds/, made by scripts/make_app_tones.py);
// Beacon names the chosen one in each notification (backend/apns.py). The choice is kept here and
// sent to Beacon again with every registration, since a new login is a new device there.

nonisolated struct SoundChoices: Codable, Sendable, Equatable {
    var requests = "default"
    var urgent = "default"
    var chat = "default"

    private static let key = "sounds"

    static func load(from defaults: UserDefaults = .standard) -> SoundChoices {
        defaults.data(forKey: key).flatMap { try? JSONDecoder().decode(SoundChoices.self, from: $0) } ?? SoundChoices()
    }

    func save(to defaults: UserDefaults = .standard) {
        defaults.set(try? JSONEncoder().encode(self), forKey: Self.key)
    }
}

nonisolated enum Tones {
    /// Beacon's names (backend/app_devices.py); apps/contract/sounds.json pins the files.
    static let files = ["chime", "ping", "knock", "rise", "bell"]
    static let all = ["default", "none"] + files

    static func label(_ tone: String) -> String {
        tone == "default" ? "System default" : tone == "none" ? "None" : tone.capitalized
    }

    static func url(_ tone: String) -> URL? {
        files.contains(tone) ? Bundle.main.url(forResource: tone, withExtension: "wav") : nil
    }
}

struct SoundsSection: View {
    @Environment(Session.self) private var session
    @State private var choices = SoundChoices.load()
    @State private var player: AVAudioPlayer?
    @State private var problem: String?

    var body: some View {
        Section {
            picker("Requests", $choices.requests)
            picker("Urgent", $choices.urgent)
            picker("Chat messages", $choices.chat)
            if let problem { Text(verbatim: problem).foregroundStyle(.red) }
        } header: {
            Text("Sounds")
        } footer: {
            Text("Choosing a tone plays it, unless the phone is on silent. Focus and the silent switch apply to notifications as usual.")
        }
    }

    private func picker(_ title: String, _ choice: Binding<String>) -> some View {
        Picker(title, selection: choice) {
            ForEach(Tones.all, id: \.self) { Text(Tones.label($0)).tag($0) }
        }
        .onChange(of: choice.wrappedValue) {
            play(choice.wrappedValue)
            choices.save()
            Task { await send() }
        }
    }

    private func play(_ tone: String) {
        player = Tones.url(tone).flatMap { try? AVAudioPlayer(contentsOf: $0) }
        player?.play()
    }

    private func send() async {
        do {
            try await session.sendSounds()
            problem = nil
        } catch APIError.refused(404, _) {
            problem = "This Beacon cannot do this yet: until it is updated, notifications play the system's sound."
        } catch {
            problem = "Not saved on Beacon: \(error.localizedDescription)"
        }
    }
}

extension Session {
    func sendSounds() async throws {
        guard let client, loggedIn else { return }
        let _: Empty = try await client.send("PUT", "/api/app/devices/self/sounds", json: SoundChoices.load())
    }
}
