package com.limira.android.data

import android.content.ContentResolver
import android.net.Uri
import com.limira.android.BuildConfig
import com.squareup.moshi.Moshi
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.ResponseBody
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import okio.IOException
import retrofit2.HttpException
import java.net.URLEncoder
import javax.inject.Inject
import javax.inject.Singleton

data class StreamEvent(
    val type: String,
    val payload: Map<String, Any?> = emptyMap(),
    val raw: Map<String, Any?> = emptyMap(),
)

@Singleton
class LimiraRepository @Inject constructor(
    private val api: LimiraApi,
    private val authStore: AuthStore,
    private val okHttpClient: OkHttpClient,
    moshi: Moshi,
) {
    private val eventAdapter = moshi.adapter(Map::class.java)
    private val eventSourceFactory = EventSources.createFactory(okHttpClient)

    val cachedUser: LimiraUser?
        get() = authStore.user

    val hasToken: Boolean
        get() = !authStore.token.isNullOrBlank()

    suspend fun bootSession(): LimiraUser? {
        if (!hasToken) return null
        return runCatching {
            api.session().also { authStore.user = it }
        }.getOrElse {
            authStore.clear()
            null
        }
    }

    suspend fun organizations(): List<LimiraOrganization> = api.organizations().organizations

    suspend fun authOptions(): Pair<Boolean, Boolean> =
        api.googleConfig().enabled to api.wechatConfig().enabled

    suspend fun enterpriseSignIn(
        organizationId: String,
        username: String,
        password: String,
    ): LimiraUser = api.enterpriseSignIn(
        EnterpriseSignInRequest(
            organizationId = organizationId,
            username = username,
            password = password,
        ),
    ).also(authStore::saveAuth)

    suspend fun personalSignIn(
        usernameOrEmail: String,
        password: String,
    ): LimiraUser {
        val value = usernameOrEmail.trim()
        return api.personalSignIn(
            PersonalSignInRequest(
                username = value.takeUnless { it.contains("@") },
                email = value.takeIf { it.contains("@") },
                password = password,
            ),
        ).also(authStore::saveAuth)
    }

    suspend fun signup(username: String?, email: String, password: String, name: String?) =
        api.signup(SignupRequest(username = username, email = email, password = password, name = name))

    suspend fun <T> runCatchingApi(block: suspend (LimiraApi) -> T): T = block(api)

    suspend fun exchangeMobileOAuth(provider: String, code: String, state: String): LimiraUser =
        api.mobileOAuthExchange(MobileOAuthExchangeRequest(provider, code, state))
            .also(authStore::saveAuth)

    fun mobileOAuthStartUrl(provider: String): String {
        val encodedRedirect = URLEncoder.encode(
            BuildConfig.LIMIRA_MOBILE_REDIRECT_URI,
            Charsets.UTF_8.name(),
        )
        return absoluteUrl("auth/mobile/$provider/start?redirect_uri=$encodedRedirect")
    }

    suspend fun signOut() {
        runCatching { api.signOut() }
        authStore.clear()
    }

    suspend fun scenarios(): List<Scenario> = api.scenarios().scenarios

    suspend fun tasks(archived: Boolean = false, query: String? = null): List<ResearchTask> =
        api.tasks(archived = archived, query = query?.takeIf { it.isNotBlank() }).tasks

    suspend fun createResearch(
        query: String,
        scenario: String?,
        conversationId: String?,
        documentIds: List<String>,
    ): ResearchTask = api.createResearch(
        ResearchRequest(
            query = query,
            scenario = scenario,
            conversationId = conversationId,
            documentIds = documentIds,
        ),
    )

    suspend fun task(taskId: String) = api.task(taskId)
    suspend fun archiveHistory(taskId: String) = api.archiveTaskHistory(taskId)
    suspend fun restoreHistory(taskId: String) = api.restoreTaskHistory(taskId)
    suspend fun deleteHistory(taskId: String) = api.deleteTaskHistory(taskId)
    suspend fun artifacts(taskId: String) = api.artifacts(taskId)
    suspend fun reports(taskId: String) = api.reports(taskId).reports
    suspend fun taskEventLogs(taskId: String) = api.taskEventLogs(taskId).events
    suspend fun adminTaskEventLogs(taskId: String) = api.adminTaskEventLogs(taskId).events
    suspend fun enterpriseMembers() = api.enterpriseMembers().members
    suspend fun enterpriseUsage() = api.enterpriseUsage().usage
    suspend fun uploads(taskId: String? = null) = api.uploads(taskId)
    suspend fun uploadHistory() = api.uploadHistory()
    suspend fun uploadStorage() = api.uploadStorage().storage
    suspend fun searchUploads(query: String, taskId: String? = null) =
        api.searchUploads(query = query, taskId = taskId).documents

    suspend fun exportPdf(taskId: String, markdown: String, evidenceRefs: List<String>): GeneratedReport =
        api.exportPdf(
            taskId,
            ReportPdfRequest(
                reportId = "android-${System.currentTimeMillis()}",
                markdown = markdown,
                evidenceRefs = evidenceRefs,
            ),
        )

    suspend fun uploadDocument(
        resolver: ContentResolver,
        uri: Uri,
        displayName: String,
        taskId: String?,
    ): UploadedDocument {
        val bytes = resolver.openInputStream(uri)?.use { it.readBytes() }
            ?: error("无法读取文件")
        val mimeType = resolver.getType(uri) ?: "application/octet-stream"
        val body = bytes.toRequestBody(mimeType.toMediaTypeOrNull())
        val part = MultipartBody.Part.createFormData("file", displayName, body)
        val taskPart = taskId?.toRequestBody("text/plain".toMediaTypeOrNull())
        return api.uploadDocument(part, taskPart)
    }

    suspend fun transcribeSpeech(
        resolver: ContentResolver,
        uri: Uri,
        displayName: String,
        language: String?,
    ): SpeechTranscriptResponse {
        val bytes = resolver.openInputStream(uri)?.use { it.readBytes() }
            ?: error("无法读取音频")
        val mimeType = resolver.getType(uri) ?: "audio/webm"
        val body = bytes.toRequestBody(mimeType.toMediaTypeOrNull())
        val file = MultipartBody.Part.createFormData("file", displayName, body)
        val languagePart = language?.takeIf { it.isNotBlank() }
            ?.toRequestBody("text/plain".toMediaTypeOrNull())
        return api.transcribeSpeech(file, languagePart)
    }

    fun taskEvents(taskId: String): Flow<StreamEvent> = callbackFlow {
        val request = Request.Builder()
            .url(absoluteUrl("tasks/$taskId/events"))
            .header("Accept", "text/event-stream")
            .build()
        val source = eventSourceFactory.newEventSource(
            request,
            object : EventSourceListener() {
                override fun onEvent(
                    eventSource: EventSource,
                    id: String?,
                    type: String?,
                    data: String,
                ) {
                    val raw = parseEvent(data)
                    val eventType = (raw["type"] ?: raw["event"] ?: type ?: "task_update").toString()
                    val payload = raw["payload"] as? Map<String, Any?> ?: emptyMap()
                    trySend(StreamEvent(type = eventType, payload = payload, raw = raw))
                }

                override fun onFailure(
                    eventSource: EventSource,
                    t: Throwable?,
                    response: okhttp3.Response?,
                ) {
                    close(t ?: IOException("SSE stream failed with ${response?.code}"))
                }
            },
        )
        awaitClose { source.cancel() }
    }

    suspend fun download(pathOrUrl: String): Pair<String, ResponseBody> {
        val request = Request.Builder().url(absoluteUrl(pathOrUrl)).build()
        val response = okHttpClient.newCall(request).execute()
        if (!response.isSuccessful) {
            response.close()
            throw IOException("下载失败：HTTP ${response.code}")
        }
        val name = response.header("Content-Disposition")
            ?.substringAfter("filename=", "")
            ?.trim('"')
            ?.takeIf { it.isNotBlank() }
            ?: pathOrUrl.substringAfterLast('/').ifBlank { "limira-download" }
        return name to (response.body ?: throw IOException("下载内容为空"))
    }

    fun absoluteUrl(pathOrUrl: String): String {
        if (pathOrUrl.startsWith("http://") || pathOrUrl.startsWith("https://")) {
            return pathOrUrl
        }
        val base = BuildConfig.LIMIRA_API_BASE_URL.toHttpUrl()
        if (pathOrUrl.startsWith("/")) {
            return base.newBuilder()
                .encodedPath(pathOrUrl)
                .encodedQuery(null)
                .build()
                .toString()
        }
        return base.resolve(pathOrUrl)?.toString() ?: (BuildConfig.LIMIRA_API_BASE_URL + pathOrUrl)
    }

    fun errorMessage(error: Throwable): String = when (error) {
        is HttpException -> "请求失败：${error.code()} ${error.response()?.errorBody()?.string().orEmpty()}"
        else -> error.message ?: "未知错误"
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseEvent(data: String): Map<String, Any?> =
        runCatching { eventAdapter.fromJson(data) as? Map<String, Any?> }
            .getOrNull()
            .orEmpty()
}
