import UIKit
import UserNotifications

/// Notifications from Beacon through APNs (backend/apns.py). A notification holds only the subject;
/// for a workflow request it also carries the request's ids and a category, which decides the actions:
///
/// - `WF_APPROVE` (approval or review, CL3 and below): Approve and Reject right on the notification.
///   They need the phone unlocked, then run in the background without opening the app.
/// - `WF_HUMAN` (CL4/CL5): only "Review…", which opens the app: Face ID cannot be asked for from a
///   notification action, and Beacon refuses those answers without its signature anyway.
/// - `WF_OPEN` (decision, question, handoff): an answer needs a choice or words, so it opens the app.
///
/// The names must be the ones Beacon sends; apps/contract/apns.json pins them for the tests.
nonisolated enum Notices {
    static let approveCategory = "WF_APPROVE", humanCategory = "WF_HUMAN", openCategory = "WF_OPEN"
    static let approveAction = "APPROVE", rejectAction = "REJECT", openAction = "OPEN"

    static func categories() -> Set<UNNotificationCategory> {
        let open = UNNotificationAction(identifier: openAction, title: "Open", options: [.foreground])
        let approve = UNNotificationAction(identifier: approveAction, title: "Approve", options: [.authenticationRequired],
                                           icon: UNNotificationActionIcon(systemImageName: "checkmark"))
        let reject = UNNotificationAction(identifier: rejectAction, title: "Reject", options: [.authenticationRequired, .destructive],
                                          icon: UNNotificationActionIcon(systemImageName: "xmark"))
        let review = UNNotificationAction(identifier: openAction, title: "Review with \(Biometry.name)…", options: [.foreground],
                                          icon: UNNotificationActionIcon(systemImageName: Biometry.name == "Touch ID" ? "touchid" : "faceid"))
        return [
            UNNotificationCategory(identifier: approveCategory, actions: [approve, reject, open], intentIdentifiers: []),
            UNNotificationCategory(identifier: humanCategory, actions: [review], intentIdentifiers: []),
            UNNotificationCategory(identifier: openCategory, actions: [open], intentIdentifiers: []),
        ]
    }

    /// The workflow request a notification is about, if any.
    static func workflowID(_ userInfo: [AnyHashable: Any]) -> Int? {
        (userInfo["beacon"] as? [String: Any])?["workflow"] as? Int
    }

    /// The agent whose secrets a notice is about, if it is one.
    static func secretsAgent(_ userInfo: [AnyHashable: Any]) -> String? {
        (userInfo["beacon"] as? [String: Any])?["agent"] as? String
    }

    static func tokenHex(_ token: Data) -> String { Signing.hex(token) }

    /// Which APNs Beacon must use for this build: Xcode's (debug) builds get sandbox tokens,
    /// TestFlight and App Store builds production ones.
    static var environment: String {
        #if DEBUG
        "sandbox"
        #else
        "production"
        #endif
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    let session = Session()

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories(Notices.categories())
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await session.registerPush(deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        session.pushProblem = error.localizedDescription
    }

    /// In the foreground: show it anyway, and refresh the list, since it is news about it.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        session.refreshes += 1
        return [.banner, .list, .sound]
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let content = response.notification.request.content
        guard let id = Notices.workflowID(content.userInfo) else {
            // Any other notice: the app opens, at what it is about if Beacon said.
            session.openSecrets = Notices.secretsAgent(content.userInfo)
            return
        }
        switch response.actionIdentifier {
        case Notices.approveAction, Notices.rejectAction:
            let result = await session.answerFromNotice(id, approve: response.actionIdentifier == Notices.approveAction)
            let note = UNMutableNotificationContent()
            note.title = result
            note.threadIdentifier = content.threadIdentifier
            try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "answer-\(id)", content: note, trigger: nil))
        default:
            session.openRequest = id
        }
    }
}

extension Session {
    /// Asks once for permission, then registers with APNs each launch (Apple may change the token).
    func startNotifications() async {
        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        if granted { UIApplication.shared.registerForRemoteNotifications() }
    }

    /// Allowed in the system's Settings after the first launch: register without asking again.
    func resumeNotifications() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        if settings.authorizationStatus == .authorized { UIApplication.shared.registerForRemoteNotifications() }
    }

    func registerPush(_ token: Data) async {
        guard let client, loggedIn else { return }
        do {
            let _: Empty = try await client.send("PUT", "/api/app/devices/self/apns",
                                                 json: ["token": Notices.tokenHex(token), "env": Notices.environment])
            pushProblem = nil
        } catch {
            pushProblem = error.localizedDescription
        }
    }

    /// Approve or Reject from the notification. What is approved is the request's plan as Beacon has
    /// it now, by its current revision and hash. Only approvals and reviews up to CL3: anything else
    /// is refused here and sent to the app, even if a notification somehow offered the action.
    func answerFromNotice(_ id: Int, approve: Bool) async -> String {
        guard let client, loggedIn else { return "Log in to Beacon to answer." }
        do {
            let request: WorkflowRequest = try await client.get("/api/workflows/\(id)")
            guard request.isOpen else { return "“\(request.title)” is already \(request.state.replacingOccurrences(of: "_", with: " "))." }
            guard !request.needsFaceID, ["approval", "review"].contains(request.kind) else {
                return "“\(request.title)” needs you in the app: open Beacon to answer."
            }
            let _: Empty = try await client.send("POST", "/api/workflows/\(id)/respond",
                                                 json: WorkflowAnswer(approve ? "approved" : "rejected", to: request))
            refreshes += 1
            return "\(approve ? "Approved" : "Rejected"): “\(request.title)”"
        } catch {
            return "Not answered: \(error.localizedDescription)"
        }
    }
}
