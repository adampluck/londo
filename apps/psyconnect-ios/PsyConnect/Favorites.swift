import Foundation
import Observation
import UserNotifications

/// Saved events and their reminders, kept on the device only. A saved
/// event is stored whole, so it still shows after it drops out of the
/// 30-day feed (or off the site).
@MainActor @Observable
final class Favorites {
    static let shared = Favorites()

    enum Reminder: String, Codable, CaseIterable, Identifiable {
        case hourBefore, threeHoursBefore, morningOf, dayBefore
        var id: Self { self }

        var label: String {
            switch self {
            case .hourBefore: "1 hour before"
            case .threeHoursBefore: "3 hours before"
            case .morningOf: "Morning of (9:00)"
            case .dayBefore: "Evening before (18:00)"
            }
        }

        func fireDate(for start: Date) -> Date {
            let cal = London.calendar
            switch self {
            case .hourBefore: return start.addingTimeInterval(-3600)
            case .threeHoursBefore: return start.addingTimeInterval(-3 * 3600)
            case .morningOf:
                return cal.date(bySettingHour: 9, minute: 0, second: 0, of: start) ?? start
            case .dayBefore:
                let prev = cal.date(byAdding: .day, value: -1, to: start) ?? start
                return cal.date(bySettingHour: 18, minute: 0, second: 0, of: prev) ?? start
            }
        }

        var lead: String {
            switch self {
            case .hourBefore: "Starts in an hour"
            case .threeHoursBefore: "Starts in 3 hours"
            case .morningOf: "Today"
            case .dayBefore: "Tomorrow"
            }
        }
    }

    private struct Saved: Codable {
        var event: Event
        var reminder: Reminder?
        var savedAt: Date
    }

    private var saved: [String: Saved] = [:] { didSet { persist() } }
    private let key = "favorites.v1"

    private init() {
        if let data = UserDefaults.standard.data(forKey: key),
           let list = try? JSONDecoder().decode([String: Saved].self, from: data) {
            saved = list
        }
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(saved) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }

    // MARK: saving

    var events: [Event] {
        saved.values.map(\.event).sorted { ($0.startAt ?? .distantFuture) < ($1.startAt ?? .distantFuture) }
    }
    var upcoming: [Event] { events.filter { ($0.endAt ?? $0.startAt ?? .distantFuture) > .now } }
    var past: [Event] { events.filter { ($0.endAt ?? $0.startAt ?? .distantFuture) <= .now }.reversed() }

    func isSaved(_ e: Event) -> Bool { saved[e.id] != nil }
    func event(id: String) -> Event? { saved[id]?.event }

    func toggle(_ e: Event) {
        if isSaved(e) { remove(e) } else { saved[e.id] = Saved(event: e, savedAt: .now) }
    }

    func remove(_ e: Event) {
        cancelReminder(for: e.id)
        saved[e.id] = nil
    }

    /// Keep saved copies current when the feed reloads (times and venues do
    /// change), and move any reminder with them.
    func refresh(from events: [Event]) {
        for e in events {
            guard var s = saved[e.id], s.event != e else { continue }
            let moved = s.event.startAt != e.startAt
            s.event = e
            saved[e.id] = s
            if moved, let r = s.reminder { Task { await setReminder(r, for: e) } }
        }
    }

    // MARK: reminders (local notifications: no server, no paid account)

    func reminder(for e: Event) -> Reminder? { saved[e.id]?.reminder }

    /// Options still in the future for this event.
    func reminderOptions(for e: Event) -> [Reminder] {
        guard let s = e.startAt else { return [] }
        return Reminder.allCases.filter {
            let fire = $0.fireDate(for: s)
            return fire > .now && fire < s
        }
    }

    /// Setting a reminder saves the event too. Returns false if the person
    /// has turned notifications off.
    @discardableResult
    func setReminder(_ r: Reminder?, for e: Event) async -> Bool {
        if saved[e.id] == nil { saved[e.id] = Saved(event: e, savedAt: .now) }
        cancelReminder(for: e.id)
        saved[e.id]?.reminder = nil
        guard let r, let start = e.startAt else { return true }

        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        guard granted else { return false }

        let content = UNMutableNotificationContent()
        content.title = e.title
        content.subtitle = "\(r.lead) · \(London.time(start))"
        content.body = [e.venueName, e.hook].compactMap { $0 }.joined(separator: " — ")
        content.sound = .default
        content.userInfo = ["eventId": e.id]

        let fire = r.fireDate(for: start)
        guard fire > .now else { return true }
        // an interval, not calendar components: immune to time-zone and
        // component-set quirks, and refresh(from:) reschedules if it moves
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: fire.timeIntervalSinceNow, repeats: false)
        try? await center.add(UNNotificationRequest(identifier: "remind:\(e.id)", content: content, trigger: trigger))
        #if DEBUG
        let pending = await center.pendingNotificationRequests().filter { $0.identifier == "remind:\(e.id)" }
        let next = (pending.first?.trigger as? UNTimeIntervalNotificationTrigger)?.nextTriggerDate()
        print("[psyconnect] reminder \(r.rawValue) for \(e.title): pending=\(pending.count) fires=\(next.map { London.longDate($0) + " " + London.time($0) } ?? "nil") expected=\(London.longDate(fire)) \(London.time(fire))")
        #endif
        saved[e.id]?.reminder = r
        return true
    }

    private func cancelReminder(for id: String) {
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ["remind:\(id)"])
    }
}
