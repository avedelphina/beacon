import SwiftUI
import UserNotifications

/// Requests on the left (or as the whole screen on iPhone), the open one on the right.
struct RequestsSplitView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase
    @State private var requests: [WorkflowRequest] = []
    @State private var selected: WorkflowRequest.ID?
    @State private var problem: String?

    var body: some View {
        NavigationSplitView {
            List(selection: $selected) {
                if let problem {
                    Text(problem).foregroundStyle(.red)
                }
                ForEach(Self.sorted(requests)) { request in
                    RequestRow(request: request).tag(request.id)
                }
            }
            .overlay {
                if requests.isEmpty && problem == nil {
                    ContentUnavailableView("Nothing waiting", systemImage: "checkmark.circle",
                                           description: Text("Requests that agents send you appear here."))
                }
            }
            .navigationTitle("Requests")
            .toolbar {
                ToolbarItem { Button("Refresh", systemImage: "arrow.clockwise") { Task { await load() } }.keyboardShortcut("r") }
            }
            .refreshable { await load() }
        } detail: {
            if let request = requests.first(where: { $0.id == selected }) {
                RequestDetailView(request: request, onAnswered: { Task { await load() } }).id(request.id)
            } else {
                ContentUnavailableView("No request selected", systemImage: "tray")
            }
        }
        .task { await load() }
        .onChange(of: phase) { if phase == .active { Task { await load() } } }
        .onChange(of: session.refreshes) { Task { await load() } }
        .task(id: session.openRequest) {
            // A tapped notification: show that request, fresh.
            guard let id = session.openRequest else { return }
            await load()
            selected = id
            session.openRequest = nil
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        do {
            requests = try await client.get("/api/workflows")
            problem = nil
            try? await UNUserNotificationCenter.current().setBadgeCount(requests.filter(\.isOpen).count)
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }

    /// As in the web page: what waits for an answer first (urgent, then soonest to expire), then what
    /// waits for its requester, then approved; the rest as the server sends them (newest first).
    static func sorted(_ items: [WorkflowRequest]) -> [WorkflowRequest] {
        let rank = ["pending": 0, "changes_requested": 1, "approved": 2, "claimed": 2]
        return items.enumerated().sorted { a, b in
            let ra = rank[a.element.state] ?? 3, rb = rank[b.element.state] ?? 3
            if ra != rb { return ra < rb }
            if ra == 0 {
                let ua = a.element.urgentReason != nil, ub = b.element.urgentReason != nil
                if ua != ub { return ua }
                if a.element.expiresAt != b.element.expiresAt { return a.element.expiresAt < b.element.expiresAt }
            }
            return a.offset < b.offset
        }.map(\.element)
    }
}

struct RequestRow: View {
    let request: WorkflowRequest

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                if request.urgentReason != nil { Text("URGENT").font(.caption.bold()).foregroundStyle(.red) }
                Text("\(request.kind.capitalized) · CL\(request.clearance)").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(request.state.replacingOccurrences(of: "_", with: " ")).font(.caption)
                    .fontWeight(request.isOpen ? .semibold : .regular)
                    // Not the accent colour: it disappears on the selected row of the iPad list.
                    .foregroundStyle(request.isOpen ? .primary : .secondary)
            }
            Text(request.title).font(.headline).lineLimit(2)
            Text("From \(request.requester)").font(.subheadline).foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}

struct RequestDetailView: View {
    @Environment(Session.self) private var session
    let request: WorkflowRequest
    let onAnswered: () -> Void

    @State private var card: Card?
    @State private var option = ""
    @State private var answer = ""
    @State private var note = ""
    @State private var asking: Action?
    @State private var noteFor: Action?
    @State private var working = false
    @State private var problem: String?

    enum Action: String, Identifiable {
        case approved, rejected, changes_requested, choose_option, answered
        var id: String { rawValue }
    }

    var body: some View {
        Form {
            Section {
                LabeledContent("Kind", value: "\(request.kind.capitalized) · CL\(request.clearance)")
                LabeledContent("From", value: request.requester)
                LabeledContent("To", value: request.eligibleResponder == "human" ? "a person" : request.eligibleResponder)
                LabeledContent("Expires", value: Date(timeIntervalSince1970: request.expiresAt).formatted(date: .abbreviated, time: .shortened))
                LabeledContent("State", value: request.state.replacingOccurrences(of: "_", with: " "))
                if let urgent = request.urgentReason { Text("Urgent: \(urgent)").foregroundStyle(.red) }
                if request.requireHuman == true { Text("Sent to a person: \(request.escalatedReason ?? "")").foregroundStyle(.orange) }
            }
            Section("Plan") {
                Text(Self.markdown(request.summary)).textSelection(.enabled)
            }
            cardSection
            consultedSection
            answerSection
            if let problem { Section { Text(problem).foregroundStyle(.red) } }
        }
        .navigationTitle(request.title)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            option = request.options.first ?? ""
            card = try? await session.client?.get("/api/board/cards/\(request.cardId)")
        }
        .confirmationDialog(confirmTitle, isPresented: Binding(get: { asking != nil }, set: { if !$0 { asking = nil } }),
                            titleVisibility: .visible, presenting: asking) { action in
            Button(confirmButton(action), role: action == .rejected ? .destructive : nil) { send(action) }
        }
        .alert(noteFor == .changes_requested ? "What needs to change?" : "Why?",
               isPresented: Binding(get: { noteFor != nil }, set: { if !$0 { noteFor = nil } }), presenting: noteFor) { action in
            TextField("Note for \(request.requester)", text: $note)
            Button("Send") { send(action) }
            Button("Cancel", role: .cancel) {}
        }
    }

    @ViewBuilder private var cardSection: some View {
        Section("Card #\(request.cardId)") {
            if let card {
                LabeledContent(card.title, value: card.state)
                if let next = card.nextAction { LabeledContent("Next action", value: next) }
                if let done = card.doneWhen { LabeledContent("Done when", value: done) }
                let sources = (card.references ?? []).map { ("\($0.provider) \($0.kind.replacingOccurrences(of: "_", with: " ")) \($0.externalId)", $0.url) }
                    + (card.links ?? []).map { ($0.label ?? $0.url, $0.url) }
                ForEach(sources, id: \.1) { label, url in
                    if let link = URL(string: url), link.scheme == "https" || link.scheme == "http" { Link(label, destination: link) }
                }
                if sources.isEmpty {
                    Text("The card links to nothing outside Beacon. Ask the requester to add the PR or ticket to it.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if let body = card.body, !body.isEmpty {
                    DisclosureGroup("About this card") { Text(Self.markdown(body)) }
                }
            } else {
                Text("Loading…").foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var consultedSection: some View {
        if let consulted = request.consulted, !consulted.isEmpty {
            Section("Consulted first") {
                ForEach(consulted, id: \.self) { c in
                    VStack(alignment: .leading) { Text(c.agent).bold(); Text(c.said) }
                }
            }
        } else if let skipped = request.consultSkipped {
            Section { Text("No peer consulted: \(skipped)").foregroundStyle(.secondary) }
        } else if request.consultFlag != nil {
            Section { Text("No peer was consulted before asking.").foregroundStyle(.orange) }
        }
    }

    @ViewBuilder private var answerSection: some View {
        if let answered = request.response?.text ?? request.response?.note {
            Section(request.state == "changes_requested" ? "Changes requested" : "Answer") { Text(Self.markdown(answered)) }
        }
        if let by = request.answeredBy {
            Section { Text("Answered by \(by), an AI client, for \(request.onBehalfOf ?? "its operator").").foregroundStyle(.secondary) }
        }
        if request.isOpen {
            Section {
                switch request.kind {
                case "question":
                    TextField("Your answer", text: $answer, axis: .vertical).lineLimit(3...8)
                    Button("Send answer") { asking = .answered }.disabled(answer.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button("Decline", role: .destructive) { asking = .rejected }
                case "decision":
                    Picker("Option", selection: $option) { ForEach(request.options, id: \.self) { Text($0).tag($0) } }
                        .pickerStyle(.inline)
                    Button(faceID("Choose this option")) { asking = .choose_option }
                    Button("Request changes") { noteFor = .changes_requested }
                    Button("Reject", role: .destructive) { asking = .rejected }
                default:
                    Button(faceID(request.kind == "handoff" ? "Accept handoff" : "Approve")) { asking = .approved }
                        .keyboardShortcut(.return, modifiers: .command)  // still asks to confirm
                    Button("Request changes") { noteFor = .changes_requested }
                    Button(request.kind == "handoff" ? "Decline" : "Reject", role: .destructive) { asking = .rejected }
                }
            } footer: {
                if request.needsFaceID { Text("CL\(request.clearance): Face ID confirms that it is you.") }
            }
            .disabled(working)
        }
    }

    private func faceID(_ label: String) -> String { request.needsFaceID ? "\(label) with Face ID" : label }

    private var confirmTitle: String { "“\(request.title)”" }

    private func confirmButton(_ action: Action) -> String {
        switch action {
        case .approved: faceID(request.kind == "handoff" ? "Accept the handoff" : "Approve")
        case .choose_option: faceID("Choose “\(option)”")
        case .answered: faceID("Send the answer")
        case .rejected: faceID(request.kind == "question" || request.kind == "handoff" ? "Decline" : "Reject")
        case .changes_requested: faceID("Request changes")
        }
    }

    /// The answer names the plan the person saw (revision and hash): if the requester changed it since,
    /// Beacon marks the request stale instead of approving something else.
    private func send(_ action: Action) {
        guard let client = session.client else { return }
        var response: [String: String] = [:]
        switch action {
        case .choose_option: response["option"] = option
        case .answered: response["text"] = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        case .changes_requested, .rejected: if !note.isEmpty { response["note"] = note }
        case .approved: break
        }
        let body = WorkflowAnswer(action.rawValue, response, to: request)
        let key = request.needsFaceID ? DeviceKey.load() : nil
        if request.needsFaceID && key == nil {
            problem = "This device has no key for Face ID any more (a face or finger was added). Log out and log in again."
            return
        }
        working = true
        problem = nil
        Task {
            defer { working = false }
            do {
                let _: Empty = try await client.send("POST", "/api/workflows/\(request.id)/respond", json: body, signedBy: key,
                                                     reason: "Answer “\(request.title)”")
                note = ""
                onAnswered()
            } catch APIError.loggedOut {
                session.forget()
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    /// Agents write Markdown; links in it are shown, not followed automatically, as on the page.
    static func markdown(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }
}
