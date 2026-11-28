import SwiftUI

struct EventListView: View {
    @Environment(EventStore.self) private var store
    @Environment(Browse.self) private var browse
    @Environment(Router.self) private var router
    @State private var showFilters = false

    private var days: [(day: Date, events: [Event])] {
        let list = browse.events(from: store)
        let groups = Dictionary(grouping: list.filter { $0.startAt != nil }) {
            London.calendar.startOfDay(for: $0.startAt!)
        }
        return groups.keys.sorted().map { ($0, groups[$0]!) }
    }

    var body: some View {
        @Bindable var browse = browse
        @Bindable var router = router
        NavigationStack(path: $router.eventsPath) {
            content
                .background(Color.background)
                .navigationTitle("PsyConnect")   // the back button's label
                .toolbar(removing: .title)
                .navigationBarTitleDisplayMode(.inline)
                .appDestinations()
                .searchable(text: $browse.query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search events, venues, hosts")
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) { Wordmark() }
                        .sharedBackgroundHidden()
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { showFilters = true } label: {
                            Image(systemName: browse.panelCount > 0
                                  ? "line.3.horizontal.decrease.circle.fill"
                                  : "line.3.horizontal.decrease.circle")
                        }
                        .badge(browse.panelCount)
                        .accessibilityLabel("Filters")
                    }
                }
                .sheet(isPresented: $showFilters) { FilterSheet() }
                #if DEBUG
                .task(id: router.debugSheet) {
                    if router.debugSheet == "filters" { router.debugSheet = nil; showFilters = true }
                }
                #endif
        }
    }

    @ViewBuilder private var content: some View {
        switch store.phase {
        case .loading where store.events.isEmpty:
            ProgressView("Gathering this month's events…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let msg):
            ContentUnavailableView {
                Label("Couldn't load events", systemImage: "wifi.slash")
            } description: { Text(msg) } actions: {
                Button("Try again") { Task { await store.load() } }
                    .buttonStyle(.borderedProminent)
            }
        default:
            list
        }
    }

    private var list: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                if store.offlineSince != nil { OfflineBanner() }
                if !browse.isRefined {
                    NewForYouView()
                    SpotlightView().padding(.top, 8)
                }

                Section {
                    PracticeRail().padding(.vertical, 10)
                    if let slug = browse.practice { GuideCard(slug: slug) }
                    if browse.panelCount > 0 { activeFilters }

                    if days.isEmpty {
                        ContentUnavailableView {
                            Label("Nothing matches", systemImage: "magnifyingglass")
                        } description: {
                            Text("Try another day or loosen a filter.")
                        } actions: {
                            if browse.isRefined {
                                Button("Clear filters") { browse.clear(); browse.query = "" }
                            }
                        }
                        .padding(.top, 30)
                    }
                    ForEach(days, id: \.day) { group in
                        DayHeader(day: group.day, count: group.events.count)
                        ForEach(group.events) { e in
                            NavigationLink(value: e) { EventRow(event: e, ours: store.isOurs(e)) }
                                .buttonStyle(.plain)
                                .eventContextMenu(e)
                            Divider().padding(.leading, 104)
                        }
                    }
                } header: {
                    DateStrip().background(Color.background)
                }
            }
            .padding(.bottom, 20)
        }
        // .immediately would close the keyboard as soon as focusing the
        // search field nudges the scroll position
        .scrollDismissesKeyboard(.interactively)
        .refreshable { await store.load() }
    }

    /// The lit panel filters, each removable in one tap.
    private var activeFilters: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                if browse.freeOnly { pill("free") { browse.freeOnly = false } }
                if let t = browse.topic { pill(t) { browse.topic = nil } }
                if let v = browse.vibe { pill(vibes.first { $0.key == v }?.label ?? v) { browse.vibe = nil } }
                if let a = browse.area { pill(a) { browse.area = nil } }
            }
            .padding(.horizontal)
        }
        .padding(.bottom, 6)
    }

    private func pill(_ label: String, clear: @escaping () -> Void) -> some View {
        Button(action: clear) { Chip(label: label, selected: true) }.buttonStyle(.plain)
    }
}

private struct DayHeader: View {
    let day: Date
    let count: Int
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(London.dayHeading(day))
                .font(.title3.weight(.semibold))
                .foregroundStyle(Color.gold)
            Spacer()
            Text("\(count) event\(count == 1 ? "" : "s")")
                .font(.caption)
                .foregroundStyle(Color.inkDim)
        }
        .padding(.horizontal).padding(.top, 16).padding(.bottom, 4)
    }
}

struct EventRow: View {
    let event: Event
    var ours = false
    @Environment(Favorites.self) private var favorites

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            EventImage(event: event)
                .frame(width: 76, height: 76)
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .overlay(alignment: .topTrailing) {
                    if favorites.isSaved(event) {
                        Image(systemName: "heart.fill")
                            .font(.caption2)
                            .foregroundStyle(.white)
                            .padding(5)
                            .background(Color.brand, in: Circle())
                            .offset(x: 5, y: -5)
                    }
                }
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(London.timeRange(event))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.gold)
                    if ours { Badge(text: "Our event") }
                    else if event.isPick { Badge(text: "✦ pick") }
                }
                Text(event.title)
                    .font(.headline)
                    .foregroundStyle(Color.ink)
                    .lineLimit(2)
                if let hook = event.hook {
                    Text(hook)
                        .font(.subheadline)
                        .foregroundStyle(Color.inkDim)
                        .lineLimit(2)
                }
                HStack(spacing: 8) {
                    if let venue = event.venueName {
                        Label(venue, systemImage: "mappin")
                            .lineLimit(1)
                    }
                    if let price = event.priceText {
                        Text(price).fontWeight(.medium)
                    }
                }
                .font(.caption)
                .foregroundStyle(Color.inkDim)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal).padding(.vertical, 12)
        .contentShape(Rectangle())
    }
}

struct Badge: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .foregroundStyle(Color.brand)
            .background(Color.brand.opacity(0.12), in: Capsule())
    }
}

extension View {
    /// Long-press: save / unsave and share, without opening the event.
    func eventContextMenu(_ e: Event) -> some View {
        modifier(EventContextMenu(event: e))
    }
}

private struct EventContextMenu: ViewModifier {
    let event: Event
    @Environment(Favorites.self) private var favorites
    func body(content: Content) -> some View {
        content.contextMenu {
            Button {
                favorites.toggle(event)
            } label: {
                favorites.isSaved(event)
                    ? Label("Remove from saved", systemImage: "heart.slash")
                    : Label("Save", systemImage: "heart")
            }
            if let url = event.url {
                ShareLink(item: url) { Label("Share", systemImage: "square.and.arrow.up") }
            }
        }
    }
}
