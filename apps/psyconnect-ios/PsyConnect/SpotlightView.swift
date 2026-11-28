import SwiftUI

/// "Our next event" + "Our upcoming top picks" (renderSpotlight in
/// web/app.js): a horizontal swipe row of large cards.
struct SpotlightView: View {
    @Environment(EventStore.self) private var store

    var body: some View {
        let featured = store.featured
        let picks = store.picks
        if featured != nil || !picks.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                if let featured {
                    heading(store.site.featured?.label ?? "Our next event",
                            accent: store.site.featured?.accent ?? "next event")
                    NavigationLink(value: featured) {
                        SpotlightCard(event: featured, featured: true)
                    }
                    .buttonStyle(.plain)
                    .eventContextMenu(featured)
                    .padding(.horizontal)
                    .padding(.bottom, 8)
                }
                if !picks.isEmpty {
                    heading(store.site.curated?.label ?? "Our top picks this week",
                            accent: store.site.curated?.accent ?? "top picks")
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(spacing: 12) {
                            ForEach(picks) { e in
                                NavigationLink(value: e) {
                                    SpotlightCard(event: e, featured: false)
                                        .frame(width: 280)
                                }
                                .buttonStyle(.plain)
                                .eventContextMenu(e)
                            }
                        }
                        .scrollTargetLayout()
                        .padding(.horizontal)
                    }
                    .scrollTargetBehavior(.viewAligned)
                }
            }
            .padding(.bottom, 4)
        }
    }

    /// The accent phrase ("top picks") takes the gold of a day heading.
    private func heading(_ label: String, accent: String) -> some View {
        var text = AttributedString(label)
        if let r = text.range(of: accent) { text[r].foregroundColor = .gold }
        return Text(text)
            .font(.title3.weight(.semibold))
            .foregroundStyle(Color.ink)
            .padding(.horizontal)
    }
}

struct SpotlightCard: View {
    let event: Event
    let featured: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Color.clear
                .frame(height: featured ? 180 : 150)
                .overlay { EventImage(event: event) }
                .clipped()
            VStack(alignment: .leading, spacing: 5) {
                if !featured, let org = event.organizerName {
                    Text(org.uppercased())
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color.brand)
                        .lineLimit(1)
                }
                Text(event.title)
                    .font(.headline)
                    .foregroundStyle(Color.ink)
                    .lineLimit(2, reservesSpace: !featured)
                if let s = event.startAt {
                    Text("\(London.shortDay(s)) · \(London.timeRange(event))")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.gold)
                }
                if let blurb = event.hook ?? event.description {
                    Text(blurb)
                        .font(.subheadline)
                        .foregroundStyle(Color.inkDim)
                        .lineLimit(featured ? 3 : 2, reservesSpace: !featured)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.card)
        .clipShape(RoundedRectangle(cornerRadius: 20))
        .overlay(RoundedRectangle(cornerRadius: 20)
            .strokeBorder(featured ? Color.gold.opacity(0.7) : Color.inkDim.opacity(0.18),
                          lineWidth: featured ? 1.5 : 1))
    }
}
