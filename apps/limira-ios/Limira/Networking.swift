import Foundation
import Security

enum AppConfiguration {
    static let allowsInAppPersonalSignup = false

    static func apiBaseURL() -> URL {
        if let override = commandLineValue(for: "-LimiraAPIBaseURL"),
           let url = URL(string: override) {
            return url
        }
        if let raw = Bundle.main.object(forInfoDictionaryKey: "LimiraAPIBaseURL") as? String,
           let url = URL(string: raw) {
            return url
        }
        return URL(string: "https://limira-inc.com")!
    }

    static var isUITestMockEnabled: Bool {
        commandLineValue(for: "-LimiraUITestMock") == "YES"
    }

    static var isUITestProbeEnabled: Bool {
        isUITestMockEnabled || commandLineValue(for: "-LimiraUITestProbe") == "YES"
    }

    static var isUITestAutoSubmitEnabled: Bool {
        commandLineValue(for: "-LimiraUITestAutoSubmit") == "YES"
    }

    static var uiTestAutoSubmitQuery: String? {
        commandLineValue(for: "-LimiraUITestAutoSubmitQuery")?.nonEmpty
    }

    private static func commandLineValue(for key: String) -> String? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: key),
              arguments.indices.contains(arguments.index(after: index)) else {
            return nil
        }
        return arguments[arguments.index(after: index)]
    }
}

protocol TokenStoring: AnyObject {
    var token: String? { get set }
}

final class MemoryTokenStore: TokenStoring {
    var token: String?
}

final class KeychainTokenStore: TokenStoring {
    private let service: String
    private let account: String

    init(service: String = "com.limira.ios", account: String = "limira-api-token") {
        self.service = service
        self.account = account
    }

    var token: String? {
        get {
            var query = baseQuery()
            query[kSecMatchLimit as String] = kSecMatchLimitOne
            query[kSecReturnData as String] = true
            var result: AnyObject?
            let status = SecItemCopyMatching(query as CFDictionary, &result)
            guard status == errSecSuccess,
                  let data = result as? Data,
                  let value = String(data: data, encoding: .utf8) else {
                return nil
            }
            return value
        }
        set {
            let query = baseQuery()
            SecItemDelete(query as CFDictionary)
            guard let newValue, let data = newValue.data(using: .utf8) else {
                return
            }
            var item = query
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemAdd(item as CFDictionary, nil)
        }
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
    }
}

enum LimiraAPIError: LocalizedError, Equatable {
    case invalidURL(String)
    case httpStatus(Int, String)
    case emptyResponse
    case decoding(String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL(let path):
            return "无效接口地址：\(path)"
        case .httpStatus(_, let detail):
            return Self.localizedDetail(detail)
        case .emptyResponse:
            return "服务没有返回内容。"
        case .decoding(let detail):
            return "服务返回的数据无法解析：\(detail)"
        case .transport(let detail):
            return detail
        }
    }

    static func localizedDetail(_ detail: String) -> String {
        let map = [
            "invalid_credentials": "账号或密码不正确。",
            "email_not_verified": "邮箱尚未验证。",
            "enterprise_login_required": "请使用企业登录。",
            "username_required": "请输入用户名或邮箱。",
            "personal_daily_quota_exceeded": "个人账号今日研究次数已用完。",
            "enterprise_cloud_storage_required": "文件能力仅支持企业账号。",
            "enterprise_cloud_storage_quota_exceeded": "企业云文件空间不足。",
            "runner_research_start_failed": "研究任务启动失败。",
            "runner_task_cancel_failed": "中断任务失败，请稍后重试。",
            "runner_task_not_found": "任务运行记录不存在。",
            "runner_task_status_failed": "任务状态刷新失败。",
            "pdf_export_failed": "PDF 导出失败。",
            "task_not_found": "任务不存在或无权访问。",
            "document_not_found": "文件不存在或无权访问。"
        ]
        return map[detail] ?? detail
    }
}

struct EmptyResponse: Decodable, Equatable {
    var ok: Bool?
    var deleted: Bool?
    var taskId: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case deleted
        case taskId = "task_id"
    }
}

struct UploadFilePayload: Equatable {
    var data: Data
    var filename: String
    var contentType: String
}

protocol LimiraServicing {
    var tokenStore: TokenStoring { get }

    func loadAuthOptions() async throws -> (OAuthConfig, OAuthConfig, [LimiraOrganization])
    func loadSession() async throws -> LimiraUser
    func signInPersonal(identifier: String, password: String) async throws -> LimiraUser
    func signInEnterprise(organizationId: String, identifier: String, password: String) async throws -> LimiraUser
    func signUp(username: String?, email: String, password: String, name: String?) async throws -> LimiraUser
    func verifyEmail(token: String) async throws -> LimiraUser
    func resendVerification(email: String) async throws
    func requestPasswordReset(email: String) async throws
    func confirmPasswordReset(token: String, password: String) async throws -> LimiraUser
    func signOut() async throws
    func loadScenarios() async throws -> [LimiraScenario]
    func loadTasks(archived: Bool, query: String?) async throws -> [LimiraTask]
    func loadTask(taskId: String) async throws -> LimiraTask
    func startResearch(query: String, scenario: String?, conversationId: String?, documentIds: [String]) async throws -> LimiraTask
    func cancelTask(taskId: String) async throws -> LimiraTask
    func archiveHistory(taskId: String) async throws -> LimiraTask
    func restoreHistory(taskId: String) async throws -> LimiraTask
    func deleteHistory(taskId: String) async throws
    func eventStream(taskId: String, lastEventId: String?) -> AsyncThrowingStream<LimiraStreamEvent, Error>
    func loadArtifacts(taskId: String) async throws -> ArtifactBuckets
    func loadEventLogs(taskId: String) async throws -> EventLogsResponse
    func loadReports(taskId: String) async throws -> [LimiraGeneratedReport]
    func loadUploads(taskId: String?) async throws -> UploadsResponse
    func loadCloudHistory() async throws -> UploadsResponse
    func loadStorage() async throws -> LimiraStoragePayload
    func uploadDocument(file: UploadFilePayload, taskId: String?) async throws -> LimiraUploadedDocument
    func searchUploads(query: String, taskId: String?) async throws -> [LimiraUploadedDocument]
    func download(relativeOrAbsolutePath: String, suggestedFilename: String) async throws -> DownloadedFile
    func exportPDF(taskId: String, reportId: String, markdown: String, evidenceRefs: [String]) async throws -> LimiraGeneratedReport
    func loadEnterpriseMembers() async throws -> [LimiraUser]
    func loadEnterpriseUsage(days: Int) async throws -> EnterpriseUsageResponse
    func createEnterpriseMember(username: String?, email: String?, password: String, name: String?, role: String) async throws -> LimiraUser
    func transcribeSpeech(file: UploadFilePayload, language: String?) async throws -> SpeechTranscriptionResponse
}

final class LimiraAPIClient: LimiraServicing {
    let baseURL: URL
    let session: URLSession
    let tokenStore: TokenStoring
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURL: URL = AppConfiguration.apiBaseURL(), session: URLSession = .shared, tokenStore: TokenStoring = KeychainTokenStore()) {
        self.baseURL = baseURL
        self.session = session
        self.tokenStore = tokenStore
        self.decoder = JSONDecoder()
        self.encoder = JSONEncoder()
    }

    func loadAuthOptions() async throws -> (OAuthConfig, OAuthConfig, [LimiraOrganization]) {
        async let google: OAuthConfig = getJSON("/api/limira/auth/google/config")
        async let wechat: OAuthConfig = getJSON("/api/limira/auth/wechat/config")
        async let organizations: OrganizationsResponse = getJSON("/api/limira/auth/organizations")
        return try await (google, wechat, organizations.organizations)
    }

    func loadSession() async throws -> LimiraUser {
        try await getJSON("/api/limira/auth/session")
    }

    func signInPersonal(identifier: String, password: String) async throws -> LimiraUser {
        let body = AuthSigninRequest(identifier: identifier, password: password)
        let user: LimiraUser = try await postJSON("/api/limira/auth/signin", body: body)
        persistToken(from: user)
        return user
    }

    func signInEnterprise(organizationId: String, identifier: String, password: String) async throws -> LimiraUser {
        let body = EnterpriseSigninRequest(organizationId: organizationId, identifier: identifier, password: password)
        let user: LimiraUser = try await postJSON("/api/limira/auth/enterprise/signin", body: body)
        persistToken(from: user)
        return user
    }

    func signUp(username: String?, email: String, password: String, name: String?) async throws -> LimiraUser {
        try await postJSON("/api/limira/auth/signup", body: SignupRequest(username: username?.nonEmpty, email: email, password: password, name: name?.nonEmpty))
    }

    func verifyEmail(token: String) async throws -> LimiraUser {
        let user: LimiraUser = try await postJSON("/api/limira/auth/verify-email", body: TokenRequest(token: token))
        persistToken(from: user)
        return user
    }

    func resendVerification(email: String) async throws {
        let _: EmptyResponse = try await postJSON("/api/limira/auth/resend-verification", body: EmailRequest(email: email))
    }

    func requestPasswordReset(email: String) async throws {
        let _: EmptyResponse = try await postJSON("/api/limira/auth/password-reset/request", body: EmailRequest(email: email))
    }

    func confirmPasswordReset(token: String, password: String) async throws -> LimiraUser {
        let user: LimiraUser = try await postJSON("/api/limira/auth/password-reset/confirm", body: PasswordResetConfirmRequest(token: token, password: password))
        persistToken(from: user)
        return user
    }

    func signOut() async throws {
        let _: EmptyResponse = try await postJSON("/api/limira/auth/signout", body: EmptyBody())
        tokenStore.token = nil
    }

    func loadScenarios() async throws -> [LimiraScenario] {
        let response: ScenariosResponse = try await getJSON("/api/limira/scenarios")
        return response.scenarios
    }

    func loadTasks(archived: Bool = false, query: String? = nil) async throws -> [LimiraTask] {
        var queryItems = [
            URLQueryItem(name: "limit", value: "30"),
            URLQueryItem(name: "archived", value: archived ? "true" : "false")
        ]
        if let query = query?.nonEmpty {
            queryItems.append(URLQueryItem(name: "query", value: query))
        }
        let response: TasksResponse = try await getJSON("/api/limira/tasks", queryItems: queryItems)
        return response.tasks
    }

    func loadTask(taskId: String) async throws -> LimiraTask {
        try await getJSON("/api/limira/tasks/\(taskId.urlPathEscaped)")
    }

    func startResearch(query: String, scenario: String?, conversationId: String?, documentIds: [String]) async throws -> LimiraTask {
        try await postJSON(
            "/api/limira/research",
            body: ResearchRequest(query: query, scenario: scenario?.nonEmpty, conversationId: conversationId?.nonEmpty, documentIds: documentIds)
        )
    }

    func cancelTask(taskId: String) async throws -> LimiraTask {
        try await postJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/cancel", body: EmptyBody())
    }

    func archiveHistory(taskId: String) async throws -> LimiraTask {
        try await postJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/history/archive", body: EmptyBody())
    }

    func restoreHistory(taskId: String) async throws -> LimiraTask {
        try await postJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/history/restore", body: EmptyBody())
    }

    func deleteHistory(taskId: String) async throws {
        let _: EmptyResponse = try await deleteJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/history")
    }

    func eventStream(taskId: String, lastEventId: String? = nil) -> AsyncThrowingStream<LimiraStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let queryItems = lastEventId?.nonEmpty.map {
                        [URLQueryItem(name: "last_event_id", value: $0)]
                    } ?? []
                    var request = try makeRequest(path: "/api/limira/tasks/\(taskId.urlPathEscaped)/events", method: "GET", queryItems: queryItems)
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    if let lastEventId = lastEventId?.nonEmpty {
                        request.setValue(lastEventId, forHTTPHeaderField: "Last-Event-ID")
                    }
                    let (bytes, response) = try await session.bytes(for: request)
                    try validateHTTP(response, data: Data())
                    var dataLines: [String] = []
                    var eventId: String?
                    for try await line in bytes.lines {
                        if Task.isCancelled { break }
                        if line.isEmpty {
                            emitSSE(dataLines: &dataLines, eventId: &eventId, continuation: continuation)
                        } else if line.hasPrefix("data:") {
                            dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
                        } else if line.hasPrefix("id:") {
                            eventId = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                        }
                    }
                    emitSSE(dataLines: &dataLines, eventId: &eventId, continuation: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    func loadArtifacts(taskId: String) async throws -> ArtifactBuckets {
        try await getJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/artifacts")
    }

    func loadEventLogs(taskId: String) async throws -> EventLogsResponse {
        try await getJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/event-logs")
    }

    func loadReports(taskId: String) async throws -> [LimiraGeneratedReport] {
        let response: ReportsResponse = try await getJSON("/api/limira/tasks/\(taskId.urlPathEscaped)/reports")
        return response.reports
    }

    func loadUploads(taskId: String?) async throws -> UploadsResponse {
        let queryItems = taskId?.nonEmpty.map { [URLQueryItem(name: "task_id", value: $0)] } ?? []
        return try await getJSON("/api/limira/uploads", queryItems: queryItems)
    }

    func loadCloudHistory() async throws -> UploadsResponse {
        try await getJSON("/api/limira/uploads/history")
    }

    func loadStorage() async throws -> LimiraStoragePayload {
        let response: StorageResponse = try await getJSON("/api/limira/uploads/storage")
        return response.storage
    }

    func uploadDocument(file: UploadFilePayload, taskId: String?) async throws -> LimiraUploadedDocument {
        var fields: [String: String] = [:]
        if let taskId = taskId?.nonEmpty {
            fields["task_id"] = taskId
        }
        let boundary = "Boundary-\(UUID().uuidString)"
        let body = Self.multipartBody(fields: fields, fileFieldName: "file", file: file, boundary: boundary)
        return try await requestJSON(
            "/api/limira/uploads",
            method: "POST",
            body: body,
            contentType: "multipart/form-data; boundary=\(boundary)"
        )
    }

    func searchUploads(query: String, taskId: String?) async throws -> [LimiraUploadedDocument] {
        var queryItems = [URLQueryItem(name: "query", value: query)]
        if let taskId = taskId?.nonEmpty {
            queryItems.append(URLQueryItem(name: "task_id", value: taskId))
        }
        let response: UploadSearchResponse = try await getJSON("/api/limira/uploads/search", queryItems: queryItems)
        return response.documents
    }

    func download(relativeOrAbsolutePath: String, suggestedFilename: String) async throws -> DownloadedFile {
        let (data, response) = try await requestData(relativeOrAbsolutePath, method: "GET", accept: "*/*")
        let filename = response.filenameFromContentDisposition ?? suggestedFilename
        let fileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension((filename as NSString).pathExtension)
        let finalURL = fileURL.deletingLastPathComponent().appendingPathComponent(filename)
        try data.write(to: finalURL, options: [.atomic])
        return DownloadedFile(url: finalURL, filename: filename, contentType: response.value(forHTTPHeaderField: "content-type"))
    }

    func exportPDF(taskId: String, reportId: String, markdown: String, evidenceRefs: [String]) async throws -> LimiraGeneratedReport {
        try await postJSON(
            "/api/limira/tasks/\(taskId.urlPathEscaped)/reports/pdf",
            body: ReportPDFRequest(reportId: reportId, reportType: "final", markdown: markdown, evidenceRefs: evidenceRefs)
        )
    }

    func loadEnterpriseMembers() async throws -> [LimiraUser] {
        let response: EnterpriseMembersResponse = try await getJSON("/api/limira/enterprise/members")
        return response.members
    }

    func loadEnterpriseUsage(days: Int = 30) async throws -> EnterpriseUsageResponse {
        try await getJSON("/api/limira/enterprise/usage", queryItems: [URLQueryItem(name: "days", value: String(days))])
    }

    func createEnterpriseMember(username: String?, email: String?, password: String, name: String?, role: String) async throws -> LimiraUser {
        struct Response: Decodable { var member: LimiraUser }
        let response: Response = try await postJSON(
            "/api/limira/enterprise/members",
            body: EnterpriseMemberCreateRequest(username: username?.nonEmpty, email: email?.nonEmpty, password: password, name: name?.nonEmpty, organizationRole: role)
        )
        return response.member
    }

    func transcribeSpeech(file: UploadFilePayload, language: String?) async throws -> SpeechTranscriptionResponse {
        var fields: [String: String] = [:]
        if let language = language?.nonEmpty {
            fields["language"] = language
        }
        let boundary = "Boundary-\(UUID().uuidString)"
        let body = Self.multipartBody(fields: fields, fileFieldName: "file", file: file, boundary: boundary)
        return try await requestJSON(
            "/api/limira/speech/transcribe",
            method: "POST",
            body: body,
            contentType: "multipart/form-data; boundary=\(boundary)"
        )
    }

    func makeURL(path: String, queryItems: [URLQueryItem] = []) throws -> URL {
        if let absolute = URL(string: path), absolute.scheme != nil {
            return try append(queryItems: queryItems, to: absolute)
        }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw LimiraAPIError.invalidURL(path)
        }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let childPath = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = "/" + [basePath, childPath].filter { !$0.isEmpty }.joined(separator: "/")
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        guard let url = components.url else {
            throw LimiraAPIError.invalidURL(path)
        }
        return url
    }

    static func multipartBody(fields: [String: String], fileFieldName: String, file: UploadFilePayload, boundary: String) -> Data {
        var data = Data()
        for (name, value) in fields {
            data.appendString("--\(boundary)\r\n")
            data.appendString("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            data.appendString("\(value)\r\n")
        }
        data.appendString("--\(boundary)\r\n")
        data.appendString("Content-Disposition: form-data; name=\"\(fileFieldName)\"; filename=\"\(file.filename)\"\r\n")
        data.appendString("Content-Type: \(file.contentType)\r\n\r\n")
        data.append(file.data)
        data.appendString("\r\n--\(boundary)--\r\n")
        return data
    }

    private func getJSON<T: Decodable>(_ path: String, queryItems: [URLQueryItem] = []) async throws -> T {
        try await requestJSON(path, method: "GET", queryItems: queryItems)
    }

    private func postJSON<T: Decodable, Body: Encodable>(_ path: String, body: Body) async throws -> T {
        let data = try encoder.encode(body)
        return try await requestJSON(path, method: "POST", body: data, contentType: "application/json")
    }

    private func deleteJSON<T: Decodable>(_ path: String) async throws -> T {
        try await requestJSON(path, method: "DELETE")
    }

    private func requestJSON<T: Decodable>(_ path: String, method: String, queryItems: [URLQueryItem] = [], body: Data? = nil, contentType: String? = nil) async throws -> T {
        let (data, _) = try await requestData(path, method: method, queryItems: queryItems, body: body, contentType: contentType, accept: "application/json")
        if data.isEmpty, T.self == EmptyResponse.self {
            return EmptyResponse() as! T
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw LimiraAPIError.decoding(error.localizedDescription)
        }
    }

    private func requestData(_ path: String, method: String, queryItems: [URLQueryItem] = [], body: Data? = nil, contentType: String? = nil, accept: String) async throws -> (Data, HTTPURLResponse) {
        var request = try makeRequest(path: path, method: method, queryItems: queryItems)
        request.httpBody = body
        request.setValue(accept, forHTTPHeaderField: "Accept")
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        do {
            let (data, response) = try await session.data(for: request)
            let http = try validateHTTP(response, data: data)
            return (data, http)
        } catch let error as LimiraAPIError {
            throw error
        } catch {
            throw LimiraAPIError.transport(error.localizedDescription)
        }
    }

    private func makeRequest(path: String, method: String, queryItems: [URLQueryItem] = []) throws -> URLRequest {
        let url = try makeURL(path: path, queryItems: queryItems)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 90
        if let token = tokenStore.token?.nonEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    @discardableResult
    private func validateHTTP(_ response: URLResponse, data: Data) throws -> HTTPURLResponse {
        guard let http = response as? HTTPURLResponse else {
            throw LimiraAPIError.emptyResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let detail = Self.responseDetail(from: data) ?? "HTTP \(http.statusCode)"
            throw LimiraAPIError.httpStatus(http.statusCode, detail)
        }
        return http
    }

    private static func responseDetail(from data: Data) -> String? {
        guard !data.isEmpty else { return nil }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let detail = object["detail"] as? String { return detail }
            if let error = object["error"] as? String { return error }
            if let detail = object["detail"] { return String(describing: detail) }
        }
        return String(data: data, encoding: .utf8)?.nonEmpty
    }

    private func persistToken(from user: LimiraUser) {
        if let token = user.token?.nonEmpty {
            tokenStore.token = token
        }
    }

    private func append(queryItems: [URLQueryItem], to url: URL) throws -> URL {
        guard !queryItems.isEmpty else { return url }
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw LimiraAPIError.invalidURL(url.absoluteString)
        }
        components.queryItems = (components.queryItems ?? []) + queryItems
        guard let result = components.url else {
            throw LimiraAPIError.invalidURL(url.absoluteString)
        }
        return result
    }

    private func emitSSE(dataLines: inout [String], eventId: inout String?, continuation: AsyncThrowingStream<LimiraStreamEvent, Error>.Continuation) {
        guard !dataLines.isEmpty else { return }
        let data = dataLines.joined(separator: "\n")
        dataLines.removeAll()
        do {
            var event = try SSEParser.parseData(data)
            event.streamEventId = eventId
            eventId = nil
            continuation.yield(event)
        } catch {
            var event = LimiraStreamEvent(event: "message", message: data)
            event.streamEventId = eventId
            eventId = nil
            continuation.yield(event)
        }
    }
}

enum SSEParser {
    static func parse(_ text: String) -> [LimiraStreamEvent] {
        var events: [LimiraStreamEvent] = []
        var dataLines: [String] = []
        for line in text.components(separatedBy: .newlines) {
            if line.isEmpty {
                if let event = try? parseData(dataLines.joined(separator: "\n")) {
                    events.append(event)
                }
                dataLines.removeAll()
            } else if line.hasPrefix("data:") {
                dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
            }
        }
        if !dataLines.isEmpty, let event = try? parseData(dataLines.joined(separator: "\n")) {
            events.append(event)
        }
        return events
    }

    static func parseData(_ data: String) throws -> LimiraStreamEvent {
        guard let jsonData = data.data(using: .utf8) else {
            return LimiraStreamEvent(event: "message", message: data)
        }
        do {
            return try JSONDecoder().decode(LimiraStreamEvent.self, from: jsonData)
        } catch {
            return LimiraStreamEvent(event: "message", message: data)
        }
    }
}

private struct EmptyBody: Encodable {}

private struct EmailRequest: Encodable {
    var email: String
}

private struct TokenRequest: Encodable {
    var token: String
}

private struct PasswordResetConfirmRequest: Encodable {
    var token: String
    var password: String
}

private struct SignupRequest: Encodable {
    var username: String?
    var email: String
    var password: String
    var name: String?
}

private struct AuthSigninRequest: Encodable {
    var username: String?
    var email: String?
    var password: String

    init(identifier: String, password: String) {
        if identifier.contains("@") {
            self.email = identifier
            self.username = nil
        } else {
            self.username = identifier
            self.email = nil
        }
        self.password = password
    }
}

private struct EnterpriseSigninRequest: Encodable {
    var organizationId: String
    var username: String?
    var email: String?
    var password: String

    enum CodingKeys: String, CodingKey {
        case organizationId = "organization_id"
        case username
        case email
        case password
    }

    init(organizationId: String, identifier: String, password: String) {
        self.organizationId = organizationId
        if identifier.contains("@") {
            self.email = identifier
            self.username = nil
        } else {
            self.username = identifier
            self.email = nil
        }
        self.password = password
    }
}

private struct ResearchRequest: Encodable {
    var query: String
    var scenario: String?
    var conversationId: String?
    var documentIds: [String]

    enum CodingKeys: String, CodingKey {
        case query
        case scenario
        case conversationId = "conversation_id"
        case documentIds = "document_ids"
    }
}

private struct ReportPDFRequest: Encodable {
    var reportId: String
    var reportType: String
    var markdown: String
    var evidenceRefs: [String]

    enum CodingKeys: String, CodingKey {
        case reportId = "report_id"
        case reportType = "report_type"
        case markdown
        case evidenceRefs = "evidence_refs"
    }
}

private struct EnterpriseMemberCreateRequest: Encodable {
    var username: String?
    var email: String?
    var password: String
    var name: String?
    var organizationRole: String

    enum CodingKeys: String, CodingKey {
        case username
        case email
        case password
        case name
        case organizationRole = "organization_role"
    }
}

private extension String {
    var urlPathEscaped: String {
        addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? self
    }
}

private extension Data {
    mutating func appendString(_ string: String) {
        append(Data(string.utf8))
    }
}

private extension HTTPURLResponse {
    var filenameFromContentDisposition: String? {
        guard let header = value(forHTTPHeaderField: "content-disposition") else {
            return nil
        }
        for part in header.components(separatedBy: ";") {
            let trimmed = part.trimmingCharacters(in: .whitespaces)
            if trimmed.lowercased().hasPrefix("filename=") {
                return trimmed.dropFirst("filename=".count).trimmingCharacters(in: CharacterSet(charactersIn: "\"")).nonEmpty
            }
        }
        return nil
    }
}
