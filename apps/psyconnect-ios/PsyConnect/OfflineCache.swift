import Foundation

/// The last successful feed, kept on disk so the app opens with events
/// even with no signal. The raw PostgREST response is stored and decoded
/// with API.decoder, so it can never drift from the live decoding.
enum OfflineCache {
    private static var dir: URL {
        let d = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("feed", isDirectory: true)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }
    private static var eventsURL: URL { dir.appendingPathComponent("events.json") }
    private static var siteURL: URL { dir.appendingPathComponent("site.json") }

    static func save(raw: Data, site: SiteConfig) {
        try? raw.write(to: eventsURL, options: .atomic)
        if let s = try? JSONEncoder().encode(site) { try? s.write(to: siteURL, options: .atomic) }
    }

    static func load() -> (events: [Event], site: SiteConfig?, fetchedAt: Date)? {
        guard let data = try? Data(contentsOf: eventsURL),
              let events = try? API.decoder.decode([Event].self, from: data),
              let attrs = try? FileManager.default.attributesOfItem(atPath: eventsURL.path),
              let date = attrs[.modificationDate] as? Date
        else { return nil }
        let site = (try? Data(contentsOf: siteURL)).flatMap { try? JSONDecoder().decode(SiteConfig.self, from: $0) }
        return (events, site, date)
    }
}
