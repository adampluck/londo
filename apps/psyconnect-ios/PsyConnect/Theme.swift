import SwiftUI

/// Colours mirror sites/psyconnect/theme.css (light + dark), defined in the
/// asset catalog.
extension Color {
    static let brand = Color("Brand")
    static let gold = Color("Gold")
    static let ink = Color("Ink")
    static let inkDim = Color("InkDim")
    static let background = Color("Background")
    static let parchment = Color("Parchment")
    static let card = Color("Card")
}

/// Stand-in artwork for events without an image: a warm two-tone wash picked
/// deterministically from the event id.
struct PlaceholderArt: View {
    let seed: String
    private static let palettes: [[Color]] = [
        [Color(red: 0.43, green: 0.25, blue: 0.38), Color(red: 0.71, green: 0.34, blue: 0.18)],
        [Color(red: 0.54, green: 0.37, blue: 0.08), Color(red: 0.43, green: 0.25, blue: 0.38)],
        [Color(red: 0.36, green: 0.45, blue: 0.36), Color(red: 0.54, green: 0.37, blue: 0.08)],
        [Color(red: 0.71, green: 0.34, blue: 0.18), Color(red: 0.91, green: 0.71, blue: 0.55)],
    ]
    var body: some View {
        let h = seed.unicodeScalars.reduce(0) { ($0 &* 31 &+ Int($1.value)) & 0xffff }
        LinearGradient(colors: Self.palettes[h % Self.palettes.count],
                       startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

struct EventImage: View {
    let event: Event
    var body: some View {
        AsyncImage(url: event.image, transaction: .init(animation: .easeOut(duration: 0.2))) { phase in
            if case .success(let img) = phase {
                img.resizable().scaledToFill()
            } else {
                PlaceholderArt(seed: event.id)
            }
        }
    }
}

/// The cross-stitch PsyConnect wordmark (sites/psyconnect/logo.png), drawn
/// as a template so it takes the brand colour: plum by day, mint by night.
struct Wordmark: View {
    var height: CGFloat = 24
    var body: some View {
        // fixed size: toolbars propose a small box and scaledToFit would
        // shrink into it (logo.png is 960×164)
        Image("Wordmark")
            .resizable()
            .frame(width: height * 960 / 164, height: height)
            .foregroundStyle(Color.brand)
            .accessibilityLabel("PsyConnect")
    }
}

extension ToolbarContent {
    /// iOS 26 puts toolbar items on a glass capsule; a logo shouldn't sit on one.
    @ToolbarContentBuilder
    func sharedBackgroundHidden() -> some ToolbarContent {
        if #available(iOS 26, *) {
            self.sharedBackgroundVisibility(.hidden)
        } else {
            self
        }
    }
}

struct OfflineBanner: View {
    @Environment(EventStore.self) private var store
    var body: some View {
        if let t = store.offlineSince {
            Label {
                Text("Offline · showing events from \(London.calendar.isDateInToday(t) ? London.time(t) : London.shortDay(t))")
            } icon: { Image(systemName: "wifi.slash") }
            .font(.footnote.weight(.semibold))
            .foregroundStyle(Color.inkDim)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(Color.card, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.inkDim.opacity(0.2)))
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
        }
    }
}
