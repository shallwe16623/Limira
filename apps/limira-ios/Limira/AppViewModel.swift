import AVFoundation
import Combine
import Foundation
import UniformTypeIdentifiers

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
    @Published var compactRoute: CompactWorkspaceRoute = .workspace
    @Published var compactShowingArtifacts = false
    @Published var selectedArtifactTaskId: String?
    @Published var historyExpanded = true
    @Published var showArchivedHistory = false
    @Published var historySearchPresented = false
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
            compactRoute = .workspace
            compactShowingArtifacts = false
            selectedArtifactTaskId = nil
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

    func openCompactRoute(_ route: CompactWorkspaceRoute) async {
        compactRoute = route
        compactShowingArtifacts = false
        switch route {
        case .workspace:
            break
        case .cloudDrive:
            await loadCloudFiles()
            await loadStorage()
        case .archivedChats:
            await loadTasks(archived: true)
        case .enterpriseAdmin:
            if user?.isEnterpriseAdmin == true {
                await loadEnterpriseAdmin()
            }
        }
    }

    func startNewChat() async {
        streamTask?.cancel()
        streamTask = nil
        compactRoute = .workspace
        compactShowingArtifacts = false
        selectedArtifactTaskId = nil
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
        compactRoute = .workspace
        compactShowingArtifacts = false
        selectedTask = task
        selectedArtifactTaskId = task.taskId
        status = task.status
        archiveStatus = task.archiveStatus ?? "pending"
        messages = conversationMessages(from: task)
        await loadArtifacts()
        await loadUploads()
        await loadReports()
        await loadEventLogs()
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
                compactShowingArtifacts = false
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
        if let publicStatus = event.publicStatus {
            status = publicStatus
        }
        if let archive = event.raw["archive_status"]?.stringValue
            ?? event.data?.string("archive_status")
            ?? event.data?["data"]?.objectValue?.string("archive_status") {
            archiveStatus = archive
        }

        switch event.event {
        case "heartbeat":
            break
        case "tool_call":
            handleToolCall(event)
        case "error":
            status = "failed"
            messages.append(AppMessage(role: .error, text: event.displayText, taskId: selectedTask?.taskId))
        case "end_of_workflow":
            status = "completed"
            messages.append(AppMessage(role: .assistant, text: "工作流已完成。", taskId: selectedTask?.taskId))
        case let value where value.hasPrefix("start_of_"):
            messages.append(AppMessage(role: .assistant, text: startMessage(for: value, event: event), taskId: selectedTask?.taskId))
        case let value where artifactEvents.contains(value):
            messages.append(AppMessage(role: .assistant, text: "\(eventLabel(value))：研究成果已更新。", taskId: selectedTask?.taskId))
            Task { await loadArtifacts() }
        default:
            messages.append(AppMessage(role: .assistant, text: "\(eventLabel(event.event))：\(event.displayText)", taskId: selectedTask?.taskId))
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
                upsertReportMessage(report, taskId: taskId, artifacts: loadedArtifacts)
            }
        } catch {
            statusMessage = displayError(error)
        }
    }

    func loadEventLogs() async {
        guard let taskId = selectedTask?.taskId else { return }
        do {
            eventLogs = try await service.loadEventLogs(taskId: taskId).events
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
        compactShowingArtifacts = true
        compactRoute = .workspace
        let targetTaskId = taskId?.nonEmpty ?? selectedTask?.taskId
        selectedArtifactTaskId = targetTaskId
        guard let targetTaskId, targetTaskId != selectedTask?.taskId else { return }
        do {
            artifacts = try await service.loadArtifacts(taskId: targetTaskId)
        } catch {
            statusMessage = displayError(error)
        }
    }

    func activateCompactArtifacts(tab: ArtifactTab, taskId: String? = nil) {
        selectedTab = tab
        compactShowingArtifacts = true
        compactRoute = .workspace
        let targetTaskId = taskId?.nonEmpty ?? selectedTask?.taskId
        selectedArtifactTaskId = targetTaskId
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
        compactShowingArtifacts = false
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

    private func handleToolCall(_ event: LimiraStreamEvent) {
        let data = event.data ?? event.payload ?? [:]
        let toolName = data.string("tool_name", "name") ?? "tool"
        if toolName == "show_text",
           let input = data["tool_input"]?.objectValue,
           let text = input.string("text") {
            finalReportMarkdown = text
            upsertReportMessage(text, taskId: selectedTask?.taskId, artifacts: artifacts)
            return
        }
        let target = data["tool_input"]?.objectValue?.string("url").map { " · \($0)" } ?? ""
        messages.append(AppMessage(role: .assistant, text: "调用工具：\(toolName)\(target)", taskId: selectedTask?.taskId))
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
        let members = task.conversationMembers?.isEmpty == false ? task.conversationMembers! : [task]
        return members.flatMap { member in
            [
                AppMessage(role: .user, text: member.query),
                AppMessage(role: .assistant, text: "任务 \(member.taskId)：\(member.status)", taskId: member.taskId)
            ]
        }
    }

    private func reportMarkdown(from buckets: ArtifactBuckets) -> String {
        buckets.reportSections.compactMap { artifact -> String? in
            let title = artifact.fields.string("title", "section_title")
            let body = artifact.fields.string("markdown", "body", "text", "content", "summary")
            guard let body, !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                return nil
            }
            if let title, !body.hasPrefix("#") {
                return "## \(title)\n\n\(body)"
            }
            return body
        }
        .joined(separator: "\n\n")
    }

    private func upsertReportMessage(_ markdown: String, taskId: String?, artifacts: ArtifactBuckets) {
        let text = markdown.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let counts = Dictionary(uniqueKeysWithValues: compactArtifactTabs().map { ($0.rawValue, artifactCount(for: $0, artifacts: artifacts)) })
        if let index = messages.lastIndex(where: { $0.isReport && $0.taskId == taskId }) {
            messages[index].text = text
            messages[index].artifactCounts = counts
        } else {
            messages.append(AppMessage(role: .assistant, text: text, taskId: taskId, isReport: true, artifactCounts: counts))
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
        archived ? [] : [mockTask]
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

    func archiveHistory(taskId: String) async throws -> LimiraTask { mockTask }
    func restoreHistory(taskId: String) async throws -> LimiraTask { mockTask }
    func deleteHistory(taskId: String) async throws {}

    func eventStream(taskId: String) -> AsyncThrowingStream<LimiraStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            continuation.yield(LimiraStreamEvent(event: "start_of_workflow", status: "running"))
            continuation.yield(LimiraStreamEvent(event: "evidence_collected", status: "running", data: ["message": .string("mock evidence")]))
            continuation.yield(LimiraStreamEvent(event: "end_of_workflow", status: "completed"))
            continuation.finish()
        }
    }

    func loadArtifacts(taskId: String) async throws -> ArtifactBuckets {
        let data = """
        {
          "evidence": [{"evidence_id":"EVID-001","title":"Mock source","url":"https://limira-inc.com","confidence":"high"}],
          "entities": [{"entity_id":"ENT-001","name":"Limira"}],
          "relations": [{"relation_id":"REL-001","source":"Limira","target":"SF server"}],
          "timeline_events": [{"event_id":"TL-001","title":"iOS smoke","date":"2026-06-10"}],
          "map_features": [{"id":"MAP-001","title":"San Francisco","geometry":{"type":"Point","coordinates":[-122.4194,37.7749]}}],
          "verifications": [{"id":"VER-001","summary":"Mock verified"}],
          "report_sections": [{"section_id":"REPORT-001","title":"Mock report","markdown":"# Mock report\\nSSE and artifacts are connected."}]
        }
        """.data(using: .utf8)!
        return try JSONDecoder().decode(ArtifactBuckets.self, from: data)
    }

    func loadEventLogs(taskId: String) async throws -> EventLogsResponse {
        EventLogsResponse(taskId: taskId, count: 1, events: [["event": .string("mock")]], adminView: nil)
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
        UploadsResponse(documents: [mockDocument(taskId: taskId)], storage: mockStorage())
    }
    func loadCloudHistory() async throws -> UploadsResponse {
        UploadsResponse(documents: [mockDocument(taskId: nil)], storage: mockStorage())
    }
    func loadStorage() async throws -> LimiraStoragePayload { mockStorage() }

    func uploadDocument(file: UploadFilePayload, taskId: String?) async throws -> LimiraUploadedDocument {
        LimiraUploadedDocument(documentId: "mock-doc", taskId: taskId, filename: file.filename, contentType: file.contentType, byteSize: file.data.count, language: nil, extractedTextChars: nil, downloadUrl: "/api/limira/uploads/mock-doc/download", score: nil, snippet: nil, matchedTerms: nil)
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
