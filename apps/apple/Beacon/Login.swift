import AuthenticationServices
import SwiftUI

/// Log in: Zitadel through the system browser sheet (PKCE, the app's own native client), then the ID
/// token to Beacon with this device's new key (backend/app_devices.py). The person types only
/// Beacon's address; Beacon says which Zitadel and client to use.
struct LoginView: View {
    @Environment(Session.self) private var session
    @Environment(\.webAuthenticationSession) private var webAuth
    @State private var address = ""
    @State private var working = false
    @State private var problem: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://beacon.example.com", text: $address)
                        .keyboardType(.URL).textContentType(.URL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit(login)
                } header: {
                    Text("Beacon address")
                } footer: {
                    Text("You log in with the same account as on the web page. Answers that need clearance CL4 or CL5 also ask for \(Biometry.name).")
                }
                Section {
                    Button(action: login) {
                        HStack { Text("Log in"); if working { Spacer(); ProgressView() } }
                    }.disabled(working || address.isEmpty)
                }
                if let problem {
                    Section { Text(problem).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Beacon")
        }
        .onAppear { if address.isEmpty { address = session.server?.absoluteString ?? "" } }
    }

    private func login() {
        guard !working else { return }
        working = true
        problem = nil
        Task {
            defer { working = false }
            do {
                let server = try Self.server(from: address)
                let result = try await Self.logIn(to: server, using: webAuth)
                try session.loggedIn(result.login, at: server)
                await session.checkVersion()
            } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
                // The person closed the sheet: nothing to say.
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    static func server(from text: String) throws -> URL {
        var text = text.trimmingCharacters(in: .whitespaces)
        if !text.contains("://") { text = "https://" + text }
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text), url.scheme == "https", url.host() != nil else {
            throw APIError.unreadable("Beacon's address must be an https:// address.")
        }
        return url
    }

    static func logIn(to server: URL, using webAuth: WebAuthenticationSession) async throws -> (login: LoginResult, key: DeviceKey) {
        let anonymous = Client(server: server, token: nil)
        let config: AppConfig
        do {
            config = try await anonymous.get("/auth/app/config")
        } catch APIError.refused(404, _) {
            throw APIError.unreadable("This Beacon has no app login yet: its operator sets ZITADEL_APP_CLIENT_ID.")
        }
        guard config.api == supportedAPI else {
            throw APIError.unreadable("This Beacon speaks app API \(config.api); this app speaks \(supportedAPI). Update the older one.")
        }

        #if !targetEnvironment(simulator)
        guard Biometry.isSetUp else {
            throw APIError.unreadable("Set up Face ID or Touch ID on this device and allow Beacon to use it: it proves that "
                                      + "CL4 and CL5 answers are yours.")
        }
        #endif
        let key = try DeviceKey.create()
        let verifier = PKCE.verifier(), state = PKCE.base64url(PKCE.random(16))
        let scheme = (Bundle.main.bundleIdentifier ?? "org.example.beacon").lowercased()
        let redirect = "\(scheme):/oauth/callback"
        var authorize = URLComponents(string: config.issuer + "/oauth/v2/authorize")!
        authorize.queryItems = [
            .init(name: "client_id", value: config.clientId), .init(name: "redirect_uri", value: redirect),
            .init(name: "response_type", value: "code"), .init(name: "scope", value: "openid profile email"),
            .init(name: "state", value: state), .init(name: "nonce", value: Signing.nonce(publicKeyX963: key.publicKeyX963)),
            .init(name: "code_challenge", value: PKCE.challenge(verifier)), .init(name: "code_challenge_method", value: "S256"),
        ]
        let callback = try await webAuth.authenticate(using: authorize.url!, callback: .customScheme(scheme),
                                                      additionalHeaderFields: [:])
        let returned = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
        if let error = returned.first(where: { $0.name == "error" })?.value {
            throw APIError.unreadable("The login was refused: \(error)")
        }
        guard returned.first(where: { $0.name == "state" })?.value == state,
              let code = returned.first(where: { $0.name == "code" })?.value else {
            throw APIError.unreadable("The login came back without a valid answer. Try again.")
        }

        let idToken = try await exchange(code: code, verifier: verifier, redirect: redirect, config: config)
        let message = Signing.loginMessage(idToken: idToken)
        let signature = try await Task.detached { try key.sign(message, reason: "Log this device in to Beacon") }.value
        let login: LoginResult = try await anonymous.send("POST", "/auth/app/login", json: [
            "id_token": idToken, "public_key": key.publicKeyX963.base64EncodedString(),
            "name": UIDevice.current.name, "signature": signature.base64EncodedString(),
        ])
        return (login, key)
    }

    /// The authorization code for an ID token, at Zitadel's token endpoint (public client: the PKCE
    /// verifier stands in for a secret).
    private static func exchange(code: String, verifier: String, redirect: String, config: AppConfig) async throws -> String {
        var form = URLComponents()
        form.queryItems = [.init(name: "grant_type", value: "authorization_code"), .init(name: "code", value: code),
                           .init(name: "redirect_uri", value: redirect), .init(name: "client_id", value: config.clientId),
                           .init(name: "code_verifier", value: verifier)]
        var request = URLRequest(url: URL(string: config.issuer + "/oauth/v2/token")!)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data((form.percentEncodedQuery ?? "").utf8)
        let (data, response) = try await URLSession.shared.data(for: request)
        struct Tokens: Decodable { let id_token: String }
        guard (response as? HTTPURLResponse)?.statusCode == 200, let tokens = try? JSONDecoder().decode(Tokens.self, from: data) else {
            throw APIError.unreadable("Zitadel did not give an ID token. Check that the app's Zitadel client allows this app.")
        }
        return tokens.id_token
    }
}
