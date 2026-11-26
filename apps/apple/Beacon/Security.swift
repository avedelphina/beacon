import CryptoKit
import Foundation
import LocalAuthentication
import Security

/// The few secrets the app keeps: Beacon's device token and the device key's handle. Only on this
/// device (`ThisDeviceOnly`: not in backups, not moved to a new phone) and only while it is unlocked.
nonisolated enum Keychain {
    private static let service = "beacon"

    static func set(_ data: Data, for account: String) throws {
        delete(account)
        let status = SecItemAdd([kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: account,
                                 kSecAttrAccessible: kSecAttrAccessibleWhenUnlockedThisDeviceOnly, kSecValueData: data] as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw APIError.unreadable("The Keychain refused to keep the login (\(status)).")
        }
    }

    static func get(_ account: String) -> Data? {
        var item: CFTypeRef?
        let status = SecItemCopyMatching([kSecClass: kSecClassGenericPassword, kSecAttrService: service,
                                          kSecAttrAccount: account, kSecReturnData: true] as CFDictionary, &item)
        return status == errSecSuccess ? item as? Data : nil
    }

    static func delete(_ account: String) {
        SecItemDelete([kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: account] as CFDictionary)
    }
}

/// The device's own key. On a phone it lives in the Secure Enclave and signs only after Face ID
/// (`.biometryCurrentSet`: adding a face or finger destroys it, and the app must log in again).
/// Beacon checks its signature on every CL4/CL5 answer (backend/app_devices.py), so the token alone
/// never approves those.
nonisolated struct DeviceKey: Sendable {
    let blob: Data            // the Secure Enclave key's handle (or, in the simulator, the key itself)
    let publicKeyX963: Data

    private static let account = "device-key"

    static func load() -> DeviceKey? {
        guard let blob = Keychain.get(account), let key = try? restore(blob, context: nil) else { return nil }
        return DeviceKey(blob: blob, publicKeyX963: key.publicKeyX963)
    }

    /// A new key, replacing any old one: made at each login.
    static func create() throws -> DeviceKey {
        let made = try make()
        try Keychain.set(made.blob, for: account)
        return made
    }

    static func forget() { Keychain.delete(account) }

    /// Signs `message` (ECDSA P-256 over SHA-256, DER). On a phone this shows Face ID with `reason`;
    /// it blocks until the person answers, so call it off the main actor.
    func sign(_ message: Data, reason: String) throws -> Data {
        let context = LAContext()
        context.localizedReason = reason
        return try Self.restore(blob, context: context).sign(message)
    }

    #if targetEnvironment(simulator)
    // ponytail: the simulator has no Secure Enclave, so there the key is a plain software key. Never on a device.
    private struct Restored { let key: P256.Signing.PrivateKey
        var publicKeyX963: Data { key.publicKey.x963Representation }
        func sign(_ m: Data) throws -> Data { try key.signature(for: m).derRepresentation }
    }
    private static func make() throws -> DeviceKey {
        let key = P256.Signing.PrivateKey()
        return DeviceKey(blob: key.rawRepresentation, publicKeyX963: key.publicKey.x963Representation)
    }
    private static func restore(_ blob: Data, context: LAContext?) throws -> Restored {
        Restored(key: try P256.Signing.PrivateKey(rawRepresentation: blob))
    }
    #else
    private struct Restored { let key: SecureEnclave.P256.Signing.PrivateKey
        var publicKeyX963: Data { key.publicKey.x963Representation }
        func sign(_ m: Data) throws -> Data { try key.signature(for: m).derRepresentation }
    }
    private static func make() throws -> DeviceKey {
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                                                           [.privateKeyUsage, .biometryCurrentSet], &error) else {
            throw error!.takeRetainedValue() as Error
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        return DeviceKey(blob: key.dataRepresentation, publicKeyX963: key.publicKey.x963Representation)
    }
    private static func restore(_ blob: Data, context: LAContext?) throws -> Restored {
        Restored(key: try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: context))
    }
    #endif
}

/// What the device signs: the format Beacon checks (backend/app_devices.py) and that
/// apps/contract/signature-v1.json pins for both sides.
nonisolated enum Signing {
    static let tag = "beacon-app-v1\n"

    static func loginMessage(idToken: String) -> Data {
        Data((tag + "login\n" + hex(SHA256.hash(data: Data(idToken.utf8)))).utf8)
    }

    /// `path` includes the query string, if any; `signedAt` is sent as-is in X-Beacon-Signed-At.
    static func requestMessage(method: String, path: String, signedAt: String, body: Data) -> Data {
        Data((tag + "\(method.uppercased())\n\(path)\n\(signedAt)\n" + hex(SHA256.hash(data: body))).utf8)
    }

    /// The OIDC nonce: binds the ID token to this device's key.
    static func nonce(publicKeyX963: Data) -> String {
        PKCE.base64url(Data(SHA256.hash(data: publicKeyX963)))
    }

    static func hex<D: Sequence>(_ bytes: D) -> String where D.Element == UInt8 {
        bytes.map { String(format: "%02x", $0) }.joined()
    }
}

/// RFC 7636: the app is a public client, so the code is bound to a secret only it knows.
nonisolated enum PKCE {
    static func verifier() -> String { base64url(random(32)) }

    static func challenge(_ verifier: String) -> String { base64url(Data(SHA256.hash(data: Data(verifier.utf8)))) }

    static func random(_ count: Int) -> Data {
        var bytes = [UInt8](repeating: 0, count: count)
        _ = SecRandomCopyBytes(kSecRandomDefault, count, &bytes)
        return Data(bytes)
    }

    static func base64url(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
