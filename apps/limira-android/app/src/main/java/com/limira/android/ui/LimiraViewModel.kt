package com.limira.android.ui

import android.content.ContentResolver
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.limira.android.BuildConfig
import com.limira.android.data.ArtifactBuckets
import com.limira.android.data.GeneratedReport
import com.limira.android.data.LimiraOrganization
import com.limira.android.data.LimiraRepository
import com.limira.android.data.LimiraUser
import com.limira.android.data.ResendVerificationRequest
import com.limira.android.data.Scenario
import com.limira.android.data.UploadStorage
import com.limira.android.data.UploadedDocument
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale
import javax.inject.Inject

private val terminalStatuses = setOf("completed", "failed", "cancelled")
private val artifactEventTypes = setOf(
    "evidence_collected",
    "entity_extracted",
    "relation_extracted",
    "timeline_event_added",
    "map_feature_added",
    "verification_result",
    "report_section_generated",
    "record_research_artifact",
)

data class LimiraUiState(
    val loading: Boolean = true,
    val busy: Boolean = false,
    val user: LimiraUser? = null,
    val organizations: List<LimiraOrganization> = emptyList(),
    val selectedOrganizationId: String = BuildConfig.LIMIRA_DEFAULT_ORGANIZATION_ID,
    val enterpriseUsername: String = BuildConfig.LIMIRA_DEFAULT_ENTERPRISE_USERNAME,
    val personalLogin: String = "",
    val password: String = "",
    val signupEmail: String = "",
    val signupUsername: String = "",
    val signupName: String = "",
    val resetToken: String = "",
    val authMode: AuthMode = AuthMode.Enterprise,
    val googleEnabled: Boolean = false,
    val wechatEnabled: Boolean = false,
    val message: String = "",
    val scenarios: List<Scenario> = emptyList(),
    val selectedScenarioId: String? = null,
    val query: String = "",
    val tasks: List<com.limira.android.data.ResearchTask> = emptyList(),
    val archivedTasks: List<com.limira.android.data.ResearchTask> = emptyList(),
    val activeTask: com.limira.android.data.ResearchTask? = null,
    val activeTab: ArtifactTab = ArtifactTab.Evidence,
    val artifacts: ArtifactBuckets = ArtifactBuckets(),
    val reports: List<GeneratedReport> = emptyList(),
    val uploads: List<UploadedDocument> = emptyList(),
    val uploadResults: List<UploadedDocument> = emptyList(),
    val uploadStorage: UploadStorage? = null,
    val uploadSearch: String = "",
    val streamMessages: List<String> = emptyList(),
    val enterpriseMembers: List<LimiraUser> = emptyList(),
    val enterpriseUsage: Map<String, Any?> = emptyMap(),
    val taskLogs: List<Map<String, Any?>> = emptyList(),
)

enum class AuthMode { Enterprise, Personal, Signup, Reset }
enum class ArtifactTab(val label: String) {
    Evidence("证据"),
    Entities("实体"),
    Graph("图谱"),
    Timeline("时间线"),
    Map("地图"),
    Report("报告"),
    Logs("日志"),
}

@HiltViewModel
class LimiraViewModel @Inject constructor(
    private val repository: LimiraRepository,
) : ViewModel() {
    private val _state = MutableStateFlow(LimiraUiState(user = repository.cachedUser))
    val state: StateFlow<LimiraUiState> = _state.asStateFlow()

    private var streamJob: Job? = null

    init {
        refreshBoot()
    }

    fun refreshBoot() = launch {
        val organizations = repository.organizations()
        val options = runCatching { repository.authOptions() }.getOrDefault(false to false)
        val session = repository.bootSession()
        val selectedOrganization = organizations.firstOrNull { it.id == BuildConfig.LIMIRA_DEFAULT_ORGANIZATION_ID }
            ?: organizations.firstOrNull()
        _state.update {
            it.copy(
                loading = false,
                user = session ?: it.user,
                organizations = organizations,
                selectedOrganizationId = selectedOrganization?.id ?: it.selectedOrganizationId,
                googleEnabled = options.first,
                wechatEnabled = options.second,
            )
        }
        if (session != null) loadWorkspace()
    }

    fun setAuthMode(mode: AuthMode) = _state.update { it.copy(authMode = mode, message = "") }
    fun setEnterpriseUsername(value: String) = _state.update { it.copy(enterpriseUsername = value) }
    fun setPersonalLogin(value: String) = _state.update { it.copy(personalLogin = value) }
    fun setPassword(value: String) = _state.update { it.copy(password = value) }
    fun setOrganization(id: String) = _state.update { it.copy(selectedOrganizationId = id) }
    fun setSignupEmail(value: String) = _state.update { it.copy(signupEmail = value) }
    fun setSignupUsername(value: String) = _state.update { it.copy(signupUsername = value) }
    fun setSignupName(value: String) = _state.update { it.copy(signupName = value) }
    fun setResetToken(value: String) = _state.update { it.copy(resetToken = value) }
    fun setQuery(value: String) = _state.update { it.copy(query = value) }
    fun setUploadSearch(value: String) = _state.update { it.copy(uploadSearch = value) }
    fun setTab(tab: ArtifactTab) = _state.update { it.copy(activeTab = tab) }
    fun selectScenario(id: String) = _state.update { it.copy(selectedScenarioId = id) }

    fun useScenarioDefault() {
        val current = _state.value
        val scenario = current.scenarios.firstOrNull { it.id == current.selectedScenarioId }
        if (scenario?.defaultQuery != null) {
            _state.update { it.copy(query = scenario.defaultQuery) }
        }
    }

    fun enterpriseLogin() = launch {
        val current = _state.value
        require(current.password.isNotBlank()) { "请输入密码" }
        val user = repository.enterpriseSignIn(
            organizationId = current.selectedOrganizationId,
            username = current.enterpriseUsername.trim(),
            password = current.password,
        )
        _state.update { it.copy(user = user, password = "", message = "企业管理员已登录") }
        loadWorkspace()
    }

    fun personalLogin() = launch {
        val current = _state.value
        require(current.personalLogin.isNotBlank() && current.password.isNotBlank()) { "请输入账号和密码" }
        val user = repository.personalSignIn(current.personalLogin, current.password)
        _state.update { it.copy(user = user, password = "", message = "已登录") }
        loadWorkspace()
    }

    fun signup() = launch {
        val current = _state.value
        repository.signup(
            username = current.signupUsername.takeIf { it.isNotBlank() },
            email = current.signupEmail,
            password = current.password,
            name = current.signupName.takeIf { it.isNotBlank() },
        )
        _state.update { it.copy(password = "", message = "注册成功，请完成邮箱验证后登录") }
    }

    fun requestPasswordReset() = launch {
        repository.runCatchingApi { api ->
            api.requestPasswordReset(com.limira.android.data.PasswordResetRequest(_state.value.personalLogin))
        }
        _state.update { it.copy(message = "如果账号存在，重置邮件已经发送") }
    }

    fun confirmPasswordReset() = launch {
        val current = _state.value
        val user = repository.runCatchingApi { api ->
            api.confirmPasswordReset(
                com.limira.android.data.PasswordResetConfirmRequest(current.resetToken, current.password),
            )
        }
        _state.update { it.copy(user = user, password = "", resetToken = "", message = "密码已重置") }
        loadWorkspace()
    }

    fun resendVerification() = launch {
        repository.runCatchingApi { api ->
            api.resendVerification(ResendVerificationRequest(_state.value.signupEmail))
        }
        _state.update { it.copy(message = "如果需要验证，邮件已经重新发送") }
    }

    fun mobileOAuthStartUrl(provider: String): String = repository.mobileOAuthStartUrl(provider)

    fun handleDeepLink(uri: Uri) {
        val provider = uri.getQueryParameter("provider")
        val code = uri.getQueryParameter("code")
        val oauthState = uri.getQueryParameter("state")
        val error = uri.getQueryParameter("auth_error")
        if (error != null) {
            _state.update { it.copy(message = "OAuth 登录失败：$error") }
            return
        }
        if (provider.isNullOrBlank() || code.isNullOrBlank() || oauthState.isNullOrBlank()) {
            return
        }
        launch {
            val user = repository.exchangeMobileOAuth(provider, code, oauthState)
            _state.update { it.copy(user = user, message = "${provider.uppercase()} 登录成功") }
            loadWorkspace()
        }
    }

    fun signOut() = launch {
        streamJob?.cancel()
        repository.signOut()
        _state.update { LimiraUiState(loading = false, organizations = it.organizations) }
    }

    fun loadWorkspace() = launch {
        val scenarios = repository.scenarios()
        val tasks = repository.tasks()
        val archived = repository.tasks(archived = true)
        val uploads = runCatching { repository.uploads() }.getOrNull()
        val members = runCatching { repository.enterpriseMembers() }.getOrDefault(emptyList())
        val usage = runCatching { repository.enterpriseUsage() }.getOrDefault(emptyMap())
        _state.update {
            it.copy(
                scenarios = scenarios,
                selectedScenarioId = it.selectedScenarioId ?: scenarios.firstOrNull()?.id,
                tasks = tasks,
                archivedTasks = archived,
                uploads = uploads?.documents ?: it.uploads,
                uploadStorage = uploads?.storage ?: it.uploadStorage,
                enterpriseMembers = members,
                enterpriseUsage = usage,
            )
        }
    }

    fun openTask(taskId: String) = launch {
        val task = repository.task(taskId)
        _state.update { it.copy(activeTask = task, message = "已打开任务 $taskId") }
        loadTaskDetail(taskId)
        if (!terminalStatuses.contains(task.status)) connectStream(taskId)
    }

    fun submitResearch() = launch {
        val current = _state.value
        require(current.query.isNotBlank()) { "请输入研究问题" }
        val selectedDocuments = current.uploadResults.ifEmpty { current.uploads }
            .take(20)
            .map { it.documentId }
        val task = repository.createResearch(
            query = current.query.trim(),
            scenario = current.selectedScenarioId,
            conversationId = current.activeTask?.conversationId,
            documentIds = selectedDocuments,
        )
        _state.update {
            it.copy(
                activeTask = task,
                query = "",
                streamMessages = listOf("研究任务已创建：${task.taskId}"),
                artifacts = ArtifactBuckets(),
                reports = emptyList(),
                taskLogs = emptyList(),
            )
        }
        loadWorkspace()
        connectStream(task.taskId)
    }

    fun archiveActiveHistory() = activeTaskAction { repository.archiveHistory(it) }
    fun restoreActiveHistory() = activeTaskAction { repository.restoreHistory(it) }
    fun deleteActiveHistory() = activeTaskAction {
        repository.deleteHistory(it)
        null
    }

    fun loadTaskDetail(taskId: String = _state.value.activeTask?.taskId.orEmpty()) = launch {
        if (taskId.isBlank()) return@launch
        val artifacts = repository.artifacts(taskId)
        val reports = repository.reports(taskId)
        val logs = runCatching { repository.taskEventLogs(taskId) }.getOrDefault(emptyList())
        _state.update { it.copy(artifacts = artifacts, reports = reports, taskLogs = logs) }
    }

    fun searchUploads() = launch {
        val query = _state.value.uploadSearch.trim()
        if (query.isBlank()) {
            _state.update { it.copy(uploadResults = emptyList()) }
            return@launch
        }
        val results = repository.searchUploads(query, _state.value.activeTask?.taskId)
        _state.update { it.copy(uploadResults = results) }
    }

    fun refreshUploads() = launch {
        val uploads = repository.uploads(_state.value.activeTask?.taskId)
        _state.update { it.copy(uploads = uploads.documents, uploadStorage = uploads.storage, uploadResults = emptyList()) }
    }

    fun uploadDocument(resolver: ContentResolver, uri: Uri, displayName: String) = launch {
        val uploaded = repository.uploadDocument(resolver, uri, displayName, _state.value.activeTask?.taskId)
        _state.update {
            it.copy(
                uploads = listOf(uploaded) + it.uploads.filterNot { doc -> doc.documentId == uploaded.documentId },
                message = "上传完成：${uploaded.filename ?: uploaded.documentId}",
            )
        }
        refreshUploads()
    }

    fun transcribeSpeech(resolver: ContentResolver, uri: Uri, displayName: String) = launch {
        val transcript = repository.transcribeSpeech(resolver, uri, displayName, null)
        _state.update {
            it.copy(
                query = if (it.query.isBlank()) transcript.text else it.query + "\n" + transcript.text,
                message = "语音转写完成",
            )
        }
    }

    fun exportPdfAndDownload(targetUri: Uri?, resolver: ContentResolver) = launch {
        val taskId = _state.value.activeTask?.taskId ?: error("没有打开任务")
        val markdown = reportMarkdown(_state.value.artifacts)
        require(markdown.isNotBlank()) { "没有可导出的报告内容" }
        val report = repository.exportPdf(taskId, markdown, evidenceRefs(_state.value.artifacts))
        _state.update { it.copy(reports = listOf(report) + it.reports, message = "PDF 已生成") }
        val pdfUrl = report.pdfUrl
        if (targetUri != null && pdfUrl != null) downloadInto(resolver, targetUri, pdfUrl)
    }

    fun downloadInto(resolver: ContentResolver, targetUri: Uri, pathOrUrl: String) = launch {
        withContext(Dispatchers.IO) {
            val (_, body) = repository.download(pathOrUrl)
            body.use { responseBody ->
                resolver.openOutputStream(targetUri)?.use { output ->
                    responseBody.byteStream().copyTo(output)
                } ?: error("无法写入文件")
            }
        }
        _state.update { it.copy(message = "下载完成") }
    }

    private fun connectStream(taskId: String) {
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            repository.taskEvents(taskId).collect { event ->
                val status = event.raw["status"]?.toString()
                    ?: event.payload["status"]?.toString()
                    ?: (event.payload["data"] as? Map<*, *>)?.get("status")?.toString()
                _state.update { current ->
                    val task = current.activeTask
                    current.copy(
                        activeTask = if (task?.taskId == taskId && status != null) {
                            task.copy(status = status)
                        } else {
                            task
                        },
                        streamMessages = (current.streamMessages + streamLine(event.type, event.payload)).takeLast(120),
                    )
                }
                if (artifactEventTypes.contains(event.type) || (status != null && terminalStatuses.contains(status))) {
                    loadTaskDetail(taskId)
                    loadWorkspace()
                }
            }
        }
    }

    private fun activeTaskAction(block: suspend (String) -> com.limira.android.data.ResearchTask?) = launch {
        val taskId = _state.value.activeTask?.taskId ?: return@launch
        val task = block(taskId)
        _state.update { it.copy(activeTask = task ?: it.activeTask, message = "任务历史已更新") }
        loadWorkspace()
    }

    private fun launch(block: suspend () -> Unit) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, message = "") }
            try {
                block()
            } catch (error: Throwable) {
                _state.update { it.copy(message = repository.errorMessage(error)) }
            } finally {
                _state.update { it.copy(busy = false, loading = false) }
            }
        }
    }

    private fun streamLine(type: String, payload: Map<String, Any?>): String {
        val label = when (type) {
            "heartbeat" -> "心跳"
            "tool_call" -> "工具调用"
            "error" -> "错误"
            "end_of_workflow" -> "工作流完成"
            else -> type
        }
        val summary = payload["message"] ?: payload["summary"] ?: payload["status"] ?: ""
        return listOf(label, summary.toString()).filter { it.isNotBlank() }.joinToString("：")
    }
}

fun reportMarkdown(artifacts: ArtifactBuckets): String =
    artifacts.reportSections.mapIndexed { index, section ->
        val title = section.textValue("title", "name") ?: "报告 ${index + 1}"
        val body = section.textValue("markdown", "text", "summary", "description") ?: section.toString()
        "## $title\n\n$body"
    }.joinToString("\n\n")

fun evidenceRefs(artifacts: ArtifactBuckets): List<String> =
    artifacts.reportSections.flatMap { section ->
        (section["evidence_refs"] as? List<*>)?.mapNotNull { it?.toString() }.orEmpty()
    }.distinct()

fun Map<String, Any?>.textValue(vararg keys: String): String? =
    keys.firstNotNullOfOrNull { key -> this[key]?.toString()?.takeIf { it.isNotBlank() } }

fun formatBytes(value: Long): String {
    if (value < 1024) return "$value B"
    val units = listOf("KB", "MB", "GB", "TB")
    var amount = value / 1024.0
    var index = 0
    while (amount >= 1024 && index < units.lastIndex) {
        amount /= 1024
        index += 1
    }
    return String.format(Locale.US, "%.1f %s", amount, units[index])
}
