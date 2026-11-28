import Foundation
import Observation

/// What the person is looking at: one state shared by the list and the
/// map, so the map is always "the current selection, on a map" (like
/// mapEvents() in web/app.js).
@MainActor @Observable
final class Browse {
    /// The app's one instance, so a notification tap can reset filters.
    private(set) static weak var shared: Browse?
    static func make() -> Browse { let b = Browse(); shared = b; return b }

    enum Day: Hashable {
        case next(Int)        // the pinned "7 days" / "30 days" ticks
        case date(Date)       // a London midnight
    }

    var day: Day = .next(7)
    var topic: String?
    var practice: String?
    var vibe: String?
    var area: String?
    var freeOnly = false
    var query = ""

    /// Facets behind the filters button (the practice rail and the strip
    /// are always on screen, so they don't count).
    var panelCount: Int {
        [topic, vibe, area].compactMap { $0 }.count + (freeOnly ? 1 : 0)
    }
    var isRefined: Bool {
        panelCount > 0 || practice != nil || !query.trimmingCharacters(in: .whitespaces).isEmpty
    }

    func clear() {
        topic = nil; practice = nil; vibe = nil; area = nil; freeOnly = false
    }

    /// browseEvents(): a live search scans the whole 30 days; otherwise the
    /// strip's window applies. Started events drop off either way.
    enum Facet { case topic, practice, vibe, area }

    func events(from store: EventStore, ignoringDay: Bool = false, skipping skip: Facet? = nil) -> [Event] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        let useDay = q.isEmpty && !ignoringDay
        return store.events.filter { e in
            if e.hasStarted { return false }
            if useDay && !inWindow(e) { return false }
            return passesFacets(e, store: store, query: q, skipping: skip)
        }
    }

    func passesFacets(_ e: Event, store: EventStore, query q: String, skipping skip: Facet? = nil) -> Bool {
        if freeOnly && e.isFree != true { return false }
        if skip != .topic, let topic, !(e.topics ?? []).contains(topic) { return false }
        if skip != .vibe, let vibe, !(e.traits ?? []).contains(vibe) { return false }
        if skip != .area, let area, e.area != area { return false }
        if skip != .practice, let practice, !store.matches(e, practice: practice) { return false }
        if !q.isEmpty {
            let hay = [e.title, e.description, e.hook, e.venueName, e.organizerName,
                       (e.tags ?? []).joined(separator: " "),
                       (e.topics ?? []).joined(separator: " "),
                       (e.traits ?? []).joined(separator: " ")]
                .compactMap { $0 }.joined(separator: " ").lowercased()
            // every word, anywhere: "sound bath hackney" finds Hackney sound baths
            if !q.split(separator: " ").allSatisfy({ hay.contains($0) }) { return false }
        }
        return true
    }

    func inWindow(_ e: Event) -> Bool {
        guard let s = e.startAt else { return false }
        let today = London.calendar.startOfDay(for: .now)
        switch day {
        case .next(let n):
            return s >= today && s < today.addingTimeInterval(Double(n) * 86_400)
        case .date(let d):
            return London.calendar.isDate(s, inSameDayAs: d)
        }
    }
}

/// VIBES in web/app.js — the traits the enrichment pass writes.
let vibes: [(key: String, label: String)] = [
    ("beginner-friendly", "beginner-friendly"),
    ("ceremony", "ceremony"),
    ("small-group", "small group"),
    ("music", "live music & sound"),
    ("workshop", "workshop"),
    ("embodied", "embodied"),
    ("talky", "talks & discussion"),
    ("daytime", "daytime"),
    ("late-night", "late night"),
    ("touch-based", "touch-based"),
    ("outdoors", "outdoors"),
    ("sober", "sober"),
]

/// AREAS in web/app.js.
let areas = ["central", "north", "east", "south", "west"]
