import SwiftUI

@main
struct LimiraApp: App {
    @StateObject private var model = AppViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .task {
                    await model.boot()
                }
        }
    }
}
