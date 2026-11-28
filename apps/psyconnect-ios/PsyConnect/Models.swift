import Foundation

/// One row of the Supabase `events` table (schema.sql). Nearly everything is
/// optional: the scrapers fill what each source offers.
struct Event: Codable, Identifiable, Hashable, Sendable {
    let source: String
    let sourceId: String
    let sourceUrl: String
    let title: String
    let description: String?
    let startAt: Date?
    let endAt: Date?
    let isAllDay: Bool?
    let venueName: String?
    let address: String?
    let latitude: Double?
    let longitude: Double?
    let imageUrl: String?
    let tags: [String]?
    let priceMin: Double?
    let priceMax: Double?
    let isFree: Bool?
    let organizerName: String?
    let organizerUrl: String?
    let category: String?
    let topics: [String]?
    let traits: [String]?
    let hook: String?
    let qualityScore: Int?
    let area: String?
    let dedupeKey: String?

    var id: String { "\(source):\(sourceId)" }

    var url: URL? { URL(string: sourceUrl) }
    var image: URL? { imageUrl.flatMap(URL.init(string:)) }

    var hasLocation: Bool { latitude != nil && longitude != nil }

    /// hasStarted() in web/app.js: all-day events stay up all day, the rest
    /// drop off once their start time has passed.
    var hasStarted: Bool {
        guard isAllDay != true, let s = startAt else { return false }
        return s < .now
    }

    /// Same threshold as PICK_THRESHOLD in web/app.js.
    var isPick: Bool { (qualityScore ?? 0) >= 75 }

    var priceText: String? {
        if isFree == true { return "Free" }
        func gbp(_ v: Double) -> String {
            v.truncatingRemainder(dividingBy: 1) == 0
                ? "£\(Int(v))" : String(format: "£%.2f", v)
        }
        switch (priceMin, priceMax) {
        case let (lo?, hi?) where hi > lo:
            return lo == 0 ? "Free–\(gbp(hi))" : "\(gbp(lo))–\(gbp(hi))"
        case let (lo?, _) where lo > 0: return gbp(lo)
        case let (_, hi?) where hi > 0: return gbp(hi)
        default: return nil
        }
    }
}

/// The SITE block of sites/psyconnect/config.js (the JSON between the
/// SITE-JSON markers). Only the keys the app uses are decoded.
struct SiteConfig: Codable, Sendable {
    struct Filter: Codable, Sendable {
        var categories: [String]?
        var topics: [String]?
        var exclude: [String]?
        var excludeText: [String]?
    }
    struct Organizers: Codable, Sendable {
        var organizers: [String]?
        var titleMatches: [String]?
        var exclude: [String]?
        var label: String?
        var accent: String?
        var alwaysPick: [String]?
        var alwaysPickMax: Int?
        var maxTotal: Int?
        var maxMobile: Int?
        var windowStages: [Int]?
        var windowDays: Int?
    }
    var displayName: String?
    var tagline: String?
    var topics: [String]?
    var filter: Filter?
    var featured: Organizers?
    var curated: Organizers?
}

/// One entry of practices.json (built by scripts/build_site.py): the
/// practice rail's chips and the /p/<slug>/ pages share it.
struct Practice: Codable, Identifiable, Hashable, Sendable {
    let slug: String
    let label: String
    let terms: [String]?
    let deep: [String]?
    let exclude: [String]?
    let parent: String?
    /// false = don't search the event's tags. JSON is `true`/`false`.
    let tags: Bool?
    var id: String { slug }

    /// Mirror of practiceMatch() in web/app.js / practice_match() in
    /// build_site.py: terms hit title/organiser/tags at a word start; only
    /// the deep phrases are trusted against the description.
    func matches(_ e: Event) -> Bool {
        let tagText = tags == false ? "" : (e.tags ?? []).joined(separator: " ")
        let hay = [e.title, e.organizerName ?? "", tagText]
            .filter { !$0.isEmpty }.joined(separator: " ").lowercased()
        let hits = { (text: String, terms: [String]?) in
            (terms ?? []).contains { SiteFilter.wordStart(text, $0) }
        }
        if hits(hay, exclude) { return false }
        if hits(hay, terms) { return true }
        return hits((e.description ?? "").lowercased(), deep)
    }

    /// SF Symbol standing in for the site's line icons (icons.json).
    var symbol: String {
        switch slug {
        case "ecstatic-dance", "5rhythms": "figure.dance"
        case "sound-bath": "waveform"
        case "cacao-ceremony": "cup.and.heat.waves"
        case "breathwork": "wind"
        case "somatics": "figure.mind.and.body"
        case "qigong": "figure.taichi"
        case "tantra": "heart.circle"
        case "family-constellations": "person.3"
        case "authentic-relating": "bubble.left.and.bubble.right"
        case "contact-improvisation": "figure.2"
        case "bohm-dialogue": "circle.dotted"
        case "kirtan": "music.mic"
        case "shamanic": "flame"
        case "sufi-whirling": "tornado"
        default: "sparkle"
        }
    }
}
