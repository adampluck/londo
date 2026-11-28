import EventKit
import EventKitUI
import SwiftUI

/// The system "New Event" sheet, pre-filled. Since iOS 17 this sheet runs
/// outside the app, so it needs no calendar permission prompt.
struct CalendarSheet: UIViewControllerRepresentable {
    let event: Event
    @Environment(\.dismiss) private var dismiss

    private static let store = EKEventStore()

    func makeUIViewController(context: Context) -> EKEventEditViewController {
        let vc = EKEventEditViewController()
        vc.eventStore = Self.store
        let ek = EKEvent(eventStore: Self.store)
        ek.title = event.title
        ek.startDate = event.startAt
        ek.endDate = event.endAt ?? event.startAt?.addingTimeInterval(2 * 3600)
        ek.isAllDay = event.isAllDay == true
        ek.timeZone = London.timeZone
        ek.location = [event.venueName, event.address].compactMap { $0 }.joined(separator: ", ")
        ek.url = event.url
        ek.notes = [event.hook, event.sourceUrl].compactMap { $0 }.joined(separator: "\n\n")
        vc.event = ek
        vc.editViewDelegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ vc: EKEventEditViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(dismiss: dismiss) }

    final class Coordinator: NSObject, EKEventEditViewDelegate {
        let dismiss: DismissAction
        init(dismiss: DismissAction) { self.dismiss = dismiss }
        func eventEditViewController(_ controller: EKEventEditViewController,
                                     didCompleteWith action: EKEventEditViewAction) {
            dismiss()
        }
    }
}
