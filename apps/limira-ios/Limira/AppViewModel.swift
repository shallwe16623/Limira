import AVFoundation
import Combine
import Foundation
import UniformTypeIdentifiers

enum ReportMarkdownExtractor {
    private static let titleFields = ["title", "section_title", "heading", "name", "label", "report_title"]
    private static let textFields = ["markdown", "body", "content", "text", "summary", "report", "final_report", "final_report_markdown"]
    private static let sectionFields = ["sections", "report_sections", "items"]
    private static let nestedFields = ["payload", "result", "data", "artifact", "report", "final_report"]
    private static let ignoredFallbackFields: Set<String> = [
        "id",
        "artifact_id",
        "section_id",
        "task_id",
        "report_id",
        "created_at",
        "updated_at",
        "type",
        "report_type"
    ]

    static func markdown(from buckets: ArtifactBuckets) -> String {
        buckets.reportSections.enumerated().compactMap { index, artifact -> String? in
            let value = JSONValue.object(artifact.fields)
            guard var body = reportText(from: value, includeTitle: false)?.nonEmpty else {
                return nil
            }
            if let title = reportTitle(from: value, index: index), !startsWithMarkdownHeading(body) {
                body = "## \(title)\n\n\(body)"
            }
            return body
        }
        .joined(separator: "\n\n")
    }

    static func markdown(from value: JSONValue, includeTitle: Bool = true) -> String? {
        reportText(from: value, includeTitle: includeTitle)?.nonEmpty
    }

    private static func reportTitle(from value: JSONValue, index: Int?) -> String? {
        if let title = reportField(from: value, fields: titleFields)?.nonEmpty {
            return title
        }
        if case .object(let object) = value,
           let reportType = stringValue(for: "report_type", in: object)?.nonEmpty {
            return reportType
                .replacingOccurrences(of: "_", with: " ")
                .capitalized
        }
        guard let index else { return nil }
        return "报告章节 \(index + 1)"
    }

    private static func reportText(from value: JSONValue, includeTitle: Bool = true, depth: Int = 0) -> String? {
        guard depth < 8 else { return nil }

        if let wrapper = reportWrapper(from: value, depth: depth) {
            if let sections = firstArray(in: wrapper, keys: sectionFields) {
                let sectionMarkdown = sections.enumerated().compactMap { offset, section -> String? in
                    guard var body = reportText(from: section, includeTitle: false, depth: depth + 1)?.nonEmpty else {
                        return nil
                    }
                    if let title = reportTitle(from: section, index: offset), !startsWithMarkdownHeading(body) {
                        body = "## \(title)\n\n\(body)"
                    }
                    return body
                }
                .joined(separator: "\n\n")
                if !sectionMarkdown.isEmpty {
                    return sectionMarkdown
                }
            }

            if let direct = firstValue(in: wrapper, keys: textFields),
               let directText = reportText(from: direct, includeTitle: includeTitle, depth: depth + 1)?.nonEmpty {
                return directText
            }

            for key in nestedFields {
                guard let nested = lookupValue(for: key, in: wrapper) else { continue }
                if let text = reportText(from: nested, includeTitle: includeTitle, depth: depth + 1)?.nonEmpty {
                    return text
                }
            }

            return structuredFallbackMarkdown(from: wrapper, includeTitle: includeTitle, depth: depth)
        }

        switch value {
        case .string(let text):
            return cleanedPlainText(text)
        case .array(let values):
            let text = values.compactMap {
                reportText(from: $0, includeTitle: includeTitle, depth: depth + 1)?.nonEmpty
            }
            .joined(separator: "\n\n")
            return text.nonEmpty
        default:
            return value.stringValue?.nonEmpty
        }
    }

    private static func reportField(from value: JSONValue, fields: [String], depth: Int = 0) -> String? {
        guard depth < 8 else { return nil }
        if let wrapper = reportWrapper(from: value, depth: depth) {
            if let direct = firstValue(in: wrapper, keys: fields),
               let text = reportText(from: direct, includeTitle: false, depth: depth + 1)?.nonEmpty {
                return text
            }
            for key in nestedFields {
                guard let nested = lookupValue(for: key, in: wrapper) else { continue }
                if let text = reportField(from: nested, fields: fields, depth: depth + 1)?.nonEmpty {
                    return text
                }
            }
        }
        return nil
    }

    private static func reportWrapper(from value: JSONValue, depth: Int) -> [String: JSONValue]? {
        switch value {
        case .object(let object):
            return object
        case .string(let text):
            guard let parsed = parsedReportObject(from: text), containsReportSignal(parsed) else {
                return nil
            }
            return parsed
        default:
            return nil
        }
    }

    private static func structuredFallbackMarkdown(from object: [String: JSONValue], includeTitle: Bool, depth: Int) -> String? {
        var sections: [String] = []
        if includeTitle, let title = firstText(in: object, keys: titleFields)?.nonEmpty {
            sections.append("# \(title)")
        }

        let bodyParts = object.sorted(by: { $0.key < $1.key }).compactMap { key, value -> String? in
            let normalizedKey = key.lowercased()
            guard !titleFields.contains(normalizedKey),
                  !ignoredFallbackFields.contains(normalizedKey),
                  !sectionFields.contains(normalizedKey),
                  !nestedFields.contains(normalizedKey) else {
                return nil
            }
            guard let text = reportText(from: value, includeTitle: false, depth: depth + 1)?.nonEmpty else {
                return nil
            }
            let label = key.replacingOccurrences(of: "_", with: " ").capitalized
            return "### \(label)\n\n\(text)"
        }
        sections.append(contentsOf: bodyParts)
        return sections.joined(separator: "\n\n").nonEmpty
    }

    private static func firstText(in object: [String: JSONValue], keys: [String]) -> String? {
        firstValue(in: object, keys: keys).flatMap { reportText(from: $0, includeTitle: false, depth: 1) }
    }

    private static func firstArray(in object: [String: JSONValue], keys: [String]) -> [JSONValue]? {
        for key in keys {
            if let array = lookupValue(for: key, in: object)?.arrayValue {
                return array
            }
        }
        return nil
    }

    private static func firstValue(in object: [String: JSONValue], keys: [String]) -> JSONValue? {
        for key in keys {
            if let value = lookupValue(for: key, in: object) {
                return value
            }
        }
        return nil
    }

    private static func lookupValue(for key: String, in object: [String: JSONValue]) -> JSONValue? {
        if let value = object[key] {
            return value
        }
        let lowercased = key.lowercased()
        return object.first { $0.key.lowercased() == lowercased }?.value
    }

    private static func stringValue(for key: String, in object: [String: JSONValue]) -> String? {
        lookupValue(for: key, in: object)?.stringValue
    }

    private static func parsedReportObject(from text: String) -> [String: JSONValue]? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        for candidate in [trimmed, embeddedJSONObjectText(in: trimmed)].compactMap({ $0 }) {
            guard let data = candidate.data(using: .utf8),
                  let value = try? JSONDecoder().decode(JSONValue.self, from: data),
                  case .object(let object) = value else {
                continue
            }
            return object
        }
        return nil
    }

    private static func embeddedJSONObjectText(in text: String) -> String? {
        guard let start = text.firstIndex(of: "{") else { return nil }
        var depth = 0
        var isEscaped = false
        var isInsideString = false
        var index = start
        while index < text.endIndex {
            let character = text[index]
            if isInsideString {
                if isEscaped {
                    isEscaped = false
                } else if character == "\\" {
                    isEscaped = true
                } else if character == "\"" {
                    isInsideString = false
                }
            } else if character == "\"" {
                isInsideString = true
            } else if character == "{" {
                depth += 1
            } else if character == "}" {
                depth -= 1
                if depth == 0 {
                    return String(text[start...index])
                }
            }
            index = text.index(after: index)
        }
        return nil
    }

    private static func containsReportSignal(_ object: [String: JSONValue]) -> Bool {
        let keys = Set(object.keys.map { $0.lowercased() })
        return keys.contains { key in
            titleFields.contains(key)
                || textFields.contains(key)
                || sectionFields.contains(key)
                || nestedFields.contains(key)
        }
    }

    private static func startsWithMarkdownHeading(_ text: String) -> Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("#")
    }

    private static func cleanedPlainText(_ text: String) -> String? {
        let trimmed = text
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if let markdown = htmlFragmentMarkdown(from: trimmed)?.nonEmpty {
            return markdown
        }
        return trimmed.nonEmpty
    }

    private static func htmlFragmentMarkdown(from text: String) -> String? {
        guard text.range(of: #"<\s*/?\s*[A-Za-z][^>]*>"#, options: .regularExpression) != nil else {
            return nil
        }

        var html = decodeHTMLEntities(text)
        html = replaceAnchorTags(in: html)
        html = html.replacingOccurrences(of: #"<\s*br\s*/?\s*>"#, with: "\n", options: [.regularExpression, .caseInsensitive])
        html = html.replacingOccurrences(of: #"</\s*(div|p|section|article|header|footer|li|ul|ol|h[1-6])\s*>"#, with: "\n", options: [.regularExpression, .caseInsensitive])
        html = html.replacingOccurrences(of: #"<\s*(div|p|section|article|header|footer|li|ul|ol|h[1-6])\b[^>]*>"#, with: "\n", options: [.regularExpression, .caseInsensitive])
        html = stripHTMLTags(from: html)
        html = html
            .replacingOccurrences(of: "🔍", with: "")
            .replacingOccurrences(of: "🌐", with: "")
        let cleanedLines = html
            .components(separatedBy: .newlines)
            .map { normalizeHTMLLine($0) }
            .compactMap(\.nonEmpty)
        return cleanedLines.joined(separator: "\n").nonEmpty
    }

    private static func replaceAnchorTags(in html: String) -> String {
        let pattern = #"<a\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive, .dotMatchesLineSeparators]) else {
            return html
        }
        var result = html
        let matches = regex.matches(in: html, range: NSRange(html.startIndex..., in: html))
        for match in matches.reversed() where match.numberOfRanges >= 3 {
            guard let matchRange = Range(match.range(at: 0), in: result),
                  let urlRange = Range(match.range(at: 1), in: html),
                  let titleRange = Range(match.range(at: 2), in: html) else {
                continue
            }
            let url = decodeHTMLEntities(String(html[urlRange])).trimmingCharacters(in: .whitespacesAndNewlines)
            let title = normalizeHTMLLine(stripHTMLTags(from: String(html[titleRange]))).nonEmpty ?? url
            let replacement = "\n- \(title)\n  \(url)\n"
            result.replaceSubrange(matchRange, with: replacement)
        }
        return result
    }

    private static func stripHTMLTags(from text: String) -> String {
        decodeHTMLEntities(
            text.replacingOccurrences(of: #"<[^>]+>"#, with: "", options: .regularExpression)
        )
    }

    private static func normalizeHTMLLine(_ line: String) -> String {
        var normalized = line
            .replacingOccurrences(of: "\u{00a0}", with: " ")
            .replacingOccurrences(of: "\t", with: " ")
        while normalized.contains("  ") {
            normalized = normalized.replacingOccurrences(of: "  ", with: " ")
        }
        normalized = normalized.trimmingCharacters(in: .whitespacesAndNewlines)
        if normalized.hasPrefix("= ") {
            normalized.removeFirst(2)
        }
        return normalized
    }

    private static func decodeHTMLEntities(_ text: String) -> String {
        var decoded = text
            .replacingOccurrences(of: "&nbsp;", with: " ")
            .replacingOccurrences(of: "&amp;", with: "&")
            .replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&#39;", with: "'")
            .replacingOccurrences(of: "&#x27;", with: "'")

        guard let regex = try? NSRegularExpression(pattern: #"&#(x?[0-9A-Fa-f]+);"#) else {
            return decoded
        }
        let matches = regex.matches(in: decoded, range: NSRange(decoded.startIndex..., in: decoded))
        for match in matches.reversed() where match.numberOfRanges >= 2 {
            guard let fullRange = Range(match.range(at: 0), in: decoded),
                  let valueRange = Range(match.range(at: 1), in: decoded) else {
                continue
            }
            let raw = String(decoded[valueRange])
            let radix = raw.lowercased().hasPrefix("x") ? 16 : 10
            let digits = radix == 16 ? String(raw.dropFirst()) : raw
            guard let scalarValue = UInt32(digits, radix: radix),
                  let scalar = UnicodeScalar(scalarValue) else {
                continue
            }
            decoded.replaceSubrange(fullRange, with: String(Character(scalar)))
        }
        return decoded
    }
}

@MainActor
final class AppViewModel: ObservableObject {
    @Published var authScope: AuthScope = .enterprise
    @Published var googleOAuth = OAuthConfig(enabled: false)
    @Published var wechatOAuth = OAuthConfig(enabled: false)
    @Published var organizations: [LimiraOrganization] = []
    @Published var selectedOrganizationCategory = "enterprise"
    @Published var selectedOrganizationId = "builtin-limira"
    @Published var user: LimiraUser?
    @Published var scenarios: [LimiraScenario] = []
    @Published var selectedScenarioId = ""
    @Published var tasks: [LimiraTask] = []
    @Published var archivedTasks: [LimiraTask] = []
    @Published var selectedTask: LimiraTask?
    @Published var messages: [AppMessage] = []
    @Published var artifacts = ArtifactBuckets()
    @Published var uploads: [LimiraUploadedDocument] = []
    @Published var cloudFiles: [LimiraUploadedDocument] = []
    @Published var storage: LimiraStoragePayload?
    @Published var reports: [LimiraGeneratedReport] = []
    @Published var eventLogs: [[String: JSONValue]] = []
    @Published var enterpriseMembers: [LimiraUser] = []
    @Published var enterpriseUsage: EnterpriseUsageResponse?
    @Published var selectedTab: ArtifactTab = .evidence
    @Published var compactPresentation = CompactShellPresentation()
    @Published var historyExpanded = true
    @Published var showArchivedHistory = false
    @Published var historySearchResults: [LimiraTask] = []
    @Published var isSearchingHistory = false
    @Published var isVoiceRecording = false
    @Published var isVoiceTranscribing = false
    @Published var voiceMessage = ""
    @Published var status = "ready"
    @Published var archiveStatus = "pending"
    @Published var queryDraft = ""
    @Published var historySearchQuery = ""
    @Published var selectedDocumentIds: Set<String> = []
    @Published var finalReportMarkdown = ""
    @Published var isBusy = false
    @Published var isStreaming = false
    @Published var statusMessage = ""
    @Published var downloadedFile: DownloadedFile?

    let service: LimiraServicing
    private let voiceRecorder: VoiceRecording
    private var streamTask: Task<Void, Never>?

    private let terminalStatuses: Set<String> = ["completed", "failed", "cancelled"]
    private let artifactEvents: Set<String> = [
        "evidence_collected",
        "entity_extracted",
        "relation_extracted",
        "timeline_event_added",
        "map_feature_added",
        "verification_result",
        "report_section_generated",
        "record_research_artifact"
    ]

    let organizationCategoryOptions = [
        OrganizationCategoryOption(value: "enterprise", label: "企业"),
        OrganizationCategoryOption(value: "public_institution", label: "事业单位"),
        OrganizationCategoryOption(value: "university", label: "高校"),
        OrganizationCategoryOption(value: "think_tank", label: "智库"),
        OrganizationCategoryOption(value: "ministry", label: "国家部委"),
        OrganizationCategoryOption(value: "local_government", label: "地方政府")
    ]

    init(service: LimiraServicing, voiceRecorder: VoiceRecording = AVVoiceRecorder()) {
        self.service = service
        self.voiceRecorder = voiceRecorder
    }

    convenience init() {
        if AppConfiguration.isUITestMockEnabled {
            self.init(service: MockLimiraService(), voiceRecorder: MockVoiceRecorder())
        } else {
            self.init(service: LimiraAPIClient())
        }
    }

    deinit {
        streamTask?.cancel()
    }

    var compactRoute: CompactWorkspaceRoute {
        compactPresentation.route
    }

    var compactShowingArtifacts: Bool {
        compactPresentation.isShowingArtifacts
    }

    var selectedArtifactTaskId: String? {
        get { compactPresentation.artifactTaskId }
        set { compactPresentation.artifactTaskId = newValue }
    }

    var historySearchPresented: Bool {
        compactPresentation.modal == .historySearch
    }

    func boot() async {
        await loadAuthOptions()
        guard service.tokenStore.token?.nonEmpty != nil else { return }
        do {
            user = try await service.loadSession()
            await refreshSignedInData()
        } catch {
            service.tokenStore.token = nil
            user = nil
        }
    }

    func loadAuthOptions() async {
        do {
            let (google, wechat, organizations) = try await service.loadAuthOptions()
            googleOAuth = google
            wechatOAuth = wechat
            self.organizations = organizations
            selectDefaultOrganizationForCategory()
        } catch {
            statusMessage = displayError(error)
        }
    }

    var organizationsForSelectedCategory: [LimiraOrganization] {
        organizations.filter { organization in
            (organization.category?.nonEmpty ?? "enterprise") == selectedOrganizationCategory
        }
    }

    func setSelectedOrganizationCategory(_ category: String) {
        selectedOrganizationCategory = category.nonEmpty ?? "enterprise"
        selectDefaultOrganizationForCategory()
    }

    func selectDefaultOrganizationForCategory() {
        let visibleOrganizations = organizationsForSelectedCategory
        if visibleOrganizations.contains(where: { $0.id == selectedOrganizationId }) {
            return
        }
        let preferred = organizations.first { organization in
            organization.slug == "limira" && (organization.category?.nonEmpty ?? "") == selectedOrganizationCategory
        }
        selectedOrganizationId = (preferred ?? visibleOrganizations.first)?.id ?? ""
    }

    func signIn(identifier: String, password: String) async {
        guard !identifier.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !password.isEmpty else {
            statusMessage = "请输入账号和密码。"
            return
        }
        await runBusy {
            let signedIn: LimiraUser
            switch authScope {
            case .personal:
                signedIn = try await service.signInPersonal(identifier: identifier, password: password)
            case .enterprise:
                let organizationId = selectedOrganizationId.nonEmpty ?? "builtin-limira"
                signedIn = try await service.signInEnterprise(organizationId: organizationId, identifier: identifier, password: password)
            }
            user = signedIn
            statusMessage = "已登录：\(signedIn.displayName)"
            await refreshSignedInData()
            if AppConfiguration.isUITestAutoSubmitEnabled {
                if let query = AppConfiguration.uiTestAutoSubmitQuery {
                    queryDraft = query
                }
                await submitResearch()
            }
        }
    }

    func signUp(username: String?, email: String, password: String, name: String?) async {
        await runBusy {
            let created = try await service.signUp(username: username, email: email, password: password, name: name)
            statusMessage = created.emailVerified == true ? "注册完成。" : "注册成功，请完成邮箱验证。"
        }
    }

    func verifyEmail(token: String) async {
        await runBusy {
            user = try await service.verifyEmail(token: token)
            await refreshSignedInData()
        }
    }

    func requestPasswordReset(email: String) async {
        await runBusy {
            try await service.requestPasswordReset(email: email)
            statusMessage = "如果邮箱已注册，重置邮件已发送。"
        }
    }

    func confirmPasswordReset(token: String, password: String) async {
        await runBusy {
            user = try await service.confirmPasswordReset(token: token, password: password)
            await refreshSignedInData()
        }
    }

    func resendVerification(email: String) async {
        await runBusy {
            try await service.resendVerification(email: email)
            statusMessage = "如果需要验证，邮件已重新发送。"
        }
    }

    func signOut() async {
        streamTask?.cancel()
        await runBusy {
            try await service.signOut()
            user = nil
            selectedTask = nil
            tasks = []
            archivedTasks = []
            messages = []
            artifacts = ArtifactBuckets()
            uploads = []
            cloudFiles = []
            reports = []
            status = "ready"
            archiveStatus = "pending"
            compactPresentation.resetToWorkspace()
            selectedDocumentIds = []
            queryDraft = ""
            voiceMessage = ""
            isVoiceRecording = false
            isVoiceTranscribing = false
            statusMessage = "已退出。"
        }
    }

    func refreshSignedInData() async {
        async let scenariosTask: Void = loadScenarios()
        async let historyTask: Void = loadTasks()
        async let cloudTask: Void = loadCloudFiles()
        _ = await (scenariosTask, historyTask, cloudTask)
        if user?.isEnterpriseAdmin == true {
            await loadEnterpriseAdmin()
        }
    }

    func loadScenarios() async {
        do {
            scenarios = try await service.loadScenarios()
            if selectedScenarioId.isEmpty {
                selectedScenarioId = scenarios.first?.id ?? ""
            }
        } catch {
            statusMessage = displayError(error)
        }
    }

    func loadTasks(archived: Bool = false, query: String? = nil) async {
        do {
            let loaded = try await service.loadTasks(archived: archived, query: query)
            if archived {
                archivedTasks = loaded
            } else {
                tasks = loaded
            }
        } catch {
            statusMessage = displayError(error)
        }
    }

    func toggleHistoryArchiveFilter() async {
        showArchivedHistory.toggle()
        await loadTasks(archived: showArchivedHistory)
    }

    func searchHistory() async {
        let query = historySearchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            historySearchResults = []
            return
        }
        isSearchingHistory = true
        defer { isSearchingHistory = false }
        do {
            async let active = service.loadTasks(archived: false, query: query)
            async let archived = service.loadTasks(archived: true, query: query)
            let activeTasks = try await active
            let archivedTasks = try await archived
            let combined = activeTasks + archivedTasks
            var seen: Set<String> = []
            historySearchResults = combined.filter { task in
                if seen.contains(task.taskId) { return false }
                seen.insert(task.taskId)
                return true
            }
        } catch {
            historySearchResults = []
            statusMessage = displayError(error)
        }
    }

    func presentCompactMenu() {
        compactPresentation.present(.menu)
    }

    func presentCompactHistoryFiles() {
        compactPresentation.present(.historyFiles)
    }

    func presentCompactHistorySearch() {
        compactPresentation.present(.historySearch)
    }

    func presentCompactFileImporter() {
        guard !AppConfiguration.isUITestMockEnabled else {
            Task { await uploadMockDocumentForUITest() }
            return
        }
        compactPresentation.present(.fileImporter)
    }

    func dismissCompactModal() {
        compactPresentation.dismissModal()
    }

    func setCompactModal(_ modal: CompactShellModal?) {
        compactPresentation.modal = modal
    }

    func isCompactModalPresented(_ modal: CompactShellModal) -> Bool {
        compactPresentation.modal == modal
    }

    func setCompactDestinationPath(_ path: [CompactShellDestination]) {
        let returnTarget = compactPresentation.returnTarget
        let normalizedPath = path.count > 1 ? Array(path.suffix(1)) : path
        if normalizedPath.last == .enterpriseAdmin, user?.isEnterpriseAdmin != true {
            compactPresentation.resetToWorkspace()
            statusMessage = "当前账号没有单位管理权限。"
            return
        }
        if normalizedPath.isEmpty {
            compactPresentation.path = []
            compactPresentation.artifactTaskId = nil
            compactPresentation.returnTarget = .workspace
            compactPresentation.modal = returnTarget == .menu ? .menu : nil
            return
        }
        compactPresentation.path = normalizedPath
        compactPresentation.dismissModal()
        if normalizedPath.last != .artifacts {
            compactPresentation.artifactTaskId = nil
        }
    }

    func openCompactRoute(_ route: CompactWorkspaceRoute, returnTarget: CompactShellReturnTarget = .workspace) async {
        switch route {
        case .workspace:
            compactPresentation.resetToWorkspace()
        case .cloudDrive:
            compactPresentation.showDestination(.cloudDrive, returnTarget: returnTarget)
            await loadCloudFiles()
            await loadStorage()
        case .archivedChats:
            compactPresentation.showDestination(.archivedChats, returnTarget: returnTarget)
            await loadTasks(archived: true)
        case .enterpriseAdmin:
            guard user?.isEnterpriseAdmin == true else {
                compactPresentation.resetToWorkspace()
                statusMessage = "当前账号没有单位管理权限。"
                return
            }
            compactPresentation.showDestination(.enterpriseAdmin, returnTarget: returnTarget)
            await loadEnterpriseAdmin()
        }
    }

    func startNewChat() async {
        streamTask?.cancel()
        streamTask = nil
        compactPresentation.resetToWorkspace()
        selectedTask = nil
        messages = []
        artifacts = ArtifactBuckets()
        uploads = []
        reports = []
        eventLogs = []
        finalReportMarkdown = ""
        downloadedFile = nil
        selectedDocumentIds = []
        queryDraft = ""
        status = "ready"
        archiveStatus = "pending"
        isStreaming = false
        await loadCloudFiles()
    }

    func submitResearch() async {
        let query = queryDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return }
        let scenario = selectedScenarioId.nonEmpty
        let conversationId = selectedTask?.conversationId ?? selectedTask?.taskId
        let documentIds = Array(selectedDocumentIds)
        messages.append(AppMessage(role: .user, text: query, taskId: selectedTask?.taskId))
        queryDraft = ""
        status = "starting"
        archiveStatus = "pending"
        finalReportMarkdown = ""
        artifacts = ArtifactBuckets()
        reports = []

        await runBusy {
            let task = try await service.startResearch(query: query, scenario: scenario, conversationId: conversationId, documentIds: documentIds)
            selectedTask = task
            status = task.status
            archiveStatus = task.archiveStatus ?? "pending"
            selectedArtifactTaskId = task.taskId
            messages.append(AppMessage(role: .assistant, text: "研究任务已创建：\(task.taskId)", taskId: task.taskId))
            remember(task)
            connectStream(taskId: task.taskId)
            await loadArtifacts()
            await loadUploads()
            await loadTasks()
        }
    }

    func selectTask(_ task: LimiraTask) async {
        compactPresentation.resetToWorkspace()
        selectedTask = task
        selectedArtifactTaskId = task.taskId
        status = task.status
        archiveStatus = task.archiveStatus ?? "pending"
        finalReportMarkdown = ""
        artifacts = ArtifactBuckets()
        eventLogs = []
        messages = conversationMessages(from: task)
        await hydrateConversationHistory(for: task)
        await loadUploads()
        await loadReports()
        if !terminalStatuses.contains(task.status) {
            connectStream(taskId: task.taskId)
        }
    }

    func archive(_ task: LimiraTask) async {
        await runBusy {
            let updated = try await service.archiveHistory(taskId: task.taskId)
            remember(updated)
            await loadTasks()
        }
    }

    func restore(_ task: LimiraTask) async {
        await runBusy {
            let updated = try await service.restoreHistory(taskId: task.taskId)
            remember(updated)
            await loadTasks()
            await loadTasks(archived: true)
        }
    }

    func delete(_ task: LimiraTask) async {
        await runBusy {
            try await service.deleteHistory(taskId: task.taskId)
            tasks.removeAll { $0.taskId == task.taskId }
            archivedTasks.removeAll { $0.taskId == task.taskId }
            historySearchResults.removeAll { $0.taskId == task.taskId }
            if selectedTask?.taskId == task.taskId {
                selectedTask = nil
                messages = []
                artifacts = ArtifactBuckets()
                reports = []
                finalReportMarkdown = ""
                selectedArtifactTaskId = nil
                if compactPresentation.isShowingArtifacts {
                    compactPresentation.resetToWorkspace()
                }
            }
        }
    }

    func connectStream(taskId: String) {
        streamTask?.cancel()
        isStreaming = true
        streamTask = Task {
            do {
                for try await event in service.eventStream(taskId: taskId) {
                    if Task.isCancelled { return }
                    await MainActor.run {
                        handleStreamEvent(event)
                    }
                }
            } catch {
                await MainActor.run {
                    if !terminalStatuses.contains(status) {
                        status = "stream reconnecting"
                        statusMessage = displayError(error)
                    }
                    isStreaming = false
                }
            }
        }
    }

    func handleStreamEvent(_ event: LimiraStreamEvent) {
        let normalizedEvent = normalizedStreamEvent(event)
        if let publicStatus = event.publicStatus ?? normalizedEvent.status {
            status = publicStatus
        }
        if let archive = event.raw["archive_status"]?.stringValue
            ?? event.data?.string("archive_status")
            ?? event.data?["data"]?.objectValue?.string("archive_status")
            ?? normalizedEvent.data.string("archive_status") {
            archiveStatus = archive
        }

        switch normalizedEvent.eventType {
        case "heartbeat":
            break
        case "tool_call":
            handleToolCall(event, eventData: normalizedEvent.data)
        case "error":
            status = "failed"
            messages.append(AppMessage(role: .error, text: event.displayText, taskId: selectedTask?.taskId))
        case "end_of_workflow":
            status = "completed"
            if !upsertReportMessageFromEventData(normalizedEvent.data, taskId: selectedTask?.taskId) {
                messages.append(AppMessage(role: .assistant, text: "工作流已完成。", taskId: selectedTask?.taskId))
            }
        case let value where value.hasPrefix("start_of_"):
            messages.append(AppMessage(role: .assistant, text: startMessage(for: value, event: event), taskId: selectedTask?.taskId))
        case let value where artifactEvents.contains(value):
            messages.append(AppMessage(role: .assistant, text: "\(eventLabel(value))：研究成果已更新。", taskId: selectedTask?.taskId))
            Task { await loadArtifacts() }
        default:
            messages.append(AppMessage(role: .assistant, text: "\(eventLabel(normalizedEvent.eventType))：\(event.displayText)", taskId: selectedTask?.taskId))
        }

        if terminalStatuses.contains(status) {
            isStreaming = false
            streamTask?.cancel()
            Task {
                await loadArtifacts()
                await loadReports()
                await loadUploads()
                await loadTasks()
            }
        }
    }

    func loadArtifacts() async {
        guard let taskId = selectedTask?.taskId else { return }
        do {
            let loadedArtifacts = try await service.loadArtifacts(taskId: taskId)
            artifacts = loadedArtifacts
            selectedArtifactTaskId = taskId
            let report = reportMarkdown(from: loadedArtifacts)
            if !report.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                finalReportMarkdown = report
                upsertReportMessage(report, taskId: taskId, artifacts: loadedArtifacts, insertAfterTaskId: taskId)
            }
        } catch {
            statusMessage = displayError(error)
        }
    }

    func loadEventLogs() async {
        guard let taskId = selectedTask?.taskId else { return }
        do {
            eventLogs = try await service.loadEventLogs(taskId: taskId).events
            restoreReportFromEventLogs(taskId: taskId)
        } catch {
            eventLogs = []
        }
    }

    func loadReports() async {
        guard let taskId = selectedTask?.taskId else { return }
        do {
            reports = try await service.loadReports(taskId: taskId)
        } catch {
            reports = []
        }
    }

    func loadUploads() async {
        guard user != nil else { return }
        do {
            let response = try await service.loadUploads(taskId: selectedTask?.taskId)
            uploads = response.documents
            storage = response.storage ?? storage
        } catch {
            uploads = []
        }
    }

    func loadCloudFiles() async {
        guard user != nil else { return }
        do {
            let response = try await service.loadCloudHistory()
            cloudFiles = response.documents
            storage = response.storage
        } catch {
            cloudFiles = []
        }
    }

    func loadStorage() async {
        guard user != nil else { return }
        do {
            storage = try await service.loadStorage()
        } catch {
            statusMessage = displayError(error)
        }
    }

    func uploadDocument(url: URL) async {
        await runBusy {
            let data = try Data(contentsOf: url)
            let file = UploadFilePayload(
                data: data,
                filename: url.lastPathComponent,
                contentType: UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
            )
            let document = try await service.uploadDocument(file: file, taskId: selectedTask?.taskId)
            selectedDocumentIds.insert(document.documentId)
            uploads.insert(document, at: 0)
            cloudFiles.insert(document, at: 0)
            await loadCloudFiles()
        }
    }

    func uploadMockDocumentForUITest() async {
        guard AppConfiguration.isUITestMockEnabled else { return }
        await runBusy {
            let file = UploadFilePayload(
                data: Data("limira ios ui mock upload".utf8),
                filename: "limira-ios-ui-upload.txt",
                contentType: "text/plain"
            )
            let document = try await service.uploadDocument(file: file, taskId: selectedTask?.taskId)
            selectedDocumentIds.insert(document.documentId)
            uploads.removeAll { $0.documentId == document.documentId }
            cloudFiles.removeAll { $0.documentId == document.documentId }
            uploads.insert(document, at: 0)
            cloudFiles.insert(document, at: 0)
            statusMessage = "已添加测试文件：\(document.filename)"
        }
    }

    func toggleSelectedDocument(_ document: LimiraUploadedDocument) {
        if selectedDocumentIds.contains(document.documentId) {
            selectedDocumentIds.remove(document.documentId)
        } else {
            selectedDocumentIds.insert(document.documentId)
        }
    }

    func removeSelectedDocument(_ documentId: String) {
        selectedDocumentIds.remove(documentId)
    }

    var selectedDocuments: [LimiraUploadedDocument] {
        let source = uploads + cloudFiles
        var seen: Set<String> = []
        return source.filter { document in
            guard selectedDocumentIds.contains(document.documentId), !seen.contains(document.documentId) else {
                return false
            }
            seen.insert(document.documentId)
            return true
        }
    }

    func searchUploads(query: String) async {
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            await loadUploads()
            return
        }
        await runBusy {
            uploads = try await service.searchUploads(query: query, taskId: selectedTask?.taskId)
        }
    }

    func downloadUpload(_ document: LimiraUploadedDocument) async {
        guard let path = document.downloadUrl else { return }
        await runBusy {
            downloadedFile = try await service.download(relativeOrAbsolutePath: path, suggestedFilename: document.filename)
        }
    }

    func downloadArchive(taskId explicitTaskId: String? = nil) async {
        let taskId = explicitTaskId?.nonEmpty ?? selectedTask?.taskId
        guard let taskId else {
            statusMessage = "当前没有可下载的归档。"
            return
        }
        let status = archiveStatus(for: taskId)
        guard status == "ready" else {
            statusMessage = status == "failed" ? "归档生成失败。" : "归档尚未生成。"
            return
        }
        let path = taskRecord(for: taskId)?.downloadUrl ?? "/api/limira/tasks/\(taskId)/archive.zip"
        await runBusy {
            downloadedFile = try await service.download(relativeOrAbsolutePath: path, suggestedFilename: "\(taskId)-archive.zip")
        }
    }

    func exportPDF() async {
        guard let task = selectedTask else { return }
        let markdown = currentReportMarkdown().trimmingCharacters(in: .whitespacesAndNewlines)
        guard !markdown.isEmpty else {
            statusMessage = "当前任务还没有可导出的报告。"
            return
        }
        await runBusy {
            let report = try await service.exportPDF(
                taskId: task.taskId,
                reportId: "ios-\(Int(Date().timeIntervalSince1970))",
                markdown: markdown,
                evidenceRefs: evidenceRefs()
            )
            reports.insert(report, at: 0)
            if let pdfUrl = report.pdfUrl {
                downloadedFile = try await service.download(relativeOrAbsolutePath: pdfUrl, suggestedFilename: "\(report.reportId).pdf")
            }
        }
    }

    func loadEnterpriseAdmin() async {
        do {
            async let members = service.loadEnterpriseMembers()
            async let usage = service.loadEnterpriseUsage(days: 30)
            enterpriseMembers = try await members
            enterpriseUsage = try await usage
        } catch {
            statusMessage = displayError(error)
        }
    }

    func createEnterpriseMember(username: String?, email: String?, password: String, name: String?, role: String) async {
        await runBusy {
            let member = try await service.createEnterpriseMember(username: username, email: email, password: password, name: name, role: role)
            enterpriseMembers.insert(member, at: 0)
        }
    }

    func toggleVoiceInput() async {
        if isVoiceRecording {
            await stopVoiceInputAndTranscribe()
        } else {
            await startVoiceInput()
        }
    }

    func startVoiceInput() async {
        guard !isVoiceRecording, !isVoiceTranscribing else { return }
        do {
            try await voiceRecorder.start()
            isVoiceRecording = true
            voiceMessage = "正在录音，点击麦克风结束并转写。"
        } catch {
            isVoiceRecording = false
            voiceMessage = displayError(error)
        }
    }

    func stopVoiceInputAndTranscribe() async {
        guard isVoiceRecording else { return }
        isVoiceRecording = false
        isVoiceTranscribing = true
        voiceMessage = "正在转写语音..."
        do {
            let file = try await voiceRecorder.stop()
            let response = try await service.transcribeSpeech(file: file, language: Locale.preferredLanguages.first ?? "zh-CN")
            appendVoiceTranscript(response.text)
            voiceMessage = "语音已写入输入框。"
        } catch {
            voiceMessage = "语音转写失败：\(displayError(error))"
        }
        isVoiceTranscribing = false
    }

    func appendVoiceTranscript(_ text: String) {
        let newText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !newText.isEmpty else { return }
        let current = queryDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        queryDraft = current.isEmpty ? newText : "\(current) \(newText)"
    }

    func openArtifacts(tab: ArtifactTab, taskId: String? = nil) async {
        selectedTab = tab
        let targetTaskId = taskId?.nonEmpty ?? selectedTask?.taskId
        compactPresentation.showArtifacts(taskId: targetTaskId)
        guard let targetTaskId, targetTaskId != selectedTask?.taskId else { return }
        do {
            artifacts = try await service.loadArtifacts(taskId: targetTaskId)
        } catch {
            statusMessage = displayError(error)
        }
    }

    func activateCompactArtifacts(tab: ArtifactTab, taskId: String? = nil) {
        selectedTab = tab
        let targetTaskId = taskId?.nonEmpty ?? selectedTask?.taskId
        compactPresentation.showArtifacts(taskId: targetTaskId)
        guard let targetTaskId, targetTaskId != selectedTask?.taskId else { return }
        Task {
            do {
                artifacts = try await service.loadArtifacts(taskId: targetTaskId)
            } catch {
                statusMessage = displayError(error)
            }
        }
    }

    func backToConversation() {
        compactPresentation.resetToWorkspace()
    }

    func artifactCount(for tab: ArtifactTab, artifacts: ArtifactBuckets? = nil) -> Int {
        let buckets = artifacts ?? self.artifacts
        switch tab {
        case .evidence:
            return buckets.evidence.count
        case .entities:
            return buckets.entities.count
        case .graph:
            return buckets.entities.count + buckets.relations.count
        case .timeline:
            return buckets.timelineEvents.count
        case .map:
            return buckets.mapFeatures.count
        case .report:
            return buckets.reportSections.count
        }
    }

    func compactArtifactTabs() -> [ArtifactTab] {
        [.evidence, .entities, .graph, .timeline, .map]
    }

    func archiveStatus(for taskId: String) -> String {
        if selectedTask?.taskId == taskId {
            return archiveStatus
        }
        return taskRecord(for: taskId)?.archiveStatus ?? "pending"
    }

    func currentReportMarkdown() -> String {
        if !finalReportMarkdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return finalReportMarkdown
        }
        let report = reportMarkdown(from: artifacts)
        if !report.isEmpty { return report }
        return messages.last(where: { $0.role == .assistant })?.text ?? ""
    }

    private func runBusy(_ operation: () async throws -> Void) async {
        isBusy = true
        defer { isBusy = false }
        do {
            try await operation()
        } catch {
            statusMessage = displayError(error)
            messages.append(AppMessage(role: .error, text: statusMessage))
        }
    }

    private func remember(_ task: LimiraTask) {
        tasks.removeAll { $0.taskId == task.taskId }
        tasks.insert(task, at: 0)
        if selectedTask?.taskId == task.taskId {
            selectedTask = task
        }
    }

    private func handleToolCall(_ event: LimiraStreamEvent, eventData: [String: JSONValue]? = nil) {
        let data = eventData ?? normalizedStreamEvent(event).data
        let toolName = data.string("tool_name", "toolName", "tool", "name") ?? "tool"
        if toolName == "show_text",
           let input = toolInput(from: data),
           let report = reportMarkdown(fromEventValue: .object(input)) {
            finalReportMarkdown = report
            upsertReportMessage(report, taskId: selectedTask?.taskId, artifacts: artifacts, insertAfterTaskId: selectedTask?.taskId)
            return
        }
        let target = toolInput(from: data)?.string("url").map { " · \($0)" } ?? ""
        messages.append(AppMessage(role: .assistant, text: "调用工具：\(toolName)\(target)", taskId: selectedTask?.taskId))
    }

    private struct NormalizedStreamEvent {
        var eventType: String
        var data: [String: JSONValue]
        var status: String?
    }

    private func normalizedStreamEvent(_ event: LimiraStreamEvent) -> NormalizedStreamEvent {
        let payload = event.payload ?? event.raw
        let data = event.data ?? event.payload ?? [:]
        let nested = data["data"]?.objectValue ?? [:]
        let embeddedEventType = data.string("event", "event_type", "eventName", "event_name", "type")
            ?? nested.string("event", "event_type", "eventName", "event_name", "type")
            ?? payload.string("event", "event_type", "eventName", "event_name", "type")
        let eventType = event.event == "task_update"
            ? (embeddedEventType ?? "task_update")
            : (event.event.nonEmpty ?? embeddedEventType ?? "task_update")
        let eventData = data.string("event", "event_type", "eventName", "event_name", "type") == eventType && !nested.isEmpty ? nested : data
        let status = event.status
            ?? data.string("status")
            ?? nested.string("status")
            ?? eventData.string("status")
            ?? payload.string("status")
        return NormalizedStreamEvent(eventType: eventType, data: eventData, status: status)
    }

    @discardableResult
    private func upsertReportMessageFromEventData(_ data: [String: JSONValue], taskId: String?) -> Bool {
        guard let report = reportMarkdown(fromEventData: data) else {
            return false
        }
        finalReportMarkdown = report
        upsertReportMessage(report, taskId: taskId, artifacts: artifacts, insertAfterTaskId: taskId)
        return true
    }

    private func reportMarkdown(fromEventData data: [String: JSONValue]) -> String? {
        for key in ["text", "markdown", "content", "body", "summary", "report", "final_report", "final_report_markdown"] {
            guard let value = lookupValue(key, in: data),
                  let report = reportMarkdown(fromEventValue: value) else {
                continue
            }
            return report
        }
        for key in ["payload", "result", "data", "artifact"] {
            guard let nested = lookupValue(key, in: data),
                  let report = reportMarkdown(fromEventValue: nested) else {
                continue
            }
            return report
        }
        return nil
    }

    private func restoreReportFromEventLogs(taskId: String) {
        guard let report = reportMarkdown(fromEventLogs: eventLogs) else { return }
        finalReportMarkdown = report
        upsertReportMessage(report, taskId: taskId, artifacts: artifacts, insertAfterTaskId: taskId)
    }

    private func reportMarkdown(fromEventLogs logs: [[String: JSONValue]]) -> String? {
        for rawEvent in logs.reversed() {
            let event = streamEvent(from: rawEvent)
            let normalized = normalizedStreamEvent(event)
            if normalized.eventType == "tool_call" {
                let data = normalized.data
                let toolName = data.string("tool_name", "toolName", "tool", "name") ?? "tool"
                if toolName == "show_text",
                   let input = toolInput(from: data),
                   let report = reportMarkdown(fromEventValue: .object(input)) {
                    return report
                }
            }
            if let report = reportMarkdown(fromEventData: normalized.data) {
                return report
            }
        }
        return nil
    }

    private func streamEvent(from raw: [String: JSONValue]) -> LimiraStreamEvent {
        guard let data = try? JSONEncoder().encode(raw),
              let event = try? JSONDecoder().decode(LimiraStreamEvent.self, from: data) else {
            return LimiraStreamEvent(event: raw.string("event", "event_type", "eventName", "event_name", "type") ?? "task_update", data: raw, raw: raw)
        }
        return event
    }

    private func toolInput(from data: [String: JSONValue]) -> [String: JSONValue]? {
        for key in ["tool_input", "toolInput", "input", "arguments", "args", "parameters"] {
            guard let value = lookupValue(key, in: data) else { continue }
            if let object = objectValue(from: value) {
                return object
            }
        }
        for key in ["payload", "result", "data"] {
            guard let nested = lookupValue(key, in: data),
                  let object = objectValue(from: nested) else {
                continue
            }
            if let direct = toolInput(from: object) {
                return direct
            }
        }
        return nil
    }

    private func reportMarkdown(fromEventValue value: JSONValue) -> String? {
        ReportMarkdownExtractor.markdown(from: value, includeTitle: true)
            ?? value.stringValue?.nonEmpty
    }

    private func objectValue(from value: JSONValue) -> [String: JSONValue]? {
        if let object = value.objectValue {
            return object
        }
        guard let text = value.stringValue?.nonEmpty,
              let data = text.data(using: .utf8),
              let decoded = try? JSONDecoder().decode(JSONValue.self, from: data),
              let object = decoded.objectValue else {
            return nil
        }
        return object
    }

    private func lookupValue(_ key: String, in object: [String: JSONValue]) -> JSONValue? {
        if let value = object[key] { return value }
        let lowercased = key.lowercased()
        return object.first { $0.key.lowercased() == lowercased }?.value
    }

    private func startMessage(for eventType: String, event: LimiraStreamEvent) -> String {
        if eventType == "start_of_workflow" { return "工作流已启动。" }
        if eventType == "start_of_agent" {
            return "智能体已启动：\(event.data?.string("agent_name", "display_name") ?? "agent")。"
        }
        if eventType == "start_of_llm" {
            return "模型步骤已启动：\(event.data?.string("agent_name") ?? "agent")。"
        }
        return eventLabel(eventType)
    }

    private func eventLabel(_ eventType: String) -> String {
        [
            "evidence_collected": "证据",
            "entity_extracted": "实体",
            "relation_extracted": "关系",
            "timeline_event_added": "时间线",
            "map_feature_added": "地图",
            "verification_result": "核验",
            "report_section_generated": "报告",
            "record_research_artifact": "成果"
        ][eventType] ?? eventType
    }

    private func conversationMessages(from task: LimiraTask) -> [AppMessage] {
        conversationMembers(for: task).flatMap { member in
            [
                AppMessage(role: .user, text: member.query, taskId: member.taskId),
                AppMessage(role: .assistant, text: "任务 \(member.taskId)：\(member.status)", taskId: member.taskId)
            ]
        }
    }

    private func conversationMembers(for task: LimiraTask) -> [LimiraTask] {
        let members = task.conversationMembers?.filter { !$0.taskId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? []
        return members.isEmpty ? [task] : members
    }

    private func hydrateConversationHistory(for task: LimiraTask) async {
        for member in conversationMembers(for: task) {
            let taskId = member.taskId
            do {
                let loadedArtifacts = try await service.loadArtifacts(taskId: taskId)
                if taskId == task.taskId {
                    artifacts = loadedArtifacts
                    selectedArtifactTaskId = taskId
                }
                let artifactReport = reportMarkdown(from: loadedArtifacts).nonEmpty
                let eventReport = artifactReport == nil ? await reportMarkdownFromEventLogs(taskId: taskId) : nil
                if let report = artifactReport ?? eventReport {
                    if taskId == task.taskId {
                        finalReportMarkdown = report
                    }
                    upsertReportMessage(report, taskId: taskId, artifacts: loadedArtifacts, insertAfterTaskId: taskId)
                }
            } catch {
                if taskId == task.taskId {
                    statusMessage = displayError(error)
                }
            }
        }
    }

    private func reportMarkdownFromEventLogs(taskId: String) async -> String? {
        do {
            let logs = try await service.loadEventLogs(taskId: taskId).events
            if selectedTask?.taskId == taskId {
                eventLogs = logs
            }
            return reportMarkdown(fromEventLogs: logs)
        } catch {
            if selectedTask?.taskId == taskId {
                eventLogs = []
            }
            return nil
        }
    }

    private func reportMarkdown(from buckets: ArtifactBuckets) -> String {
        ReportMarkdownExtractor.markdown(from: buckets)
    }

    private func upsertReportMessage(_ markdown: String, taskId: String?, artifacts: ArtifactBuckets, insertAfterTaskId: String? = nil) {
        let text = markdown.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let counts = Dictionary(uniqueKeysWithValues: compactArtifactTabs().map { ($0.rawValue, artifactCount(for: $0, artifacts: artifacts)) })
        if let index = messages.lastIndex(where: { $0.isReport && $0.taskId == taskId }) {
            messages[index].text = text
            messages[index].artifactCounts = counts
        } else {
            let message = AppMessage(role: .assistant, text: text, taskId: taskId, isReport: true, artifactCounts: counts)
            if let insertAfterTaskId,
               let index = messages.lastIndex(where: { $0.taskId == insertAfterTaskId && !$0.isReport }) {
                messages.insert(message, at: messages.index(after: index))
            } else {
                messages.append(message)
            }
        }
    }

    private func taskRecord(for taskId: String) -> LimiraTask? {
        if selectedTask?.taskId == taskId { return selectedTask }
        return tasks.first { $0.taskId == taskId }
            ?? archivedTasks.first { $0.taskId == taskId }
            ?? historySearchResults.first { $0.taskId == taskId }
    }

    private func evidenceRefs() -> [String] {
        artifacts.evidence.compactMap { $0.fields.string("evidence_id", "id", "ref") }
    }

    private func displayError(_ error: Error) -> String {
        if let localized = error as? LocalizedError, let message = localized.errorDescription {
            return message
        }
        return error.localizedDescription
    }
}

protocol VoiceRecording: AnyObject {
    func start() async throws
    func stop() async throws -> UploadFilePayload
}

enum VoiceRecordingError: LocalizedError, Equatable {
    case permissionDenied
    case startFailed
    case notRecording
    case emptyAudio

    var errorDescription: String? {
        switch self {
        case .permissionDenied:
            return "麦克风权限未开启，请在系统设置中允许 Limira 使用麦克风。"
        case .startFailed:
            return "录音没有成功开始，请稍后重试。"
        case .notRecording:
            return "当前没有正在录制的语音。"
        case .emptyAudio:
            return "录音内容为空，请重新录制。"
        }
    }
}

final class AVVoiceRecorder: NSObject, VoiceRecording {
    private var recorder: AVAudioRecorder?
    private var outputURL: URL?

    func start() async throws {
        let granted = await requestPermission()
        guard granted else { throw VoiceRecordingError.permissionDenied }

        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try session.setActive(true)

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("limira-voice-\(UUID().uuidString)")
            .appendingPathExtension("m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 44_100,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue
        ]
        let recorder = try AVAudioRecorder(url: url, settings: settings)
        recorder.isMeteringEnabled = true
        guard recorder.record() else {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw VoiceRecordingError.startFailed
        }
        self.recorder = recorder
        outputURL = url
    }

    func stop() async throws -> UploadFilePayload {
        guard let recorder, let url = outputURL else {
            throw VoiceRecordingError.notRecording
        }
        recorder.stop()
        self.recorder = nil
        outputURL = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)

        let data = try Data(contentsOf: url)
        try? FileManager.default.removeItem(at: url)
        guard !data.isEmpty else { throw VoiceRecordingError.emptyAudio }
        return UploadFilePayload(data: data, filename: "voice.m4a", contentType: "audio/mp4")
    }

    private func requestPermission() async -> Bool {
        if #available(iOS 17.0, *) {
            return await AVAudioApplication.requestRecordPermission()
        }
        return await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }
}

final class MockVoiceRecorder: VoiceRecording {
    private var recording = false

    func start() async throws {
        recording = true
    }

    func stop() async throws -> UploadFilePayload {
        guard recording else { throw VoiceRecordingError.notRecording }
        recording = false
        return UploadFilePayload(data: Data("mock voice".utf8), filename: "voice.m4a", contentType: "audio/mp4")
    }
}

final class MockLimiraService: LimiraServicing {
    let tokenStore: TokenStoring = MemoryTokenStore()
    var mockReportSectionsEnabled = true
    var mockEventLogs: [[String: JSONValue]] = [["event": .string("mock")]]
    var mockEventLogsByTaskId: [String: [[String: JSONValue]]] = [:]
    var mockArtifactsByTaskId: [String: ArtifactBuckets] = [:]
    private var deletedTaskIds: Set<String> = []
    private var mockUploadedDocuments: [LimiraUploadedDocument] = []
    private var mockTask = LimiraTask(
        taskId: "mock-task-001",
        conversationId: "mock-task-001",
        query: "测试旧金山服务器连接状态",
        status: "completed",
        archiveStatus: "ready",
        historyArchived: false,
        scenario: "geopolitical_risk_assessment",
        error: nil,
        modelSummary: ["provider": .string("mock")],
        downloadUrl: "/api/limira/tasks/mock-task-001/archive.zip",
        eventsUrl: "/api/limira/tasks/mock-task-001/events",
        artifactsUrl: "/api/limira/tasks/mock-task-001/artifacts",
        conversationMembers: nil,
        conversationCount: nil,
        uploadedDocuments: nil
    )

    func loadAuthOptions() async throws -> (OAuthConfig, OAuthConfig, [LimiraOrganization]) {
        (OAuthConfig(enabled: false), OAuthConfig(enabled: false), [LimiraOrganization(id: "builtin-limira", name: "Limira", slug: "limira", active: true, category: "enterprise", categoryLabel: "企业", billingMode: "metered")])
    }

    func loadSession() async throws -> LimiraUser { mockUser() }
    func signInPersonal(identifier: String, password: String) async throws -> LimiraUser { mockUser(accountType: "personal") }
    func signInEnterprise(organizationId: String, identifier: String, password: String) async throws -> LimiraUser {
        tokenStore.token = "mock-token"
        return mockUser()
    }
    func signUp(username: String?, email: String, password: String, name: String?) async throws -> LimiraUser { mockUser(accountType: "personal") }
    func verifyEmail(token: String) async throws -> LimiraUser { mockUser(accountType: "personal") }
    func resendVerification(email: String) async throws {}
    func requestPasswordReset(email: String) async throws {}
    func confirmPasswordReset(token: String, password: String) async throws -> LimiraUser { mockUser(accountType: "personal") }
    func signOut() async throws { tokenStore.token = nil }

    func loadScenarios() async throws -> [LimiraScenario] {
        [LimiraScenario(id: "geopolitical_risk_assessment", title: "Geopolitical risk assessment", description: "Assess current political and trade risks.", defaultQuery: "Assess current supply-chain risk.", focusAreas: ["official sources", "timeline"])]
    }

    func loadTasks(archived: Bool, query: String?) async throws -> [LimiraTask] {
        guard !deletedTaskIds.contains(mockTask.taskId),
              mockTask.historyArchived == archived,
              taskMatches(mockTask, query: query) else {
            return []
        }
        return [mockTask]
    }

    func startResearch(query: String, scenario: String?, conversationId: String?, documentIds: [String]) async throws -> LimiraTask {
        mockTask = LimiraTask(
            taskId: "mock-task-\(Int(Date().timeIntervalSince1970))",
            conversationId: conversationId,
            query: query,
            status: "completed",
            archiveStatus: "ready",
            historyArchived: false,
            scenario: scenario,
            error: nil,
            modelSummary: nil,
            downloadUrl: "/api/limira/tasks/mock-task/archive.zip",
            eventsUrl: "/api/limira/tasks/mock-task/events",
            artifactsUrl: nil,
            conversationMembers: nil,
            conversationCount: nil,
            uploadedDocuments: nil
        )
        return mockTask
    }

    func archiveHistory(taskId: String) async throws -> LimiraTask {
        guard taskId == mockTask.taskId, !deletedTaskIds.contains(taskId) else { return mockTask }
        mockTask.historyArchived = true
        return mockTask
    }

    func restoreHistory(taskId: String) async throws -> LimiraTask {
        guard taskId == mockTask.taskId, !deletedTaskIds.contains(taskId) else { return mockTask }
        mockTask.historyArchived = false
        return mockTask
    }

    func deleteHistory(taskId: String) async throws {
        deletedTaskIds.insert(taskId)
    }

    func eventStream(taskId: String) -> AsyncThrowingStream<LimiraStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            continuation.yield(LimiraStreamEvent(event: "start_of_workflow", status: "running"))
            continuation.yield(LimiraStreamEvent(event: "evidence_collected", status: "running", data: ["message": .string("mock evidence")]))
            continuation.yield(LimiraStreamEvent(event: "end_of_workflow", status: "completed"))
            continuation.finish()
        }
    }

    func loadArtifacts(taskId: String) async throws -> ArtifactBuckets {
        if let buckets = mockArtifactsByTaskId[taskId] {
            return buckets
        }
        var buckets = ArtifactBuckets()
        buckets.evidence = [ResearchArtifact(fields: ["evidence_id": .string("EVID-001"), "title": .string("Mock source"), "url": .string("https://limira-inc.com"), "confidence": .string("high")])]
        buckets.entities = [ResearchArtifact(fields: ["entity_id": .string("ENT-001"), "name": .string("Limira")])]
        buckets.relations = [ResearchArtifact(fields: ["relation_id": .string("REL-001"), "source": .string("Limira"), "target": .string("SF server")])]
        buckets.timelineEvents = [ResearchArtifact(fields: ["event_id": .string("TL-001"), "title": .string("iOS smoke"), "date": .string("2026-06-10")])]
        buckets.mapFeatures = [ResearchArtifact(fields: ["id": .string("MAP-001"), "title": .string("San Francisco"), "geometry": .object(["type": .string("Point"), "coordinates": .array([.number(-122.4194), .number(37.7749)])])])]
        buckets.verifications = [ResearchArtifact(fields: ["id": .string("VER-001"), "summary": .string("Mock verified")])]
        if mockReportSectionsEnabled {
            buckets.reportSections = [ResearchArtifact(fields: ["section_id": .string("REPORT-001"), "title": .string("Mock report"), "markdown": .string("# Mock report\nSSE and artifacts are connected.")])]
        }
        return buckets
    }

    func loadEventLogs(taskId: String) async throws -> EventLogsResponse {
        let logs = mockEventLogsByTaskId[taskId] ?? mockEventLogs
        return EventLogsResponse(taskId: taskId, count: logs.count, events: logs, adminView: nil)
    }

    func loadReports(taskId: String) async throws -> [LimiraGeneratedReport] {
        [
            LimiraGeneratedReport(
                reportId: "mock-report-001",
                taskId: taskId,
                reportType: "final",
                evidenceRefs: ["EVID-001"],
                markdownChars: 64,
                htmlChars: nil,
                pdfSizeBytes: 4,
                pdfSha256: "mock",
                pdfUrl: "/api/limira/tasks/\(taskId)/reports/mock-report-001/pdf"
            )
        ]
    }
    func loadUploads(taskId: String?) async throws -> UploadsResponse {
        let documents = mockUploadedDocuments.filter { $0.taskId == taskId } + [mockDocument(taskId: taskId)]
        return UploadsResponse(documents: documents, storage: mockStorage())
    }
    func loadCloudHistory() async throws -> UploadsResponse {
        let documents = mockUploadedDocuments.filter { $0.taskId == nil } + [mockDocument(taskId: nil)]
        return UploadsResponse(documents: documents, storage: mockStorage())
    }
    func loadStorage() async throws -> LimiraStoragePayload { mockStorage() }

    func uploadDocument(file: UploadFilePayload, taskId: String?) async throws -> LimiraUploadedDocument {
        let document = LimiraUploadedDocument(documentId: "mock-doc", taskId: taskId, filename: file.filename, contentType: file.contentType, byteSize: file.data.count, language: nil, extractedTextChars: nil, downloadUrl: "/api/limira/uploads/mock-doc/download", score: nil, snippet: "mock uploaded document", matchedTerms: nil)
        mockUploadedDocuments.removeAll { $0.documentId == document.documentId }
        mockUploadedDocuments.insert(document, at: 0)
        return document
    }

    func searchUploads(query: String, taskId: String?) async throws -> [LimiraUploadedDocument] { [mockDocument(taskId: taskId)] }

    func download(relativeOrAbsolutePath: String, suggestedFilename: String) async throws -> DownloadedFile {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(suggestedFilename)
        try Data("mock".utf8).write(to: url, options: .atomic)
        return DownloadedFile(url: url, filename: suggestedFilename, contentType: "text/plain")
    }

    func exportPDF(taskId: String, reportId: String, markdown: String, evidenceRefs: [String]) async throws -> LimiraGeneratedReport {
        LimiraGeneratedReport(reportId: reportId, taskId: taskId, reportType: "final", evidenceRefs: evidenceRefs, markdownChars: markdown.count, htmlChars: nil, pdfSizeBytes: 4, pdfSha256: "mock", pdfUrl: "/api/limira/tasks/\(taskId)/reports/\(reportId)/pdf")
    }

    func loadEnterpriseMembers() async throws -> [LimiraUser] { [mockUser()] }
    func loadEnterpriseUsage(days: Int) async throws -> EnterpriseUsageResponse {
        EnterpriseUsageResponse(organization: LimiraOrganization(id: "builtin-limira", name: "Limira", slug: "limira", active: true, category: "enterprise", categoryLabel: "企业", billingMode: "metered"), usage: ["days": .number(Double(days)), "totals": .object(["research_tasks": .number(1)])])
    }

    func createEnterpriseMember(username: String?, email: String?, password: String, name: String?, role: String) async throws -> LimiraUser {
        LimiraUser(id: UUID().uuidString, email: email, username: username, name: name, role: "user", emailVerified: true, accountType: "enterprise", organizationId: "builtin-limira", organizationRole: role, dailyResearchLimit: nil, token: nil, tokenType: nil, organization: nil)
    }

    func transcribeSpeech(file: UploadFilePayload, language: String?) async throws -> SpeechTranscriptionResponse {
        SpeechTranscriptionResponse(text: "mock speech", language: language, durationSeconds: 1, contentType: file.contentType, filename: file.filename)
    }

    private func mockUser(accountType: String = "enterprise") -> LimiraUser {
        LimiraUser(
            id: "mock-user",
            email: "admin@limira.local",
            username: "limira-admin",
            name: "Limira Admin",
            role: "admin",
            emailVerified: true,
            accountType: accountType,
            organizationId: accountType == "enterprise" ? "builtin-limira" : nil,
            organizationRole: accountType == "enterprise" ? "admin" : nil,
            dailyResearchLimit: nil,
            token: "mock-token",
            tokenType: "bearer",
            organization: nil
        )
    }

    private func mockStorage() -> LimiraStoragePayload {
        LimiraStoragePayload(usedBytes: 1024, quotaBytes: 10_485_760, remainingBytes: 10_484_736, usageRatio: 0.000097)
    }

    private func taskMatches(_ task: LimiraTask, query: String?) -> Bool {
        guard let query = query?.trimmingCharacters(in: .whitespacesAndNewlines),
              !query.isEmpty else {
            return true
        }
        return task.query.localizedCaseInsensitiveContains(query) ||
            task.status.localizedCaseInsensitiveContains(query) ||
            task.taskId.localizedCaseInsensitiveContains(query)
    }

    private func mockDocument(taskId: String?) -> LimiraUploadedDocument {
        LimiraUploadedDocument(
            documentId: taskId == nil ? "mock-cloud-doc" : "mock-task-doc",
            taskId: taskId,
            filename: taskId == nil ? "limira-cloud-smoke.txt" : "limira-task-smoke.txt",
            contentType: "text/plain",
            byteSize: 128,
            language: nil,
            extractedTextChars: 64,
            downloadUrl: "/api/limira/uploads/mock-doc/download",
            score: nil,
            snippet: "mock uploaded document",
            matchedTerms: nil
        )
    }
}
