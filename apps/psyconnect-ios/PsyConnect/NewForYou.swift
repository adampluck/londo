import SwiftUI

/// Events from followed practices and hosts that haven't been shown yet.
/// Stays until "Got it", so nothing slips by unseen.
struct NewForYouView: View {
    @Environment(EventStore.self) private var store
    @Environment(Follows.self) private var follows

    var body: some View {
        let fresh = follows.newEvents(in: store)
        if !fresh.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline) {
                    (Text("New ").foregroundStyle(Color.gold) + Text("for you"))
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(Color.ink)
                    Spacer()
                    Button("Got it") { withAnimation { follows.markSeen(fresh) } }
                        .font(.subheadline.weight(.semibold))
                }
                .padding(.horizontal).padding(.top, 8)
                ForEach(fresh.prefix(6)) { e in
                    NavigationLink(value: e) {
                        VStack(alignment: .leading, spacing: 0) {
                            if let why = follows.reason(e, store: store) {
                                Label(why, systemImage: "bell.badge")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(Color.brand)
                                    .padding(.horizontal).padding(.top, 10)
                            }
                            EventRow(event: e, ours: store.isOurs(e))
                        }
                    }
                    .buttonStyle(.plain)
                    .eventContextMenu(e)
                }
                if fresh.count > 6 {
                    Text("+ \(fresh.count - 6) more in the list below")
                        .font(.caption).foregroundStyle(Color.inkDim).padding(.horizontal)
                }
                Divider().padding(.top, 8)
            }
        }
    }
}

/// "New to cacao?" under the rail when a practice is picked.
struct GuideCard: View {
    let slug: String
    @Environment(EventStore.self) private var store

    var body: some View {
        if let p = store.practice(slug), store.guides[slug] != nil {
            NavigationLink(value: PracticeRoute(slug: slug)) {
                HStack(spacing: 12) {
                    Image(systemName: p.symbol)
                        .foregroundStyle(Color.brand)
                        .frame(width: 36, height: 36)
                        .background(Color.brand.opacity(0.1), in: Circle())
                    VStack(alignment: .leading, spacing: 2) {
                        Text("New to \(p.label)?").font(.subheadline.weight(.semibold)).foregroundStyle(Color.ink)
                        Text("What happens, what to bring, what it costs")
                            .font(.caption).foregroundStyle(Color.inkDim)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").foregroundStyle(Color.inkDim)
                }
                .padding(12)
                .background(Color.card, in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.inkDim.opacity(0.15)))
            }
            .buttonStyle(.plain)
            .padding(.horizontal)
            .padding(.bottom, 6)
        }
    }
}
