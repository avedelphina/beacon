import SwiftUI

// Chat with an agent you own (backend/chat.py, docs/chat-plan.md): plain conversations. Face ID opens
// it; Beacon keeps that open for 15 minutes from the last use. Nothing is kept on the phone, and
// nothing said here is an approval of anything.

nonisolated struct ChatAgent: Decodable, Identifiable, Sendable, Hashable {
    let id: String
    let connectedAt: Double?   // when its plugin last asked Beacon; nil: never

    /// The plugin asks every half minute or so.
    func isConnected(now: Double = Date().timeIntervalSince1970) -> Bool {
        connectedAt.map { now - $0 < 90 } ?? false
    }
}

nonisolated struct ChatThread: Decodable, Identifiable, Sendable, Hashable {
    let id: Int
    let agent: String
    let title: String
    let origin: String         // app, home (scheduled results and what the agent sends on its own)
    let updatedAt: Double
    let unread: Int
    let last: String
}

nonisolated struct ChatMessage: Decodable, Identifiable, Sendable, Hashable {
    let id: Int
    let sender: String         // owner, agent
    let text: String
    let state: String          // queued, delivered, cancelled, expired
    let replyTo: Int?
    let createdAt: Double
    let final: Bool            // false while the answer is still being written

    var fromOwner: Bool { sender == "owner" }
}

nonisolated struct ChatAnswer: Decodable, Sendable {
    let thread: ChatThread
    let messages: [ChatMessage]
    let rev: Int
}

nonisolated struct ChatSend: Encodable, Sendable {
    var agent: String? = nil
    let text: String
    var replyTo: Int? = nil
    enum CodingKeys: String, CodingKey { case agent, text, replyTo = "reply_to" }
}

nonisolated enum Chat {
    /// A long poll waits up to 25 s on Beacon; give it room.
    static let pollTimeout: TimeInterval = 40

    /// Changed and new messages, in order.
    static func merge(_ known: [ChatMessage], _ changed: [ChatMessage]) -> [ChatMessage] {
        var byID = Dictionary(uniqueKeysWithValues: known.map { ($0.id, $0) })
        for message in changed { byID[message.id] = message }
        return byID.values.sorted { $0.id < $1.id }
    }

    /// Hermes asks before a dangerous command and waits. Its prompt also offers "always", which
    /// Beacon refuses, so the app offers its own two buttons while the prompt is the latest thing said.
    static func awaitsApproval(_ messages: [ChatMessage]) -> Bool {
        guard let latest = messages.last, !latest.fromOwner else { return false }
        return latest.text.contains("requires approval") && latest.text.contains("/approve")
    }
}

/// Face ID, then Beacon opens chat for this device.
private func unlockChat(_ client: Client) async throws {
    guard let key = DeviceKey.load() else {
        throw APIError.unreadable("This device has no signing key any more. Log out and log in again.")
    }
    let _: Empty = try await client.send("POST", "/api/chat/unlock", body: nil, signedBy: key, reason: "Open chat with your agents")
}

struct ChatsView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase
    @State private var threads: [ChatThread] = []
    @State private var agents: [ChatAgent] = []
    @State private var path: [ChatTarget] = []
    @State private var locked = false
    @State private var problem: String?

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let problem { Text(verbatim: problem).foregroundStyle(.red) }
                if locked {
                    Button("Unlock with \(Biometry.name)", systemImage: "lock") { Task { await unlock() } }
                }
                ForEach(threads) { thread in
                    NavigationLink(value: ChatTarget(agent: thread.agent, thread: thread.id)) { ThreadRow(thread: thread) }
                        .swipeActions { Button("Delete", role: .destructive) { delete(thread) } }
                }
            }
            .overlay {
                if threads.isEmpty && !locked && problem == nil {
                    ContentUnavailableView("No conversations", systemImage: "bubble.left.and.bubble.right",
                                           description: Text(agents.isEmpty
                                               ? "An agent you own appears here once chat is turned on for it."
                                               : "Start one with the pencil."))
                }
            }
            .navigationTitle("Chats")
            .toolbar {
                ToolbarItem {
                    Menu("New conversation", systemImage: "square.and.pencil") {
                        ForEach(agents) { agent in
                            Button(agent.isConnected() ? agent.id : "\(agent.id) (away)") {
                                path = [ChatTarget(agent: agent.id, thread: nil)]
                            }
                        }
                    }
                    .disabled(agents.isEmpty)
                }
            }
            .navigationDestination(for: ChatTarget.self) { target in
                ChatThreadView(target: target, onChange: { Task { await load() } })
            }
            .refreshable { await load() }
        }
        .task { await load(); if locked { await unlock() } }
        .onChange(of: path.isEmpty) { if path.isEmpty { Task { await load() } } }
        .onChange(of: phase) { if phase == .active { Task { await load() } } }
        .onChange(of: session.refreshes) { Task { await load() } }
        // A tapped notification: that conversation.
        .onChange(of: session.openChat, initial: true) {
            if let chat = session.openChat {
                session.openChat = nil
                path = [ChatTarget(agent: chat.agent, thread: chat.thread)]
            }
        }
        .privacyBlank(phase != .active)
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            agents = try await client.get("/api/chat/agents")
            threads = try await client.get("/api/chat/threads")
            locked = false
            problem = nil
        } catch APIError.loggedOut {
            session.forget()
        } catch APIError.refused(423, _) {
            locked = true
            threads = []
        } catch APIError.refused(404, _) {
            problem = "This Beacon has no chat yet: update it."
        } catch {
            problem = error.localizedDescription
        }
    }

    private func unlock() async {
        guard let client = session.client else { return }
        do {
            try await unlockChat(client)
            await load()
        } catch {
            problem = error.localizedDescription
        }
    }

    private func delete(_ thread: ChatThread) {
        Task {
            do {
                let _: Empty = try await session.client!.send("DELETE", "/api/chat/threads/\(thread.id)", body: nil)
            } catch {
                problem = error.localizedDescription
            }
            await load()
        }
    }
}

nonisolated struct ChatTarget: Hashable, Sendable {
    let agent: String
    let thread: Int?   // nil: a conversation that starts with the first message
}

struct ThreadRow: View {
    let thread: ChatThread

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(verbatim: thread.agent).font(.headline)
                if thread.unread > 0 {
                    Text("\(thread.unread)").font(.caption.bold()).padding(.horizontal, 6)
                        .background(.tint, in: Capsule()).foregroundStyle(.white)
                        .accessibilityLabel("\(thread.unread) unread")
                }
                Spacer()
                Text(Date(timeIntervalSince1970: thread.updatedAt), format: .relative(presentation: .named))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text(verbatim: thread.title).font(.subheadline).lineLimit(1)
            Text(AgentText.render(thread.last)).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
        }
    }
}

struct ChatThreadView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase
    let target: ChatTarget
    let onChange: () -> Void

    @State private var threadID: Int?
    @State private var messages: [ChatMessage] = []
    @State private var text = ""
    @State private var replying: ChatMessage?
    @State private var sending = false
    @State private var locked = false
    @State private var problem: String?

    var body: some View {
        ScrollViewReader { scroll in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(messages) { message in
                        Bubble(message: message, reply: { replying = message }, cancel: { cancel(message) })
                            .id(message.id)
                    }
                    if let problem { Text(verbatim: problem).foregroundStyle(.red) }
                    if locked { Button("Unlock with \(Biometry.name)", systemImage: "lock") { Task { await unlock() } } }
                    Color.clear.frame(height: 1).id("end")
                }
                .padding()
            }
            .defaultScrollAnchor(.bottom)
            .onChange(of: messages) { scroll.scrollTo("end", anchor: .bottom) }
        }
        .safeAreaInset(edge: .bottom) { composer }
        .toolbar(.hidden, for: .tabBar)
        .navigationTitle(target.agent)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem {
                Button("Stop the agent", systemImage: "stop.circle") { send("/stop") }
                    .disabled(threadID == nil || sending)
            }
        }
        .onAppear { if threadID == nil { threadID = target.thread } }
        // Asks again and again while this conversation is in front; leaving it or the app ends that.
        .task(id: poll) { await follow() }
        .privacyBlank(phase != .active)
    }

    private struct Poll: Equatable { let thread: Int?; let active: Bool; let locked: Bool }
    private var poll: Poll { Poll(thread: threadID ?? target.thread, active: phase == .active, locked: locked) }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            if Chat.awaitsApproval(messages) {
                HStack {
                    Button("Approve the command") { send("/approve") }.buttonStyle(.borderedProminent)
                    Button("Deny", role: .destructive) { send("/deny") }.buttonStyle(.bordered)
                }
                .disabled(sending)
            }
            if let replying {
                HStack {
                    Text(verbatim: "Replying to: \(replying.text)").font(.caption).lineLimit(1).foregroundStyle(.secondary)
                    Spacer()
                    Button("Not a reply", systemImage: "xmark.circle") { self.replying = nil }.labelStyle(.iconOnly)
                }
            }
            HStack(alignment: .bottom) {
                TextField("Message \(target.agent)", text: $text, axis: .vertical)
                    .lineLimit(1...6).textFieldStyle(.roundedBorder)
                Button("Send", systemImage: "arrow.up.circle.fill") { send(text) }
                    .labelStyle(.iconOnly).font(.title2)
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(sending || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(10)
        .background(.bar)
    }

    private func follow() async {
        guard phase == .active, !locked, let client = session.client, let thread = threadID ?? target.thread else { return }
        var revision: Int?
        while !Task.isCancelled {
            do {
                let path = "/api/chat/threads/\(thread)" + (revision.map { "?since=\($0)" } ?? "")
                let answer: ChatAnswer = try await client.send("GET", path, body: nil, timeout: Chat.pollTimeout)
                messages = revision == nil ? answer.messages : Chat.merge(messages, answer.messages)
                revision = answer.rev
                problem = nil
            } catch APIError.loggedOut {
                session.forget()
                return
            } catch APIError.refused(423, _) {
                locked = true
                return
            } catch {
                if Task.isCancelled { return }
                problem = error.localizedDescription
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func unlock() async {
        guard let client = session.client else { return }
        do {
            try await unlockChat(client)
            locked = false
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    private func send(_ words: String) {
        let words = words.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client = session.client, !words.isEmpty, !sending else { return }
        sending = true
        problem = nil
        let command = words.hasPrefix("/")
        let reply = command ? nil : replying?.id
        Task {
            defer { sending = false }
            do {
                if let thread = threadID ?? target.thread {
                    let _: ChatMessage = try await client.send("POST", "/api/chat/threads/\(thread)/messages",
                                                              json: ChatSend(text: words, replyTo: reply))
                } else {
                    let started: ChatThread = try await client.send("POST", "/api/chat/threads",
                                                                    json: ChatSend(agent: target.agent, text: words))
                    threadID = started.id
                }
                if words == text.trimmingCharacters(in: .whitespacesAndNewlines) { text = "" }
                replying = nil
                onChange()
            } catch APIError.loggedOut {
                session.forget()
            } catch APIError.refused(423, _) {
                locked = true
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    private func cancel(_ message: ChatMessage) {
        Task {
            do {
                let _: Empty = try await session.client!.send("DELETE", "/api/chat/messages/\(message.id)", body: nil)
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}

struct Bubble: View {
    let message: ChatMessage
    let reply: () -> Void
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: message.fromOwner ? .trailing : .leading, spacing: 2) {
            Group {
                // An agent's words: formatted, never a tappable link (AgentText).
                if message.fromOwner { Text(verbatim: message.text) } else { Text(AgentText.render(message.text)) }
            }
            .textSelection(.enabled)
            .padding(10)
            .background(message.fromOwner ? AnyShapeStyle(.tint.opacity(0.18)) : AnyShapeStyle(.quaternary),
                        in: RoundedRectangle(cornerRadius: 14))
            .contextMenu {
                if !message.fromOwner { Button("Reply", systemImage: "arrowshape.turn.up.left", action: reply) }
                if message.state == "queued" { Button("Do not send", systemImage: "trash", role: .destructive, action: cancel) }
            }
            if let note = Self.note(message) { Text(note).font(.caption2).foregroundStyle(.secondary) }
        }
        .frame(maxWidth: .infinity, alignment: message.fromOwner ? .trailing : .leading)
    }

    static func note(_ message: ChatMessage) -> String? {
        if !message.final { return "writing…" }
        switch message.state {
        case "queued": return "waiting for the agent"
        case "cancelled": return "not sent: you took it back"
        case "expired": return "not delivered: the agent was away too long"
        default: return nil
        }
    }
}

extension View {
    /// Nothing of a conversation in the app switcher.
    func privacyBlank(_ hidden: Bool) -> some View {
        overlay { if hidden { Rectangle().fill(.background).ignoresSafeArea() } }
    }
}
