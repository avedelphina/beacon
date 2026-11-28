import SwiftUI
import UserNotifications

@main
struct BeaconApp: App {
    // The delegate owns the session: notifications arrive there, before or without any view.
    @UIApplicationDelegateAdaptor private var delegate: AppDelegate

    var body: some Scene {
        WindowGroup {
            RootView().environment(delegate.session)
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
    /// A request to show, from a tapped notification.
    var openRequest: Int?
    /// An agent whose secrets to show, from a tapped notification ("alpha has a secret for you").
    var openSecrets: String?
    /// A project to show, from a tapped notification.
    var openProject: Int?
    /// A conversation to show, from a tapped notification ("alpha sent you a message").
    var openChat: ChatTarget?
    /// Cards to show, from a tapped notification ("2 board cards not moving").
    var openCards: [Int]?
    /// What waits on this person (GET /api/app/badge): the app icon's number and the tabs'.
    var waiting = Waiting()
    /// Bumped when something changed the requests (a notice arrived, an answer from a notification).
    var refreshes = 0
    var pushProblem: String?

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
        // A logged-out phone shows nothing of Beacon's: no more notices, none left on the lock screen.
        UIApplication.shared.unregisterForRemoteNotifications()
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
        UNUserNotificationCenter.current().setBadgeCount(0)
        waiting = Waiting()
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
            MainView().task {
                await session.checkVersion()
                await session.startNotifications()
            }
        } else {
            LoginView()
        }
    }
}

/// A tab bar on iPhone; on iPad the same tabs can be a sidebar.
struct MainView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase
    @Environment(\.horizontalSizeClass) private var width
    @State private var tab = Tabs.requests

    enum Tabs: Hashable { case requests, chats, agents, board, projects, settings }

    var body: some View {
        TabView(selection: $tab) {
            Tab("Requests", systemImage: "tray", value: Tabs.requests) { RequestsSplitView() }
                .badge(session.waiting.requests)
            Tab("Chats", systemImage: "bubble.left.and.bubble.right", value: Tabs.chats) { ChatsView() }
                .badge(session.waiting.chats)
            Tab("Agents", systemImage: "cpu", value: Tabs.agents) { AgentsView() }
            Tab("Board", systemImage: "rectangle.stack", value: Tabs.board) { BoardView() }
            Tab("Projects", systemImage: "flag", value: Tabs.projects) { ProjectsView() }
            // A phone's tab bar holds five: there Settings is the gear on Requests.
            if width != .compact {
                Tab("Settings", systemImage: "gearshape", value: Tabs.settings) { SettingsView() }
            }
        }
        .tabViewStyle(.sidebarAdaptable)
        .onChange(of: phase) {
            if phase == .active { Task { await session.resumeNotifications() } }
            Task { await session.refreshBadge() }
        }
        // At launch, after a notice, and when moving between tabs (something may have been read or answered).
        .task(id: "\(session.refreshes)-\(tab)") { await session.refreshBadge() }
        // A tapped notification is about a request, whichever tab was open.
        .onChange(of: session.openRequest, initial: true) { if session.openRequest != nil { tab = .requests } }
        .onChange(of: session.openSecrets, initial: true) { if session.openSecrets != nil { tab = .agents } }
        .onChange(of: session.openProject, initial: true) { if session.openProject != nil { tab = .projects } }
        .onChange(of: session.openChat, initial: true) { if session.openChat != nil { tab = .chats } }
        .onChange(of: session.openCards, initial: true) { if session.openCards != nil { tab = .board } }
    }
}
