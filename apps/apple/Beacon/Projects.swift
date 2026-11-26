import SwiftUI

// Projects (backend/projects.py): long-running work with an owner and a goal. The app reads them and
// does the two things that are handy from a phone, pausing one (the brake for agents going in circles)
// and saying where it stands. Creating projects, editing goals and milestones, and closing them stay
// in the web page.

nonisolated struct ProjectRights: Decodable, Sendable { let steer: Bool }

nonisolated struct ProjectSummary: Decodable, Identifiable, Sendable {
    let id: Int
    let name: String
    let owner: String?       // an agent, a person, or nil: the operators
    let state: String        // active, paused, done, abandoned
    let status: String?
    let cards: [String: Int]  // how many cards in each board state
}

nonisolated struct Milestone: Decodable, Identifiable, Sendable {
    let id: Int
    let title: String
    let doneWhen: String
    let due: String?
    let state: String        // open, done
    let evidence: Evidence?

    nonisolated struct Evidence: Decodable, Sendable {
        let links: [Card.Link]?
        let noneBecause: String?
    }
}

nonisolated struct ProjectEvent: Decodable, Identifiable, Sendable {
    let id: Int
    let author: String
    let kind: String
    let body: String
    let createdAt: Double
}

nonisolated struct Project: Decodable, Identifiable, Sendable {
    let id: Int
    let name: String
    let goal: String
    let doneWhen: String?
    let owner: String?
    let state: String
    let status: String?
    let statusAt: Double?
    let pausedBy: String?
    let links: [Card.Link]
    let milestones: [Milestone]
    let events: [ProjectEvent]
    let cardList: [CardSummary]
    let can: ProjectRights

    var isClosed: Bool { state == "done" || state == "abandoned" }
}

nonisolated struct ProjectChange: Encodable, Sendable {
    var state: String?
    var status: String?
}

nonisolated enum Projects {
    /// "2 open · 3 done", from the count of cards in each state.
    static func progress(_ cards: [String: Int]) -> String {
        let done = cards["done"] ?? 0
        let open = cards.values.reduce(0, +) - done
        return open + done == 0 ? "no cards" : "\(open) open · \(done) done"
    }
}

struct ProjectsView: View {
    @Environment(Session.self) private var session
    @State private var projects: [ProjectSummary] = []
    @State private var selected: Int?
    @State private var showClosed = false
    @State private var problem: String?

    var body: some View {
        NavigationSplitView {
            List(selection: $selected) {
                if let problem { Text(verbatim: problem).foregroundStyle(.red) }
                ForEach(projects) { project in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(verbatim: project.name).font(.headline)
                            Spacer()
                            Text(project.state).font(.caption)
                                .foregroundStyle(project.state == "paused" ? .orange : .secondary)
                        }
                        Text(verbatim: "\(project.owner ?? "operators") · \(Projects.progress(project.cards))")
                            .font(.subheadline).foregroundStyle(.secondary)
                        if let status = project.status { Text(verbatim: status).font(.caption).lineLimit(2) }
                    }
                    .tag(project.id)
                }
                Toggle("Show closed projects", isOn: $showClosed)
            }
            .overlay {
                if projects.isEmpty && problem == nil { ContentUnavailableView("No projects", systemImage: "flag") }
            }
            .navigationTitle("Projects")
            .toolbar {
                ToolbarItem { Button("Refresh", systemImage: "arrow.clockwise") { Task { await load() } }.keyboardShortcut("r") }
            }
            .refreshable { await load() }
        } detail: {
            NavigationStack {
                if let selected {
                    ProjectView(id: selected, onChange: { Task { await load() } }).id(selected)
                } else {
                    ContentUnavailableView("No project selected", systemImage: "flag")
                }
            }
        }
        .task(id: showClosed) { await load() }
        .onChange(of: session.openProject, initial: true) {
            // A tapped notification. The project is fetched by id, so a closed one opens too.
            if let id = session.openProject {
                selected = id
                session.openProject = nil
                Task { await load() }
            }
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            projects = try await client.get("/api/projects?include_closed=\(showClosed)")
            problem = nil
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }
}

struct ProjectView: View {
    @Environment(Session.self) private var session
    let id: Int
    let onChange: () -> Void

    @State private var project: Project?
    @State private var status = ""
    @State private var comment = ""
    @State private var confirmPause = false
    @State private var working = false
    @State private var problem: String?

    var body: some View {
        Form {
            if let project {
                Section {
                    Text(verbatim: project.name).font(.headline)
                    LabeledContent("State", value: project.state)
                    LabeledContent("Owner", value: project.owner ?? "operators")
                    if project.state == "paused", let by = project.pausedBy { LabeledContent("Paused by", value: by) }
                    if project.can.steer && project.state == "active" {
                        Button("Pause project…", systemImage: "pause.circle") { confirmPause = true }.disabled(working)
                    }
                    if project.can.steer && project.state == "paused" {
                        Button("Resume project", systemImage: "play.circle") { change(ProjectChange(state: "active")) }.disabled(working)
                    }
                }
                Section("Goal") {
                    Text(project.goal.isEmpty ? AttributedString("None set") : AgentText.render(project.goal)).textSelection(.enabled)
                    if let done = project.doneWhen, !done.isEmpty {
                        LabeledContent("Done when") { Text(verbatim: done) }
                    }
                }
                Section {
                    if let current = project.status { Text(AgentText.render(current)).textSelection(.enabled) }
                    if let at = project.statusAt {
                        Text(Date(timeIntervalSince1970: at).formatted(.relative(presentation: .named)))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if project.can.steer && !project.isClosed {
                        TextField("New status", text: $status, axis: .vertical).lineLimit(1...5)
                        Button("Post status") { change(ProjectChange(status: status.trimmingCharacters(in: .whitespacesAndNewlines))) }
                            .disabled(working || status.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                } header: {
                    Text("Status")
                }
                if !project.milestones.isEmpty { milestones(project.milestones) }
                let links = SafeLinks.from(project.links.map { ($0.label ?? $0.url, $0.url) })
                if !links.isEmpty { Section("Links") { LinkList(links: links) } }
                if !project.cardList.isEmpty {
                    Section("Cards") {
                        ForEach(project.cardList) { card in
                            NavigationLink {
                                CardView(id: card.id, onChange: { Task { await load() } })
                            } label: {
                                VStack(alignment: .leading) {
                                    CardRow(card: card)
                                    Text(card.state).font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
                Section("History") {
                    ForEach(project.events.reversed()) { event in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: "\(event.author) · \(Date(timeIntervalSince1970: event.createdAt).formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                            Text(AgentText.render(event.body)).textSelection(.enabled)
                        }
                    }
                }
                Section {
                    TextField("Comment", text: $comment, axis: .vertical).lineLimit(2...6)
                    Button("Add comment", action: addComment)
                        .disabled(working || comment.trimmingCharacters(in: .whitespaces).isEmpty)
                        .keyboardShortcut(.return, modifiers: .command)
                }
            } else if problem == nil {
                ProgressView()
            }
            if let problem { Section { Text(verbatim: problem).foregroundStyle(.red) } }
        }
        .navigationTitle("Project")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog("Pause this project?", isPresented: $confirmPause, titleVisibility: .visible) {
            Button("Pause", role: .destructive) { change(ProjectChange(state: "paused")) }
        } message: {
            Text("Its agents are told to stop work on it. Only a person can restart a project a person paused.")
        }
    }

    private func milestones(_ items: [Milestone]) -> some View {
        Section("Milestones") {
            ForEach(items) { milestone in
                VStack(alignment: .leading, spacing: 2) {
                    Label {
                        Text(verbatim: milestone.title)
                    } icon: {
                        Image(systemName: milestone.state == "done" ? "checkmark.circle.fill" : "circle")
                            .accessibilityLabel(milestone.state)
                    }
                    Text(verbatim: "Done when: \(milestone.doneWhen)").font(.caption)
                    if let due = milestone.due { Text(verbatim: "Due \(due)").font(.caption).foregroundStyle(.secondary) }
                    if let evidence = milestone.evidence {
                        LinkList(links: SafeLinks.from((evidence.links ?? []).map { ($0.label ?? $0.url, $0.url) }))
                        if let why = evidence.noneBecause {
                            Text(verbatim: "No evidence, because: \(why)").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            project = try await client.get("/api/projects/\(id)")
            problem = nil
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }

    private func change(_ change: ProjectChange) {
        send(onSuccess: { status = "" }) { client in try await client.send("PATCH", "/api/projects/\(id)", json: change) }
    }

    private func addComment() {
        let text = comment.trimmingCharacters(in: .whitespacesAndNewlines)
        send(onSuccess: { comment = "" }) { client in
            try await client.send("POST", "/api/projects/\(id)/comments", json: ["text": text])
        }
    }

    /// Beacon answers a change with the project as it now is; who may do what is its rule, shown as it says it.
    private func send(onSuccess: @escaping () -> Void, _ request: @escaping (Client) async throws -> Project) {
        guard let client = session.client else { return }
        working = true
        problem = nil
        Task {
            defer { working = false }
            do {
                project = try await request(client)
                onSuccess()
                onChange()
            } catch APIError.loggedOut {
                session.forget()
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}
