import Foundation

/// Port of siteMatch() in web/app.js (mirrored by site_match in
/// scripts/build_site.py). Keep all three in step; the rules themselves come
/// from SiteConfig, fetched live.
struct SiteFilter: Sendable {
    let site: SiteConfig

    func isOurs(_ e: Event) -> Bool {
        let org = (e.organizerName ?? "").lowercased()
        return (site.featured?.organizers ?? []).contains { org == $0.lowercased() }
    }

    func isCurated(_ e: Event) -> Bool {
        guard let cur = site.curated else { return false }
        let org = (e.organizerName ?? "").lowercased()
        let title = e.title.lowercased()
        if (cur.exclude ?? []).contains(where: { title.contains($0.lowercased()) }) { return false }
        if (cur.organizers ?? []).contains(where: { org == $0.lowercased() }) { return true }
        return (cur.titleMatches ?? []).contains { title.contains($0.lowercased()) }
    }

    /// Exclude terms match at the start of a word, like the JS
    /// `(^|[^a-z0-9])term` regex: title + organiser + tags for `exclude`,
    /// plus the description for `excludeText`.
    func isExcluded(_ e: Event) -> Bool {
        let exclude = site.filter?.exclude ?? []
        let excludeText = site.filter?.excludeText ?? []
        if exclude.isEmpty && excludeText.isEmpty { return false }
        let hay = [e.title, e.organizerName, (e.tags ?? []).joined(separator: " ")]
            .compactMap { $0 }.filter { !$0.isEmpty }
            .joined(separator: " ").lowercased()
        if exclude.contains(where: { Self.wordStart(hay, $0) }) { return true }
        if excludeText.isEmpty { return false }
        let deep = (hay + " " + (e.description ?? "")).lowercased()
        return excludeText.contains { Self.wordStart(deep, $0) }
    }

    func matches(_ e: Event) -> Bool {
        if isOurs(e) { return true }
        guard let filter = site.filter else { return true }
        if isExcluded(e) { return false }
        if isCurated(e) { return true }

        let topics = e.topics ?? []
        let techish = ["tech & ai", "startups & work"]
        let strongScene = ["psychedelics", "consciousness", "spirituality & ritual", "connection & intimacy"]
        // "healing & wellbeing" + tech is how health hackathons leak in
        if topics.contains(where: techish.contains),
           !topics.contains(where: strongScene.contains),
           e.category != "expand" {
            return false
        }
        if let cat = e.category, (filter.categories ?? []).contains(cat) { return true }
        return topics.contains { (filter.topics ?? []).contains($0) }
    }

    static func wordStart(_ hay: String, _ term: String) -> Bool {
        var from = hay.startIndex
        while let r = hay.range(of: term, range: from..<hay.endIndex) {
            if r.lowerBound == hay.startIndex { return true }
            let prev = hay[hay.index(before: r.lowerBound)]
            let isAlnum = prev.isASCII && (prev.isLetter && prev.isLowercase || prev.isNumber)
            if !isAlnum { return true }
            from = hay.index(after: r.lowerBound)
        }
        return false
    }
}
