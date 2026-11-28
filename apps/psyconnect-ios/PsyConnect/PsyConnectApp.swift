import BackgroundTasks
import CoreSpotlight
import SwiftUI
import UserNotifications

let refreshTaskID = "london.psyconnect.app.refresh"


@main
struct PsyConnectApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var store = EventStore.shared
    @State private var browse = Browse.make()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // room for event images, so the feed still has pictures on the tube
        // (best effort: each image host's cache headers still apply)
        URLCache.shared = URLCache(memoryCapacity: 64 << 20, diskCapacity: 512 << 20)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .environment(browse)
                .environment(Favorites.shared)
                .environment(Router.shared)
                .environment(Follows.shared)
                .tint(.brand)
                .task {
                    await store.load()
                    #if DEBUG
                    debugLaunch()
                    #endif
                }
                // a tapped iPhone-search result
                .onContinueUserActivity(CSSearchableItemActionType) { activity in
                    if let id = activity.userInfo?[CSSearchableItemActivityIdentifier] as? String {
                        Router.shared.open(eventId: id)
                    }
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { Task { await store.refreshIfStale() } }
                    if phase == .background { scheduleRefresh() }
                }
        }
        // iOS decides when (often hours apart, rarer for sideloaded builds);
        // the in-app "New for you" row covers whatever this misses
        .backgroundTask(.appRefresh(refreshTaskID)) {
            await scheduleRefresh()
            await EventStore.shared.load()
            await Follows.shared.notifyNew(in: EventStore.shared)
        }
    }

    nonisolated private func scheduleRefresh() {
        let req = BGAppRefreshTaskRequest(identifier: refreshTaskID)
        req.earliestBeginDate = Date(timeIntervalSinceNow: 4 * 3600)
        try? BGTaskScheduler.shared.submit(req)
    }
}

#if DEBUG
extension PsyConnectApp {
    /// Screenshot helpers for development: `-tab map|saved`, and
    /// `-open <title words>` pushes the first matching event.
    @MainActor func debugLaunch() {
        let d = UserDefaults.standard
        if let q = d.string(forKey: "query") { browse.query = q }
        if let s = d.string(forKey: "followTest") { Follows.shared.debugFollow(s, store: store) }
        if let s = d.string(forKey: "guide") { Router.shared.eventsPath = NavigationPath([PracticeRoute(slug: s)]) }
        if let t = d.string(forKey: "tab") {
            Router.shared.tab = t == "map" ? .map : t == "saved" ? .saved : .events
        }
        Router.shared.debugSheet = d.string(forKey: "sheet")
        if let q = d.string(forKey: "save")?.lowercased(),
           let e = store.events.first(where: { $0.title.lowercased().contains(q) }) {
            if !Favorites.shared.isSaved(e) { Favorites.shared.toggle(e) }
        }
        if let q = d.string(forKey: "remind")?.lowercased(),
           let e = store.events.first(where: { $0.title.lowercased().contains(q) && !$0.hasStarted }),
           let r = Favorites.shared.reminderOptions(for: e).first {
            Task { await Favorites.shared.setReminder(r, for: e) }
        }
        if let q = d.string(forKey: "open")?.lowercased(),
           let e = store.events.first(where: { $0.title.lowercased().contains(q) }) {
            Router.shared.eventsPath = NavigationPath([e])
        }
    }
}
#endif

/// Tab selection and the Saved tab's stack, so a tapped reminder can open
/// its event.
@MainActor @Observable
final class Router {
    static let shared = Router()
    enum TabID: Hashable { case events, map, saved }
    var tab: TabID = .events
    var eventsPath = NavigationPath()
    var savedPath = NavigationPath()
    /// DEBUG launch arg `-sheet filters|calendar`
    var debugSheet: String?

    /// A "N new events for you" alert: the top of the list, where the
    /// "New for you" row sits.
    func openNew() {
        tab = .events
        eventsPath = NavigationPath()
        Browse.shared?.clear()
    }

    /// Reminder, follow alert and Spotlight taps land here. Saved events open
    /// in the Saved tab; anything else in Events.
    func open(eventId: String) {
        if let e = Favorites.shared.event(id: eventId) {
            tab = .saved
            savedPath = NavigationPath([e])
        } else if let e = EventStore.shared.event(id: eventId) {
            tab = .events
            eventsPath = NavigationPath([e])
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    /// Show reminders even while the app is open.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        let id = info["eventId"] as? String
        let route = info["route"] as? String
        await MainActor.run {
            if let id { Router.shared.open(eventId: id) }
            else if route == "new" { Router.shared.openNew() }
        }
    }
}

struct RootView: View {
    @Environment(Router.self) private var router
    @Environment(Favorites.self) private var favorites

    var body: some View {
        @Bindable var router = router
        TabView(selection: $router.tab) {
            Tab("Events", systemImage: "sparkles", value: .events) { EventListView() }
            Tab("Map", systemImage: "map", value: .map) { EventMapView() }
            Tab("Saved", systemImage: "heart", value: .saved) { SavedView() }
                .badge(favorites.upcoming.count)
        }
    }
}
