import SwiftUI

/// The fleet, read mostly: who answers, and the one everyday fix (restart). A member sees their own
/// agents; Beacon decides what each person may do, and the app only hides what it knows is refused.
struct AgentsView: View {
    @Environment(Session.self) private var session
    @State private var agents: [Agent] = []
    @State private var availability: [String: Availability] = [:]
    @State private var selected: Agent.ID?
    @State private var search = ""
    @State private var problem: String?

    var body: some View {
        NavigationSplitView {
            List(selection: $selected) {
                if let problem { Text(problem).foregroundStyle(.red) }
                ForEach(Self.matching(agents, search)) { agent in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(agent.id).font(.headline)
                            Spacer()
                            AvailabilityPill(state: availability[agent.id]?.state ?? "unknown")
                        }
                        Text(verbatim: ([agent.host] + agent.tags).joined(separator: " · ")).font(.subheadline).foregroundStyle(.secondary)
                        if let owner = agent.owner { Text(verbatim: owner).font(.caption).foregroundStyle(.secondary) }
                    }
                    .tag(agent.id)
                }
            }
            .overlay {
                if agents.isEmpty && problem == nil {
                    ContentUnavailableView("No agents", systemImage: "cpu")
                }
            }
            .navigationTitle("Agents")
            .searchable(text: $search, prompt: "Name, host, tag or owner")
            .toolbar {
                ToolbarItem { Button("Refresh", systemImage: "arrow.clockwise") { Task { await load() } }.keyboardShortcut("r") }
            }
            .refreshable { await load() }
        } detail: {
            NavigationStack {  // so that "Logs" pushes onto it
                if let agent = agents.first(where: { $0.id == selected }) {
                    AgentDetailView(agent: agent, availability: availability[agent.id]).id(agent.id)
                } else {
                    ContentUnavailableView("No agent selected", systemImage: "cpu")
                }
            }
        }
        .task { await load() }
        .onChange(of: session.openSecrets, initial: true) { if let id = session.openSecrets { selected = id } }
    }

    static func matching(_ agents: [Agent], _ search: String) -> [Agent] {
        let needle = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !needle.isEmpty else { return agents }
        return agents.filter { a in ([a.id, a.host, a.owner ?? ""] + a.tags).contains { $0.lowercased().contains(needle) } }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            agents = try await client.get("/api/agents")
            availability = (try? await client.get("/api/availability")) ?? [:]
            problem = nil
            // A notice about an agent that is gone, or not this person's: drop it, or its sheet opens later.
            if let wanted = session.openSecrets, !agents.contains(where: { $0.id == wanted }) {
                session.openSecrets = nil
                problem = "\(wanted) is not an agent you can see."
            }
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }
}

struct AvailabilityPill: View {
    let state: String

    var body: some View {
        Label(state, systemImage: "circle.fill")
            .labelStyle(PillStyle(color: color))
    }

    private var color: Color {
        switch state {
        case "online": .green
        case "sync-only", "away": .orange
        case "offline": .red
        default: .gray
        }
    }

    private struct PillStyle: LabelStyle {
        let color: Color
        func makeBody(configuration: Configuration) -> some View {
            HStack(spacing: 4) {
                configuration.icon.imageScale(.small).foregroundStyle(color)
                configuration.title.font(.caption)  // the word, not only the colour
            }
        }
    }
}

struct AgentDetailView: View {
    @Environment(Session.self) private var session
    let agent: Agent
    let availability: Availability?

    @State private var status: AgentStatus?
    @State private var statusProblem: String?
    @State private var events: [HealthEvent] = []
    @State private var wouldRun: WouldRun?
    @State private var restarting = false
    @State private var result: CommandResult?
    @State private var problem: String?
    @State private var showSecrets = false

    private var isOperator: Bool { session.role == "operator" }
    /// Logs are an operator's, or the owner's where an operator turned them on.
    private var mayReadLogs: Bool { isOperator || agent.memberLogs == true }

    var body: some View {
        Form {
            Section {
                if let availability {
                    LabeledContent("Reachable") { AvailabilityPill(state: availability.state) }
                    if let detail = availability.detail, !detail.isEmpty { Text(detail).font(.footnote).foregroundStyle(.secondary) }
                }
                LabeledContent("Service") {
                    if let status { Text(status.state) } else if let statusProblem { Text(statusProblem) } else { ProgressView() }
                }
                if let since = status?.since { LabeledContent("Since", value: since) }
                if let detail = status?.detail { Text(detail).font(.footnote).foregroundStyle(.secondary) }
            }
            Section {
                LabeledContent("Type", value: agent.type)
                LabeledContent("Host", value: agent.host)
                if let owner = agent.owner { LabeledContent("Owner", value: owner) }
                if !agent.tags.isEmpty { LabeledContent("Tags", value: agent.tags.joined(separator: ", ")) }
                if let notes = agent.notes, !notes.isEmpty { Text(notes) }
            }
            Section {
                Button(restarting ? "Restarting…" : "Restart gateway…") { ask() }.disabled(restarting)
                    .confirmationDialog("Restart \(agent.id)?", isPresented: Binding(get: { wouldRun != nil }, set: { if !$0 { wouldRun = nil } }),
                                        titleVisibility: .visible, presenting: wouldRun) { _ in
                        Button("Restart", role: .destructive) { restart() }
                    } message: { gate in
                        Text(gate.detail)  // Beacon's own words: what would run, at which clearance
                    }
                if let result {
                    Label(result.ok ? "Restarted" : "Restart failed", systemImage: result.ok ? "checkmark.circle" : "xmark.octagon")
                        .foregroundStyle(result.ok ? .green : .red)
                    if !result.output.isEmpty { Text(result.output).font(.footnote.monospaced()).textSelection(.enabled) }
                }
                if mayReadLogs {
                    NavigationLink("Logs") { LogsView(agent: agent.id) }
                }
                // A client is a program on someone's computer: there is no host to hold a file.
                if agent.type != "client" {
                    Button("Secrets…") { showSecrets = true }
                }
            }
            if !events.isEmpty {
                Section("Health events") {
                    ForEach(Array(events.enumerated()), id: \.offset) { _, event in
                        LabeledContent(event.state, value: Date(timeIntervalSince1970: event.at).formatted(.relative(presentation: .named)))
                        if let detail = event.detail, !detail.isEmpty { Text(detail).font(.footnote).foregroundStyle(.secondary) }
                    }
                }
            }
            if let problem { Section { Text(problem).foregroundStyle(.red) } }
        }
        .navigationTitle(agent.id)
        .task { await load() }
        .sheet(isPresented: $showSecrets) { SecretsView(agent: agent.id) }
        .onChange(of: session.openSecrets, initial: true) {
            if session.openSecrets == agent.id {
                session.openSecrets = nil
                showSecrets = true
            }
        }
        .refreshable { await load() }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            status = try await client.get("/api/agents/\(agent.id)/status")
            statusProblem = nil
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            statusProblem = "unknown"
            problem = error.localizedDescription
        }
        // Health events are not on a member's list, so they are not asked for.
        if isOperator { events = (try? await client.get("/api/agents/\(agent.id)/health-events?limit=20")) ?? [] }
    }

    /// The confirm gate, as for every client: ask first, show what Beacon would do, then confirm.
    private func ask() {
        guard let client = session.client else { return }
        problem = nil
        result = nil
        Task {
            do {
                wouldRun = try await client.post("/api/agents/\(agent.id)/restart")
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    private func restart() {
        guard let client = session.client else { return }
        restarting = true
        Task {
            defer { restarting = false }
            do {
                // Beacon waits up to two minutes for the host (backend/drivers/hermes.py).
                result = try await client.post("/api/agents/\(agent.id)/restart?confirm=true", waiting: 180)
                await load()
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}

struct LogsView: View {
    @Environment(Session.self) private var session
    let agent: String
    @State private var text: String?
    @State private var problem: String?

    var body: some View {
        ScrollView([.vertical, .horizontal]) {
            Text(text ?? problem ?? "Loading…")
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
        }
        .defaultScrollAnchor(.bottomLeading)  // the newest lines are at the end
        .navigationTitle("Logs")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem { Button("Refresh", systemImage: "arrow.clockwise") { Task { await load() } }.keyboardShortcut("r") } }
        .task { await load() }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            let logs: Logs = try await client.get("/api/agents/\(agent)/logs?lines=200")
            text = logs.text.isEmpty ? "(no log lines)" : logs.text
        } catch {
            text = nil
            problem = error.localizedDescription
        }
    }
}
