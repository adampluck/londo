import AppIntents
import CoreSpotlight
import Foundation

// MARK: Siri / Shortcuts — "What's on at PsyConnect tonight?"

enum WhenOption: String, AppEnum {
    case tonight, tomorrow, weekend

    static let typeDisplayRepresentation: TypeDisplayRepresentation = "When"
    static let caseDisplayRepresentations: [WhenOption: DisplayRepresentation] = [
        .tonight: "tonight",
        .tomorrow: "tomorrow",
        .weekend: "this weekend",
    ]
}

struct WhatsOnIntent: AppIntent {
    static let title: LocalizedStringResource = "What's on"
    static let description = IntentDescription("Hear what PsyConnect has listed tonight, tomorrow or this weekend.")

    @Parameter(title: "When", default: .tonight)
    var when: WhenOption

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let store = EventStore.shared
        if store.events.isEmpty { await store.load() }
        let list = Self.events(store.events, when: when)
        let label = WhenOption.caseDisplayRepresentations[when]?.title ?? "\(when.rawValue)"
        guard !list.isEmpty else {
            return .result(dialog: "Nothing's listed \(label) on PsyConnect yet.")
        }
        let top = list.prefix(3).map { e in
            let t = e.startAt.map { London.time($0) } ?? ""
            return "\(e.title) at \(t)"
        }
        let more = list.count > 3 ? ", and \(list.count - 3) more" : ""
        return .result(dialog: "\(list.count) on \(label): \(top.joined(separator: "; "))\(more).")
    }

    static func events(_ all: [Event], when: WhenOption) -> [Event] {
        let cal = London.calendar
        let today = cal.startOfDay(for: .now)
        return all.filter { e in
            guard !e.hasStarted, let s = e.startAt else { return false }
            switch when {
            case .tonight:
                return cal.isDate(s, inSameDayAs: today)
            case .tomorrow:
                return cal.isDateInTomorrow(s)
            case .weekend:
                // Saturday and Sunday of this week (today, if it's the weekend)
                let wd = cal.component(.weekday, from: today) // 1 Sun … 7 Sat
                let toSat = wd == 1 ? -1 : 7 - wd
                guard let sat = cal.date(byAdding: .day, value: toSat, to: today),
                      let mon = cal.date(byAdding: .day, value: 2, to: sat) else { return false }
                return s >= sat && s < mon
            }
        }
    }
}

struct PsyConnectShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: WhatsOnIntent(),
            phrases: [
                "What's on at \(.applicationName) \(\.$when)",
                "What's on \(.applicationName) \(\.$when)",
                "What's on at \(.applicationName)",
                "Ask \(.applicationName) what's on \(\.$when)",
            ],
            shortTitle: "What's on",
            systemImageName: "sparkles"
        )
    }
}

// MARK: Spotlight — events show up in iPhone search

enum SpotlightIndex {
    static let domain = "events"

    /// Replace the index with the current feed. Each item expires when its
    /// event ends, so search never offers last week's ceremony.
    static func update(with events: [Event]) {
        let items: [CSSearchableItem] = events.filter { !$0.hasStarted }.map { e in
            let a = CSSearchableItemAttributeSet(contentType: .content)
            a.title = e.title
            let when = e.startAt.map { "\(London.shortDay($0)) \(London.timeRange(e))" }
            a.contentDescription = [when, e.venueName, e.hook].compactMap { $0 }.joined(separator: " · ")
            a.keywords = (e.topics ?? []) + (e.tags ?? []) + [e.organizerName, e.venueName].compactMap { $0 }
            a.startDate = e.startAt
            a.endDate = e.endAt
            let item = CSSearchableItem(uniqueIdentifier: e.id, domainIdentifier: domain, attributeSet: a)
            item.expirationDate = e.endAt ?? e.startAt?.addingTimeInterval(3 * 3600) ?? .now.addingTimeInterval(86_400)
            return item
        }
        let index = CSSearchableIndex.default()
        index.deleteSearchableItems(withDomainIdentifiers: [domain]) { _ in
            index.indexSearchableItems(items) { error in
                #if DEBUG
                print("[psyconnect] spotlight indexed \(items.count) events\(error.map { " error: \($0)" } ?? "")")
                #endif
            }
        }
    }
}
