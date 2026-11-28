import SwiftUI

/// practice-guides.json (build_site.practice_guides_json): the /p/ pages'
/// intro and FAQ, keyed by practice slug.
struct Guide: Codable, Sendable {
    let title: String
    let intro: [String]
    let faq: [[String]]
}

/// Navigation values besides Event.
struct PracticeRoute: Hashable { let slug: String }
struct HostRoute: Hashable { let name: String }
struct PracticesIndexRoute: Hashable {}

extension View {
    /// Every stack can open events, practice guides and host pages.
    func appDestinations() -> some View {
        self
            .navigationDestination(for: Event.self) { EventDetailView(event: $0) }
            .navigationDestination(for: PracticeRoute.self) { PracticeGuideView(slug: $0.slug) }
            .navigationDestination(for: HostRoute.self) { HostView(name: $0.name) }
            .navigationDestination(for: PracticesIndexRoute.self) { _ in PracticesIndexView() }
    }
}

// MARK: guide

struct PracticeGuideView: View {
    let slug: String
    @Environment(EventStore.self) private var store
    @Environment(Browse.self) private var browse
    @Environment(Router.self) private var router
    @Environment(Follows.self) private var follows

    private var practice: Practice? { store.practice(slug) }
    private var guide: Guide? { store.guides[slug] }
    private var upcoming: [Event] {
        store.events.filter { !$0.hasStarted && store.matches($0, practice: slug) }
    }
    private var children: [Practice] { store.practices.filter { $0.parent == slug } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header

                if let guide {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(guide.intro, id: \.self) { p in
                            Text(p).font(.body).foregroundStyle(Color.ink)
                        }
                    }
                }

                if !children.isEmpty {
                    HStack {
                        Text("Includes").font(.subheadline).foregroundStyle(Color.inkDim)
                        ForEach(children) { c in
                            NavigationLink(value: PracticeRoute(slug: c.slug)) {
                                Chip(label: c.label, symbol: c.symbol, selected: false)
                            }.buttonStyle(.plain)
                        }
                    }
                }

                upcomingSection

                if let guide, !guide.faq.isEmpty || priceAnswer != nil {
                    VStack(alignment: .leading, spacing: 4) {
                        SectionTitle("Questions people ask")
                        if let priceAnswer {
                            FAQRow(q: "What does it cost?", a: priceAnswer)
                        }
                        ForEach(guide.faq, id: \.self) { qa in
                            if qa.count == 2 { FAQRow(q: qa[0], a: qa[1]) }
                        }
                    }
                }
            }
            .padding()
        }
        .background(Color.background)
        .navigationTitle(practice?.label.capitalizedFirst ?? "Practice")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                ShareLink(item: URL(string: "https://psyconnect.london/p/\(slug)/")!)
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 14) {
                Image(systemName: practice?.symbol ?? "sparkle")
                    .font(.title)
                    .foregroundStyle(Color.brand)
                    .frame(width: 58, height: 58)
                    .background(Color.brand.opacity(0.1), in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(guide?.title ?? practice?.label.capitalizedFirst ?? slug)
                        .font(.title2.weight(.bold))
                        .foregroundStyle(Color.ink)
                    Text(upcoming.isEmpty ? "Nothing listed right now"
                         : "\(upcoming.count) coming up in the next month")
                        .font(.subheadline)
                        .foregroundStyle(Color.gold)
                }
            }
            FollowButton(item: .practice(slug), label: practice?.label ?? slug)
        }
    }

    @ViewBuilder private var upcomingSection: some View {
        if !upcoming.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                SectionTitle("Coming up")
                ForEach(upcoming.prefix(5)) { e in
                    NavigationLink(value: e) { EventRow(event: e, ours: store.isOurs(e)).padding(.horizontal, -16) }
                        .buttonStyle(.plain)
                    Divider()
                }
                if upcoming.count > 5 {
                    Button("See all \(upcoming.count) on the list") {
                        browse.clear()
                        browse.query = ""
                        browse.practice = slug
                        browse.day = .next(30)
                        router.eventsPath = NavigationPath()
                        router.tab = .events
                    }
                    .font(.subheadline.weight(.semibold))
                    .padding(.top, 12)
                }
            }
        }
    }

    /// The page's live price answer, worked out from the events in hand.
    private var priceAnswer: String? {
        let prices = upcoming.compactMap { e -> Double? in
            if e.isFree == true { return 0 }
            return e.priceMin.flatMap { $0 > 0 ? $0 : nil } ?? e.priceMax.flatMap { $0 > 0 ? $0 : nil }
        }
        guard prices.count >= 3 else { return nil }
        let sorted = prices.sorted()
        let free = sorted.filter { $0 == 0 }.count
        let paid = sorted.filter { $0 > 0 }
        guard !paid.isEmpty else { return "Everything listed right now is free." }
        let median = paid[paid.count / 2]
        func gbp(_ v: Double) -> String { v.rounded() == v ? "£\(Int(v))" : String(format: "£%.2f", v) }
        var s = "Of the \(prices.count) coming up, tickets run from \(gbp(paid.first!)) to \(gbp(paid.last!)), and \(gbp(median)) is typical."
        if free > 0 { s += " \(free) \(free == 1 ? "is" : "are") free or donation-based." }
        return s
    }
}

private struct FAQRow: View {
    let q: String
    let a: String
    @State private var open = false
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(.snappy) { open.toggle() } } label: {
                HStack(alignment: .firstTextBaseline) {
                    Text(q).font(.headline).foregroundStyle(Color.ink).multilineTextAlignment(.leading)
                    Spacer()
                    Image(systemName: "chevron.down")
                        .rotationEffect(.degrees(open ? 180 : 0))
                        .foregroundStyle(Color.inkDim)
                }
                .padding(.vertical, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                Text(a).font(.body).foregroundStyle(Color.ink).padding(.bottom, 12)
            }
            Divider()
        }
    }
}

struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.title3.weight(.semibold))
            .foregroundStyle(Color.gold)
            .padding(.bottom, 4)
    }
}

struct FollowButton: View {
    let item: Follows.Item
    let label: String
    @Environment(Follows.self) private var follows
    @Environment(EventStore.self) private var store

    var body: some View {
        let on = follows.isFollowing(item)
        Button { follows.toggle(item, store: store) } label: {
            Label(on ? "Following \(label)" : "Follow \(label)",
                  systemImage: on ? "checkmark" : "plus")
                .font(.subheadline.weight(.semibold))
                .padding(.horizontal, 14).padding(.vertical, 9)
                .foregroundStyle(on ? Color.brand : Color.background)
                .background(on ? Color.brand.opacity(0.12) : Color.brand, in: Capsule())
        }
        .buttonStyle(.plain)
        .sensoryFeedback(.success, trigger: on)
        .accessibilityHint("Get a notification when new events are listed")
    }
}

// MARK: index

struct PracticesIndexView: View {
    @Environment(EventStore.self) private var store

    var body: some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
                ForEach(store.railPractices) { p in
                    let n = store.events.filter { !$0.hasStarted && store.matches($0, practice: p.slug) }.count
                    NavigationLink(value: PracticeRoute(slug: p.slug)) {
                        VStack(alignment: .leading, spacing: 10) {
                            Image(systemName: p.symbol)
                                .font(.title2)
                                .foregroundStyle(Color.brand)
                            Text(p.label.capitalizedFirst)
                                .font(.headline)
                                .foregroundStyle(Color.ink)
                            Text(n == 0 ? "Nothing on right now" : "\(n) coming up")
                                .font(.caption)
                                .foregroundStyle(Color.inkDim)
                        }
                        .frame(maxWidth: .infinity, minHeight: 110, alignment: .topLeading)
                        .padding(14)
                        .background(Color.card, in: RoundedRectangle(cornerRadius: 18))
                        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.inkDim.opacity(0.15)))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding()
        }
        .background(Color.background)
        .navigationTitle("Practices")
    }
}

// MARK: host

struct HostView: View {
    let name: String
    @Environment(EventStore.self) private var store

    private var upcoming: [Event] {
        store.events.filter { !$0.hasStarted && ($0.organizerName ?? "").lowercased() == name.lowercased() }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(name).font(.title2.weight(.bold)).foregroundStyle(Color.ink)
                FollowButton(item: .host(name), label: name)
                if upcoming.isEmpty {
                    Text("Nothing listed in the next month. Follow to hear when they post something.")
                        .foregroundStyle(Color.inkDim)
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                        SectionTitle("Coming up")
                        ForEach(upcoming) { e in
                            NavigationLink(value: e) { EventRow(event: e, ours: store.isOurs(e)).padding(.horizontal, -16) }
                                .buttonStyle(.plain)
                            Divider()
                        }
                    }
                }
            }
            .padding()
        }
        .background(Color.background)
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
    }
}

extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
