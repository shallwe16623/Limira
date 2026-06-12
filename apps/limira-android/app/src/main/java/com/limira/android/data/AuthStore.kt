package com.limira.android.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class AuthStore @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private val preferences: SharedPreferences by lazy {
        runCatching {
            val key = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                context,
                "limira_secure_auth",
                key,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
        }.getOrElse {
            throw IllegalStateException("Unable to initialize encrypted Limira auth storage", it)
        }
    }

    var token: String?
        get() = preferences.getString(KEY_TOKEN, null)
        set(value) {
            preferences.edit().apply {
                if (value.isNullOrBlank()) remove(KEY_TOKEN) else putString(KEY_TOKEN, value)
            }.apply()
        }

    var user: LimiraUser?
        get() = preferences.getString(KEY_USER_NAME, null)?.let {
            LimiraUser(
                id = preferences.getString(KEY_USER_ID, "") ?: "",
                email = preferences.getString(KEY_USER_EMAIL, null),
                username = preferences.getString(KEY_USER_USERNAME, null),
                name = it,
                role = preferences.getString(KEY_USER_ROLE, "user") ?: "user",
                accountType = preferences.getString(KEY_ACCOUNT_TYPE, "personal") ?: "personal",
                organizationId = preferences.getString(KEY_ORG_ID, null),
                organizationRole = preferences.getString(KEY_ORG_ROLE, null),
            )
        }
        set(value) {
            preferences.edit().apply {
                if (value == null) {
                    remove(KEY_USER_ID)
                    remove(KEY_USER_EMAIL)
                    remove(KEY_USER_USERNAME)
                    remove(KEY_USER_NAME)
                    remove(KEY_USER_ROLE)
                    remove(KEY_ACCOUNT_TYPE)
                    remove(KEY_ORG_ID)
                    remove(KEY_ORG_ROLE)
                } else {
                    putString(KEY_USER_ID, value.id)
                    putString(KEY_USER_EMAIL, value.email)
                    putString(KEY_USER_USERNAME, value.username)
                    putString(KEY_USER_NAME, value.name ?: value.username ?: value.email ?: value.id)
                    putString(KEY_USER_ROLE, value.role)
                    putString(KEY_ACCOUNT_TYPE, value.accountType)
                    putString(KEY_ORG_ID, value.organizationId)
                    putString(KEY_ORG_ROLE, value.organizationRole)
                }
            }.apply()
        }

    fun saveAuth(user: LimiraUser) {
        token = user.token ?: token
        this.user = user
    }

    fun clear() {
        preferences.edit().clear().apply()
    }

    private companion object {
        const val KEY_TOKEN = "token"
        const val KEY_USER_ID = "user_id"
        const val KEY_USER_EMAIL = "user_email"
        const val KEY_USER_USERNAME = "user_username"
        const val KEY_USER_NAME = "user_name"
        const val KEY_USER_ROLE = "user_role"
        const val KEY_ACCOUNT_TYPE = "account_type"
        const val KEY_ORG_ID = "organization_id"
        const val KEY_ORG_ROLE = "organization_role"
    }
}
