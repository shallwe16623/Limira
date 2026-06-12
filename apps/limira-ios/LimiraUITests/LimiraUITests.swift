import Foundation
import XCTest

final class LimiraUITests: XCTestCase {
    func testEnterpriseLoginResearchAndArtifactsWithMockService() {
        let app = launchMockEnterpriseApp(autoSubmit: true)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)
        let status = app.descendants(matching: .any)["TaskStatusProbe"]
        XCTAssertTrue(status.waitForExistence(timeout: 5))
        expectation(for: NSPredicate(format: "label == %@", "completed"), evaluatedWith: status)
        waitForExpectations(timeout: 5)

        let evidence = app.buttons["CompactArtifactControl-证据"]
        XCTAssertTrue(scrollTo(evidence, in: app))
        XCTAssertTrue(evidence.isHittable)
        evidence.tap()
        let artifactMode = app.descendants(matching: .any)["CompactArtifactModeProbe"]
        expectation(for: NSPredicate(format: "label == %@", "artifacts"), evaluatedWith: artifactMode)
        waitForExpectations(timeout: 5)
        let selectedTab = app.descendants(matching: .any)["SelectedArtifactTabProbe"]
        expectation(for: NSPredicate(format: "label == %@", "证据"), evaluatedWith: selectedTab)
        waitForExpectations(timeout: 5)
        XCTAssertTrue(app.buttons["BackToConversationButton"].waitForExistence(timeout: 3))
    }

    func testPostLoginWorkspaceOperationsWithMockEnterpriseAccount() {
        let app = launchMockEnterpriseApp(autoSubmit: true)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)
        XCTAssertTrue(app.staticTexts["limira OSINT"].waitForExistence(timeout: 5))

        let status = app.descendants(matching: .any)["TaskStatusProbe"]
        XCTAssertTrue(status.waitForExistence(timeout: 5))
        expectation(for: NSPredicate(format: "label == %@", "completed"), evaluatedWith: status)
        waitForExpectations(timeout: 5)

        let sidebar = app.buttons["MainSidebarOpenButton"]
        XCTAssertTrue(sidebar.waitForExistence(timeout: 3))
        openSidebar(in: app)

        let cloud = app.buttons["CompactCloudDriveButton"]
        XCTAssertTrue(scrollTo(cloud, in: app))
        cloud.tap()
        waitForRoute("cloudDrive", in: app)
        returnFromSettingsSubpageToMenu(app)

        let archived = app.buttons["CompactArchivedChatsButton"]
        XCTAssertTrue(scrollTo(archived, in: app))
        archived.tap()
        waitForRoute("archivedChats", in: app)
        returnFromSettingsSubpageToMenu(app)

        let admin = app.buttons["CompactEnterpriseAdminButton"]
        XCTAssertTrue(scrollTo(admin, in: app))
        admin.tap()
        waitForRoute("enterpriseAdmin", in: app)
    }

    func testCompactMenuDrawerGesturesWithMockEnterpriseAccount() {
        let app = launchMockEnterpriseApp(autoSubmit: false)

        XCTAssertTrue(app.textViews["QueryEditor"].waitForExistence(timeout: 5))
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)

        openSidebar(in: app)
        XCTAssertTrue(app.descendants(matching: .any)["CompactMenuView"].waitForExistence(timeout: 3))

        closeSidebarWithSwipe(in: app)
        XCTAssertTrue(app.descendants(matching: .any)["CompactMenuView"].waitForNonExistence(timeout: 3))
        waitForCompactModal("none", in: app)

        openSidebar(in: app)
        XCTAssertTrue(app.descendants(matching: .any)["CompactMenuView"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["CompactSidebarCloseButton"].waitForExistence(timeout: 3))
    }

    func testVoiceButtonTranscribesIntoComposerWithMockService() {
        let app = launchMockEnterpriseApp(autoSubmit: false)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)
        let voice = app.buttons["VoiceInputButton"]
        XCTAssertTrue(voice.waitForExistence(timeout: 3))

        XCTAssertTrue(voice.isHittable)
        voice.tap()
        let voiceStatus = app.descendants(matching: .any)["VoiceStatusProbe"]
        expectation(for: NSPredicate(format: "label CONTAINS %@", "正在录音"), evaluatedWith: voiceStatus)
        waitForExpectations(timeout: 5)
        let stopVoice = app.buttons["VoiceInputButton"]
        XCTAssertTrue(stopVoice.isHittable)
        stopVoice.tap()

        let queryProbe = app.descendants(matching: .any)["QueryDraftProbe"]
        expectation(for: NSPredicate(format: "label CONTAINS %@", "mock speech"), evaluatedWith: queryProbe)
        waitForExpectations(timeout: 5)
    }

    func testCompactAccessibilityRegressionWithMockService() {
        let app = launchMockEnterpriseApp(autoSubmit: false)
        let taskId = "mock-task-001"

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)

        tapHittable(app.buttons["UploadMenuButton"], in: app)
        tapHittable(app.buttons["UploadFileMenuItem"], in: app)
        XCTAssertTrue(app.descendants(matching: .any)["SelectedDocumentChips"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.descendants(matching: .any)["SelectedDocumentChip-mock-doc"].waitForExistence(timeout: 3))

        tapHittable(app.buttons["UploadMenuButton"], in: app)
        tapHittable(app.buttons["HistoryFilesMenuItem"], in: app)
        tapHittable(app.buttons["HistoryFilesRefreshButton"], in: app)
        tapHittable(app.buttons["HistoryFileToggle-mock-cloud-doc"], in: app)
        tapHittable(app.buttons["HistoryFilesDoneButton"], in: app)

        XCTAssertTrue(app.descendants(matching: .any)["SelectedDocumentChip-mock-cloud-doc"].waitForExistence(timeout: 3))

        openSidebar(in: app)
        let search = app.buttons["HistorySearchButton"]
        XCTAssertTrue(scrollTo(search, in: app))
        search.tap()

        let searchField = app.textFields["HistorySearchField"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 3))
        XCTAssertTrue(searchField.isHittable)
        searchField.tap()
        searchField.typeText("测试")
        tapHittable(app.buttons["HistorySearchSubmitButton"], in: app)
        dismissKeyboard(in: app)

        let searchRow = app.buttons["CompactHistoryRow-\(taskId)"]
        XCTAssertTrue(searchRow.waitForExistence(timeout: 3))
        tapHistoryMoreAction("CompactHistoryArchiveButton-\(taskId)", taskId: taskId, in: app)
        tapHittable(app.buttons["HistorySearchDoneButton"], in: app)

        openSidebar(in: app)
        tapHittable(app.buttons["HistoryArchiveToggleButton"], in: app)
        let archivedRow = app.buttons["CompactHistoryRow-\(taskId)"]
        XCTAssertTrue(archivedRow.waitForExistence(timeout: 3))
        tapHistoryMoreAction("CompactHistoryArchiveButton-\(taskId)", taskId: taskId, in: app)
        XCTAssertTrue(archivedRow.waitForNonExistence(timeout: 3))

        tapHittable(app.buttons["HistoryArchiveToggleButton"], in: app)
        let activeRow = app.buttons["CompactHistoryRow-\(taskId)"]
        XCTAssertTrue(activeRow.waitForExistence(timeout: 3))
        tapHistoryMoreAction("CompactHistoryDeleteButton-\(taskId)", taskId: taskId, in: app)
        let deleteAlert = app.alerts["删除这条对话？"]
        XCTAssertTrue(deleteAlert.waitForExistence(timeout: 3))
        let confirmDelete = app.buttons.matching(identifier: "CompactHistoryDeleteConfirmButton-\(taskId)").firstMatch
        XCTAssertTrue(confirmDelete.waitForExistence(timeout: 3))
        XCTAssertTrue(confirmDelete.isHittable)
        confirmDelete.tap()
        XCTAssertTrue(activeRow.waitForNonExistence(timeout: 3))
        closeSidebarIfVisible(in: app)

        openSidebar(in: app)
        let cloud = app.buttons["CompactCloudDriveButton"]
        XCTAssertTrue(scrollTo(cloud, in: app))
        cloud.tap()
        waitForRoute("cloudDrive", in: app)

        tapHittable(app.buttons["CompactCloudFileReferenceButton-mock-cloud-doc"], in: app)
        tapHittable(app.buttons["CompactCloudFileDownloadButton-mock-cloud-doc"], in: app)
        XCTAssertTrue(app.descendants(matching: .any)["DownloadPanel"].waitForExistence(timeout: 3))
    }

    func testLiveEnterpriseLoginAgainstSanFranciscoWhenCredentialsProvided() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["LIMIRA_LIVE_UI_SMOKE"] == "YES" else {
            throw XCTSkip("Set LIMIRA_LIVE_UI_SMOKE=YES with live credentials to run the production smoke test.")
        }

        let identifier = try XCTUnwrap(nonEmptyEnvironmentValue("LIMIRA_LIVE_IDENTIFIER"), "Set LIMIRA_LIVE_IDENTIFIER for the live smoke test.")
        let password = try XCTUnwrap(nonEmptyEnvironmentValue("LIMIRA_LIVE_PASSWORD"), "Set LIMIRA_LIVE_PASSWORD for the live smoke test.")
        let baseURL = nonEmptyEnvironmentValue("LIMIRA_LIVE_BASE_URL") ?? "https://limira-inc.com"
        let runResearch = environment["LIMIRA_LIVE_RUN_RESEARCH"] == "YES"
        let smokeQuery = nonEmptyEnvironmentValue("LIMIRA_LIVE_QUERY")
            ?? "Limira iOS live smoke: produce a short public market overview for San Francisco AI companies."

        let app = XCUIApplication()
        var launchArguments = [
            "-LimiraAPIBaseURL", baseURL,
            "-LimiraUITestProbe", "YES"
        ]
        if runResearch {
            launchArguments += [
                "-LimiraUITestAutoSubmit", "YES",
                "-LimiraUITestAutoSubmitQuery", smokeQuery
            ]
        }
        app.launchArguments = launchArguments
        app.launch()

        let identifierField = app.textFields["AuthIdentifierField"]
        XCTAssertTrue(identifierField.waitForExistence(timeout: 15))
        identifierField.tap()
        identifierField.typeText(identifier)

        let passwordField = app.secureTextFields["AuthPasswordField"]
        passwordField.tap()
        passwordField.typeText(password)

        dismissKeyboard(in: app)
        tapHittable(app.buttons["SignInButton"], in: app)
        _ = app.keyboards.firstMatch.waitForNonExistence(timeout: 2)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 45))
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)

        guard runResearch else { return }

        let status = app.descendants(matching: .any)["TaskStatusProbe"]
        XCTAssertTrue(status.waitForExistence(timeout: 15))
        expectation(for: NSPredicate(format: "label IN %@", ["completed", "failed", "cancelled"]), evaluatedWith: status)
        waitForExpectations(timeout: 420)
        XCTAssertEqual(status.label, "completed")

        let tabPicker = app.descendants(matching: .any)["ArtifactTabPicker"]
        XCTAssertTrue(tabPicker.waitForExistence(timeout: 30))
    }

    private func waitForRoute(_ route: String, in app: XCUIApplication, timeout: TimeInterval = 5) {
        let routeProbe = app.descendants(matching: .any)["CompactRouteProbe"]
        expectation(for: NSPredicate(format: "label == %@", route), evaluatedWith: routeProbe)
        waitForExpectations(timeout: timeout)
    }

    private func waitForCompactModal(_ modal: String, in app: XCUIApplication, timeout: TimeInterval = 5) {
        let modalProbe = app.descendants(matching: .any)["CompactModalProbe"]
        expectation(for: NSPredicate(format: "label == %@", modal), evaluatedWith: modalProbe)
        waitForExpectations(timeout: timeout)
    }

    private func openSidebar(in app: XCUIApplication) {
        let sidebar = app.buttons["MainSidebarOpenButton"]
        if sidebar.waitForExistence(timeout: 3), sidebar.isHittable {
            sidebar.tap()
        } else {
            tapSidebarCoordinate(in: app)
        }
        let menu = app.descendants(matching: .any)["CompactMenuView"]
        if !menu.waitForExistence(timeout: 2) {
            tapSidebarCoordinate(in: app)
        }
        XCTAssertTrue(menu.waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["CompactSidebarCloseButton"].waitForExistence(timeout: 3))
    }

    private func tapSidebarCoordinate(in app: XCUIApplication) {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.13, dy: 0.11)).tap()
    }

    private func openSidebarWithSwipe(in app: XCUIApplication) {
        let sidebar = app.buttons["MainSidebarOpenButton"]
        if sidebar.exists {
            sidebar.swipeRight()
        } else {
            app.swipeRight()
        }
    }

    private func closeSidebarWithSwipe(in app: XCUIApplication) {
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.78, dy: 0.5))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.08, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: end)
    }

    private func closeSidebarIfVisible(in app: XCUIApplication) {
        let menu = app.descendants(matching: .any)["CompactMenuView"]
        guard menu.exists else { return }
        let close = app.buttons["CompactSidebarCloseButton"]
        if close.waitForExistence(timeout: 1), close.isHittable {
            close.tap()
        }
    }

    private func tapBackToWorkspace(_ app: XCUIApplication) {
        let back = app.buttons["CompactRouteBackButton"]
        XCTAssertTrue(back.waitForExistence(timeout: 3))
        XCTAssertTrue(back.isHittable)
        back.tap()
        waitForRoute("workspace", in: app)
    }

    private func returnFromSettingsSubpageToMenu(_ app: XCUIApplication) {
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.01, dy: 0.5))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.86, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: end)

        let menu = app.descendants(matching: .any)["CompactMenuView"]
        if !menu.waitForExistence(timeout: 2) {
            let back = app.navigationBars.buttons.firstMatch
            XCTAssertTrue(back.waitForExistence(timeout: 2))
            back.tap()
        }
        waitForRoute("workspace", in: app)
        waitForCompactModal("menu", in: app)
        XCTAssertTrue(menu.waitForExistence(timeout: 3))
    }

    private func launchMockEnterpriseApp(autoSubmit: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        var arguments = ["-LimiraUITestMock", "YES"]
        if autoSubmit {
            arguments += [
                "-LimiraUITestAutoSubmit", "YES",
                "-LimiraUITestAutoSubmitQuery", "Mock iOS research smoke"
            ]
        }
        app.launchArguments = arguments
        app.launch()

        let identifier = app.textFields["AuthIdentifierField"]
        XCTAssertTrue(identifier.waitForExistence(timeout: 5))
        identifier.tap()
        identifier.typeText("limira-admin")

        let password = app.secureTextFields["AuthPasswordField"]
        password.tap()
        password.typeText("password")

        dismissKeyboard(in: app)
        tapHittable(app.buttons["SignInButton"], in: app)
        dismissSystemPrompts(in: app)
        dismissKeyboard(in: app)
        return app
    }

    private func tapHistoryMoreAction(_ actionIdentifier: String, taskId: String, in app: XCUIApplication) {
        let more = app.buttons["CompactHistoryMoreButton-\(taskId)"]
        XCTAssertTrue(scrollTo(more, in: app))
        XCTAssertTrue(more.isHittable)
        more.tap()
        tapHittable(app.buttons[actionIdentifier], in: app)
    }

    private func tapHittable(_ element: XCUIElement, in app: XCUIApplication, timeout: TimeInterval = 3) {
        XCTAssertTrue(element.waitForExistence(timeout: timeout))
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            dismissSystemPrompts(in: app)
            if element.isHittable {
                element.tap()
                return
            }
            dismissKeyboard(in: app)
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        XCTAssertTrue(element.isHittable)
        element.tap()
    }

    private func dismissSystemPrompts(in app: XCUIApplication) {
        let deadline = Date().addingTimeInterval(1.0)
        repeat {
            for title in ["以后", "Not Now"] {
                let button = app.buttons[title]
                if button.exists {
                    button.tap()
                    return
                }
            }
            Thread.sleep(forTimeInterval: 0.1)
        } while Date() < deadline
    }

    private func dismissKeyboard(in app: XCUIApplication) {
        guard app.keyboards.firstMatch.exists else { return }
        let returnButton = app.keyboards.buttons["return"]
        if returnButton.exists {
            returnButton.tap()
            _ = app.keyboards.firstMatch.waitForNonExistence(timeout: 1)
        }
        if app.keyboards.firstMatch.exists {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.08)).tap()
            _ = app.keyboards.firstMatch.waitForNonExistence(timeout: 1)
        }
    }

    @discardableResult
    private func scrollTo(_ element: XCUIElement, in app: XCUIApplication, maxSwipes: Int = 8) -> Bool {
        if element.exists, element.isHittable {
            return true
        }
        for _ in 0..<maxSwipes {
            app.swipeUp()
            if element.exists, element.isHittable {
                return true
            }
        }
        return element.exists && element.isHittable
    }

    private func nonEmptyEnvironmentValue(_ key: String) -> String? {
        guard let value = ProcessInfo.processInfo.environment[key],
              !value.isEmpty else {
            return nil
        }
        return value
    }
}
