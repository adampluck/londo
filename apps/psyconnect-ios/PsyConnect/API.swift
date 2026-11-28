import Foundation

/// Reads the same public Supabase endpoint as web/app.js. The anon key is
/// public by design: RLS makes the table read-only for anonymous users.
enum API {
    static let supabaseURL = "https://nhjovwymgfsukpdgvajd.supabase.co"
    static let anonKey =
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5oam92d3ltZ2ZzdWtwZGd2YWpkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODExNjg0NTAsImV4cCI6MjA5Njc0NDQ1MH0.ZcZvGZs0Uhqw4-ZR-s97Y7ktC5YZw-V6XkVzZ43XFFc"
    static let siteConfigURL = URL(string: "https://psyconnect.london/config.js")!

    /// Matches HORIZON_DAYS in web/app.js.
    static let horizonDays = 30

    // MARK: events

    /// Mirror of fetchEvents() in web/app.js: today (London midnight) to
    /// +30 days, canonical rows only, in person, seen in the last 3 days,
    /// not hidden by moderation.
    static func fetchEvents() async throws -> (events: [Event], raw: Data) {
        #if DEBUG
        if UserDefaults.standard.bool(forKey: "offline") { throw URLError(.notConnectedToInternet) }
        #endif
        let today = London.calendar.startOfDay(for: .now)
        let horizon = today.addingTimeInterval(Double(horizonDays) * 86_400)
        let stale = Date.now.addingTimeInterval(-3 * 86_400)
        let iso = { (d: Date) in d.formatted(.iso8601) }

        var c = URLComponents(string: "\(supabaseURL)/rest/v1/events")!
        c.queryItems = [
            .init(name: "select", value: "*"),
            .init(name: "order", value: "start_at.asc"),
            .init(name: "limit", value: "1000"),
            .init(name: "start_at", value: "gte.\(iso(today))"),
            .init(name: "start_at", value: "lte.\(iso(horizon))"),
            .init(name: "duplicate_of", value: "is.null"),
            .init(name: "is_online", value: "eq.false"),
            .init(name: "last_seen_at", value: "gte.\(iso(stale))"),
            .init(name: "hidden", value: "is.false"),
        ]
        var req = URLRequest(url: c.url!)
        req.setValue(anonKey, forHTTPHeaderField: "apikey")
        req.setValue("Bearer \(anonKey)", forHTTPHeaderField: "Authorization")

        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw URLError(.badServerResponse)
        }
        return (try decoder.decode([Event].self, from: data), data)
    }

    /// The first request after a cold launch can fail before the network is
    /// up (same reason as fetchEventsWithRetry in web/app.js).
    static func fetchEventsWithRetry(attempts: Int = 4) async throws -> (events: [Event], raw: Data) {
        var last: Error = URLError(.unknown)
        for i in 0..<attempts {
            do { return try await fetchEvents() } catch {
                last = error
                if i < attempts - 1 {
                    try? await Task.sleep(for: .milliseconds(500 * (i + 1)))
                }
            }
        }
        throw last
    }

    // MARK: site config

    /// Fetch the live SITE block so the app's filter follows any edit to
    /// sites/psyconnect/config.js without an app update; fall back to the
    /// copy bundled at build time.
    static func fetchSiteConfig() async -> SiteConfig {
        if let (data, _) = try? await URLSession.shared.data(from: siteConfigURL),
           let js = String(data: data, encoding: .utf8),
           let site = parseSiteJSON(js) {
            return site
        }
        return bundledSiteConfig
    }

    /// practices.json from the live site (built by build_site.py), falling
    /// back to the copy bundled at build time.
    static func fetchPractices() async -> [Practice] {
        let url = URL(string: "https://psyconnect.london/practices.json")!
        if let (data, _) = try? await URLSession.shared.data(from: url),
           let list = try? JSONDecoder().decode([Practice].self, from: data), !list.isEmpty {
            return list
        }
        return bundledPractices
    }

    /// Practice guides: the live file once the site deploys it, with the
    /// bundled copy filling any practice the live one lacks.
    static func fetchGuides() async -> [String: Guide] {
        var guides = bundledGuides
        let url = URL(string: "https://psyconnect.london/practice-guides.json")!
        if let (data, resp) = try? await URLSession.shared.data(from: url),
           (resp as? HTTPURLResponse)?.statusCode == 200,
           let live = try? JSONDecoder().decode([String: Guide].self, from: data) {
            guides.merge(live) { _, new in new }
        }
        return guides
    }

    static var bundledGuides: [String: Guide] {
        guard let url = Bundle.main.url(forResource: "practice-guides", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let g = try? JSONDecoder().decode([String: Guide].self, from: data)
        else { return [:] }
        return g
    }

    static var bundledPractices: [Practice] {
        guard let url = Bundle.main.url(forResource: "practices", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let list = try? JSONDecoder().decode([Practice].self, from: data)
        else { return [] }
        return list
    }

    static var bundledSiteConfig: SiteConfig {
        guard let url = Bundle.main.url(forResource: "site", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let site = try? JSONDecoder().decode(SiteConfig.self, from: data)
        else { return SiteConfig() }
        return site
    }

    /// Same extraction as scripts/build_site.py:
    /// /\*SITE-JSON\*/(.*?)/\*END-SITE-JSON\*/
    static func parseSiteJSON(_ js: String) -> SiteConfig? {
        guard let start = js.range(of: "/*SITE-JSON*/"),
              let end = js.range(of: "/*END-SITE-JSON*/", range: start.upperBound..<js.endIndex)
        else { return nil }
        let body = js[start.upperBound..<end.lowerBound]
        return try? JSONDecoder().decode(SiteConfig.self, from: Data(body.utf8))
    }

    // MARK: decoding

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        d.dateDecodingStrategy = .custom { dec in
            let s = try dec.singleValueContainer().decode(String.self)
            if let date = parseTimestamp(s) { return date }
            throw DecodingError.dataCorrupted(
                .init(codingPath: dec.codingPath, debugDescription: "bad date \(s)"))
        }
        return d
    }()

    /// PostgREST timestamptz: "2026-11-28T19:00:00+00:00", sometimes with
    /// fractional seconds (up to 6 digits). Strip the fraction, then parse.
    static func parseTimestamp(_ s: String) -> Date? {
        var t = s
        if let dot = t.firstIndex(of: "."), let tz = t[dot...].firstIndex(where: { $0 == "+" || $0 == "-" || $0 == "Z" }) {
            t.removeSubrange(dot..<tz)
        }
        if let d = try? Date(t, strategy: Date.ISO8601FormatStyle(timeZoneSeparator: .colon)) {
            return d
        }
        return try? Date(t, strategy: .iso8601)
    }
}
