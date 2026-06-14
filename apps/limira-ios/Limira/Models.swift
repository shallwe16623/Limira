import CoreLocation
import Foundation

enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var stringValue: String? {
        switch self {
        case .string(let value):
            return value
        case .number(let value):
            return value.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(value)) : String(value)
        case .bool(let value):
            return value ? "true" : "false"
        default:
            return nil
        }
    }

    var doubleValue: Double? {
        switch self {
        case .number(let value):
            return value
        case .string(let value):
            return Double(value)
        default:
            return nil
        }
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    func string(_ keys: String...) -> String? {
        for key in keys {
            if let value = self[key]?.stringValue, !value.isEmpty {
                return value
            }
        }
        return nil
    }

    func object(_ keys: String...) -> [String: JSONValue]? {
        for key in keys {
            if let value = self[key]?.objectValue {
                return value
            }
        }
        return nil
    }
}

struct OAuthConfig: Decodable, Equatable {
    var enabled: Bool
}

struct LimiraOrganization: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var slug: String?
    var active: Bool?
    var category: String?
    var categoryLabel: String?
    var billingMode: String?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case slug
        case active
        case category
        case categoryLabel = "category_label"
        case billingMode = "billing_mode"
    }
}

struct OrganizationCategoryOption: Identifiable, Hashable {
    var value: String
    var label: String

    var id: String { value }
}

struct OrganizationsResponse: Decodable {
    var organizations: [LimiraOrganization]
    var count: Int?
}

struct LimiraUser: Codable, Identifiable, Equatable {
    var id: String
    var email: String?
    var username: String?
    var name: String?
    var role: String
    var emailVerified: Bool?
    var accountType: String?
    var organizationId: String?
    var organizationRole: String?
    var dailyResearchLimit: Int?
    var token: String?
    var tokenType: String?
    var organization: LimiraOrganization?

    enum CodingKeys: String, CodingKey {
        case id
        case email
        case username
        case name
        case role
        case emailVerified = "email_verified"
        case accountType = "account_type"
        case organizationId = "organization_id"
        case organizationRole = "organization_role"
        case dailyResearchLimit = "daily_research_limit"
        case token
        case tokenType = "token_type"
        case organization
    }

    var displayName: String {
        name?.nonEmpty ?? username?.nonEmpty ?? email?.nonEmpty ?? id
    }

    var isEnterpriseAdmin: Bool {
        accountType == "enterprise" && (organizationRole == "admin" || role == "admin")
    }
}

struct LimiraScenario: Codable, Identifiable, Hashable {
    var id: String
    var title: String
    var description: String
    var defaultQuery: String?
    var focusAreas: [String]?

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case description
        case defaultQuery = "default_query"
        case focusAreas = "focus_areas"
    }
}

struct ScenariosResponse: Decodable {
    var scenarios: [LimiraScenario]
    var count: Int?
}

struct LimiraTask: Codable, Identifiable, Hashable {
    var taskId: String
    var conversationId: String?
    var query: String
    var status: String
    var archiveStatus: String?
    var historyArchived: Bool?
    var scenario: String?
    var error: String?
    var modelSummary: [String: JSONValue]?
    var downloadUrl: String?
    var eventsUrl: String?
    var artifactsUrl: String?
    var conversationMembers: [LimiraTask]?
    var conversationCount: Int?
    var uploadedDocuments: [LimiraUploadedDocument]?

    var id: String { taskId }

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case conversationId = "conversation_id"
        case query
        case status
        case archiveStatus = "archive_status"
        case historyArchived = "history_archived"
        case scenario
        case error
        case modelSummary = "model_summary"
        case downloadUrl = "download_url"
        case eventsUrl = "events_url"
        case artifactsUrl = "artifacts_url"
        case conversationMembers = "conversation_members"
        case conversationCount = "conversation_count"
        case uploadedDocuments = "uploaded_documents"
    }
}

struct TasksResponse: Decodable {
    var tasks: [LimiraTask]
    var count: Int?
    var archived: Bool?
    var query: String?
}

struct LimiraUploadedDocument: Codable, Identifiable, Hashable {
    var documentId: String
    var taskId: String?
    var filename: String
    var contentType: String?
    var byteSize: Int?
    var language: String?
    var extractedTextChars: Int?
    var downloadUrl: String?
    var score: Double?
    var snippet: String?
    var matchedTerms: [String]?

    var id: String { documentId }

    enum CodingKeys: String, CodingKey {
        case documentId = "document_id"
        case taskId = "task_id"
        case filename
        case contentType = "content_type"
        case byteSize = "byte_size"
        case language
        case extractedTextChars = "extracted_text_chars"
        case downloadUrl = "download_url"
        case score
        case snippet
        case matchedTerms = "matched_terms"
    }
}

struct UploadsResponse: Decodable {
    var documents: [LimiraUploadedDocument]
    var storage: LimiraStoragePayload?
}

struct UploadSearchResponse: Decodable {
    var query: String
    var taskId: String?
    var documents: [LimiraUploadedDocument]

    enum CodingKeys: String, CodingKey {
        case query
        case taskId = "task_id"
        case documents
    }
}

struct LimiraStoragePayload: Codable, Equatable {
    var usedBytes: Int
    var quotaBytes: Int
    var remainingBytes: Int
    var usageRatio: Double

    enum CodingKeys: String, CodingKey {
        case usedBytes = "used_bytes"
        case quotaBytes = "quota_bytes"
        case remainingBytes = "remaining_bytes"
        case usageRatio = "usage_ratio"
    }
}

struct StorageResponse: Decodable {
    var storage: LimiraStoragePayload
}

struct LimiraGeneratedReport: Codable, Identifiable, Hashable {
    var reportId: String
    var taskId: String
    var reportType: String
    var evidenceRefs: [String]
    var markdownChars: Int?
    var htmlChars: Int?
    var pdfSizeBytes: Int?
    var pdfSha256: String?
    var pdfUrl: String?

    var id: String { reportId }

    enum CodingKeys: String, CodingKey {
        case reportId = "report_id"
        case taskId = "task_id"
        case reportType = "report_type"
        case evidenceRefs = "evidence_refs"
        case markdownChars = "markdown_chars"
        case htmlChars = "html_chars"
        case pdfSizeBytes = "pdf_size_bytes"
        case pdfSha256 = "pdf_sha256"
        case pdfUrl = "pdf_url"
    }
}

struct ReportsResponse: Decodable {
    var reports: [LimiraGeneratedReport]
    var count: Int?
}

struct ResearchArtifact: Codable, Identifiable, Hashable {
    var fields: [String: JSONValue]

    var id: String {
        fields.string("id", "artifact_id", "evidence_id", "entity_id", "relation_id", "event_id", "section_id", "ref")
            ?? String(fields.hashValue)
    }

    var title: String {
        fields.string("title", "name", "label", "summary", "section_title") ?? id
    }

    var subtitle: String {
        fields.string("source", "url", "confidence", "published_at", "type") ?? ""
    }

    var evidenceIdentifier: String {
        fields.string("evidence_id", "ref_id", "id", "ref") ?? id
    }

    var evidenceSummary: String {
        fields.string("summary", "key_findings", "snippet", "text", "description") ?? ""
    }

    var confidence: String? {
        fields.string("confidence")
    }

    var publishedAt: String? {
        fields.string("published_at", "publishedAt", "date")
    }

    var sourceURLString: String? {
        if let value = fields.string("url", "source_url", "sourceUrl", "link", "href") {
            return value
        }
        return fields["source_urls"]?.arrayValue?
            .compactMap { $0.stringValue?.nonEmpty }
            .first
    }

    var sourceURL: URL? {
        guard var value = sourceURLString?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else {
            return nil
        }
        if value.hasPrefix("www.") {
            value = "https://\(value)"
        }
        guard let url = URL(string: value),
              let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme) else {
            return nil
        }
        return url
    }

    init(fields: [String: JSONValue]) {
        self.fields = fields
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        fields = try container.decode([String: JSONValue].self)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(fields)
    }

    func coordinate() -> CLLocationCoordinate2D? {
        if let geometry = fields.object("geometry"),
           let type = geometry.string("type")?.lowercased(),
           type == "point",
           let coordinates = geometry["coordinates"]?.arrayValue,
           coordinates.count >= 2,
           let longitude = coordinates[0].doubleValue,
           let latitude = coordinates[1].doubleValue {
            return CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
        }
        if let longitude = fields["longitude"]?.doubleValue ?? fields["lon"]?.doubleValue,
           let latitude = fields["latitude"]?.doubleValue ?? fields["lat"]?.doubleValue {
            return CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
        }
        return nil
    }
}

struct ArtifactBuckets: Decodable, Equatable {
    var evidence: [ResearchArtifact] = []
    var entities: [ResearchArtifact] = []
    var relations: [ResearchArtifact] = []
    var timelineEvents: [ResearchArtifact] = []
    var mapFeatures: [ResearchArtifact] = []
    var verifications: [ResearchArtifact] = []
    var reportSections: [ResearchArtifact] = []

    enum CodingKeys: String, CodingKey {
        case evidence
        case entities
        case relations
        case timelineEvents = "timeline_events"
        case timeline
        case mapFeatures = "map_features"
        case verifications
        case reportSections = "report_sections"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        evidence = try container.decodeIfPresent([ResearchArtifact].self, forKey: .evidence) ?? []
        entities = try container.decodeIfPresent([ResearchArtifact].self, forKey: .entities) ?? []
        relations = try container.decodeIfPresent([ResearchArtifact].self, forKey: .relations) ?? []
        timelineEvents = try container.decodeIfPresent([ResearchArtifact].self, forKey: .timelineEvents)
            ?? container.decodeIfPresent([ResearchArtifact].self, forKey: .timeline)
            ?? []
        mapFeatures = try container.decodeIfPresent([ResearchArtifact].self, forKey: .mapFeatures) ?? []
        verifications = try container.decodeIfPresent([ResearchArtifact].self, forKey: .verifications) ?? []
        reportSections = try container.decodeIfPresent([ResearchArtifact].self, forKey: .reportSections) ?? []
    }
}

struct LimiraStreamEvent: Codable, Identifiable, Equatable {
    var id = UUID()
    var streamEventId: String?
    var event: String
    var type: String?
    var status: String?
    var message: String?
    var data: [String: JSONValue]?
    var payload: [String: JSONValue]?
    var raw: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case event
        case eventType = "event_type"
        case eventName = "event_name"
        case type
        case status
        case message
        case data
        case payload
    }

    init(event: String, type: String? = nil, status: String? = nil, message: String? = nil, data: [String: JSONValue]? = nil, payload: [String: JSONValue]? = nil, raw: [String: JSONValue] = [:]) {
        self.event = event
        self.type = type
        self.status = status
        self.message = message
        self.data = data
        self.payload = payload
        self.raw = raw
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        event = try container.decodeIfPresent(String.self, forKey: .event)
            ?? container.decodeIfPresent(String.self, forKey: .eventType)
            ?? container.decodeIfPresent(String.self, forKey: .eventName)
            ?? container.decodeIfPresent(String.self, forKey: .type)
            ?? "task_update"
        type = try container.decodeIfPresent(String.self, forKey: .type)
        status = try container.decodeIfPresent(String.self, forKey: .status)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        data = try container.decodeIfPresent([String: JSONValue].self, forKey: .data)
        payload = try container.decodeIfPresent([String: JSONValue].self, forKey: .payload)
        raw = try [String: JSONValue](from: decoder)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(event, forKey: .event)
        try container.encodeIfPresent(type, forKey: .type)
        try container.encodeIfPresent(status, forKey: .status)
        try container.encodeIfPresent(message, forKey: .message)
        try container.encodeIfPresent(data, forKey: .data)
        try container.encodeIfPresent(payload, forKey: .payload)
    }

    var publicStatus: String? {
        status
            ?? data?.string("status")
            ?? data?["data"]?.objectValue?.string("status")
            ?? payload?.string("status")
    }

    var displayText: String {
        message
            ?? data?.string("message", "summary")
            ?? payload?.string("message", "summary")
            ?? event
    }
}

struct TaskProgressStep: Identifiable, Equatable {
    var id = UUID()
    var kind: String
    var title: String
    var detail: String = ""
    var status: String = "active"
    var meta: String = ""

    var signature: String {
        [kind, title, detail, meta]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .joined(separator: "\u{1F}")
    }
}

struct EnterpriseMembersResponse: Decodable {
    var organizationId: String?
    var members: [LimiraUser]
    var count: Int?

    enum CodingKeys: String, CodingKey {
        case organizationId = "organization_id"
        case members
        case count
    }
}

struct EnterpriseUsageResponse: Decodable {
    var organization: LimiraOrganization?
    var usage: [String: JSONValue]
}

struct EventLogsResponse: Decodable {
    var taskId: String
    var count: Int?
    var events: [[String: JSONValue]]
    var adminView: Bool?

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case count
        case events
        case adminView = "admin_view"
    }
}

struct SpeechTranscriptionResponse: Decodable, Equatable {
    var text: String
    var language: String?
    var durationSeconds: Double?
    var contentType: String?
    var filename: String?

    enum CodingKeys: String, CodingKey {
        case text
        case language
        case durationSeconds = "duration_seconds"
        case contentType = "content_type"
        case filename
    }
}

struct DownloadedFile: Identifiable, Equatable {
    var id = UUID()
    var url: URL
    var filename: String
    var contentType: String?
}

struct AppMessage: Identifiable, Equatable {
    enum Role: String {
        case user
        case assistant
        case system
        case error
    }

    var id = UUID()
    var role: Role
    var text: String
    var taskId: String?
    var isReport = false
    var artifactCounts: [String: Int] = [:]
}

enum ArtifactTab: String, CaseIterable, Identifiable {
    case evidence = "证据"
    case entities = "实体"
    case graph = "图谱"
    case timeline = "时间线"
    case map = "地图"
    case report = "报告"

    var id: String { rawValue }
}

enum CompactWorkspaceRoute: String, CaseIterable, Identifiable {
    case workspace
    case cloudDrive
    case archivedChats
    case enterpriseAdmin

    var id: String { rawValue }
}

enum CompactShellDestination: String, CaseIterable, Hashable, Identifiable {
    case cloudDrive
    case archivedChats
    case enterpriseAdmin
    case artifacts

    var id: String { rawValue }

    var route: CompactWorkspaceRoute {
        switch self {
        case .cloudDrive:
            return .cloudDrive
        case .archivedChats:
            return .archivedChats
        case .enterpriseAdmin:
            return .enterpriseAdmin
        case .artifacts:
            return .workspace
        }
    }
}

enum CompactShellModal: String, Identifiable {
    case menu
    case historyFiles
    case historySearch
    case fileImporter

    var id: String { rawValue }
}

enum CompactShellReturnTarget: String, Equatable {
    case workspace
    case menu
}

struct CompactShellPresentation: Equatable {
    var path: [CompactShellDestination] = []
    var modal: CompactShellModal?
    var artifactTaskId: String?
    var returnTarget: CompactShellReturnTarget = .workspace

    var currentDestination: CompactShellDestination? {
        path.last
    }

    var route: CompactWorkspaceRoute {
        currentDestination?.route ?? .workspace
    }

    var isShowingArtifacts: Bool {
        currentDestination == .artifacts
    }

    mutating func present(_ modal: CompactShellModal) {
        self.modal = modal
    }

    mutating func dismissModal() {
        modal = nil
    }

    mutating func showDestination(_ destination: CompactShellDestination?, returnTarget: CompactShellReturnTarget = .workspace) {
        path = destination.map { [$0] } ?? []
        modal = nil
        self.returnTarget = destination == nil ? .workspace : returnTarget
        if destination != .artifacts {
            artifactTaskId = nil
        }
    }

    mutating func showArtifacts(taskId: String?) {
        path = [.artifacts]
        modal = nil
        artifactTaskId = taskId
        returnTarget = .workspace
    }

    mutating func resetToWorkspace() {
        path = []
        modal = nil
        artifactTaskId = nil
        returnTarget = .workspace
    }
}

enum AuthScope: String, CaseIterable, Identifiable {
    case personal = "个人"
    case enterprise = "企业"

    var id: String { rawValue }
}

extension String {
    var nonEmpty: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
