import MapKit
import SwiftUI

/// The list's current selection (day, practice, filters, search) on a map —
/// mapEvents() in web/app.js.
struct EventMapView: View {
    @Environment(EventStore.self) private var store
    @Environment(Browse.self) private var browse
    @State private var selection: String?
    @State private var showFilters = false
    @State private var position: MapCameraPosition = .region(.init(
        center: .init(latitude: 51.515, longitude: -0.10),
        latitudinalMeters: 16_000, longitudinalMeters: 16_000))

    private var visible: [Event] { browse.events(from: store).filter(\.hasLocation) }
    private var selected: Event? { visible.first { $0.id == selection } }

    var body: some View {
        NavigationStack {
            Map(position: $position, selection: $selection) {
                ForEach(visible) { e in
                    Marker(e.title, systemImage: "sparkle",
                           coordinate: .init(latitude: e.latitude!, longitude: e.longitude!))
                        .tint(store.isOurs(e) ? Color.gold : Color.brand)
                        .tag(e.id)
                }
                UserAnnotation()
            }
            .mapControls { MapCompass(); MapScaleView() }
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    DateStrip()
                    PracticeRail().padding(.bottom, 8)
                }
                .background(.bar)
            }
            .safeAreaInset(edge: .bottom) {
                if let e = selected {
                    NavigationLink(value: e) {
                        EventRow(event: e, ours: store.isOurs(e))
                            .background(Color.card, in: RoundedRectangle(cornerRadius: 20))
                            .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
                    }
                    .buttonStyle(.plain)
                    .padding()
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                } else {
                    Text(visible.isEmpty ? "No events with a location here — try another day"
                                         : "\(visible.count) event\(visible.count == 1 ? "" : "s")")
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 12).padding(.vertical, 7)
                        .background(.regularMaterial, in: Capsule())
                        .padding(.bottom, 10)
                }
            }
            .animation(.snappy, value: selection)
            .onChange(of: visible.map(\.id)) { _, ids in
                if let s = selection, !ids.contains(s) { selection = nil }
            }
            .navigationTitle("Map")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showFilters = true } label: {
                        Image(systemName: browse.panelCount > 0
                              ? "line.3.horizontal.decrease.circle.fill"
                              : "line.3.horizontal.decrease.circle")
                    }
                    .badge(browse.panelCount)
                }
            }
            .sheet(isPresented: $showFilters) { FilterSheet() }
            .appDestinations()
        }
    }
}
