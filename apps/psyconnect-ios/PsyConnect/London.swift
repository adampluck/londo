import Foundation

/// Every date in the app is shown in London time, whatever the phone's zone.
enum London {
    static let timeZone = TimeZone(identifier: "Europe/London")!

    static let calendar: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = timeZone
        c.locale = Locale(identifier: "en_GB")
        c.firstWeekday = 2
        return c
    }()

    private static func style(_ f: Date.FormatStyle) -> Date.FormatStyle {
        var f = f
        f.timeZone = timeZone
        f.locale = Locale(identifier: "en_GB")
        f.calendar = calendar
        return f
    }

    /// 24-hour "19:30", like formatTime() on the site.
    static func time(_ d: Date) -> String {
        let c = calendar.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    /// Strip ticks: "mon" / "1".
    static func weekdayShort(_ d: Date) -> String {
        d.formatted(style(.dateTime.weekday(.abbreviated))).lowercased()
    }
    static func dayNumber(_ d: Date) -> String {
        String(calendar.component(.day, from: d))
    }

    static func dayHeading(_ d: Date) -> String {
        if calendar.isDateInToday(d) { return "Today" }
        if calendar.isDateInTomorrow(d) { return "Tomorrow" }
        return d.formatted(style(.dateTime.weekday(.wide).day().month(.wide)))
    }

    static func longDate(_ d: Date) -> String {
        d.formatted(style(.dateTime.weekday(.wide).day().month(.wide)))
    }

    static func shortDay(_ d: Date) -> String {
        d.formatted(style(.dateTime.weekday(.abbreviated).day().month(.abbreviated)))
    }

    static func timeRange(_ e: Event) -> String {
        guard let s = e.startAt else { return "" }
        if e.isAllDay == true { return "All day" }
        guard let end = e.endAt, end > s else { return time(s) }
        if calendar.isDate(s, inSameDayAs: end) { return "\(time(s)) – \(time(end))" }
        return "\(time(s)) – \(shortDay(end)) \(time(end))"
    }
}
