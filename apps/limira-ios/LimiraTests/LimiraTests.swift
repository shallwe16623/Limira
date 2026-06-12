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
    func testCompactArtifactTabsOmitReport() {
        let model = AppViewModel(service: MockLimiraService(), voiceRecorder: MockVoiceRecorder())
        XCTAssertEqual(model.compactArtifactTabs(), [.evidence, .entities, .graph, .timeline, .map])
        XCTAssertFalse(model.compactArtifactTabs().contains(.report))
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
