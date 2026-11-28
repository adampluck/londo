import Foundation
import Observation

@MainActor @Observable
final class EventStore {
    static let shared = EventStore()

    enum Phase: Equatable { case loading, loaded, failed(String) }

    private(set) var phase: Phase = .loading
    private(set) var events: [Event] = []
    private(set) var site: SiteConfig = API.bundledSiteConfig
    private(set) var practices: [Practice] = API.bundledPractices
    private(set) var guides: [String: Guide] = API.bundledGuides
    private(set) var lastLoaded: Date?
    /// Set when the last refresh failed and we're showing the cached feed:
    /// the time that feed was fetched.
    private(set) var offlineSince: Date?

    var topics: [String] { site.topics ?? [] }
    var railPractices: [Practice] { practices.filter { $0.parent == nil } }

    func load() async {
        // cold start: show the last feed straight away, then refresh
        if events.isEmpty, let cached = OfflineCache.load() {
            apply(rows: cached.events, site: cached.site ?? site)
            lastLoaded = cached.fetchedAt
        }
        if events.isEmpty { phase = .loading }
        async let siteTask = API.fetchSiteConfig()
        async let practicesTask = API.fetchPractices()
        async let guidesTask = API.fetchGuides()
        do {
            let (rows, raw) = try await API.fetchEventsWithRetry()
            let site = await siteTask
            self.practices = await practicesTask
            self.guides = await guidesTask
            apply(rows: rows, site: site)
            self.lastLoaded = .now
            self.offlineSince = nil
            OfflineCache.save(raw: raw, site: site)
            SpotlightIndex.update(with: events)
            // read by the dev check that compares against build_site.py
            print("[psyconnect] matched \(events.count) of \(rows.count) rows")
        } catch {
            _ = await siteTask
            _ = await practicesTask
            _ = await guidesTask
            if events.isEmpty { phase = .failed(error.localizedDescription) }
            else { offlineSince = lastLoaded ?? .now }
        }
    }

    private func apply(rows: [Event], site: SiteConfig) {
        self.site = site
        self.events = rows.filter(SiteFilter(site: site).matches)
        self.phase = .loaded
        Favorites.shared.refresh(from: events)
    }

    func event(id: String) -> Event? { events.first { $0.id == id } }

    /// Reload when the app comes back after a while (data refreshes hourly).
    func refreshIfStale() async {
        guard let t = lastLoaded, Date.now.timeIntervalSince(t) > 15 * 60 else { return }
        await load()
    }

    func isOurs(_ e: Event) -> Bool { SiteFilter(site: site).isOurs(e) }

    func practice(_ slug: String) -> Practice? { practices.first { $0.slug == slug } }

    /// A parent practice takes its children too (ecstatic dance includes
    /// the 5Rhythms nights) — modalityMatch() in web/app.js.
    func matches(_ e: Event, practice slug: String) -> Bool {
        if let p = practice(slug), p.matches(e) { return true }
        return practices.contains { $0.parent == slug && $0.matches(e) }
    }

    // MARK: spotlight (renderSpotlight / pickCurated in web/app.js)

    /// "Our next event": the first upcoming event from SITE.featured.
    var featured: Event? {
        events.first { isOurs($0) && ($0.startAt ?? .distantPast) > .now }
    }

    /// Port of pickCurated(): trusted organisers within the nearest window
    /// stage that fills the row, alwaysPick titles pinned first, one per
    /// organiser round-robin, then repeats; sorted by date.
    var picks: [Event] {
        guard let cfg = site.curated else { return [] }
        let filter = SiteFilter(site: site)
        let maxTotal = cfg.maxTotal ?? 3
        let limit = max(maxTotal, cfg.maxMobile ?? 0)
        guard limit > 0 else { return [] }
        let skipURL = featured?.sourceUrl
        let now = Date.now
        let today = London.calendar.startOfDay(for: now)
        let excludeTerms = (cfg.exclude ?? []).map { $0.lowercased() }

        func eligible(_ e: Event, days: Int, allowToday: Bool = false) -> Bool {
            if let skipURL, e.sourceUrl == skipURL { return false }
            guard let t = e.startAt, t > now, t <= now.addingTimeInterval(Double(days) * 86_400)
            else { return false }
            if !allowToday && London.calendar.isDate(t, inSameDayAs: today) { return false }
            let title = e.title.lowercased()
            return !excludeTerms.contains { title.contains($0) }
        }

        let stages = (cfg.windowStages?.isEmpty == false) ? cfg.windowStages! : [cfg.windowDays ?? 7]
        var candidates: [Event] = []
        for days in stages {
            candidates = events.filter { filter.isCurated($0) && eligible($0, days: days) }
            if candidates.count >= limit { break }
        }

        let alwaysTerms = (cfg.alwaysPick ?? []).map { $0.lowercased() }
        func isAlways(_ e: Event) -> Bool {
            let text = "\(e.title) \(e.description ?? "")".lowercased()
            return alwaysTerms.contains { text.contains($0) }
        }
        var picks: [Event] = []
        if !alwaysTerms.isEmpty {
            picks = Array(events
                .filter { eligible($0, days: stages.last!, allowToday: true) && isAlways($0) }
                .prefix(min(limit, cfg.alwaysPickMax ?? limit)))
            candidates.removeAll(where: isAlways)
        }

        var byOrg: [String: [Event]] = [:]
        var orgOrder: [String] = []
        for e in candidates {
            let k = (e.organizerName ?? "").lowercased()
            if byOrg[k] == nil { orgOrder.append(k) }
            byOrg[k, default: []].append(e)
        }
        var used = Set(picks.map(\.id))
        for org in orgOrder where picks.count < limit {
            let e = byOrg[org]![0]
            picks.append(e); used.insert(e.id)
        }
        for e in candidates where picks.count < limit && !used.contains(e.id) {
            picks.append(e); used.insert(e.id)
        }
        return picks.sorted { ($0.startAt ?? .distantFuture) < ($1.startAt ?? .distantFuture) }
    }
}
