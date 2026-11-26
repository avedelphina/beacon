import SwiftUI

@main
struct BeaconApp: App {
    @State private var session = Session()

    var body: some Scene {
        WindowGroup {
            RootView().environment(session)
        }
    }
}

/// Who is logged in, to which Beacon. The token is in the Keychain; the rest is not secret.
@Observable
final class Session {
    var server: URL? { didSet { UserDefaults.standard.set(server?.absoluteString, forKey: "server") } }
    var email: String? { didSet { UserDefaults.standard.set(email, forKey: "email") } }
    var role: String? { didSet { UserDefaults.standard.set(role, forKey: "role") } }
    var deviceId: String? { didSet { UserDefaults.standard.set(deviceId, forKey: "deviceId") } }
    private(set) var token: String?
    /// Set when this Beacon speaks an API the app does not know: nothing else is shown then.
    var incompatible: String?

    init() {
        let defaults = UserDefaults.standard
        server = defaults.string(forKey: "server").flatMap(URL.init(string:))
        email = defaults.string(forKey: "email")
        role = defaults.string(forKey: "role")
        deviceId = defaults.string(forKey: "deviceId")
        token = Keychain.get("token").flatMap { String(data: $0, encoding: .utf8) }
    }

    var loggedIn: Bool { server != nil && token != nil }
    var client: Client? { server.map { Client(server: $0, token: token) } }

    func loggedIn(_ result: LoginResult, at server: URL) throws {
        try Keychain.set(Data(result.token.utf8), for: "token")
        token = result.token
        self.server = server
        email = result.email
        role = result.role
        deviceId = result.deviceId
    }

    /// Forget the login here. The server side (the device row) is deleted by Settings' log out, or
    /// expires on its own; a 401 means it is gone already.
    func forget() {
        Keychain.delete("token")
        DeviceKey.forget()
        token = nil
        deviceId = nil
        role = nil
    }

    /// Checks that this Beacon's `api` is one the app understands. Called after login and at launch.
    func checkVersion() async {
        guard let client else { return }
        do {
            let version: Version = try await client.get("/api/version")
            incompatible = (version.api ?? 0) == supportedAPI ? nil
                : (version.api ?? 0) > supportedAPI ? "This Beacon (\(version.version)) is newer than the app understands. Update the app."
                : "This Beacon (\(version.version)) is too old for the app. Update Beacon."
        } catch APIError.loggedOut {
            forget()
        } catch {
            // Offline or unreachable: the screens say so when they load.
        }
    }
}

struct RootView: View {
    @Environment(Session.self) private var session

    var body: some View {
        if let problem = session.incompatible {
            ContentUnavailableView("Update needed", systemImage: "arrow.triangle.2.circlepath", description: Text(problem))
        } else if session.loggedIn {
            RequestsSplitView().task { await session.checkVersion() }
        } else {
            LoginView()
        }
    }
}
