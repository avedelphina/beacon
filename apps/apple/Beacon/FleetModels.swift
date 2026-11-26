import Foundation

// Agents and the board (shapes pinned by apps/contract/). A member gets trimmed answers
// (backend/members.py), so what only an operator sees is optional here.

nonisolated struct Agent: Decodable, Identifiable, Sendable, Hashable {
    let id: String
    let type: String
    let host: String
    let profile: String?
    let owner: String?
    let tags: [String]
    let notes: String?
    let memberLogs: Bool?
    let reach: Reach?

    nonisolated struct Reach: Decodable, Sendable, Hashable {
        let network: String?
        let board: Bool?
        let endpoint: String?  // operators only
    }
}

nonisolated struct Availability: Decodable, Sendable {
    let state: String  // online, sync-only, offline, away, no-a2a, client
    let detail: String?
    let lastPullSecondsAgo: Double?
}

nonisolated struct AgentStatus: Decodable, Sendable {
    let state: String  // active, inactive, failed, crashlooping, not-installed, unreachable, unknown
    let activeState: String?
    let subState: String?
    let since: String?
    let detail: String?
}

nonisolated struct HealthEvent: Decodable, Sendable {
    let state: String
    let detail: String?
    let at: Double
}

nonisolated struct Logs: Decodable, Sendable { let text: String }

/// What an action that needs confirmation answers first (backend/app.py, _confirm_gate).
nonisolated struct WouldRun: Decodable, Sendable {
    let wouldRun: Bool
    let clearance: String
    let detail: String
}

nonisolated struct CommandResult: Decodable, Sendable {
    let ok: Bool
    let output: String
}

nonisolated struct CardSummary: Decodable, Identifiable, Sendable, Hashable {
    let id: Int
    let title: String
    let state: String
    let assignee: String?
    let project: String?
    let blockedReason: String?
    let waitingOn: String?
    let blockedByCount: Int?
    let updatedAt: Double
}

nonisolated struct CardEvent: Decodable, Identifiable, Sendable {
    let id: Int
    let author: String
    let kind: String
    let body: String?
    let createdAt: Double
}

nonisolated enum Board {
    /// The columns of the web page, in its order (backend/board.py, STATES).
    static let states = ["todo", "doing", "blocked", "review", "done"]
    /// A blocked card must say why (backend/board.py, BLOCKED_REASONS).
    static let blockedReasons = ["unclear", "waiting", "cannot"]
}

/// A change to a card. Only what is set is sent: the rest is left as it is.
nonisolated struct CardChange: Encodable, Sendable {
    var state: String?
    var blockedReason: String?
    var waitingOn: String?

    enum CodingKeys: String, CodingKey { case state, blockedReason = "blocked_reason", waitingOn = "waiting_on" }
}
