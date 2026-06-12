import Foundation
import XCTest

final class LimiraUITests: XCTestCase {
    func testEnterpriseLoginResearchAndArtifactsWithMockService() {
        let app = launchMockEnterpriseApp(autoSubmit: true)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        let status = app.descendants(matching: .any)["TaskStatusProbe"]
        XCTAssertTrue(status.waitForExistence(timeout: 5))
        expectation(for: NSPredicate(format: "label == %@", "completed"), evaluatedWith: status)
        waitForExpectations(timeout: 5)

        let evidence = app.buttons["CompactArtifactControl-证据"]
        XCTAssertTrue(scrollTo(evidence, in: app))
        forceTap(evidence)
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
        XCTAssertTrue(app.staticTexts["limira OSINT"].waitForExistence(timeout: 5))

        let status = app.descendants(matching: .any)["TaskStatusProbe"]
        XCTAssertTrue(status.waitForExistence(timeout: 5))
        expectation(for: NSPredicate(format: "label == %@", "completed"), evaluatedWith: status)
        waitForExpectations(timeout: 5)

        let sidebar = app.buttons["MainSidebarOpenButton"]
        XCTAssertTrue(sidebar.waitForExistence(timeout: 3))
        openSidebar(in: app)

        let cloud = app.buttons["管理云盘"]
        XCTAssertTrue(scrollTo(cloud, in: app))
        cloud.tap()
        waitForRoute("cloudDrive", in: app)
        tapBackToWorkspace(app)

        openSidebar(in: app)
        let archived = app.buttons["已归档对话"]
        XCTAssertTrue(scrollTo(archived, in: app))
        archived.tap()
        waitForRoute("archivedChats", in: app)
        tapBackToWorkspace(app)

        openSidebar(in: app)
        let admin = app.buttons["单位管理"]
        XCTAssertTrue(scrollTo(admin, in: app))
        admin.tap()
        waitForRoute("enterpriseAdmin", in: app)
    }

    func testVoiceButtonTranscribesIntoComposerWithMockService() {
        let app = launchMockEnterpriseApp(autoSubmit: false)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        let voice = app.buttons["VoiceInputButton"]
        XCTAssertTrue(voice.waitForExistence(timeout: 3))

        forceTap(voice)
        let voiceStatus = app.descendants(matching: .any)["VoiceStatusProbe"]
        expectation(for: NSPredicate(format: "label CONTAINS %@", "正在录音"), evaluatedWith: voiceStatus)
        waitForExpectations(timeout: 5)
        forceTap(app.buttons["VoiceInputButton"])

        let queryProbe = app.descendants(matching: .any)["QueryDraftProbe"]
        expectation(for: NSPredicate(format: "label CONTAINS %@", "mock speech"), evaluatedWith: queryProbe)
        waitForExpectations(timeout: 5)
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

        forceTap(app.buttons["SignInButton"])
        _ = app.keyboards.firstMatch.waitForNonExistence(timeout: 2)

        let editor = app.textViews["QueryEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 45))

        guard runResearch else { return }

        let status = app.descendants(matching: .any)["TaskStatusProbe"]
        XCTAssertTrue(status.waitForExistence(timeout: 15))
        expectation(for: NSPredicate(format: "label IN %@", ["completed", "failed", "cancelled"]), evaluatedWith: status)
        waitForExpectations(timeout: 420)
        XCTAssertEqual(status.label, "completed")

        let tabPicker = app.descendants(matching: .any)["ArtifactTabPicker"]
        XCTAssertTrue(tabPicker.waitForExistence(timeout: 30))
    }

    private func forceTap(_ element: XCUIElement) {
        element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
    }

    private func waitForRoute(_ route: String, in app: XCUIApplication, timeout: TimeInterval = 5) {
        let routeProbe = app.descendants(matching: .any)["CompactRouteProbe"]
        expectation(for: NSPredicate(format: "label == %@", route), evaluatedWith: routeProbe)
        waitForExpectations(timeout: timeout)
    }

    private func openSidebar(in app: XCUIApplication) {
        let sidebar = app.buttons["MainSidebarOpenButton"]
        XCTAssertTrue(sidebar.waitForExistence(timeout: 3))
        forceTap(sidebar)
        XCTAssertTrue(app.buttons["CompactSidebarCloseButton"].waitForExistence(timeout: 3))
    }

    private func tapBackToWorkspace(_ app: XCUIApplication) {
        let back = app.buttons["返回工作台"]
        XCTAssertTrue(back.waitForExistence(timeout: 3))
        back.tap()
        waitForRoute("workspace", in: app)
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

        forceTap(app.buttons["SignInButton"])
        _ = app.keyboards.firstMatch.waitForNonExistence(timeout: 2)
        return app
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
