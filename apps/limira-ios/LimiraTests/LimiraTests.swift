import Foundation
import XCTest
@testable import Limira

final class LimiraTests: XCTestCase {
    override func tearDown() {
        MockURLProtocol.handler = nil
        super.tearDown()
    }

    func testRelativeURLAndAuthorizationHeader() async throws {
        let store = MemoryTokenStore()
        store.token = "test-token"
        let client = LimiraAPIClient(
            baseURL: URL(string: "https://limira-inc.com")!,
            session: Self.mockSession(),
            tokenStore: store
        )
        let url = try client.makeURL(path: "/api/limira/scenarios", queryItems: [URLQueryItem(name: "limit", value: "1")])
        XCTAssertEqual(url.absoluteString, "https://limira-inc.com/api/limira/scenarios?limit=1")

        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
            XCTAssertEqual(request.url?.absoluteString, "https://limira-inc.com/api/limira/scenarios")
            let data = #"{"scenarios":[],"count":0}"#.data(using: .utf8)!
            return (HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: ["content-type": "application/json"])!, data)
        }

        let scenarios = try await client.loadScenarios()
        XCTAssertEqual(scenarios.count, 0)
    }

    func testSSEParserParsesMultipleEvents() throws {
        let text = """
        data: {"event":"heartbeat","status":"running"}

        data: {"event":"end_of_workflow","status":"completed","data":{"message":"done"}}

        """
        let events = SSEParser.parse(text)
        XCTAssertEqual(events.map(\.event), ["heartbeat", "end_of_workflow"])
        XCTAssertEqual(events.last?.publicStatus, "completed")
        XCTAssertEqual(events.last?.displayText, "done")
    }

    func testTaskDecodingSupportsConversationFields() throws {
        let data = """
        {
          "task_id":"task-a",
          "conversation_id":"root-a",
          "query":"hello",
          "status":"completed",
          "archive_status":"ready",
          "history_archived":false,
          "download_url":"/api/limira/tasks/task-a/archive.zip",
          "conversation_members":[
            {"task_id":"task-a","conversation_id":"root-a","query":"hello","status":"completed","archive_status":"ready"}
          ],
          "model_summary":{"provider":"openai","latency":1.25}
        }
        """.data(using: .utf8)!
        let task = try JSONDecoder().decode(LimiraTask.self, from: data)
        XCTAssertEqual(task.taskId, "task-a")
        XCTAssertEqual(task.conversationId, "root-a")
        XCTAssertEqual(task.conversationMembers?.count, 1)
        XCTAssertEqual(task.modelSummary?["provider"]?.stringValue, "openai")
    }

    func testMultipartBodyContainsFieldsAndFile() throws {
        let file = UploadFilePayload(data: Data("hello".utf8), filename: "limira-ios-smoke.txt", contentType: "text/plain")
        let body = LimiraAPIClient.multipartBody(fields: ["task_id": "task-a"], fileFieldName: "file", file: file, boundary: "Boundary-Test")
        let text = String(data: body, encoding: .utf8)
        XCTAssertNotNil(text)
        XCTAssertTrue(text!.contains("name=\"task_id\""))
        XCTAssertTrue(text!.contains("task-a"))
        XCTAssertTrue(text!.contains("filename=\"limira-ios-smoke.txt\""))
        XCTAssertTrue(text!.contains("Content-Type: text/plain"))
        XCTAssertTrue(text!.hasSuffix("--Boundary-Test--\r\n"))
    }

    func testSpeechTranscriptionRequestUsesMultipart() async throws {
        let client = LimiraAPIClient(
            baseURL: URL(string: "https://limira-inc.com")!,
            session: Self.mockSession(),
            tokenStore: MemoryTokenStore()
        )
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.absoluteString, "https://limira-inc.com/api/limira/speech/transcribe")
            XCTAssertTrue(request.value(forHTTPHeaderField: "Content-Type")?.contains("multipart/form-data; boundary=") == true)
            let body = try Self.requestBody(from: request)
            let text = String(data: body, encoding: .utf8)
            XCTAssertTrue(text?.contains("name=\"language\"") == true)
            XCTAssertTrue(text?.contains("zh-CN") == true)
            XCTAssertTrue(text?.contains("filename=\"voice.m4a\"") == true)
            XCTAssertTrue(text?.contains("Content-Type: audio/mp4") == true)
            let data = #"{"text":"转写文本","language":"zh-CN","duration_seconds":1.0,"content_type":"audio/mp4","filename":"voice.m4a"}"#.data(using: .utf8)!
            return (HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: ["content-type": "application/json"])!, data)
        }

        let response = try await client.transcribeSpeech(
            file: UploadFilePayload(data: Data("voice".utf8), filename: "voice.m4a", contentType: "audio/mp4"),
            language: "zh-CN"
        )
        XCTAssertEqual(response.text, "转写文本")
    }

    @MainActor
    func testVoiceTranscriptionAppendsDraft() async throws {
        let service = MockLimiraService()
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.user = try await service.signInEnterprise(organizationId: "builtin-limira", identifier: "admin", password: "password")
        model.queryDraft = "已有内容"

        await model.toggleVoiceInput()
        XCTAssertTrue(model.isVoiceRecording)
        await model.toggleVoiceInput()

        XCTAssertFalse(model.isVoiceRecording)
        XCTAssertFalse(model.isVoiceTranscribing)
        XCTAssertEqual(model.queryDraft, "已有内容 mock speech")
    }

    @MainActor
    func testCompactRouteLoadsCloudDrive() async throws {
        let service = MockLimiraService()
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.user = try await service.signInEnterprise(organizationId: "builtin-limira", identifier: "admin", password: "password")

        await model.openCompactRoute(.cloudDrive)

        XCTAssertEqual(model.compactRoute, .cloudDrive)
        XCTAssertNotNil(model.storage)
        XCTAssertEqual(model.cloudFiles.first?.filename, "limira-cloud-smoke.txt")
    }

    @MainActor
    func testCompactPresentationClearsModalsWhenOpeningDestinations() async throws {
        let service = MockLimiraService()
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.user = try await service.signInEnterprise(organizationId: "builtin-limira", identifier: "admin", password: "password")

        model.presentCompactMenu()
        XCTAssertEqual(model.compactPresentation.modal, .menu)

        await model.openCompactRoute(.cloudDrive)
        XCTAssertEqual(model.compactPresentation.path, [.cloudDrive])
        XCTAssertNil(model.compactPresentation.modal)
        XCTAssertEqual(model.compactRoute, .cloudDrive)
        XCTAssertFalse(model.compactShowingArtifacts)

        model.presentCompactHistoryFiles()
        XCTAssertEqual(model.compactPresentation.modal, .historyFiles)
        await model.openCompactRoute(.archivedChats)
        XCTAssertEqual(model.compactPresentation.path, [.archivedChats])
        XCTAssertNil(model.compactPresentation.modal)

        model.presentCompactHistorySearch()
        XCTAssertEqual(model.compactPresentation.modal, .historySearch)
        await model.openCompactRoute(.enterpriseAdmin)
        XCTAssertEqual(model.compactPresentation.path, [.enterpriseAdmin])
        XCTAssertNil(model.compactPresentation.modal)
    }

    @MainActor
    func testCompactStartNewChatResetsPresentationAndDraftState() async throws {
        let service = MockLimiraService()
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.user = try await service.signInEnterprise(organizationId: "builtin-limira", identifier: "admin", password: "password")
        model.selectedTask = try await service.startResearch(query: "old", scenario: nil, conversationId: nil, documentIds: [])
        model.messages = [AppMessage(role: .user, text: "old")]
        model.selectedDocumentIds = ["mock-cloud-doc"]
        model.queryDraft = "draft"
        model.downloadedFile = DownloadedFile(url: URL(fileURLWithPath: "/tmp/mock.txt"), filename: "mock.txt", contentType: "text/plain")
        model.compactPresentation.showArtifacts(taskId: "mock-task")
        model.presentCompactHistoryFiles()

        await model.startNewChat()

        XCTAssertTrue(model.compactPresentation.path.isEmpty)
        XCTAssertNil(model.compactPresentation.modal)
        XCTAssertNil(model.compactPresentation.artifactTaskId)
        XCTAssertEqual(model.compactRoute, .workspace)
        XCTAssertFalse(model.compactShowingArtifacts)
        XCTAssertNil(model.selectedTask)
        XCTAssertTrue(model.messages.isEmpty)
        XCTAssertTrue(model.selectedDocumentIds.isEmpty)
        XCTAssertEqual(model.queryDraft, "")
        XCTAssertNil(model.downloadedFile)
    }

    @MainActor
    func testCompactEnterpriseAdminRequiresAdminRole() async throws {
        let service = MockLimiraService()
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.user = LimiraUser(
            id: "member-user",
            email: "member@limira.local",
            username: "member",
            name: "Member",
            role: "user",
            emailVerified: true,
            accountType: "enterprise",
            organizationId: "builtin-limira",
            organizationRole: "member",
            dailyResearchLimit: nil,
            token: nil,
            tokenType: nil,
            organization: nil
        )

        model.presentCompactMenu()
        await model.openCompactRoute(.enterpriseAdmin)

        XCTAssertTrue(model.compactPresentation.path.isEmpty)
        XCTAssertNil(model.compactPresentation.modal)
        XCTAssertEqual(model.compactRoute, .workspace)
        XCTAssertTrue(model.statusMessage.contains("单位管理权限"))
    }

    @MainActor
    func testCompactArtifactTabsOmitReport() {
        let model = AppViewModel(service: MockLimiraService(), voiceRecorder: MockVoiceRecorder())
        XCTAssertEqual(model.compactArtifactTabs(), [.evidence, .entities, .graph, .timeline, .map])
        XCTAssertFalse(model.compactArtifactTabs().contains(.report))
    }

    @MainActor
    func testToolCallShowTextFromNestedJSONStringUpsertsFinalReport() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.selectedTask = try await service.startResearch(query: "movie values", scenario: nil, conversationId: nil, documentIds: [])

        model.handleStreamEvent(
            LimiraStreamEvent(
                event: "task_update",
                status: "running",
                data: [
                    "event": .string("tool_call"),
                    "tool_name": .string("show_text"),
                    "tool_input": .string(##"{"text":"# Final answer\nYoung audiences are split."}"##)
                ]
            )
        )

        XCTAssertEqual(model.finalReportMarkdown, "# Final answer\nYoung audiences are split.")
        XCTAssertEqual(model.messages.last?.isReport, true)
        XCTAssertEqual(model.messages.last?.text, "# Final answer\nYoung audiences are split.")
    }

    @MainActor
    func testSelectTaskRestoresFinalReportFromEventLogsWhenArtifactsDoNotContainReportSections() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        service.mockEventLogs = [
            [
                "event": .string("task_update"),
                "data": .object([
                    "event": .string("tool_call"),
                    "tool_name": .string("show_text"),
                    "tool_input": .string(##"{"text":"# Restored final answer\nThe web answer should also appear on iOS."}"##)
                ])
            ]
        ]
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        let task = try await service.startResearch(query: "restore report", scenario: nil, conversationId: nil, documentIds: [])

        await model.selectTask(task)

        XCTAssertEqual(model.finalReportMarkdown, "# Restored final answer\nThe web answer should also appear on iOS.")
        XCTAssertEqual(model.messages.last?.isReport, true)
        XCTAssertEqual(model.messages.last?.taskId, task.taskId)
    }

    @MainActor
    func testSelectTaskRestoresEventTypeToolCallLogs() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        service.mockEventLogs = [
            [
                "event_type": .string("tool_call"),
                "payload": .object([
                    "toolName": .string("show_text"),
                    "toolInput": .string(##"{"text":"# Restored from event_type\nThis mirrors live event logs."}"##)
                ])
            ]
        ]
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        let task = try await service.startResearch(query: "restore event type", scenario: nil, conversationId: nil, documentIds: [])

        await model.selectTask(task)

        XCTAssertEqual(model.finalReportMarkdown, "# Restored from event_type\nThis mirrors live event logs.")
        XCTAssertEqual(model.messages.last?.isReport, true)
        XCTAssertEqual(model.messages.last?.taskId, task.taskId)
    }

    @MainActor
    func testSelectTaskHydratesConversationMemberReportAfterMatchingStatus() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        service.mockEventLogsByTaskId = [
            "task-cancelled": [],
            "task-completed": [
                [
                    "event_type": .string("tool_call"),
                    "payload": .object([
                        "tool_name": .string("show_text"),
                        "tool_input": .string(##"{"text":"# Completed conversation report\nThe completed member should render in place."}"##)
                    ])
                ]
            ]
        ]
        let cancelled = LimiraTask(
            taskId: "task-cancelled",
            conversationId: "conversation-1",
            query: "first attempt",
            status: "cancelled",
            archiveStatus: nil,
            historyArchived: false,
            scenario: nil,
            error: nil,
            modelSummary: nil,
            downloadUrl: nil,
            eventsUrl: nil,
            artifactsUrl: nil,
            conversationMembers: nil,
            conversationCount: nil,
            uploadedDocuments: nil
        )
        let completed = LimiraTask(
            taskId: "task-completed",
            conversationId: "conversation-1",
            query: "second attempt",
            status: "completed",
            archiveStatus: "ready",
            historyArchived: false,
            scenario: nil,
            error: nil,
            modelSummary: nil,
            downloadUrl: nil,
            eventsUrl: nil,
            artifactsUrl: nil,
            conversationMembers: nil,
            conversationCount: nil,
            uploadedDocuments: nil
        )
        let conversation = LimiraTask(
            taskId: completed.taskId,
            conversationId: "conversation-1",
            query: completed.query,
            status: completed.status,
            archiveStatus: completed.archiveStatus,
            historyArchived: false,
            scenario: nil,
            error: nil,
            modelSummary: nil,
            downloadUrl: nil,
            eventsUrl: nil,
            artifactsUrl: nil,
            conversationMembers: [cancelled, completed],
            conversationCount: 2,
            uploadedDocuments: nil
        )
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())

        await model.selectTask(conversation)

        let userIndex = try XCTUnwrap(model.messages.firstIndex { $0.role == .user && $0.taskId == completed.taskId })
        let reportIndex = try XCTUnwrap(model.messages.firstIndex { $0.isReport && $0.taskId == completed.taskId })
        XCTAssertGreaterThan(reportIndex, userIndex)
        XCTAssertFalse(model.messages.contains { $0.text.contains("任务 task-completed") })
        XCTAssertEqual(model.messages[reportIndex].text, "# Completed conversation report\nThe completed member should render in place.")
        XCTAssertEqual(model.finalReportMarkdown, "# Completed conversation report\nThe completed member should render in place.")
    }

    @MainActor
    func testActiveTaskSubmitCancelsInsteadOfPostingSecondResearch() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        let task = try await service.startResearch(query: "first", scenario: nil, conversationId: nil, documentIds: [])
        model.selectedTask = task
        model.activeTaskId = task.taskId
        model.status = "running"
        model.queryDraft = "second"

        await model.submitResearch()

        XCTAssertEqual(service.startResearchCallCount, 1)
        XCTAssertEqual(service.cancelTaskCallCount, 1)
        XCTAssertEqual(model.queryDraft, "second")
        XCTAssertFalse(model.messages.contains { $0.role == .user && $0.text == "second" })
    }

    @MainActor
    func testSubmitResearchBindsLatestUserMessageToNewTask() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        service.mockStreamEvents = []
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        model.queryDraft = "fresh research"

        await model.submitResearch()

        XCTAssertEqual(service.startResearchCallCount, 1)
        let userMessage = try XCTUnwrap(model.messages.first { $0.role == .user && $0.text == "fresh research" })
        let taskId = try XCTUnwrap(model.selectedTask?.taskId)
        XCTAssertEqual(userMessage.taskId, taskId)
    }

    @MainActor
    func testStatusAndArchiveEventsDoNotEnterChatMessages() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        let task = try await service.startResearch(query: "status hygiene", scenario: nil, conversationId: nil, documentIds: [])
        model.selectedTask = task
        model.activeTaskId = task.taskId
        model.status = "running"
        model.messages = [AppMessage(role: .user, text: "status hygiene", taskId: task.taskId)]

        model.handleStreamEvent(
            LimiraStreamEvent(
                event: "archive_generated",
                status: "running",
                data: [
                    "archive_status": .string("ready"),
                    "archive_url": .string("/api/limira/tasks/\(task.taskId)/archive.zip")
                ]
            )
        )
        model.handleStreamEvent(
            LimiraStreamEvent(
                event: "status",
                status: "completed",
                data: [
                    "status": .string("completed"),
                    "archive_status": .string("ready"),
                    "terminal": .bool(true)
                ]
            )
        )
        model.handleStreamEvent(
            LimiraStreamEvent(
                event: "message",
                message: #"{"task_id":"\#(task.taskId)","type":"archive_generated","payload":{"archive_status":"ready"}}"#
            )
        )

        XCTAssertEqual(model.messages.count, 1)
        XCTAssertTrue(model.thinkingSteps.contains { $0.kind == "archive" })
        XCTAssertFalse(model.messages.contains { $0.text.contains("{\"task_id\"") || $0.text.contains("archive_generated") })
    }

    @MainActor
    func testEventStreamReconnectUsesLastEventId() async throws {
        let service = MockLimiraService()
        service.mockReportSectionsEnabled = false
        var event = LimiraStreamEvent(event: "evidence_collected", status: "running", data: ["message": .string("first source")])
        event.streamEventId = "event-1"
        service.mockStreamEvents = [event]
        let model = AppViewModel(service: service, voiceRecorder: MockVoiceRecorder())
        let task = try await service.startResearch(query: "resume stream", scenario: nil, conversationId: nil, documentIds: [])
        model.selectedTask = task
        model.activeTaskId = task.taskId
        model.status = "running"

        model.connectStream(taskId: task.taskId)
        try await Task.sleep(nanoseconds: 100_000_000)
        model.connectStream(taskId: task.taskId)
        try await Task.sleep(nanoseconds: 100_000_000)

        XCTAssertEqual(service.lastStreamLastEventId, "event-1")
        XCTAssertTrue(model.thinkingSteps.contains { $0.detail.contains("first source") })
    }

    func testResearchArtifactEvidenceSourceURLHelpers() {
        let item = ResearchArtifact(fields: [
            "evidence_id": .string("EVID-777"),
            "title": .string("Source title"),
            "source_url": .string("https://example.com/report"),
            "summary": .string("Useful source summary."),
            "confidence": .string("high"),
            "published_at": .string("2026-06-12")
        ])

        XCTAssertEqual(item.evidenceIdentifier, "EVID-777")
        XCTAssertEqual(item.evidenceSummary, "Useful source summary.")
        XCTAssertEqual(item.confidence, "high")
        XCTAssertEqual(item.publishedAt, "2026-06-12")
        XCTAssertEqual(item.sourceURL?.absoluteString, "https://example.com/report")
    }

    func testReportMarkdownExtractorUnwrapsStringifiedReportJSON() throws {
        let data = """
        {
          "report_sections": [
            {
              "section_id": "REPORT-RAW",
              "title": "Raw container",
              "markdown": "{\\"title\\":\\"Executive summary\\",\\"markdown\\":\\"# Executive summary\\\\n\\\\n- Export controls changed.\\\\n- Semiconductor supply chains are exposed.\\"}"
            }
          ]
        }
        """.data(using: .utf8)!

        let buckets = try JSONDecoder().decode(ArtifactBuckets.self, from: data)
        let markdown = ReportMarkdownExtractor.markdown(from: buckets)

        XCTAssertTrue(markdown.contains("# Executive summary"))
        XCTAssertTrue(markdown.contains("- Export controls changed."))
        XCTAssertFalse(markdown.contains("\\\"markdown\\\""))
        XCTAssertFalse(markdown.contains("Raw container"))
    }

    func testReportMarkdownExtractorBuildsNestedSections() throws {
        let data = """
        {
          "report_sections": [
            {
              "section_id": "REPORT-NESTED",
              "content": {
                "sections": [
                  {"title": "Findings", "text": "First finding."},
                  {"section_title": "Risks", "summary": "Second finding."}
                ]
              }
            }
          ]
        }
        """.data(using: .utf8)!

        let buckets = try JSONDecoder().decode(ArtifactBuckets.self, from: data)
        let markdown = ReportMarkdownExtractor.markdown(from: buckets)

        XCTAssertTrue(markdown.contains("## Findings"))
        XCTAssertTrue(markdown.contains("First finding."))
        XCTAssertTrue(markdown.contains("## Risks"))
        XCTAssertTrue(markdown.contains("Second finding."))
        XCTAssertFalse(markdown.contains("section_id"))
    }

    func testReportMarkdownExtractorSanitizesToolHTMLFragments() throws {
        let data = """
        {
          "report_sections": [
            {
              "section_id": "REPORT-HTML",
              "markdown": "执行结果<div class=\\"search-card\\"><div class=\\"search-header\\"><span class=\\"search-icon\\">🔍</span><span class=\\"search-query\\">Search: \\"US company Indian agency factory worker data collection training embodied AI\\"</span></div><div class=\\"search-count\\">= Found 10 results</div><div class=\\"search-results\\"><a href=\\"https://www.youtube.com/watch?v=0nS6i5uZJp0\\" target=\\"_blank\\" class=\\"search-result-item\\"><span class=\\"result-icon\\">🌐</span><span class=\\"result-title\\">Meet the factory workers training A.I. to replace themselves - YouTube</span></a><a href=\\"https://www.reddit.com/r/GenAI4all/comments/1sijyds/indian_factory_workers_wear_headmounted_cameras/\\" target=\\"_blank\\" class=\\"search-result-item\\"><span class=\\"result-icon\\">🌐</span><span class=\\"result-title\\">Indian factory workers wear headmounted cameras</span></a></div></div>"
            }
          ]
        }
        """.data(using: .utf8)!

        let buckets = try JSONDecoder().decode(ArtifactBuckets.self, from: data)
        let markdown = ReportMarkdownExtractor.markdown(from: buckets)

        XCTAssertTrue(markdown.contains("执行结果"))
        XCTAssertTrue(markdown.contains("Search:"))
        XCTAssertTrue(markdown.contains("Found 10 results"))
        XCTAssertTrue(markdown.contains("Meet the factory workers training A.I. to replace themselves - YouTube"))
        XCTAssertTrue(markdown.contains("https://www.youtube.com/watch?v=0nS6i5uZJp0"))
        XCTAssertFalse(markdown.contains("<div"))
        XCTAssertFalse(markdown.contains("class="))
        XCTAssertFalse(markdown.contains("</span>"))
    }

    func testReportMarkdownParserKeepsBlockStructureAndTables() {
        let markdown = """
        # 美国人对制裁中国的态度：系统性研究报告

        报告日期：2026年6月10日
        研究方法：基于多项权威民调机构数据分析

        ---

        ## 二、关键数据速览

        | 情境/制裁类型 | 支持率 | 反对率 | 数据来源与时间 |
        | --- | ---: | ---: | --- |
        | 台湾危机中对华经济外交制裁 | 71% | — | Chicago Council, 2025年秋 |
        | 对华高科技出口管制 | 68%-69% | — | Chicago Council/Ipsos |
        """

        let blocks = ReportMarkdownParser.parse(markdown)

        XCTAssertEqual(blocks.first, .heading(level: 1, text: "美国人对制裁中国的态度：系统性研究报告"))
        XCTAssertTrue(blocks.contains(.horizontalRule))
        XCTAssertTrue(blocks.contains(.heading(level: 2, text: "二、关键数据速览")))
        guard case .table(let headers, let rows) = blocks.last else {
            return XCTFail("Expected final block to be a table.")
        }
        XCTAssertEqual(headers, ["情境/制裁类型", "支持率", "反对率", "数据来源与时间"])
        XCTAssertEqual(rows.count, 2)
        XCTAssertEqual(rows[0][1], "71%")
    }

    func testInlineMarkdownParserHandlesQuotedChineseBoldAndItalic() {
        let text = #"Reddit r/rs_x（2025年）的讨论中，有年轻观众尖锐指出电影展示的是一种**"被宠坏的生活" (coddled life)，那些"富人子女觉得自己被承诺可以拥有的生活"** [3]。*The Avocado* 提到了**"可疑的同意" (dubious consent)**。"#

        let segments = InlineMarkdownParser.parse(text)
        let rendered = segments.map(\.text).joined()

        XCTAssertFalse(rendered.contains("**"))
        XCTAssertFalse(rendered.contains("*The Avocado*"))
        XCTAssertTrue(segments.contains { $0.isBold && $0.text.contains("被宠坏的生活") })
        XCTAssertTrue(segments.contains { $0.isBold && $0.text.contains("可疑的同意") })
        XCTAssertTrue(segments.contains { $0.isItalic && $0.text == "The Avocado" })
    }

    func testBinaryDownloadWritesSuggestedFile() async throws {
        let client = LimiraAPIClient(
            baseURL: URL(string: "https://limira-inc.com")!,
            session: Self.mockSession(),
            tokenStore: MemoryTokenStore()
        )
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "*/*")
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: [
                        "content-type": "application/zip",
                        "content-disposition": #"attachment; filename="archive.zip""#
                    ]
                )!,
                Data("zip".utf8)
            )
        }

        let file = try await client.download(relativeOrAbsolutePath: "/api/limira/tasks/task-a/archive.zip", suggestedFilename: "fallback.zip")
        XCTAssertEqual(file.filename, "archive.zip")
        XCTAssertEqual(try Data(contentsOf: file.url), Data("zip".utf8))
    }

    private static func mockSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private static func requestBody(from request: URLRequest) throws -> Data {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return Data()
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count < 0 {
                throw stream.streamError ?? LimiraAPIError.transport("failed to read request body stream")
            }
            if count == 0 {
                break
            }
            data.append(buffer, count: count)
        }
        return data
    }
}

private final class MockURLProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: LimiraAPIError.transport("missing mock handler"))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
