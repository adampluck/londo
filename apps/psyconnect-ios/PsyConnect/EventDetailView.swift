import MapKit
import SwiftUI

struct EventDetailView: View {
    let event: Event
    @Environment(\.openURL) private var openURL
    @Environment(Favorites.self) private var favorites
    @Environment(EventStore.self) private var store
    @State private var showCalendar = false
    @State private var notificationsOff = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                // A scaledToFill image reports its own (wider) size; hosting it
                // in an overlay on a fixed-height clear box keeps the page at
                // screen width whatever the image's aspect ratio.
                Color.clear
                    .frame(height: 260)
                    .frame(maxWidth: .infinity)
                    .overlay { EventImage(event: event) }
                    .clipped()

                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(event.title)
                            .font(.title2.weight(.bold))
                            .foregroundStyle(Color.ink)
                        if let hook = event.hook {
                            Text(hook)
                                .font(.body.italic())
                                .foregroundStyle(Color.gold)
                        }
                    }

                    facts

                    practiceLinks

                    actions

                    if let url = event.url {
                        Button { openURL(url) } label: {
                            Label(event.isFree == true ? "Details & RSVP" : "Get tickets",
                                  systemImage: "ticket")
                                .font(.headline)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 6)
                        }
                        .buttonStyle(.borderedProminent)
                        .buttonBorderShape(.capsule)
                    }

                    if let lat = event.latitude, let lon = event.longitude {
                        VenueMap(name: event.venueName ?? event.title,
                                 coordinate: .init(latitude: lat, longitude: lon))
                    }

                    if let desc = event.description, !desc.isEmpty {
                        Text(desc)
                            .font(.body)
                            .foregroundStyle(Color.ink)
                            .textSelection(.enabled)
                    }
                }
                .padding()
            }
        }
        .background(Color.background)
        .ignoresSafeArea(edges: .top)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button { favorites.toggle(event) } label: {
                    Image(systemName: favorites.isSaved(event) ? "heart.fill" : "heart")
                }
                .sensoryFeedback(.success, trigger: favorites.isSaved(event))
                .accessibilityLabel(favorites.isSaved(event) ? "Remove from saved" : "Save")
                if let url = event.url {
                    ShareLink(item: url, subject: Text(event.title),
                              message: Text(event.hook ?? event.title))
                }
            }
        }
        .sheet(isPresented: $showCalendar) { CalendarSheet(event: event).ignoresSafeArea() }
        #if DEBUG
        .task {
            // one shot: only the first event page opened after launch
            if Router.shared.debugSheet == "calendar" { Router.shared.debugSheet = nil; showCalendar = true }
        }
        #endif
        .alert("Notifications are off", isPresented: $notificationsOff) {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
            }
            Button("Not now", role: .cancel) {}
        } message: {
            Text("Turn on notifications for PsyConnect in Settings to get reminders.")
        }
    }

    /// Save · Remind me · Calendar — the things only the app can do.
    private var actions: some View {
        let saved = favorites.isSaved(event)
        let reminder = favorites.reminder(for: event)
        let options = favorites.reminderOptions(for: event)
        return HStack(spacing: 10) {
            ActionButton(title: saved ? "Saved" : "Save",
                         icon: saved ? "heart.fill" : "heart", lit: saved) {
                favorites.toggle(event)
            }
            Menu {
                ForEach(options) { r in
                    Button {
                        Task {
                            if await !favorites.setReminder(r, for: event) { notificationsOff = true }
                        }
                    } label: {
                        if reminder == r { Label(r.label, systemImage: "checkmark") } else { Text(r.label) }
                    }
                }
                if reminder != nil {
                    Divider()
                    Button("No reminder", role: .destructive) {
                        Task { await favorites.setReminder(nil, for: event) }
                    }
                }
            } label: {
                ActionLabel(title: reminder == nil ? "Remind me" : "Reminder set",
                            icon: reminder == nil ? "bell" : "bell.fill", lit: reminder != nil)
            }
            .disabled(options.isEmpty && reminder == nil)
            ActionButton(title: "Calendar", icon: "calendar.badge.plus", lit: false) {
                showCalendar = true
            }
            .disabled(event.startAt == nil)
        }
    }

    private var facts: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let start = event.startAt {
                Fact(icon: "calendar", title: London.longDate(start), detail: London.timeRange(event))
            }
            if event.venueName != nil || event.address != nil {
                Fact(icon: "mappin.and.ellipse",
                     title: event.venueName ?? event.address ?? "",
                     detail: event.venueName != nil ? event.address : nil)
            }
            if let price = event.priceText {
                Fact(icon: "sterlingsign.circle", title: price, detail: nil)
            }
            if let org = event.organizerName {
                NavigationLink(value: HostRoute(name: org)) {
                    HStack {
                        Fact(icon: "person.2", title: org, detail: "More from this host")
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(Color.inkDim)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    /// The practices this event counts as, each opening its guide.
    @ViewBuilder private var practiceLinks: some View {
        let ps = store.practices.filter { store.guides[$0.slug] != nil && $0.matches(event) }
        if !ps.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("New to this?").font(.subheadline.weight(.semibold)).foregroundStyle(Color.inkDim)
                FlowLayout(spacing: 8) {
                    ForEach(ps) { p in
                        NavigationLink(value: PracticeRoute(slug: p.slug)) {
                            Chip(label: "About \(p.label)", symbol: p.symbol, selected: false)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

private struct ActionButton: View {
    let title: String
    let icon: String
    let lit: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) { ActionLabel(title: title, icon: icon, lit: lit) }
            .buttonStyle(.plain)
    }
}

private struct ActionLabel: View {
    let title: String
    let icon: String
    let lit: Bool
    var body: some View {
        VStack(spacing: 5) {
            Image(systemName: icon).font(.title3)
            Text(title).font(.caption.weight(.semibold)).lineLimit(1).minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .foregroundStyle(lit ? Color.background : Color.brand)
        .background(lit ? Color.brand : Color.brand.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
        .contentShape(RoundedRectangle(cornerRadius: 14))
    }
}

private struct Fact: View {
    let icon: String
    let title: String
    let detail: String?
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Image(systemName: icon)
                .foregroundStyle(Color.brand)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Color.ink)
                if let detail, !detail.isEmpty {
                    Text(detail).font(.subheadline).foregroundStyle(Color.inkDim)
                }
            }
        }
    }
}

private struct VenueMap: View {
    let name: String
    let coordinate: CLLocationCoordinate2D

    var body: some View {
        Map(initialPosition: .region(.init(center: coordinate,
                                           latitudinalMeters: 900, longitudinalMeters: 900)),
            interactionModes: []) {
            Marker(name, coordinate: coordinate).tint(Color.brand)
        }
        .frame(height: 170)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(alignment: .bottomTrailing) {
            Button("Directions", systemImage: "arrow.triangle.turn.up.right.diamond") {
                let item: MKMapItem
                if #available(iOS 26, *) {
                    item = MKMapItem(location: CLLocation(latitude: coordinate.latitude,
                                                          longitude: coordinate.longitude),
                                     address: nil)
                } else {
                    item = MKMapItem(placemark: MKPlacemark(coordinate: coordinate))
                }
                item.name = name
                item.openInMaps(launchOptions: [MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeTransit])
            }
            .font(.caption.weight(.semibold))
            .buttonStyle(.borderedProminent)
            .buttonBorderShape(.capsule)
            .padding(10)
        }
    }
}
