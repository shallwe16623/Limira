package com.limira.android.data

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query

interface LimiraApi {
    @GET("auth/organizations")
    suspend fun organizations(): OrganizationListResponse

    @GET("auth/google/config")
    suspend fun googleConfig(): AuthOptions

    @GET("auth/wechat/config")
    suspend fun wechatConfig(): AuthOptions

    @POST("auth/signin")
    suspend fun personalSignIn(@Body request: PersonalSignInRequest): LimiraUser

    @POST("auth/signup")
    suspend fun signup(@Body request: SignupRequest): LimiraUser

    @POST("auth/enterprise/signin")
    suspend fun enterpriseSignIn(@Body request: EnterpriseSignInRequest): LimiraUser

    @POST("auth/mobile/exchange")
    suspend fun mobileOAuthExchange(@Body request: MobileOAuthExchangeRequest): LimiraUser

    @POST("auth/password-reset/request")
    suspend fun requestPasswordReset(@Body request: PasswordResetRequest): OkResponse

    @POST("auth/password-reset/confirm")
    suspend fun confirmPasswordReset(@Body request: PasswordResetConfirmRequest): LimiraUser

    @POST("auth/verify-email")
    suspend fun verifyEmail(@Body request: VerifyEmailRequest): LimiraUser

    @POST("auth/resend-verification")
    suspend fun resendVerification(@Body request: ResendVerificationRequest): OkResponse

    @POST("auth/signout")
    suspend fun signOut(): OkResponse

    @GET("auth/session")
    suspend fun session(): LimiraUser

    @GET("scenarios")
    suspend fun scenarios(): ScenariosResponse

    @POST("research")
    suspend fun createResearch(@Body request: ResearchRequest): ResearchTask

    @GET("tasks")
    suspend fun tasks(
        @Query("limit") limit: Int = 30,
        @Query("archived") archived: Boolean = false,
        @Query("query") query: String? = null,
    ): TaskListResponse

    @GET("tasks/{taskId}")
    suspend fun task(@Path("taskId") taskId: String): ResearchTask

    @POST("tasks/{taskId}/history/archive")
    suspend fun archiveTaskHistory(@Path("taskId") taskId: String): ResearchTask

    @POST("tasks/{taskId}/history/restore")
    suspend fun restoreTaskHistory(@Path("taskId") taskId: String): ResearchTask

    @DELETE("tasks/{taskId}/history")
    suspend fun deleteTaskHistory(@Path("taskId") taskId: String): OkResponse

    @GET("tasks/{taskId}/event-logs")
    suspend fun taskEventLogs(
        @Path("taskId") taskId: String,
        @Query("limit") limit: Int = 500,
    ): EventLogsResponse

    @GET("tasks/{taskId}/artifacts")
    suspend fun artifacts(@Path("taskId") taskId: String): ArtifactBuckets

    @GET("tasks/{taskId}/reports")
    suspend fun reports(@Path("taskId") taskId: String): ReportsResponse

    @POST("tasks/{taskId}/reports/pdf")
    suspend fun exportPdf(
        @Path("taskId") taskId: String,
        @Body request: ReportPdfRequest,
    ): GeneratedReport

    @GET("admin/tasks/{taskId}")
    suspend fun adminTask(@Path("taskId") taskId: String): ResearchTask

    @GET("admin/tasks/{taskId}/event-logs")
    suspend fun adminTaskEventLogs(
        @Path("taskId") taskId: String,
        @Query("limit") limit: Int = 500,
    ): EventLogsResponse

    @POST("admin/organizations")
    suspend fun createOrganization(@Body request: OrganizationCreateRequest): OrganizationCreateResponse

    @GET("enterprise/members")
    suspend fun enterpriseMembers(): EnterpriseMembersResponse

    @POST("enterprise/members")
    suspend fun createEnterpriseMember(
        @Body request: EnterpriseMemberCreateRequest,
    ): EnterpriseMemberCreateResponse

    @GET("enterprise/usage")
    suspend fun enterpriseUsage(@Query("days") days: Int = 30): EnterpriseUsageResponse

    @GET("uploads")
    suspend fun uploads(@Query("task_id") taskId: String? = null): UploadsResponse

    @GET("uploads/history")
    suspend fun uploadHistory(): UploadsResponse

    @GET("uploads/storage")
    suspend fun uploadStorage(): StorageResponse

    @GET("uploads/search")
    suspend fun searchUploads(
        @Query("query") query: String,
        @Query("task_id") taskId: String? = null,
        @Query("limit") limit: Int = 10,
    ): UploadSearchResponse

    @GET("uploads/{documentId}")
    suspend fun uploadedDocument(@Path("documentId") documentId: String): UploadedDocument

    @Multipart
    @POST("uploads")
    suspend fun uploadDocument(
        @Part file: MultipartBody.Part,
        @Part("task_id") taskId: RequestBody? = null,
    ): UploadedDocument

    @Multipart
    @POST("speech/transcribe")
    suspend fun transcribeSpeech(
        @Part file: MultipartBody.Part,
        @Part("language") language: RequestBody? = null,
    ): SpeechTranscriptResponse
}
