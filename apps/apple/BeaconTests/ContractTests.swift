import CryptoKit
import Foundation
import Testing
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
