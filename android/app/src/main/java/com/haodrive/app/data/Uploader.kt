package com.haodrive.app.data

import android.content.ContentResolver
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.IOException
import java.util.UUID
import kotlin.math.min

/** Uploads one file through the browser-style part API, one part at a time. */
class Uploader(private val api: Api) {

    data class Progress(
        val sent: Long,
        val total: Long,
        val part: Int,
        val parts: Int,
        val bytesPerSecond: Long = 0,
    )

    suspend fun upload(
        resolver: ContentResolver,
        uri: Uri,
        folderId: Long,
        name: String,
        size: Long,
        onProgress: (Progress) -> Unit,
    ) {
        if (size <= 0) throw ApiException("无法读取文件大小")
        val limits = api.limits()
        if (size > limits.maxBytes) {
            throw ApiException("文件超过服务器上限 ${formatBytes(limits.maxBytes)}")
        }
        val partBytes = limits.partBytes.coerceAtLeast(1)
        val parts = (((size + partBytes - 1) / partBytes).toInt()).coerceAtLeast(1)
        val groupKey = UUID.randomUUID().toString().replace("-", "")
        val meter = SpeedMeter()
        withContext(Dispatchers.IO) {
        val stream = resolver.openInputStream(uri) ?: throw IOException("无法打开文件")
        stream.use {
            var sentTotal = 0L
            for (index in 0 until parts) {
                val partSize = min(partBytes, size - sentTotal)
                val created = createPartWithRetry(folderId, name, size, partBytes, groupKey, index, parts)
                if (created.optString("state") == "done") {
                    sentTotal += partSize
                    onProgress(Progress(sentTotal, size, index + 1, parts, meter.last()))
                    continue
                }
                val uploadId = created.optString("id")
                if (uploadId.isBlank()) throw ApiException("服务器没有返回上传会话")
                var offset = created.optLong("offset", 0L)
                val chunk = created.optLong("chunk_size", limits.chunkSize)
                    .coerceIn(64L * 1024, 4L * 1024 * 1024).toInt()
                val buffer = ByteArray(chunk)
                while (offset < partSize) {
                    val want = min(partSize - offset, chunk.toLong()).toInt()
                    var read = 0
                    while (read < want) {
                        val n = stream.read(buffer, read, want - read)
                        if (n < 0) throw IOException("文件读取不完整")
                        read += n
                    }
                    offset = api.putChunk(uploadId, offset, buffer, read)
                    onProgress(Progress(sentTotal + offset, size, index + 1, parts,
                        meter.sample(sentTotal + offset)))
                }
                sentTotal += partSize
                val jobId = api.completeUpload(uploadId)
                waitJob(jobId)
                onProgress(Progress(sentTotal, size, index + 1, parts, meter.last()))
            }
        }
        }
    }

    private suspend fun createPartWithRetry(
        folderId: Long, name: String, size: Long, partBytes: Long,
        groupKey: String, index: Int, parts: Int,
    ): org.json.JSONObject {
        var lastError: Exception? = null
        repeat(8) { attempt ->
            try {
                return api.createPart(folderId, name, size, partBytes, groupKey, index, parts)
            } catch (error: ApiException) {
                lastError = error
                if (error.code != 507 && error.code != 409 && error.code != 429) throw error
                delay(3000L * (attempt + 1))
            }
        }
        throw lastError ?: ApiException("无法开始上传")
    }

    private suspend fun waitJob(id: String) {
        val deadline = System.currentTimeMillis() + 30 * 60 * 1000
        while (System.currentTimeMillis() < deadline) {
            val state = api.job(id)
            when (state.optString("state")) {
                "done" -> return
                "error" -> throw ApiException(state.optString("error").ifBlank { "Telegram 发送失败" })
                else -> delay(1000)
            }
        }
        throw ApiException("上传超时")
    }
}

/** Short EMA over half-second windows, so the number does not jump per chunk. */
class SpeedMeter {
    private var lastBytes = 0L
    private var lastNanos = System.nanoTime()
    private var speed = 0.0

    fun sample(bytes: Long): Long {
        val now = System.nanoTime()
        val seconds = (now - lastNanos) / 1_000_000_000.0
        if (seconds >= 0.5) {
            val instant = ((bytes - lastBytes) / seconds).coerceAtLeast(0.0)
            speed = if (speed > 0) speed * 0.6 + instant * 0.4 else instant
            lastBytes = bytes
            lastNanos = now
        }
        return speed.toLong()
    }

    fun last(): Long = speed.toLong()
}

fun formatSpeed(bytesPerSecond: Long): String {
    if (bytesPerSecond <= 0) return ""
    return if (bytesPerSecond >= 1024 * 1024) {
        "%.1f MB/s".format(bytesPerSecond / 1024.0 / 1024.0)
    } else {
        "${maxOf(1, bytesPerSecond / 1024)} KB/s"
    }
}

fun formatBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val units = arrayOf("KB", "MB", "GB", "TB")
    var value = bytes.toDouble()
    var unit = -1
    while (value >= 1024 && unit < units.size - 1) {
        value /= 1024
        unit++
    }
    return if (value >= 100) "${value.toInt()} ${units[unit]}" else "%.1f %s".format(value, units[unit])
}

fun formatTime(epochSeconds: Long): String {
    if (epochSeconds <= 0) return ""
    val date = java.util.Date(epochSeconds * 1000)
    return java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.getDefault()).format(date)
}
