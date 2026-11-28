import SwiftUI
import UserNotifications

/// Requests on the left (or as the whole screen on iPhone), the open one on the right.
struct RequestsSplitView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase
    /// The window's width, not the sidebar column's: a column is narrow on an iPad too.
    @Environment(\.horizontalSizeClass) private var width
    @State private var settings = false
    @State private var requests: [WorkflowRequest] = []
    @State private var selected: WorkflowRequest.ID?
    @State private var problem: String?

    var body: some View {
        NavigationSplitView {
            List(selection: $selected) {
                if let problem {
                    Text(problem).foregroundStyle(.red)
                }
                let split = Self.split(requests, me: session.email)
                if !split.mine.isEmpty {
                    Section("Waiting on you") { ForEach(split.mine) { RequestRow(request: $0).tag($0.id) } }
                }
                if !split.others.isEmpty {
                    Section(split.mine.isEmpty ? "Requests" : "Others") { ForEach(split.others) { RequestRow(request: $0).tag($0.id) } }
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
                if width == .compact {
                    ToolbarItem(placement: .topBarLeading) { Button("Settings", systemImage: "gearshape") { settings = true } }
                }
            }
            .refreshable { await load() }
        } detail: {
            if let request = requests.first(where: { $0.id == selected }) {
                RequestDetailView(request: request, onAnswered: { Task { await load() } }).id(request.id)
            } else if selected != nil && !requests.isEmpty {
                // A notification about a request that is gone, or not this person's to see.
                ContentUnavailableView("Request not available", systemImage: "tray",
                                       description: Text("It may have been withdrawn, or it is not yours to see."))
            } else {
                ContentUnavailableView("No request selected", systemImage: "tray")
            }
        }
        .sheet(isPresented: $settings) { SettingsView(asSheet: true) }
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
            try? await UNUserNotificationCenter.current().setBadgeCount(Self.split(requests, me: session.email).mine.count)
        } catch APIError.loggedOut {
            session.forget()
        } catch {
            problem = error.localizedDescription
        }
    }

    /// Whether a pending request is this person's to answer: asked of "a person" or of them by name,
    /// or sent to a person by a reviewer. Never their own.
    static func waitsOn(_ request: WorkflowRequest, me: String?) -> Bool {
        guard request.isOpen, let me = me?.lowercased(), request.requester.lowercased() != me else { return false }
        return request.eligibleResponder == "human" || request.eligibleResponder.lowercased() == me || request.requireHuman == true
    }

    /// What waits on this person, then everything else they can see; each in the page's order.
    static func split(_ items: [WorkflowRequest], me: String?) -> (mine: [WorkflowRequest], others: [WorkflowRequest]) {
        let ordered = sorted(items)
        return (ordered.filter { waitsOn($0, me: me) }, ordered.filter { !waitsOn($0, me: me) })
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
    @State private var replaced: WorkflowRequest?
    @State private var option = ""
    @State private var answer = ""
    @State private var note = ""
    @State private var asking: Action?
    @State private var askingChanges = false
    @State private var escalating = false
    @State private var reason = ""
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
                LabeledContent("To", value: request.responderLabel)
                LabeledContent("Expires", value: Date(timeIntervalSince1970: request.expiresAt).formatted(date: .abbreviated, time: .shortened))
                LabeledContent("State", value: request.state.replacingOccurrences(of: "_", with: " "))
                if let urgent = request.urgentReason { Text(verbatim: "Urgent: \(urgent)").foregroundStyle(.red) }
                if request.requireHuman == true { Text(verbatim: "Sent to a person: \(request.escalatedReason ?? "")").foregroundStyle(.orange) }
            }
            Section("Plan") {
                Text(AgentText.render(request.summary)).textSelection(.enabled)
            }
            replacedSection
            reportSection
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
            if let old = request.replacesId { replaced = try? await session.client?.get("/api/workflows/\(old)") }
        }
        // The requester changed the plan while a dialog was open: ask again, over what is on screen now.
        .onChange(of: request.planHash) {
            asking = nil
            askingChanges = false
            if !request.options.contains(option) { option = request.options.first ?? "" }
        }
        .alert("What needs to change?", isPresented: $askingChanges) {
            TextField("Note for \(request.requester)", text: $note)
            Button("Send") { send(.changes_requested) }
            Button("Cancel", role: .cancel) { note = "" }
        }
        .alert("Send to a person", isPresented: $escalating) {
            TextField("Why should a person look?", text: $reason)
            Button("Send") { sendToPerson() }
            Button("Cancel", role: .cancel) { reason = "" }
        } message: {
            Text("Then only a person can answer it: no AI client may. This cannot be undone.")
        }
    }

    @ViewBuilder private var replacedSection: some View {
        if let old = request.replacesId {
            Section {
                DisclosureGroup("Replaces #\(old): what it said") {
                    if let replaced {
                        Text("\(replaced.wasApproved ? "It had been approved. " : "")Now \(replaced.state.replacingOccurrences(of: "_", with: " ")).")
                            .foregroundStyle(.secondary)
                        Text(verbatim: replaced.title).bold()
                        Text(AgentText.render(replaced.summary))
                    } else {
                        Text("Not loaded.").foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    @ViewBuilder private var reportSection: some View {
        if let report = request.report {
            Section {
                LabeledContent("Outcome", value: report.outcome)
                Text(AgentText.render(report.note))
                LinkList(links: SafeLinks.from((report.links ?? []).map { ($0.label ?? $0.url, $0.url) }))
            } header: {
                Text(verbatim: "Report from \(report.by)")
            } footer: {
                Text("The requester's own account. Beacon did not check it.")
            }
        }
    }

    @ViewBuilder private var cardSection: some View {
        Section("Card #\(request.cardId)") {
            if let card {
                LabeledContent(card.title, value: card.state)
                if let next = card.nextAction { LabeledContent("Next action", value: next) }
                if let done = card.doneWhen { LabeledContent("Done when", value: done) }
                SourceLinks(card: card)
                if card.sources.isEmpty {
                    Text("The card links to nothing outside Beacon. Ask the requester to add the PR or ticket to it.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if let body = card.body, !body.isEmpty {
                    DisclosureGroup("About this card") { Text(AgentText.render(body)) }
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
            Section { Text(verbatim: "No peer consulted: \(skipped)").foregroundStyle(.secondary) }
        } else if request.consultFlag != nil {
            Section { Text("No peer was consulted before asking.").foregroundStyle(.orange) }
        }
    }

    @ViewBuilder private var answerSection: some View {
        if let answered = request.response?.text ?? request.response?.note {
            Section(request.state == "changes_requested" ? "Changes requested" : "Answer") { Text(AgentText.render(answered)) }
        }
        if let by = request.answeredBy {
            Section { Text("Answered by \(by), an AI client, for \(request.onBehalfOf ?? "its operator").").foregroundStyle(.secondary) }
        }
        if request.isOpen {
            Section {
                switch request.kind {
                case "question":
                    TextField("Your answer", text: $answer, axis: .vertical).lineLimit(3...8)
                    answerButton("Send answer", .answered).disabled(answer.trimmingCharacters(in: .whitespaces).isEmpty)
                    answerButton("Decline", .rejected, role: .destructive)
                case "decision":
                    Picker("Option", selection: $option) { ForEach(request.options, id: \.self) { Text($0).tag($0) } }
                        .pickerStyle(.inline)
                    answerButton(faceID("Choose this option"), .choose_option)
                    Button("Request changes") { note = ""; askingChanges = true }
                    answerButton("Reject", .rejected, role: .destructive)
                default:
                    answerButton(faceID(request.kind == "handoff" ? "Accept handoff" : "Approve"), .approved)
                        .keyboardShortcut(.return, modifiers: .command)  // still asks to confirm
                    Button("Request changes") { note = ""; askingChanges = true }
                    answerButton(request.kind == "handoff" ? "Decline" : "Reject", .rejected, role: .destructive)
                }
            } footer: {
                if request.needsFaceID { Text("CL\(request.clearance): \(Biometry.name) confirms that it is you.") }
            }
            .disabled(working)
            // An operator's way to say "no AI client may answer this one". CL4/CL5 need a person already.
            if session.role == "operator", !request.needsFaceID, request.requireHuman != true {
                Section {
                    Button("Send to a person…") { reason = ""; escalating = true }.disabled(working)
                }
            }
        }
    }

    /// A button with its own confirmation: iOS points the confirmation at the view that carries it,
    /// so on the whole form it appears in the middle of the plan, away from the button.
    private func answerButton(_ label: String, _ action: Action, role: ButtonRole? = nil) -> some View {
        Button(label, role: role) { asking = action }
            .confirmationDialog(confirmTitle, isPresented: Binding(get: { asking == action }, set: { if !$0 && asking == action { asking = nil } }),
                                titleVisibility: .visible) {
                Button(confirmButton(action), role: action == .rejected ? .destructive : nil) { send(action) }
            }
    }

    private func sendToPerson() {
        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client = session.client, !why.isEmpty else {
            problem = "Say why a person should look at it."
            return
        }
        working = true
        problem = nil
        Task {
            defer { working = false }
            do {
                let _: Empty = try await client.send("POST", "/api/workflows/\(request.id)/require-human", json: ["reason": why])
                reason = ""
                onAnswered()
            } catch APIError.loggedOut {
                session.forget()
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    private func faceID(_ label: String) -> String { request.needsFaceID ? "\(label) with \(Biometry.name)" : label }

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
    /// Beacon refuses the answer instead of approving something else.
    private func send(_ action: Action) {
        guard let client = session.client else { return }
        var response: [String: String] = [:]
        switch action {
        case .choose_option: response["option"] = option
        case .answered: response["text"] = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        case .changes_requested: if !note.isEmpty { response["note"] = note }
        case .approved, .rejected: break
        }
        let body = WorkflowAnswer(action.rawValue, response, to: request)
        let key = request.needsFaceID ? DeviceKey.load() : nil
        if request.needsFaceID && key == nil {
            problem = "This device has no signing key any more. Log out and log in again."
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
                onAnswered()  // the plan may have changed: show what there is to answer now
            }
        }
    }
}
