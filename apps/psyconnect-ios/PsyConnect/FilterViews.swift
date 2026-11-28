import SwiftUI

/// The site's week strip: pinned "7 days" / "30 days", then today, tmrw and
/// every day of the 30-day horizon.
struct DateStrip: View {
    @Environment(Browse.self) private var browse
    @Environment(EventStore.self) private var store

    private var days: [Date] {
        let today = London.calendar.startOfDay(for: .now)
        return (0..<API.horizonDays).compactMap {
            London.calendar.date(byAdding: .day, value: $0, to: today)
        }
    }

    /// Days with nothing on are dimmed, never hidden (the strip shouldn't
    /// jump around as filters change).
    private var busyDays: Set<Date> {
        Set(browse.events(from: store, ignoringDay: true).compactMap {
            $0.startAt.map { London.calendar.startOfDay(for: $0) }
        })
    }

    var body: some View {
        let searching = !browse.query.trimmingCharacters(in: .whitespaces).isEmpty
        let busy = busyDays
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    tick("7", "days", .next(7), searching: searching)
                    tick("30", "days", .next(30), searching: searching)
                    Divider().frame(height: 30).padding(.horizontal, 2)
                    ForEach(Array(days.enumerated()), id: \.element) { i, d in
                        let main = i == 0 ? "today" : i == 1 ? "tmrw" : London.weekdayShort(d)
                        let sub = i < 2 ? London.weekdayShort(d) : London.dayNumber(d)
                        tick(main, sub, .date(d), searching: searching, dim: !busy.contains(d))
                    }
                }
                .padding(.horizontal)
                .padding(.vertical, 6)
            }
            .onChange(of: browse.day) { _, day in
                withAnimation { proxy.scrollTo(day, anchor: .center) }
            }
        }
        .sensoryFeedback(.selection, trigger: browse.day)
    }

    private func tick(_ main: String, _ sub: String, _ day: Browse.Day,
                      searching: Bool, dim: Bool = false) -> some View {
        let on = !searching && browse.day == day
        return Button { browse.day = day } label: {
            VStack(spacing: 1) {
                Text(main).font(.subheadline.weight(.semibold))
                Text(sub).font(.caption2)
            }
            .frame(minWidth: 46)
            .padding(.vertical, 6)
            .foregroundStyle(on ? Color.background : Color.ink)
            .background(on ? Color.brand : Color.card, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.inkDim.opacity(on ? 0 : 0.2)))
            .opacity(dim && !on ? 0.45 : 1)
        }
        .buttonStyle(.plain)
        .id(day)
    }
}

/// psyconnect's practice rail: one icon pill per top-level practice.
struct PracticeRail: View {
    @Environment(Browse.self) private var browse
    @Environment(EventStore.self) private var store

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(store.railPractices) { p in
                    Button {
                        browse.practice = browse.practice == p.slug ? nil : p.slug
                    } label: {
                        Chip(label: p.label, symbol: p.symbol, selected: browse.practice == p.slug)
                    }
                }
                NavigationLink(value: PracticesIndexRoute()) {
                    Chip(label: "All practices", symbol: "book", selected: false)
                }
            }
            .padding(.horizontal)
        }
        .buttonStyle(.plain)
        .sensoryFeedback(.selection, trigger: browse.practice)
    }
}

struct Chip: View {
    let label: String
    var symbol: String?
    let selected: Bool
    var dim = false
    var body: some View {
        HStack(spacing: 6) {
            if let symbol { Image(systemName: symbol).imageScale(.small) }
            Text(label)
            if selected { Image(systemName: "xmark").imageScale(.small).fontWeight(.bold) }
        }
        .font(.subheadline.weight(.medium))
        .padding(.horizontal, 13).padding(.vertical, 8)
        .foregroundStyle(selected ? Color.background : Color.ink)
        .background(selected ? Color.brand : Color.card, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.inkDim.opacity(selected ? 0 : 0.25)))
        .opacity(dim && !selected ? 0.4 : 1)
    }
}

/// The site's filters panel: what / modality / vibe / where, plus free.
/// Single-select per row; tapping the lit chip clears it. Options with
/// nothing behind them are dimmed, not hidden.
struct FilterSheet: View {
    @Environment(Browse.self) private var browse
    @Environment(EventStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var browse = browse
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Toggle("Free events only", isOn: $browse.freeOnly)
                        .tint(.brand)
                    row("What", options: store.topics.map { ($0, $0) }, value: $browse.topic, facet: .topic,
                        test: { e, v in (e.topics ?? []).contains(v) })
                    row("Practice", options: store.practices.map { ($0.slug, $0.parent == nil ? $0.label : "↳ \($0.label)") },
                        value: $browse.practice, facet: .practice, test: { e, v in store.matches(e, practice: v) })
                    row("Vibe", options: vibes.filter { v in
                            store.events.filter { ($0.traits ?? []).contains(v.key) }.count >= 5
                        }.map { ($0.key, $0.label) },
                        value: $browse.vibe, facet: .vibe, test: { e, v in (e.traits ?? []).contains(v) })
                    row("Where", options: areas.map { ($0, $0) }, value: $browse.area, facet: .area,
                        test: { e, v in e.area == v })
                }
                .padding()
            }
            .background(Color.background)
            .navigationTitle("Filters")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Clear all") { browse.clear() }.disabled(!browse.isRefined)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Show \(browse.events(from: store).count)") { dismiss() }
                        .fontWeight(.semibold)
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    /// `test` counts each option against everything else that's lit, so a
    /// chip is only dimmed when picking it really would show nothing.
    private func row(_ title: String, options: [(String, String)], value: Binding<String?>,
                     facet: Browse.Facet, test: @escaping (Event, String) -> Bool) -> some View {
        let base = browse.events(from: store, skipping: facet)
        return VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundStyle(Color.inkDim)
            FlowLayout(spacing: 8) {
                ForEach(options, id: \.0) { key, label in
                    Button {
                        value.wrappedValue = value.wrappedValue == key ? nil : key
                    } label: {
                        Chip(label: label, selected: value.wrappedValue == key,
                             dim: !base.contains { test($0, key) })
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

/// Wraps chips onto as many lines as they need.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(proposal.width ?? .infinity, subviews)
        let h = rows.map(\.height).reduce(0, +) + spacing * CGFloat(max(rows.count - 1, 0))
        return CGSize(width: proposal.width ?? rows.map(\.width).max() ?? 0, height: h)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in arrange(bounds.width, subviews) {
            var x = bounds.minX
            for i in row.items {
                let s = subviews[i].sizeThatFits(.unspecified)
                subviews[i].place(at: CGPoint(x: x, y: y), proposal: .init(s))
                x += s.width + spacing
            }
            y += row.height + spacing
        }
    }

    private struct Row { var items: [Int] = []; var width: CGFloat = 0; var height: CGFloat = 0 }

    private func arrange(_ maxWidth: CGFloat, _ subviews: Subviews) -> [Row] {
        var rows: [Row] = [Row()]
        for (i, v) in subviews.enumerated() {
            let s = v.sizeThatFits(.unspecified)
            if !rows[rows.count - 1].items.isEmpty && rows[rows.count - 1].width + spacing + s.width > maxWidth {
                rows.append(Row())
            }
            var r = rows[rows.count - 1]
            r.width += (r.items.isEmpty ? 0 : spacing) + s.width
            r.height = max(r.height, s.height)
            r.items.append(i)
            rows[rows.count - 1] = r
        }
        return rows
    }
}
