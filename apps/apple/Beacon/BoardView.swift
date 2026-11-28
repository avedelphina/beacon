import SwiftUI

/// The board: read cards, comment, move them. Making and editing cards stays in the web page.
struct BoardView: View {
    @Environment(Session.self) private var session
    @State private var cards: [CardSummary] = []
    @State private var selected: CardSummary.ID?
    @State private var showDone = false
    @State private var search = ""
    @State private var problem: String?

    var body: some View {
        NavigationSplitView {
            List(selection: $selected) {
                if let problem { Text(problem).foregroundStyle(.red) }
                // Not moving: first, whatever column they are in. Listed once, so selecting stays simple.
                let stuck = matching.filter { $0.stuck == true }
                if !stuck.isEmpty {
                    Section("Not moving") { ForEach(stuck) { card in CardRow(card: card, showState: true).tag(card.id) } }
                }
                ForEach(Board.states, id: \.self) { state in
                    let column = matching.filter { $0.state == state && $0.stuck != true }
                    if !column.isEmpty {
                        Section(state.capitalized) {
                            ForEach(column) { card in CardRow(card: card).tag(card.id) }
                        }
                    }
                }
                Toggle("Show done cards", isOn: $showDone)
            }
            .overlay {
                if cards.isEmpty && problem == nil { ContentUnavailableView("No cards", systemImage: "rectangle.stack") }
            }
            .navigationTitle("Board")
            .searchable(text: $search, prompt: "Title, assignee or project")
            .toolbar {
                ToolbarItem { Button("Refresh", systemImage: "arrow.clockwise") { Task { await load() } }.keyboardShortcut("r") }
            }
            .refreshable { await load() }
        } detail: {
            if let selected {
                CardView(id: selected, onChange: { Task { await load() } }).id(selected)
            } else {
                ContentUnavailableView("No card selected", systemImage: "rectangle.stack")
            }
        }
        .task(id: showDone) { await load() }
        // A tapped "cards not moving" notice: they are at the top; a single one is opened.
        .task(id: session.openCards) {
            guard let ids = session.openCards else { return }
            search = ""
            await load()
            if ids.count == 1 { selected = ids[0] }
            session.openCards = nil
        }
    }

    private var matching: [CardSummary] {
        let needle = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !needle.isEmpty else { return cards }
        return cards.filter { c in [c.title, c.assignee ?? "", c.project ?? ""].contains { $0.lowercased().contains(needle) } }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            cards = try await client.get("/api/board?include_done=\(showDone)")
            problem = nil
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }
}

struct CardRow: View {
    let card: CardSummary
    var showState = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(card.title).lineLimit(2)
            if card.stuck == true { Text("STUCK").font(.caption.bold()).foregroundStyle(.red) }
            Text(["#\(card.id)", showState ? card.state : nil, card.assignee ?? "unassigned", card.project, card.blockedReason,
                  card.helpBy == nil ? nil : "help wanted",
                  card.blockedByCount.flatMap { $0 > 0 ? "after \($0) other\($0 == 1 ? "" : "s")" : nil }]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct CardView: View {
    @Environment(Session.self) private var session
    let id: Int
    let onChange: () -> Void

    @State private var card: Card?
    @State private var comment = ""
    @State private var blocking = false
    @State private var working = false
    @State private var problem: String?

    var body: some View {
        Form {
            if let card {
                Section {
                    Text(card.title).font(.headline)
                    if card.stuck == true {
                        Text("Stuck: people were told it is not moving, and nothing has changed since.")
                            .font(.footnote).foregroundStyle(.red)
                    }
                    LabeledContent("Assignee", value: card.assignee ?? "unassigned")
                    if let project = card.project { LabeledContent("Project", value: project) }
                    LabeledContent("Made by", value: card.createdBy)
                    // A closure, not `set: move`: Swift 6.3 (Xcode 26, as on CI) crashes on the method reference.
                    if card.mayChange {
                        Picker("State", selection: Binding(get: { card.state }, set: { move(to: $0) })) {
                            ForEach(Board.states, id: \.self) { Text($0).tag($0) }
                        }
                        .disabled(working)
                    } else {
                        LabeledContent("State", value: card.state)
                        Text("Someone else's card in a project you take part in: you can read and comment.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    if let milestone = card.milestoneId { LabeledContent("Milestone", value: "#\(milestone)") }
                    if let asking = card.helpBy {
                        let roles = (card.helpRoles ?? []).joined(separator: ", ")
                        LabeledContent("Help wanted", value: roles.isEmpty ? "asked by \(asking)" : "\(roles), asked by \(asking)")
                    }
                    if card.state == "blocked" {
                        LabeledContent("Why", value: [card.blockedReason, card.waitingOn].compactMap { $0 }.joined(separator: ": "))
                    }
                    if let next = card.nextAction { LabeledContent("Next action", value: next) }
                    if let done = card.doneWhen { LabeledContent("Done when", value: done) }
                }
                if let body = card.body, !body.isEmpty {
                    Section("Description") { Text(AgentText.render(body)).textSelection(.enabled) }
                }
                if !card.sources.isEmpty {
                    Section("Links") { SourceLinks(card: card) }
                }
                Section("History") {
                    ForEach(card.events ?? []) { event in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: "\(event.author) · \(Date(timeIntervalSince1970: event.createdAt).formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                            Text(AgentText.render(event.body ?? event.kind)).textSelection(.enabled)
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
            if let problem { Section { Text(problem).foregroundStyle(.red) } }
        }
        .navigationTitle("Card #\(id)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $blocking) {
            BlockedSheet { reason, waitingOn in change(CardChange(state: "blocked", blockedReason: reason, waitingOn: waitingOn)) }
        }
    }

    private func move(to state: String) {
        guard state != card?.state else { return }
        if state == "blocked" {
            blocking = true  // a blocked card says why
        } else {
            change(CardChange(state: state))
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            card = try await client.get("/api/board/cards/\(id)")
            problem = nil
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }

    private func change(_ change: CardChange) {
        send { client in try await client.send("PATCH", "/api/board/cards/\(id)", json: change) }
    }

    private func addComment() {
        let text = comment.trimmingCharacters(in: .whitespacesAndNewlines)
        send(onSuccess: { comment = "" }) { client in
            try await client.send("POST", "/api/board/cards/\(id)/comments", json: ["text": text])
        }
    }

    /// Beacon answers a change with the card as it now is; its refusals (a rule of the board) show as they are.
    private func send(onSuccess: @escaping () -> Void = {}, _ request: @escaping (Client) async throws -> Card) {
        guard let client = session.client else { return }
        working = true
        problem = nil
        Task {
            defer { working = false }
            do {
                card = try await request(client)
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

struct BlockedSheet: View {
    @Environment(\.dismiss) private var dismiss
    let block: (String, String?) -> Void
    @State private var reason = "unclear"
    @State private var waitingOn = ""

    var body: some View {
        NavigationStack {
            Form {
                Picker("Why", selection: $reason) { ForEach(Board.blockedReasons, id: \.self) { Text($0).tag($0) } }
                    .pickerStyle(.segmented)
                if reason != "unclear" {
                    TextField(reason == "waiting" ? "Waiting on whom or what" : "What is missing (workflow:<id>)", text: $waitingOn)
                        .autocorrectionDisabled()
                }
            }
            .navigationTitle("Blocked")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Set") {
                        let on = waitingOn.trimmingCharacters(in: .whitespaces)
                        block(reason, reason == "unclear" || on.isEmpty ? nil : on)
                        dismiss()
                    }
                }
            }
        }
        .presentationDetents([.medium])
    }
}
