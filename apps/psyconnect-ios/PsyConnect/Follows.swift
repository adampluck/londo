import Foundation
import Observation
import UserNotifications

/// Practices and hosts the person follows, and which of their events
/// they've already been told about. Kept on the device only.
@MainActor @Observable
final class Follows {
    static let shared = Follows()

    enum Item: Hashable, Codable, Identifiable {
        case practice(String)   // slug
        case host(String)       // organiser name as listed

        var id: String {
            switch self {
            case .practice(let s): "practice:\(s)"
            case .host(let n): "host:\(n.lowercased())"
            }
        }
    }

    private(set) var items: [Item] = [] { didSet { save() } }
    /// Event ids already surfaced, in the app or by notification. A new
    /// follow marks everything it matches today as seen, so following
    /// cacao doesn't announce the 12 cacao nights already listed.
    private var seen: Set<String> = [] { didSet { save() } }
    /// Event ids already sent in a notification (seen in-app or not).
    private var notified: Set<String> = [] { didSet { save() } }

    private let key = "follows.v1"
    private struct Stored: Codable { var items: [Item]; var seen: [String]; var notified: [String] }

    private init() {
        if let data = UserDefaults.standard.data(forKey: key),
           let s = try? JSONDecoder().decode(Stored.self, from: data) {
            items = s.items; seen = Set(s.seen); notified = Set(s.notified)
        }
    }

    private func save() {
        let s = Stored(items: items, seen: Array(seen), notified: Array(notified))
        if let data = try? JSONEncoder().encode(s) { UserDefaults.standard.set(data, forKey: key) }
    }

    func isFollowing(_ i: Item) -> Bool { items.contains(i) }

    func toggle(_ i: Item, store: EventStore) {
        if let idx = items.firstIndex(of: i) {
            items.remove(at: idx)
        } else {
            items.append(i)
            let ids = store.events.filter { matches($0, i, store: store) }.map(\.id)
            seen.formUnion(ids)
            notified.formUnion(ids)
            Task { _ = try? await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge]) }
        }
    }

    func matches(_ e: Event, _ i: Item, store: EventStore) -> Bool {
        switch i {
        case .practice(let slug): store.matches(e, practice: slug)
        case .host(let name): (e.organizerName ?? "").lowercased() == name.lowercased()
        }
    }

    /// What the event matched, for the "new" label ("cacao", "Numinity").
    func reason(_ e: Event, store: EventStore) -> String? {
        for i in items where matches(e, i, store: store) {
            switch i {
            case .practice(let slug): return store.practice(slug)?.label ?? slug
            case .host(let name): return name
            }
        }
        return nil
    }

    /// Upcoming events from anything followed that haven't been shown yet.
    func newEvents(in store: EventStore) -> [Event] {
        guard !items.isEmpty else { return [] }
        return store.events.filter { e in
            !e.hasStarted && !seen.contains(e.id) && items.contains { matches(e, $0, store: store) }
        }
    }

    func markSeen(_ events: [Event]) { seen.formUnion(events.map(\.id)) }

    /// Background check: one summary notification for anything new that
    /// hasn't been notified yet. Returns how many were announced.
    @discardableResult
    func notifyNew(in store: EventStore) async -> Int {
        let fresh = newEvents(in: store).filter { !notified.contains($0.id) }
        guard !fresh.isEmpty else { return 0 }
        notified.formUnion(fresh.map(\.id))

        let content = UNMutableNotificationContent()
        if fresh.count == 1, let e = fresh.first {
            content.title = "New: \(e.title)"
            content.body = [e.startAt.map { "\(London.shortDay($0)) \(London.time($0))" }, e.venueName]
                .compactMap { $0 }.joined(separator: " · ")
            content.userInfo = ["eventId": e.id]
        } else {
            let reasons = Array(Set(fresh.compactMap { reason($0, store: store) })).sorted()
            content.title = "\(fresh.count) new events for you"
            content.body = "From " + ListFormatter.localizedString(byJoining: Array(reasons.prefix(3)))
            content.userInfo = ["route": "new"]
        }
        content.sound = .default
        try? await UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "follows:\(UUID().uuidString)", content: content, trigger: nil))
        return fresh.count
    }

    #if DEBUG
    /// `-followTest <slug>`: follow a practice, then forget the two
    /// soonest matches so the "new" check has exactly those to find.
    func debugFollow(_ slug: String, store: EventStore) {
        let item = Item.practice(slug)
        if !isFollowing(item) { toggle(item, store: store) }
        let forget = store.events.filter { !$0.hasStarted && matches($0, item, store: store) }.prefix(2).map(\.id)
        seen.subtract(forget); notified.subtract(forget)
        print("[psyconnect] followTest \(slug): forgot \(forget.count), new=\(newEvents(in: store).count)")
    }
    #endif
}
