import Foundation

/// The `api` this app understands (GET /api/version). Beacon raises it only for a change that breaks
/// the app; the app then refuses to run rather than misread an answer.
nonisolated let supportedAPI = 1

// MARK: - What Beacon answers (shapes pinned by apps/contract/, decoded with snake_case → camelCase)

nonisolated struct AppConfig: Decodable, Sendable {
    let issuer: String
    let clientId: String
    let api: Int
}

nonisolated struct LoginResult: Decodable, Sendable {
    let token: String
    let deviceId: String
    let email: String
    let name: String
    let role: String
}

nonisolated struct Version: Decodable, Sendable {
    let version: String
    let api: Int?
}

nonisolated struct WorkflowRequest: Decodable, Identifiable, Sendable, Hashable {
    let id: Int
    let cardId: Int
    let kind: String           // decision, review, approval, question, handoff
    let clearance: Int
    let state: String          // pending, changes_requested, approved, rejected, answered, expired, stale, ...
    let title: String
    let summary: String
    let requester: String
    let eligibleResponder: String
    let options: [String]
    let expiresAt: Double
    let planRevision: String
    let planHash: String
    let urgentReason: String?
    let consulted: [Consulted]?
    let consultSkipped: String?
    let consultFlag: String?
    let requireHuman: Bool?
    let escalatedReason: String?
    let answeredBy: String?
    let onBehalfOf: String?
    let response: Answer?

    nonisolated struct Consulted: Decodable, Sendable, Hashable {
        let agent: String
        let said: String
    }

    /// What was answered. The server stores whatever the answer held; the app reads the three
    /// fields the page shows and ignores anything else (including an answer that is not an object).
    nonisolated struct Answer: Decodable, Sendable, Hashable {
        var note: String?
        var text: String?
        var option: String?

        init(from decoder: Decoder) throws {
            guard let fields = try? decoder.container(keyedBy: Keys.self) else { return }
            note = try? fields.decode(String.self, forKey: .note)
            text = try? fields.decode(String.self, forKey: .text)
            option = try? fields.decode(String.self, forKey: .option)
        }
        enum Keys: String, CodingKey { case note, text, option }
    }

    /// Who is asked, in words: a person, any agent of a class ("role:reviewer"), or one by name.
    var responderLabel: String {
        if eligibleResponder == "human" { return "a person" }
        if eligibleResponder.hasPrefix("role:") { return "any \(eligibleResponder.dropFirst(5))" }
        return eligibleResponder
    }

    var isOpen: Bool { state == "pending" }
    var needsFaceID: Bool { clearance >= 4 }
}

/// An answer to a workflow request. It names the plan revision and hash of the request it answers:
/// if the requester changed the plan since, Beacon marks the request stale instead of approving
/// something else.
nonisolated struct WorkflowAnswer: Encodable, Sendable {
    let action: String         // approved, rejected, changes_requested, choose_option, answered
    let response: [String: String]
    let planRevision: String
    let planHash: String

    init(_ action: String, _ response: [String: String] = [:], to request: WorkflowRequest) {
        self.action = action
        self.response = response
        planRevision = request.planRevision
        planHash = request.planHash
    }

    enum CodingKeys: String, CodingKey { case action, response, planRevision = "plan_revision", planHash = "plan_hash" }
}

nonisolated struct Card: Decodable, Sendable {
    let id: Int
    let title: String
    let state: String
    let body: String?
    let assignee: String?
    let project: String?
    let createdBy: String
    let nextAction: String?
    let doneWhen: String?
    let references: [Reference]?
    let links: [Link]?
    let blockedReason: String?
    let waitingOn: String?
    let events: [CardEvent]?
    let milestoneId: Int?
    let helpBy: String?          // who is asking for help; nil when none is wanted
    let helpRoles: [String]?
    /// Only in a member's answer: false for someone else's card in a project they take part in.
    let can: Rights?

    nonisolated struct Rights: Decodable, Sendable { let change: Bool }
    var mayChange: Bool { can?.change ?? true }

    nonisolated struct Reference: Decodable, Sendable, Identifiable {
        let id: Int
        let provider: String
        let kind: String
        let externalId: String
        let url: String
        let label: String
        let lastSeenState: String?
    }

    nonisolated struct Link: Decodable, Sendable {
        let url: String
        let label: String?
    }
}

nonisolated struct Device: Decodable, Sendable, Identifiable {
    let id: String
    let email: String
    let name: String
    let lastSeenAt: Double
    let current: Bool
}

// MARK: - Talking to Beacon

nonisolated enum APIError: LocalizedError {
    case loggedOut
    case refused(Int, String)  // Beacon's own words: they say what to do, as in the web page
    case unreadable(String)

    var errorDescription: String? {
        switch self {
        case .loggedOut: "This device is logged out. Log in again."
        case .refused(_, let detail): detail
        case .unreadable(let what): what
        }
    }
}

/// One request at a time to one Beacon. A CL4/CL5 request is signed by the device key after Face ID.
nonisolated struct Client: Sendable {
    let server: URL
    let token: String?

    /// No disk cache and no cookies: what Beacon answers (plans, logs, a revealed secret) is not kept.
    static let session = URLSession(configuration: .ephemeral)

    static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        try await send("GET", path, body: nil)
    }

    /// A POST with no body, which may take Beacon a while: it runs something on a host.
    func post<T: Decodable>(_ path: String, waiting timeout: TimeInterval = 60) async throws -> T {
        try await send("POST", path, body: nil, timeout: timeout)
    }

    func send<T: Decodable>(_ method: String, _ path: String, json: some Encodable,
                            signedBy key: DeviceKey? = nil, reason: String = "") async throws -> T {
        try await send(method, path, body: try JSONEncoder().encode(json), signedBy: key, reason: reason)
    }

    func send<T: Decodable>(_ method: String, _ path: String, body: Data?,
                            signedBy key: DeviceKey? = nil, reason: String = "", timeout: TimeInterval = 60) async throws -> T {
        guard let url = URL(string: path, relativeTo: server) else { throw APIError.unreadable("bad path \(path)") }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.httpBody = body
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let key {
            let signedAt = String(Date().timeIntervalSince1970)
            let message = Signing.requestMessage(method: method, path: path, signedAt: signedAt, body: body ?? Data())
            let signature: Data
            do {
                signature = try await Task.detached { try key.sign(message, reason: reason) }.value
            } catch {
                // The system's own error says little. Either the person cancelled, or the key is gone
                // (`.biometryCurrentSet`: a face or finger added since login destroys it).
                throw APIError.unreadable("Not signed, so nothing was sent. If you did not cancel \(Biometry.name): a face or "
                                          + "finger added since you logged in ends this device's key. Log out and log in again.")
            }
            request.setValue(signedAt, forHTTPHeaderField: "X-Beacon-Signed-At")
            request.setValue(signature.base64EncodedString(), forHTTPHeaderField: "X-Beacon-Signature")
        }
        let (data, response) = try await Self.session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401, token != nil { throw APIError.loggedOut }
        guard (200..<300).contains(status) else {
            let detail = (try? JSONDecoder().decode(Detail.self, from: data))?.detail.text
            throw APIError.refused(status, detail ?? "Beacon answered \(status).")
        }
        do {
            return try Self.decoder.decode(T.self, from: data)
        } catch {
            throw APIError.unreadable("Beacon's answer to \(path) was not what this app expects. Is the app up to date?")
        }
    }

    /// `{"detail": "..."}`, or FastAPI's validation list.
    private struct Detail: Decodable {
        let detail: Text
        enum Text: Decodable {
            case one(String), many([Item])
            struct Item: Decodable { let msg: String }
            init(from decoder: Decoder) throws {
                let value = try decoder.singleValueContainer()
                if let text = try? value.decode(String.self) { self = .one(text) } else { self = .many(try value.decode([Item].self)) }
            }
            var text: String {
                switch self { case .one(let t): t; case .many(let items): items.map(\.msg).joined(separator: "; ") }
            }
        }
    }
}

nonisolated struct Empty: Decodable, Sendable {}
