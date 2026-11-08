package com.haodrive.app.data

import android.content.ContentResolver
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import kotlin.math.min

/** Streams a logical file (the server stitches its parts) into a user-picked Uri. */
class Downloader(private val api: Api) {

    suspend fun download(
        resolver: ContentResolver,
        folderId: Long,
        messageId: Long,
        target: Uri,
        onProgress: (Long, Long) -> Unit,
    ) = withContext(Dispatchers.IO) {
        api.openDownload(folderId, messageId).use { response ->
            val total = response.body?.contentLength() ?: -1L
            val input = response.body?.byteStream() ?: throw IOException("没有响应内容")
            val output = resolver.openOutputStream(target) ?: throw IOException("无法写入目标文件")
            input.use { source ->
                output.use { sink ->
                    val buffer = ByteArray(256 * 1024)
                    var done = 0L
                    while (true) {
                        val read = source.read(buffer)
                        if (read < 0) break
                        sink.write(buffer, 0, read)
                        done += read
                        onProgress(done, if (total > 0) total else done)
                    }
                    sink.flush()
                    if (total > 0 && done != total) throw IOException("下载不完整：$done/$total")
                }
            }
        }
    }
}
