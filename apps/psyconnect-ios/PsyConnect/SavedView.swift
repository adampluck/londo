import SwiftUI

struct SavedView: View {
    @Environment(Favorites.self) private var favorites
    @Environment(Router.self) private var router
    @Environment(EventStore.self) private var store
    @Environment(Follows.self) private var follows

    var body: some View {
        @Bindable var router = router
        NavigationStack(path: $router.savedPath) {
            Group {
                if favorites.events.isEmpty && follows.items.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing saved yet", systemImage: "heart")
                    } description: {
                        Text("Tap the heart on an event to keep it here, and set a reminder so you don't miss it. Follow a practice or a host to hear when they list something new.")
                    }
                } else {
                    List {
                        if !follows.items.isEmpty {
                            Section("Following") {
                                ForEach(follows.items) { item in followRow(item) }
                            }
                        }
                        if !favorites.upcoming.isEmpty {
                            Section("Coming up") { rows(favorites.upcoming) }
                        }
                        if !favorites.past.isEmpty {
                            Section("Been and gone") { rows(favorites.past).opacity(0.6) }
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            .background(Color.background)
            .navigationTitle("Saved")
            .appDestinations()
        }
    }

    @ViewBuilder private func followRow(_ item: Follows.Item) -> some View {
        let n = store.events.filter { !$0.hasStarted && follows.matches($0, item, store: store) }.count
        let detail = Text(n == 0 ? "nothing listed right now" : "\(n) coming up")
        Group {
            switch item {
            case .practice(let slug):
                let p = store.practice(slug)
                NavigationLink(value: PracticeRoute(slug: slug)) {
                    Label { Text(p?.label.capitalizedFirst ?? slug).foregroundStyle(Color.ink) + Text("  ") + detail.foregroundStyle(Color.inkDim).font(.caption) }
                        icon: { Image(systemName: p?.symbol ?? "sparkle").foregroundStyle(Color.brand) }
                }
            case .host(let name):
                NavigationLink(value: HostRoute(name: name)) {
                    Label { Text(name).foregroundStyle(Color.ink) + Text("  ") + detail.foregroundStyle(Color.inkDim).font(.caption) }
                        icon: { Image(systemName: "person.2").foregroundStyle(Color.brand) }
                }
            }
        }
        .listRowBackground(Color.background)
        .swipeActions {
            Button("Unfollow", systemImage: "minus.circle", role: .destructive) {
                follows.toggle(item, store: store)
            }
        }
    }

    private func rows(_ list: [Event]) -> some View {
        ForEach(list) { e in
            NavigationLink(value: e) {
                VStack(alignment: .leading, spacing: 0) {
                    if let s = e.startAt {
                        HStack(spacing: 6) {
                            Text(London.dayHeading(s))
                            if let r = favorites.reminder(for: e) {
                                Label(r.label, systemImage: "bell.fill").labelStyle(.titleAndIcon)
                            }
                        }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.brand)
                        .padding(.horizontal)
                        .padding(.top, 8)
                    }
                    EventRow(event: e, ours: store.isOurs(e))
                }
            }
            .listRowInsets(EdgeInsets())
            .listRowBackground(Color.background)
            .swipeActions {
                Button("Remove", systemImage: "heart.slash", role: .destructive) { favorites.remove(e) }
            }
        }
    }
}
