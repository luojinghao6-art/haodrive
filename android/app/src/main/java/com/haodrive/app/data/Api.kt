package com.haodrive.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

// ---- models --------------------------------------------------------------

data class Folder(val id: Long, val name: String)

data class FileItem(
    val id: Long,
    val name: String,
    val size: Long,
    val mime: String,
    val kind: String,
    val date: Long,
    val parts: Int,
    val hasThumb: Boolean,
) {
    val isVideo get() = kind == "video"
    val isAudio get() = kind == "audio"
    val isImage get() = kind == "image"
    val isPdf get() = kind == "pdf"
    val isText get() = kind == "text"
}

data class FilesPage(val items: List<FileItem>, val nextOffset: Long)

data class DayTraffic(val day: String, val up: Long, val down: Long)

data class FolderUsage(val name: String, val bytes: Long, val files: Long)

data class Stats(
    val usedBytes: Long,
    val files: Long,
    val folders: Long,
    val quotaBytes: Long,
    val up: Long,
    val down: Long,
    val upFiles: Long,
    val downFiles: Long,
    val diskTotal: Long,
    val diskFree: Long,
    val days: List<DayTraffic>,
    val byFolder: List<FolderUsage>,
)

data class TgStatus(val state: String, val user: String)

data class Limits(val partBytes: Long, val maxBytes: Long, val chunkSize: Long)

class ApiException(message: String, val code: Int = 0) : IOException(message)

// ---- client --------------------------------------------------------------

class Api(val prefs: Prefs) {
    @Volatile
    var baseUrl: String = prefs.baseUrl

    val client: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(PrefCookieJar(prefs))
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(300, TimeUnit.SECONDS)
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()
    private val octet = "application/octet-stream".toMediaType()

    fun base(): String = baseUrl.trim().trimEnd('/')

    fun url(path: String): String = base() + path

    fun normalize(input: String): String {
        var value = input.trim()
        if (value.isEmpty()) return value
        if (!value.startsWith("http://") && !value.startsWith("https://")) value = "https://$value"
        return value.trimEnd('/')
    }

    /** Cookie header for media players that bypass the OkHttp cookie jar. */
    fun cookieHeader(): String? {
        val raw = prefs.cookie ?: return null
        val parsed = base().toHttpUrlOrNull() ?: return null
        val cookie = try {
            Cookie.parse(parsed, raw)
        } catch (_: Exception) {
            null
        } ?: return null
        if (cookie.expiresAt < System.currentTimeMillis()) return null
        return "${cookie.name}=${cookie.value}"
    }

    private fun builder(path: String, mutation: Boolean): Request.Builder {
        val b = Request.Builder().url(url(path))
        if (mutation) b.header("X-Requested-With", "haodrive")
        return b
    }

    private suspend fun call(
        method: String,
        path: String,
        payload: JSONObject? = null,
        mutation: Boolean = false,
    ): JSONObject = withContext(Dispatchers.IO) {
        val body = payload?.toString()?.toRequestBody(jsonMedia)
        val request = when (method) {
            "GET" -> builder(path, mutation).get()
            "POST" -> builder(path, mutation).post(body ?: ByteArray(0).toRequestBody(jsonMedia))
            "PUT" -> builder(path, mutation).put(body ?: ByteArray(0).toRequestBody(jsonMedia))
            "DELETE" -> builder(path, mutation).delete()
            else -> builder(path, mutation).get()
        }.build()
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val detail = runCatching { JSONObject(text).optString("detail") }.getOrDefault("")
                throw ApiException(detail.ifBlank { "HTTP ${response.code}" }, response.code)
            }
            if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    // ---- auth --------------------------------------------------------------

    suspend fun login(rawBase: String, password: String) {
        baseUrl = normalize(rawBase)
        if (baseUrl.isEmpty()) throw ApiException("请填写服务器地址")
        call("POST", "/api/login", JSONObject().put("password", password))
        prefs.baseUrl = baseUrl
    }

    suspend fun session(): Boolean {
        if (base().isEmpty()) return false
        val data = call("GET", "/api/session")
        return data.optBoolean("authenticated", false)
    }

    suspend fun logout() {
        call("POST", "/api/logout", mutation = true)
        prefs.clearSession()
    }

    // ---- folders / files ---------------------------------------------------

    suspend fun folders(): List<Folder> {
        val data = call("GET", "/api/folders")
        val array = data.optJSONArray("folders") ?: JSONArray()
        return buildList {
            for (i in 0 until array.length()) {
                val o = array.getJSONObject(i)
                add(Folder(o.optLong("id"), o.optString("name")))
            }
        }
    }

    suspend fun files(folderId: Long, offset: Long = 0, limit: Int = 60, query: String? = null): FilesPage {
        val q = if (query.isNullOrBlank()) "" else "&q=${java.net.URLEncoder.encode(query, "UTF-8")}"
        val data = call("GET", "/api/folders/$folderId/files?offset=$offset&limit=$limit$q")
        val array = data.optJSONArray("items") ?: JSONArray()
        val items = buildList {
            for (i in 0 until array.length()) {
                val o = array.getJSONObject(i)
                add(
                    FileItem(
                        id = o.optLong("id"),
                        name = o.optString("name"),
                        size = o.optLong("size"),
                        mime = o.optString("mime"),
                        kind = o.optString("kind", "file"),
                        date = o.optLong("date"),
                        parts = o.optInt("parts", 1),
                        hasThumb = o.optBoolean("has_thumb", false),
                    )
                )
            }
        }
        return FilesPage(items, data.optLong("next_offset", 0))
    }

    suspend fun rename(folderId: Long, messageId: Long, name: String) {
        call("POST", "/api/folders/$folderId/files/$messageId/rename",
            JSONObject().put("name", name), mutation = true)
    }

    suspend fun delete(folderId: Long, messageId: Long) {
        call("DELETE", "/api/folders/$folderId/files/$messageId", mutation = true)
    }

    suspend fun createFolder(name: String): Folder {
        val data = call("POST", "/api/folders", JSONObject().put("name", name), mutation = true)
        return Folder(data.optLong("id"), data.optString("name", name))
    }

    // ---- stats / status ----------------------------------------------------

    suspend fun stats(days: Int = 30): Stats {
        val data = call("GET", "/api/stats?days=$days")
        val storage = data.optJSONObject("storage") ?: JSONObject()
        val traffic = data.optJSONObject("traffic") ?: JSONObject()
        val totals = traffic.optJSONObject("totals") ?: JSONObject()
        val disk = data.optJSONObject("vps_disk") ?: JSONObject()
        val dayArray = traffic.optJSONArray("days") ?: JSONArray()
        val dayList = buildList {
            for (i in 0 until dayArray.length()) {
                val o = dayArray.getJSONObject(i)
                add(DayTraffic(o.optString("day"), o.optLong("up"), o.optLong("down")))
            }
        }
        val folderArray = storage.optJSONArray("by_folder") ?: JSONArray()
        val folderList = buildList {
            for (i in 0 until folderArray.length()) {
                val o = folderArray.getJSONObject(i)
                add(FolderUsage(o.optString("name"), o.optLong("bytes"), o.optLong("files")))
            }
        }
        return Stats(
            usedBytes = storage.optLong("used_bytes"),
            files = storage.optLong("files"),
            folders = storage.optLong("folders"),
            quotaBytes = storage.optLong("quota_bytes"),
            up = totals.optLong("up"),
            down = totals.optLong("down"),
            upFiles = totals.optLong("up_files"),
            downFiles = totals.optLong("down_files"),
            diskTotal = disk.optLong("total"),
            diskFree = disk.optLong("free"),
            days = dayList,
            byFolder = folderList,
        )
    }

    suspend fun status(): TgStatus {
        val data = call("GET", "/api/status")
        val tg = data.optJSONObject("telegram") ?: JSONObject()
        val user = tg.optJSONObject("user")
        val label = user?.optString("name").orEmpty().ifBlank { user?.optString("username").orEmpty() }
        return TgStatus(tg.optString("state", "unknown"), label)
    }

    // ---- uploads -----------------------------------------------------------

    suspend fun limits(): Limits {
        val data = call("GET", "/api/parts/limits")
        return Limits(
            partBytes = data.optLong("part_bytes", 1_900_000_000L),
            maxBytes = data.optLong("max_bytes", 20L * 1024 * 1024 * 1024),
            chunkSize = data.optLong("chunk_size", 8L * 1024 * 1024),
        )
    }

    suspend fun createPart(
        folderId: Long, name: String, size: Long, partBytes: Long,
        groupKey: String, partIndex: Int, partCount: Int,
    ): JSONObject = call(
        "POST", "/api/parts",
        JSONObject()
            .put("folder_id", folderId)
            .put("name", name)
            .put("size", size)
            .put("part_bytes", partBytes)
            .put("group_key", groupKey)
            .put("part_index", partIndex)
            .put("part_count", partCount),
        mutation = true,
    )

    suspend fun putChunk(uploadId: String, offset: Long, data: ByteArray, length: Int = data.size): Long =
        withContext(Dispatchers.IO) {
            val request = builder("/api/uploads/$uploadId?offset=$offset", mutation = true)
                .put(data.toRequestBody(octet, 0, length))
                .build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    val detail = runCatching { JSONObject(text).optString("detail") }.getOrDefault("")
                    throw ApiException(detail.ifBlank { "上传失败 HTTP ${response.code}" }, response.code)
                }
                JSONObject(text).optLong("offset", offset + length)
            }
        }

    suspend fun completeUpload(uploadId: String): String {
        val data = call("POST", "/api/uploads/$uploadId/complete", mutation = true)
        return data.optString("id")
    }

    suspend fun job(id: String): JSONObject = call("GET", "/api/jobs/$id")

    // ---- media -------------------------------------------------------------

    fun streamUrl(folderId: Long, messageId: Long, inline: Boolean = true) =
        url("/api/folders/$folderId/files/$messageId/stream?inline=${if (inline) 1 else 0}")

    fun downloadUrl(folderId: Long, messageId: Long) =
        url("/api/folders/$folderId/files/$messageId/download")

    fun thumbUrl(folderId: Long, messageId: Long) =
        url("/api/folders/$folderId/files/$messageId/thumb")

    suspend fun openDownload(folderId: Long, messageId: Long): Response = withContext(Dispatchers.IO) {
        val request = builder("/api/folders/$folderId/files/$messageId/download", false).get().build()
        val response = client.newCall(request).execute()
        if (!response.isSuccessful) {
            response.close()
            throw ApiException("下载失败 HTTP ${response.code}", response.code)
        }
        response
    }

    suspend fun fetchBytes(url: String): ByteArray? = withContext(Dispatchers.IO) {
        runCatching {
            client.newCall(Request.Builder().url(url).get().build()).execute().use { response ->
                if (!response.isSuccessful) null else response.body?.bytes()
            }
        }.getOrNull()
    }

    private class PrefCookieJar(private val prefs: Prefs) : CookieJar {
        override fun loadForRequest(url: HttpUrl): List<Cookie> {
            val raw = prefs.cookie ?: return emptyList()
            val cookie = try {
                Cookie.parse(url, raw)
            } catch (_: Exception) {
                null
            } ?: return emptyList()
            if (cookie.expiresAt < System.currentTimeMillis()) {
                prefs.clearSession()
                return emptyList()
            }
            return listOf(cookie)
        }

        override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
            val cookie = cookies.lastOrNull() ?: return
            if (cookie.expiresAt < System.currentTimeMillis()) {
                prefs.clearSession()
            } else {
                prefs.cookie = cookie.toString()
            }
        }
    }
}
