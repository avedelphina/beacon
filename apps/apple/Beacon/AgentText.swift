import LocalAuthentication
import SwiftUI

/// Text that agents write, and what a person may tap in it.
nonisolated enum AgentText {
    /// Markdown for bold, italics and code, but never a link: as on the web page, an agent's words are
    /// not tappable, so "[the PR](https://elsewhere)" cannot send someone where they did not mean to go.
    /// A link is shown as its label with the address written out beside it.
    static func render(_ text: String) -> AttributedString {
        guard let parsed = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) else {
            return AttributedString(text)
        }
        var out = AttributedString()
        for (link, range) in parsed.runs[\.link] {
            var piece = AttributedString(parsed[range])
            piece.link = nil
            out += piece
            if let link, String(piece.characters) != link.absoluteString {
                out += AttributedString(" (\(link.absoluteString))")
            }
        }
        return out
    }
}

/// Where a card points outside Beacon (its references, then its links). These are tappable, so each
/// shows the host it goes to: the label is whatever the agent typed.
struct SourceLinks: View {
    let card: Card

    var body: some View { LinkList(links: card.sources) }
}

struct LinkList: View {
    let links: [(label: String, url: URL)]

    var body: some View {
        ForEach(links, id: \.url) { source in
            Link(destination: source.url) {
                VStack(alignment: .leading) {
                    Text(verbatim: source.label)
                    Text(verbatim: source.url.host() ?? source.url.absoluteString).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}

nonisolated enum SafeLinks {
    /// Web addresses only, each once: never a file:, tel: or another app's scheme that an agent typed.
    static func from(_ typed: [(String, String)]) -> [(label: String, url: URL)] {
        var seen = Set<URL>()
        return typed.compactMap { label, address in
            guard let url = URL(string: address), url.scheme == "https" || url.scheme == "http", seen.insert(url).inserted else { return nil }
            return (label, url)
        }
    }
}

nonisolated extension Card {
    var sources: [(label: String, url: URL)] {
        SafeLinks.from((references ?? []).map { ("\($0.provider) \($0.kind.replacingOccurrences(of: "_", with: " ")) \($0.externalId)", $0.url) }
            + (links ?? []).map { ($0.label ?? $0.url, $0.url) })
    }
}

/// What this device calls its biometry: many iPads have Touch ID.
nonisolated enum Biometry {
    static var name: String {
        let context = LAContext()
        _ = context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        return context.biometryType == .touchID ? "Touch ID" : "Face ID"
    }

    static var isSetUp: Bool { LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil) }
}
