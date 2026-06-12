package com.limira.android.data

import com.squareup.moshi.Json

data class LimiraUser(
    val id: String,
    val email: String? = null,
    val username: String? = null,
    val name: String? = null,
    val role: String = "user",
    @Json(name = "email_verified") val emailVerified: Boolean = false,
    @Json(name = "account_type") val accountType: String = "personal",
    @Json(name = "organization_id") val organizationId: String? = null,
    @Json(name = "organization_role") val organizationRole: String? = null,
    @Json(name = "daily_research_limit") val dailyResearchLimit: Int? = null,
    val organization: LimiraOrganization? = null,
    val token: String? = null,
    @Json(name = "token_type") val tokenType: String? = null,
) {
    val isEnterpriseAdmin: Boolean
        get() = accountType == "enterprise" && organizationRole == "admin"
}

data class LimiraOrganization(
    val id: String,
    val name: String,
    val slug: String? = null,
    val category: String? = null,
    @Json(name = "category_label") val categoryLabel: String? = null,
    @Json(name = "billing_mode") val billingMode: String? = null,
)

data class OrganizationListResponse(
    val organizations: List<LimiraOrganization> = emptyList(),
    val count: Int = 0,
)

data class AuthOptions(
    val enabled: Boolean = false,
)

data class EnterpriseSignInRequest(
    @Json(name = "organization_id") val organizationId: String,
    val username: String? = null,
    val email: String? = null,
    val password: String,
)

data class PersonalSignInRequest(
    val username: String? = null,
    val email: String? = null,
    val password: String,
)

data class SignupRequest(
    val username: String? = null,
    val email: String,
    val password: String,
    val name: String? = null,
)

data class PasswordResetRequest(val email: String)
data class PasswordResetConfirmRequest(val token: String, val password: String)
data class VerifyEmailRequest(val token: String)
data class ResendVerificationRequest(val email: String)

data class MobileOAuthExchangeRequest(
    val provider: String,
    val code: String,
    val state: String,
)

data class Scenario(
    val id: String,
    val title: String,
    val description: String? = null,
    @Json(name = "default_query") val defaultQuery: String? = null,
    @Json(name = "focus_areas") val focusAreas: List<String> = emptyList(),
)

data class ScenariosResponse(
    val scenarios: List<Scenario> = emptyList(),
    val count: Int = 0,
)

data class ResearchRequest(
    val query: String,
    val scenario: String? = null,
    @Json(name = "conversation_id") val conversationId: String? = null,
    @Json(name = "document_ids") val documentIds: List<String> = emptyList(),
)

data class ResearchTask(
    @Json(name = "task_id") val taskId: String,
    @Json(name = "conversation_id") val conversationId: String? = null,
    val query: String? = null,
    val status: String? = null,
    @Json(name = "archive_status") val archiveStatus: String? = null,
    @Json(name = "history_archived") val historyArchived: Boolean = false,
    val scenario: String? = null,
    val error: String? = null,
    @Json(name = "download_url") val downloadUrl: String? = null,
    @Json(name = "events_url") val eventsUrl: String? = null,
    @Json(name = "artifacts_url") val artifactsUrl: String? = null,
    @Json(name = "conversation_members") val conversationMembers: List<ResearchTask> = emptyList(),
    @Json(name = "conversation_count") val conversationCount: Int? = null,
    @Json(name = "uploaded_documents") val uploadedDocuments: List<UploadedDocument> = emptyList(),
    @Json(name = "model_summary") val modelSummary: Map<String, Any?> = emptyMap(),
)

data class TaskListResponse(
    val tasks: List<ResearchTask> = emptyList(),
    val count: Int = 0,
    val archived: Boolean = false,
    val query: String? = null,
)

data class EventLogsResponse(
    @Json(name = "task_id") val taskId: String? = null,
    val count: Int = 0,
    val events: List<Map<String, Any?>> = emptyList(),
    @Json(name = "admin_view") val adminView: Boolean = false,
)

data class ArtifactBuckets(
    val evidence: List<Map<String, Any?>> = emptyList(),
    val entities: List<Map<String, Any?>> = emptyList(),
    val relations: List<Map<String, Any?>> = emptyList(),
    @Json(name = "timeline_events") val timelineEvents: List<Map<String, Any?>> = emptyList(),
    @Json(name = "map_features") val mapFeatures: List<Map<String, Any?>> = emptyList(),
    val verifications: List<Map<String, Any?>> = emptyList(),
    @Json(name = "report_sections") val reportSections: List<Map<String, Any?>> = emptyList(),
)

data class GeneratedReport(
    @Json(name = "report_id") val reportId: String,
    @Json(name = "task_id") val taskId: String,
    @Json(name = "report_type") val reportType: String? = null,
    @Json(name = "evidence_refs") val evidenceRefs: List<String> = emptyList(),
    @Json(name = "markdown_chars") val markdownChars: Int = 0,
    @Json(name = "html_chars") val htmlChars: Int = 0,
    @Json(name = "pdf_size_bytes") val pdfSizeBytes: Long? = null,
    @Json(name = "pdf_sha256") val pdfSha256: String? = null,
    @Json(name = "pdf_url") val pdfUrl: String? = null,
)

data class ReportsResponse(
    val reports: List<GeneratedReport> = emptyList(),
    val count: Int = 0,
)

data class ReportPdfRequest(
    @Json(name = "report_id") val reportId: String? = null,
    @Json(name = "report_type") val reportType: String = "final",
    val markdown: String,
    @Json(name = "evidence_refs") val evidenceRefs: List<String> = emptyList(),
)

data class UploadedDocument(
    @Json(name = "document_id") val documentId: String,
    @Json(name = "task_id") val taskId: String? = null,
    val filename: String? = null,
    @Json(name = "content_type") val contentType: String? = null,
    @Json(name = "byte_size") val byteSize: Long = 0,
    val language: String? = null,
    @Json(name = "extracted_text_chars") val extractedTextChars: Int = 0,
    @Json(name = "download_url") val downloadUrl: String? = null,
    val score: Double? = null,
    val snippet: String? = null,
    @Json(name = "matched_terms") val matchedTerms: List<String> = emptyList(),
)

data class UploadStorage(
    @Json(name = "used_bytes") val usedBytes: Long = 0,
    @Json(name = "quota_bytes") val quotaBytes: Long? = null,
    @Json(name = "remaining_bytes") val remainingBytes: Long? = null,
)

data class UploadsResponse(
    val documents: List<UploadedDocument> = emptyList(),
    val storage: UploadStorage? = null,
)

data class UploadSearchResponse(
    val query: String? = null,
    @Json(name = "task_id") val taskId: String? = null,
    val documents: List<UploadedDocument> = emptyList(),
)

data class StorageResponse(val storage: UploadStorage? = null)

data class EnterpriseMembersResponse(
    @Json(name = "organization_id") val organizationId: String? = null,
    val members: List<LimiraUser> = emptyList(),
    val count: Int = 0,
)

data class EnterpriseMemberCreateRequest(
    val username: String? = null,
    val email: String? = null,
    val password: String,
    val name: String? = null,
    @Json(name = "organization_role") val organizationRole: String = "member",
)

data class EnterpriseMemberCreateResponse(
    val member: LimiraUser? = null,
    @Json(name = "organization_id") val organizationId: String? = null,
)

data class EnterpriseUsageResponse(
    val organization: LimiraOrganization? = null,
    val usage: Map<String, Any?> = emptyMap(),
)

data class OrganizationCreateRequest(val name: String, val slug: String? = null)
data class OrganizationCreateResponse(
    val organization: LimiraOrganization? = null,
    val admin: String? = null,
)

data class SpeechTranscriptResponse(
    val text: String = "",
    val language: String? = null,
    @Json(name = "duration_seconds") val durationSeconds: Double? = null,
    @Json(name = "content_type") val contentType: String? = null,
    val filename: String? = null,
)

data class OkResponse(val ok: Boolean = true)
