import CryptoKit
import Foundation
import Testing
import UserNotifications
@testable import Beacon

/// apps/contract/ is what Beacon's own tests produce and check (tests/test_app_contract.py,
/// tests/test_app_devices.py). Read in place: the simulator sees the checkout.
private func contract(_ name: String) throws -> Data {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    return try Data(contentsOf: root.appending(path: "contract/\(name)"))
}

private struct Vector: Decodable {
    let publicKey: String
    let privateScalarHex: String
    let login: Login
    let request: Request
    struct Login: Decodable { let idToken, nonce, message, signature: String }
    struct Request: Decodable { let method, path, signedAt, body, message, signature: String }
}

private func vector() throws -> Vector { try Client.decoder.decode(Vector.self, from: contract("signature-v1.json")) }

struct SigningTests {
    @Test func theAppBuildsTheMessagesBeaconChecks() throws {
        let v = try vector()
        #expect(String(decoding: Signing.loginMessage(idToken: v.login.idToken), as: UTF8.self) == v.login.message)
        let message = Signing.requestMessage(method: v.request.method, path: v.request.path, signedAt: v.request.signedAt,
                                             body: Data(v.request.body.utf8))
        #expect(String(decoding: message, as: UTF8.self) == v.request.message)
        #expect(Signing.nonce(publicKeyX963: Data(base64Encoded: v.publicKey)!) == v.login.nonce)
    }

    @Test func beaconsSignaturesVerifyHereAndOursVerifyThere() throws {
        let v = try vector()
        let publicKey = try P256.Signing.PublicKey(x963Representation: Data(base64Encoded: v.publicKey)!)
        for (message, signature) in [(v.login.message, v.login.signature), (v.request.message, v.request.signature)] {
            let der = try P256.Signing.ECDSASignature(derRepresentation: Data(base64Encoded: signature)!)
            #expect(publicKey.isValidSignature(der, for: Data(message.utf8)))
            #expect(!publicKey.isValidSignature(der, for: Data((message + "x").utf8)))
        }
        // The same key in CryptoKit gives the same public key, so a CryptoKit signature is one Beacon accepts.
        let scalar = Data(stride(from: 0, to: v.privateScalarHex.count, by: 2).map {
            UInt8(v.privateScalarHex.dropFirst($0).prefix(2), radix: 16)!
        })
        #expect(try P256.Signing.PrivateKey(rawRepresentation: scalar).publicKey.x963Representation == publicKey.x963Representation)
    }

    @Test func theSimulatorsDeviceKeySignsWhatItsPublicKeyVerifies() throws {
        let key = try DeviceKey.create()
        defer { DeviceKey.forget() }
        let signature = try key.sign(Data("hello".utf8), reason: "test")
        let publicKey = try P256.Signing.PublicKey(x963Representation: key.publicKeyX963)
        #expect(publicKey.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: signature), for: Data("hello".utf8)))
        #expect(DeviceKey.load()?.publicKeyX963 == key.publicKeyX963)
    }
}

struct PKCETests {
    @Test func rfc7636AppendixB() {
        #expect(PKCE.challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    }

    @Test func aVerifierIsLongEnoughAndUrlSafe() {
        let verifier = PKCE.verifier()
        #expect(verifier.count >= 43 && !verifier.contains(where: { "+/=".contains($0) }))
        #expect(PKCE.verifier() != verifier)
    }
}

struct DecodingTests {
    @Test func aWorkflowRequestDecodes() throws {
        let request = try Client.decoder.decode(WorkflowRequest.self, from: contract("workflow.json"))
        #expect(request.kind == "decision" && request.options == ["P-256", "P-384"] && request.clearance == 2)
        #expect(request.urgentReason == "expires Friday" && request.consulted?.first?.agent == "beta")
        #expect(!request.planHash.isEmpty && request.isOpen && !request.needsFaceID)
    }

    @Test func aReportAndAReplacedRequestDecode() throws {
        var fields = try JSONSerialization.jsonObject(with: contract("workflow.json")) as! [String: Any]
        fields["replaces_id"] = 7
        fields["report"] = ["outcome": "failed", "note": "the host was down", "by": "alpha", "at": 1.0,
                            "links": [["url": "https://git.example.com/infra/pulls/42", "label": "PR"]]]
        fields["events"] = [["kind": "responded", "to_state": "approved"], ["kind": "state_changed", "to_state": "withdrawn"]]
        let request = try Client.decoder.decode(WorkflowRequest.self, from: JSONSerialization.data(withJSONObject: fields))
        #expect(request.replacesId == 7 && request.report?.outcome == "failed" && request.report?.links?.first?.label == "PR")
        #expect(request.wasApproved)
    }

    @Test func theListDecodes() throws {
        let list = try Client.decoder.decode([WorkflowRequest].self, from: contract("workflows.json"))
        #expect(list.count == 1)
    }

    @Test func aCardDecodesWithItsReferences() throws {
        let card = try Client.decoder.decode(Card.self, from: contract("card.json"))
        #expect(card.references?.first?.url == "https://git.example.com/infra/pulls/42" && card.assignee == "alpha")
    }

    @Test func theVersionCarriesTheAPIThisAppSpeaks() throws {
        let version = try Client.decoder.decode(Version.self, from: contract("version.json"))
        #expect(version.api == supportedAPI)
    }

    @Test func anAnswerThatIsNotAnObjectStillDecodes() throws {
        let json = String(decoding: try contract("workflow.json"), as: UTF8.self)
            .replacingOccurrences(of: "\"response\": null", with: "\"response\": \"just text\"")
        let request = try Client.decoder.decode(WorkflowRequest.self, from: Data(json.utf8))
        #expect(request.response?.text == nil)
    }
}

struct OrderTests {
    @Test func urgentAndSoonestFirstThenWaitingOnTheRequester() throws {
        let base = try JSONSerialization.jsonObject(with: contract("workflow.json")) as! [String: Any]
        func make(_ id: Int, _ state: String, urgent: Bool = false, expires: Double = 100) throws -> WorkflowRequest {
            var fields = base
            fields["id"] = id; fields["state"] = state; fields["expires_at"] = expires
            fields["urgent_reason"] = urgent ? "now" : NSNull()
            return try Client.decoder.decode(WorkflowRequest.self, from: JSONSerialization.data(withJSONObject: fields))
        }
        let items = [try make(1, "approved"), try make(2, "pending", expires: 50), try make(3, "changes_requested"),
                     try make(4, "pending", urgent: true, expires: 90), try make(5, "pending", expires: 10)]
        #expect(RequestsSplitView.sorted(items).map(\.id) == [4, 5, 2, 3, 1])
    }
}

struct NotificationTests {
    private func sent() throws -> [String: [AnyHashable: Any]] {
        try JSONSerialization.jsonObject(with: contract("apns.json")) as! [String: [AnyHashable: Any]]
    }

    @Test func everyCategoryBeaconSendsIsOneTheAppRegisters() throws {
        let registered = Set(Notices.categories().map(\.identifier))
        let used = try sent().values.compactMap { ($0["aps"] as? [String: Any])?["category"] as? String }
        #expect(Set(used) == registered)
    }

    @Test func onlyCL3ApprovalsGetLockScreenActions() throws {
        let actions = Dictionary(uniqueKeysWithValues: Notices.categories().map { ($0.identifier, Set($0.actions.map(\.identifier))) })
        let category = { (name: String) in try (self.sent()[name]?["aps"] as? [String: Any])?["category"] as? String }
        #expect(actions[try category("approve")!] == [Notices.approveAction, Notices.rejectAction, Notices.openAction])
        #expect(actions[try category("human")!] == [Notices.openAction])  // CL4: the app, for Face ID
        #expect(actions[try category("open")!] == [Notices.openAction])
        for action in Notices.categories().flatMap(\.actions) where action.identifier != Notices.openAction {
            #expect(action.options.contains(.authenticationRequired))  // a locked phone cannot approve
        }
    }

    @Test func theRequestToOpenComesFromThePayload() throws {
        let sent = try sent()
        #expect(Notices.workflowID(sent["approve"]!) == 17)
        #expect(Notices.workflowID(sent["human"]!) == 18)
        #expect(Notices.workflowID(sent["plain"]!) == nil)
    }

    @Test func theDeviceTokenIsSentAsLowercaseHex() {
        #expect(Notices.tokenHex(Data([0x0A, 0xFF, 0x00])) == "0aff00")
    }
}

struct FleetDecodingTests {
    private func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try Client.decoder.decode(T.self, from: contract(name))
    }

    @Test func operatorAndMemberViewsOfAgentsBothDecode() throws {
        let full = try decode([Agent].self, "agents.json")
        #expect(full.first?.reach?.endpoint == "http://alpha.test:9900" && full.first?.tags == ["family-a"])
        let trimmed = try decode([Agent].self, "agents-member.json")
        #expect(trimmed.first?.reach?.endpoint == nil && trimmed.first?.memberLogs == false)
        #expect(try decode([String: Availability].self, "availability.json")["alpha"]?.state == "online")
        #expect(try decode([String: Availability].self, "availability-member.json")["alpha"]?.state == "online")
    }

    @Test func statusHealthLogsAndRestartDecode() throws {
        #expect(try decode(AgentStatus.self, "status.json").state == "active")
        #expect(try decode(AgentStatus.self, "status-member.json").subState == "running")
        #expect(try decode([HealthEvent].self, "health-events.json").first?.state == "restarted")
        #expect(try !decode(Logs.self, "logs.json").text.isEmpty)
        let gate = try decode(WouldRun.self, "restart-gate.json")
        #expect(gate.wouldRun && gate.clearance == "CL2")
        #expect(try decode(CommandResult.self, "restart.json").ok)
    }

    @Test func theBoardAndACardsHistoryDecode() throws {
        let cards = try decode([CardSummary].self, "board.json")
        #expect(cards.first?.assignee == "alpha" && Board.states.contains(cards.first!.state))
        #expect(try decode(Card.self, "card.json").events?.isEmpty == false)
    }

    @Test func anAgentIdWithAnUnderscoreKeepsItsName() throws {
        // convertFromSnakeCase must not rename dictionary keys, or availability would miss such an agent.
        let json = Data(#"{"my_agent": {"state": "online"}}"#.utf8)
        #expect(try Client.decoder.decode([String: Availability].self, from: json)["my_agent"] != nil)
    }

    @Test func aCardChangeSendsOnlyWhatChanged() throws {
        let sent = try JSONSerialization.jsonObject(with: JSONEncoder().encode(CardChange(state: "doing"))) as! [String: Any]
        #expect(Array(sent.keys) == ["state"])
        let blocked = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(CardChange(state: "blocked", blockedReason: "waiting", waitingOn: "beta"))) as! [String: Any]
        #expect(blocked["blocked_reason"] as? String == "waiting" && blocked["waiting_on"] as? String == "beta")
    }

    @Test func agentsAreFoundByNameHostOrTag() throws {
        let agents = try decode([Agent].self, "agents.json")
        #expect(AgentsView.matching(agents, "FAMILY").count == 1 && AgentsView.matching(agents, "h1").count == 1)
        #expect(AgentsView.matching(agents, "nothing").isEmpty && AgentsView.matching(agents, "  ").count == 1)
    }
}

struct AgentTextTests {
    @Test func anAgentsLinkIsNeverTappableAndItsAddressIsShown() {
        let shown = AgentText.render("Please check [the **PR**](https://evil.example/x) and <https://ok.example> now")
        #expect(shown.runs.allSatisfy { $0.link == nil })
        #expect(String(shown.characters) == "Please check the PR (https://evil.example/x) and https://ok.example now")
    }

    @Test func otherMarkdownStillRenders() {
        let shown = AgentText.render("1. Publish the **new** key")
        #expect(String(shown.characters) == "1. Publish the new key")
        #expect(shown.runs.contains { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true })
    }

    @Test func onlyWebAddressesBecomeLinksAndEachOnce() throws {
        var fields = try JSONSerialization.jsonObject(with: contract("card.json")) as! [String: Any]
        fields["links"] = [["url": "tel:123"], ["url": "file:///etc/passwd"],
                           ["url": "https://git.example.com/infra/pulls/42", "label": "again"], ["url": "https://b.example"]]
        let card = try Client.decoder.decode(Card.self, from: JSONSerialization.data(withJSONObject: fields))
        #expect(card.sources.map(\.url.absoluteString) == ["https://git.example.com/infra/pulls/42", "https://b.example"])
    }
}

struct MineTests {
    private func make(_ id: Int, to: String, from: String = "alpha", state: String = "pending", sent: Bool = false) throws -> WorkflowRequest {
        var fields = try JSONSerialization.jsonObject(with: contract("workflow.json")) as! [String: Any]
        fields["id"] = id; fields["eligible_responder"] = to; fields["requester"] = from; fields["state"] = state
        fields["require_human"] = sent; fields["urgent_reason"] = NSNull()
        return try Client.decoder.decode(WorkflowRequest.self, from: JSONSerialization.data(withJSONObject: fields))
    }

    @Test func whatWaitsOnMeComesApart() throws {
        let items = [try make(1, to: "human"), try make(2, to: "Tom@Example.com"), try make(3, to: "beta"),
                     try make(4, to: "beta", sent: true), try make(5, to: "ann@example.com"),
                     try make(6, to: "human", from: "tom@example.com"), try make(7, to: "human", state: "approved")]
        let split = RequestsSplitView.split(items, me: "tom@example.com")
        #expect(split.mine.map(\.id).sorted() == [1, 2, 4])
        #expect(split.others.map(\.id).sorted() == [3, 5, 6, 7])
        #expect(RequestsSplitView.split(items, me: nil).mine.isEmpty)
    }

    @Test func notificationStateIsSaidInWords() {
        #expect(SettingsView.describe(.denied) == "Off" && SettingsView.describe(.authorized) == "On")
        #expect(SettingsView.describe(.notDetermined) == "Not asked yet" && SettingsView.describe(nil) == "-")
    }
}

struct SecretTests {
    @Test func theAnswersDecode() throws {
        #expect(try Client.decoder.decode(Secrets.Names.self, from: contract("secrets.json")).names == ["db-password"])
        #expect(try Client.decoder.decode(Secrets.Revealed.self, from: contract("secret-reveal.json")).value == "correct horse")
        #expect(try Client.decoder.decode(Secrets.Saved.self, from: contract("secret-put.json")).path.hasSuffix("api-key"))
    }

    @Test func aNameIsCheckedAsBeaconChecksIt() {
        for good in ["a", "api-key", "db_password.v2", String(repeating: "x", count: 64)] { #expect(Secrets.isName(good)) }
        for bad in ["", "-a", ".env", "a b", "a/b", "a?b", "é", String(repeating: "x", count: 65)] { #expect(!Secrets.isName(bad)) }
    }

    @Test func aPasteCarriesItsConfirmation() throws {
        let sent = try JSONSerialization.jsonObject(with: JSONEncoder().encode(Secrets.Paste(name: "k", value: "v"))) as! [String: Any]
        #expect(sent["confirm"] as? Bool == true && sent["value"] as? String == "v")
    }

    @Test func aTappedSecretNoticeNamesItsAgent() throws {
        let sent = try JSONSerialization.jsonObject(with: contract("apns.json")) as! [String: [AnyHashable: Any]]
        #expect(Notices.secretsAgent(sent["secret"]!) == "alpha" && Notices.workflowID(sent["secret"]!) == nil)
        #expect(Notices.secretsAgent(sent["plain"]!) == nil)
    }

    @Test func nothingBeaconAnswersIsCachedOnDisk() {
        #expect(Client.session.configuration.urlCache?.diskCapacity ?? 0 == 0)
    }
}

struct ProjectTests {
    @Test func theListAndAProjectDecode() throws {
        let list = try Client.decoder.decode([ProjectSummary].self, from: contract("projects.json"))
        #expect(list.first?.name == "infra" && list.first?.cards["todo"] == 1)
        let project = try Client.decoder.decode(Project.self, from: contract("project.json"))
        #expect(project.can.steer && project.state == "active" && !project.isClosed)
        #expect(project.milestones.first?.due == "2026-12-01" && project.status == "Waiting for the approval")
        #expect(project.cardList.count == 1 && !project.events.isEmpty && project.links.first?.label == "repo")
    }

    @Test func progressCountsWhatIsLeft() {
        #expect(Projects.progress(["todo": 1, "doing": 1, "done": 3]) == "2 open · 3 done")
        #expect(Projects.progress([:]) == "no cards")
    }

    @Test func aChangeSendsOnlyWhatChanged() throws {
        let sent = try JSONSerialization.jsonObject(with: JSONEncoder().encode(ProjectChange(state: "paused"))) as! [String: Any]
        #expect(Array(sent.keys) == ["state"])  // never "owner": leaving it out keeps it
    }

    @Test func aMembersCardSaysWhetherTheyMayChangeIt() throws {
        #expect(try Client.decoder.decode(Card.self, from: contract("card-member.json")).can?.change == true)
        #expect(try Client.decoder.decode(Card.self, from: contract("card.json")).mayChange)  // an operator: no `can`
    }

    @Test func aTappedProjectNoticeNamesIt() throws {
        let sent = try JSONSerialization.jsonObject(with: contract("apns.json")) as! [String: [AnyHashable: Any]]
        #expect(Notices.projectID(sent["project"]!) == 3 && Notices.projectID(sent["secret"]!) == nil)
    }

    @Test func aReviewAskedOfAClassSaysSo() throws {
        var fields = try JSONSerialization.jsonObject(with: contract("workflow.json")) as! [String: Any]
        for (asked, label) in [("role:reviewer", "any reviewer"), ("human", "a person"), ("beta", "beta")] {
            fields["eligible_responder"] = asked
            let request = try Client.decoder.decode(WorkflowRequest.self, from: JSONSerialization.data(withJSONObject: fields))
            #expect(request.responderLabel == label)
        }
    }
}

struct ChatTests {
    private func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try Client.decoder.decode(T.self, from: contract(name))
    }

    @Test func whatBeaconAnswersDecodes() throws {
        let agents = try decode([ChatAgent].self, "chat-agents.json")
        #expect(agents.first?.id == "alpha" && agents.first?.connectedAt != nil)
        #expect(try decode([ChatThread].self, "chat-threads.json").first?.agent == "alpha")
        let answer = try decode(ChatAnswer.self, "chat-thread.json")
        #expect(answer.messages.map(\.sender) == ["owner", "agent"] && answer.rev > 0)
        #expect(try decode(ChatMessage.self, "chat-sent.json").state == "queued")
    }

    @Test func otherConversationsDecode() throws {
        let found = try decode(ChatSessions.self, "chat-sessions.json")
        #expect(found.sessions.first?.name == "Otter plans" && found.hints == ["telegram:42"])
        #expect(try decode(ChatHistory.self, "chat-history.json").messages.first?.role == "user")
        #expect(try decode(ChatThread.self, "chat-continued.json").origin == "continued")
    }

    @Test func changedMessagesReplaceAndKeepTheOrder() throws {
        let answer = try decode(ChatAnswer.self, "chat-thread.json")
        let merged = Chat.merge(answer.messages, [answer.messages[1], answer.messages[0]])
        #expect(merged.map(\.id) == answer.messages.map(\.id))
    }

    @Test func aMessageIsSentAsBeaconReadsIt() throws {
        let body = String(decoding: try JSONEncoder().encode(ChatSend(text: "yes", replyTo: 3)), as: UTF8.self)
        #expect(body.contains("\"reply_to\":3") && !body.contains("agent"))
    }

    @Test func anApprovalPromptIsRecognisedOnlyWhileItIsTheLastThingSaid() throws {
        func message(_ id: Int, _ sender: String, _ text: String) throws -> ChatMessage {
            try Client.decoder.decode(ChatMessage.self, from: JSONSerialization.data(withJSONObject: [
                "id": id, "sender": sender, "text": text, "state": "delivered", "created_at": 1.0, "final": true, "rev": id]))
        }
        let prompt = try message(1, "agent", "Dangerous command requires approval: rm -rf x. Reply `/approve` to execute")
        #expect(Chat.awaitsApproval([prompt]))
        #expect(!Chat.awaitsApproval([prompt, try message(2, "owner", "/deny")]))
        #expect(!Chat.awaitsApproval([try message(3, "agent", "You can type /approve when asked.")]))
    }

    @Test func aTappedChatNoticeOpensTheConversationNotTheSecrets() throws {
        let sent = try JSONSerialization.jsonObject(with: contract("apns.json")) as! [String: [AnyHashable: Any]]
        let chat = sent["chat"]!
        #expect(Notices.chat(chat) == ChatTarget(agent: "alpha", thread: 5))
        #expect(Notices.secretsAgent(chat) == nil && Notices.workflowID(chat) == nil)
        #expect(Notices.secretsAgent(sent["secret"]!) == "alpha" && Notices.chat(sent["secret"]!) == nil)
    }
}

struct SoundTests {
    @Test func theAppShipsEveryToneBeaconMayName() throws {
        let named = try JSONDecoder().decode([String].self, from: contract("sounds.json"))
        #expect(named == Tones.files)
        for tone in named {
            #expect(Tones.url(tone) != nil, "\(tone).wav is not in the app's bundle")
        }
        #expect(Tones.url("default") == nil && Tones.url("none") == nil && Tones.url("../x") == nil)
    }

    @Test func theChoiceIsKeptAndSentAsBeaconReadsIt() throws {
        let defaults = try #require(UserDefaults(suiteName: "sounds-test"))
        defaults.removePersistentDomain(forName: "sounds-test")
        #expect(SoundChoices.load(from: defaults) == SoundChoices())
        SoundChoices(requests: "bell", urgent: "default", chat: "none").save(to: defaults)
        #expect(SoundChoices.load(from: defaults).chat == "none")
        let sent = String(decoding: try JSONEncoder().encode(SoundChoices.load(from: defaults)), as: UTF8.self)
        #expect(sent.contains("\"requests\":\"bell\""))
    }
}

struct WaitingTests {
    @Test func theBadgeAndTheStuckFlagDecode() throws {
        let waiting = try Client.decoder.decode(Waiting.self, from: contract("badge.json"))
        #expect(waiting.requests >= 0 && waiting.chats >= 0)
        let cards = try Client.decoder.decode([CardSummary].self, from: contract("board.json"))
        #expect(cards.allSatisfy { $0.stuck == false })
    }

    @Test func aTappedStuckCardsNoticeNamesTheCards() throws {
        let sent = try JSONSerialization.jsonObject(with: contract("apns.json")) as! [String: [AnyHashable: Any]]
        let notice = sent["cards"]!
        #expect(Notices.cardIDs(notice) == [4, 7] && Notices.workflowID(notice) == nil)
        #expect(Notices.chat(notice) == nil && Notices.secretsAgent(notice) == nil && Notices.projectID(notice) == nil)
        #expect((notice["aps"] as? [String: Any])?["badge"] as? Int == 3)
        #expect(Notices.cardIDs(sent["chat"]!) == nil)
    }
}
