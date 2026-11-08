package com.haodrive.app.data

import android.content.Context

/** Small persistent store: server address, session cookie, last folder. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("haodrive", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = sp.getString("base_url", "") ?: ""
        set(value) = sp.edit().putString("base_url", value).apply()

    var cookie: String?
        get() = sp.getString("cookie", null)
        set(value) = sp.edit().putString("cookie", value).apply()

    var lastFolderId: Long
        get() = sp.getLong("folder_id", 0L)
        set(value) = sp.edit().putLong("folder_id", value).apply()

    fun clearSession() {
        sp.edit().remove("cookie").apply()
    }
}
