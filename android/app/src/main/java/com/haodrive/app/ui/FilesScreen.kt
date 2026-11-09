package com.haodrive.app.ui

import android.net.Uri
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.haodrive.app.data.Api
import com.haodrive.app.data.Downloader
import com.haodrive.app.data.FileItem
import com.haodrive.app.data.Folder
import com.haodrive.app.data.Prefs
import com.haodrive.app.data.Uploader
import com.haodrive.app.data.DownloadProgress
import com.haodrive.app.data.formatBytes
import com.haodrive.app.data.formatSpeed
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private data class UploadUi(
    val name: String,
    val sent: Long,
    val total: Long,
    val part: Int,
    val parts: Int,
    val bytesPerSecond: Long = 0,
)

@Composable
fun FilesScreen(api: Api) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val prefs = remember { Prefs(context) }
    val uploader = remember { Uploader(api) }
    val downloader = remember { Downloader(api) }

    var folders by remember { mutableStateOf<List<Folder>>(emptyList()) }
    var folderId by remember { mutableLongStateOf(prefs.lastFolderId) }
    var files by remember { mutableStateOf<List<FileItem>>(emptyList()) }
    var nextOffset by remember { mutableStateOf(0L) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf("") }
    var query by remember { mutableStateOf("") }
    var searchOpen by remember { mutableStateOf(false) }
    var folderMenu by remember { mutableStateOf(false) }
    var preview by remember { mutableStateOf<FileItem?>(null) }
    var actionItem by remember { mutableStateOf<FileItem?>(null) }
    var renameItem by remember { mutableStateOf<FileItem?>(null) }
    var deleteItem by remember { mutableStateOf<FileItem?>(null) }
    var pendingDownload by remember { mutableStateOf<FileItem?>(null) }
    var downloadProgress by remember { mutableStateOf<DownloadProgress?>(null) }
    var uploadState by remember { mutableStateOf<UploadUi?>(null) }

    fun toast(message: String) {
        Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
    }

    suspend fun load(reset: Boolean) {
        if (reset) {
            loading = true
            error = ""
        }
        try {
            val page = api.files(folderId, if (reset) 0L else nextOffset, 60, query.ifBlank { null })
            files = if (reset) page.items else files + page.items
            nextOffset = page.nextOffset
        } catch (e: Exception) {
            error = e.message ?: "加载失败"
        } finally {
            loading = false
        }
    }

    LaunchedEffect(Unit) {
        try {
            folders = api.folders()
            if (folders.isNotEmpty() && folders.none { it.id == folderId }) {
                folderId = folders.first().id
            }
            if (folders.isEmpty()) loading = false
        } catch (e: Exception) {
            error = e.message ?: "无法获取文件夹"
            loading = false
        }
    }

    LaunchedEffect(folderId, query) {
        if (query.isNotBlank()) delay(250)
        prefs.lastFolderId = folderId
        load(true)
    }

    fun startUpload(uris: List<Uri>) {
        if (uris.isEmpty()) return
        scope.launch {
            for (uri in uris) {
                val meta = queryMeta(context, uri)
                if (meta.second <= 0) {
                    toast("无法读取文件：${meta.first}")
                    continue
                }
                uploadState = UploadUi(meta.first, 0, meta.second, 1, 1)
                try {
                    uploader.upload(context.contentResolver, uri, folderId, meta.first, meta.second) { p ->
                        uploadState = UploadUi(meta.first, p.sent, p.total, p.part, p.parts,
                            p.bytesPerSecond)
                    }
                    toast("已上传：${meta.first}")
                } catch (e: Exception) {
                    toast("上传失败：${e.message}")
                } finally {
                    uploadState = null
                }
            }
            load(true)
        }
    }

    val uploadPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris -> startUpload(uris) }

    val downloadSaver = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/octet-stream")
    ) { uri ->
        val item = pendingDownload
        pendingDownload = null
        if (uri != null && item != null) {
            scope.launch {
                downloadProgress = DownloadProgress(0L, item.size)
                try {
                    downloader.download(context.contentResolver, folderId, item.id, uri) { p ->
                        downloadProgress = p
                    }
                    toast("下载完成：${item.name}")
                } catch (e: Exception) {
                    toast("下载失败：${e.message}")
                } finally {
                    downloadProgress = null
                }
            }
        }
    }

    val folderName = folders.firstOrNull { it.id == folderId }?.name ?: "文件"

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(Modifier.weight(1f)) {
                TextButton(onClick = { folderMenu = true }) {
                    Text(
                        folderName,
                        style = MaterialTheme.typography.titleLarge,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Icon(Icons.Filled.ArrowDropDown, contentDescription = null)
                }
                DropdownMenu(expanded = folderMenu, onDismissRequest = { folderMenu = false }) {
                    folders.forEach { folder ->
                        DropdownMenuItem(
                            text = { Text(folder.name) },
                            onClick = {
                                folderMenu = false
                                if (folder.id != folderId) {
                                    folderId = folder.id
                                    files = emptyList()
                                    nextOffset = 0
                                }
                            },
                        )
                    }
                }
            }
            IconButton(onClick = { searchOpen = !searchOpen; if (!searchOpen) query = "" }) {
                Icon(Icons.Filled.Search, contentDescription = "搜索")
            }
            IconButton(onClick = { scope.launch { load(true) } }) {
                Icon(Icons.Filled.Refresh, contentDescription = "刷新")
            }
        }

        if (searchOpen) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
                singleLine = true,
                placeholder = { Text("搜索文件名") },
            )
        }

        Box(Modifier.weight(1f)) {
            when {
                loading && files.isEmpty() -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    CircularProgressIndicator()
                }
                error.isNotBlank() && files.isEmpty() -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(error, color = MaterialTheme.colorScheme.error)
                        TextButton(onClick = { scope.launch { load(true) } }) { Text("重试") }
                    }
                }
                files.isEmpty() -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    Text("这里还是空的", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                else -> LazyColumn(contentPadding = PaddingValues(bottom = 96.dp)) {
                    items(files, key = { it.id }) { item ->
                        FileRow(
                            api = api,
                            folderId = folderId,
                            item = item,
                            onClick = {
                                if (item.isVideo || item.isAudio || item.isImage ||
                                    item.isPdf || item.isText
                                ) {
                                    preview = item
                                } else {
                                    pendingDownload = item
                                    downloadSaver.launch(item.name)
                                }
                            },
                            onMenu = { actionItem = item },
                        )
                    }
                    if (nextOffset > 0) {
                        item {
                            TextButton(
                                onClick = { scope.launch { load(false) } },
                                modifier = Modifier.fillMaxWidth(),
                            ) { Text("加载更多") }
                        }
                    }
                }
            }

            FloatingActionButton(
                onClick = { uploadPicker.launch(arrayOf("*/*")) },
                modifier = Modifier.align(Alignment.BottomEnd).padding(20.dp),
            ) {
                Icon(Icons.Filled.Add, contentDescription = "上传")
            }
        }
    }

    // ---- action menu -------------------------------------------------------

    val menuItem = actionItem
    if (menuItem != null) {
        AlertDialog(
            onDismissRequest = { actionItem = null },
            title = { Text(menuItem.name, maxLines = 2, overflow = TextOverflow.Ellipsis) },
            text = {
                Column {
                    TextButton(onClick = {
                        actionItem = null
                        pendingDownload = menuItem
                        downloadSaver.launch(menuItem.name)
                    }, modifier = Modifier.fillMaxWidth()) { Text("下载") }
                    if (menuItem.isVideo || menuItem.isAudio || menuItem.isImage ||
                        menuItem.isPdf || menuItem.isText
                    ) {
                        TextButton(onClick = {
                            actionItem = null
                            preview = menuItem
                        }, modifier = Modifier.fillMaxWidth()) { Text("预览") }
                    }
                    TextButton(onClick = {
                        actionItem = null
                        renameItem = menuItem
                    }, modifier = Modifier.fillMaxWidth()) { Text("重命名") }
                    TextButton(onClick = {
                        actionItem = null
                        deleteItem = menuItem
                    }, modifier = Modifier.fillMaxWidth()) { Text("删除") }
                }
            },
            confirmButton = {},
            dismissButton = { TextButton(onClick = { actionItem = null }) { Text("取消") } },
        )
    }

    val renameTarget = renameItem
    if (renameTarget != null) {
        var name by remember(renameTarget.id) { mutableStateOf(renameTarget.name) }
        AlertDialog(
            onDismissRequest = { renameItem = null },
            title = { Text("重命名") },
            text = { OutlinedTextField(value = name, onValueChange = { name = it }, singleLine = true) },
            confirmButton = {
                TextButton(onClick = {
                    val target = renameTarget
                    renameItem = null
                    scope.launch {
                        try {
                            api.rename(folderId, target.id, name)
                            load(true)
                        } catch (e: Exception) {
                            toast("重命名失败：${e.message}")
                        }
                    }
                }) { Text("确定") }
            },
            dismissButton = { TextButton(onClick = { renameItem = null }) { Text("取消") } },
        )
    }

    val deleteTarget = deleteItem
    if (deleteTarget != null) {
        AlertDialog(
            onDismissRequest = { deleteItem = null },
            title = { Text("删除文件") },
            text = { Text("确定删除「${deleteTarget.name}」？这会同时删除 Telegram 频道里的消息。") },
            confirmButton = {
                TextButton(onClick = {
                    val target = deleteTarget
                    deleteItem = null
                    scope.launch {
                        try {
                            api.delete(folderId, target.id)
                            load(true)
                        } catch (e: Exception) {
                            toast("删除失败：${e.message}")
                        }
                    }
                }) { Text("删除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { deleteItem = null }) { Text("取消") } },
        )
    }

    // ---- progress dialogs --------------------------------------------------

    val upload = uploadState
    if (upload != null) {
        val fraction = if (upload.total > 0) (upload.sent.toFloat() / upload.total) else 0f
        AlertDialog(
            onDismissRequest = {},
            title = { Text("正在上传") },
            text = {
                Column {
                    Text(upload.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(progress = { fraction }, modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(6.dp))
                    val detail = buildString {
                        append(formatBytes(upload.sent))
                        append(" / ")
                        append(formatBytes(upload.total))
                        if (upload.parts > 1) append(" · 第 ${upload.part}/${upload.parts} 片")
                        formatSpeed(upload.bytesPerSecond).takeIf { it.isNotEmpty() }
                            ?.let { append(" · ").append(it) }
                    }
                    Text(
                        detail,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            },
            confirmButton = {},
        )
    }

    val progress = downloadProgress
    if (progress != null) {
        val fraction = if (progress.total > 0) (progress.done.toFloat() / progress.total) else 0f
        AlertDialog(
            onDismissRequest = {},
            title = { Text("正在下载") },
            text = {
                Column {
                    LinearProgressIndicator(progress = { fraction }, modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "${formatBytes(progress.done)} / ${formatBytes(progress.total)}" +
                            formatSpeed(progress.bytesPerSecond).let { if (it.isEmpty()) "" else " · $it" },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            },
            confirmButton = {},
        )
    }

    val previewItem = preview
    if (previewItem != null) {
        PreviewDialog(
            api = api,
            folderId = folderId,
            item = previewItem,
            onClose = { preview = null },
            onDownload = {
                preview = null
                pendingDownload = previewItem
                downloadSaver.launch(previewItem.name)
            },
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun FileRow(
    api: Api,
    folderId: Long,
    item: FileItem,
    onClick: () -> Unit,
    onMenu: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .combinedClickable(onClick = onClick, onLongClick = onMenu)
            .padding(horizontal = 16.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (item.isImage) {
            RemoteImage(
                api = api,
                url = api.thumbUrl(folderId, item.id),
                modifier = Modifier.size(46.dp),
            )
        } else {
            Box(
                Modifier.size(46.dp)
                    .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(10.dp)),
                contentAlignment = Alignment.Center,
            ) { Text(kindEmoji(item.kind)) }
        }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                item.name,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                buildString {
                    append(formatBytes(item.size))
                    if (item.parts > 1) append(" · ${item.parts} 片")
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        IconButton(onClick = onMenu) {
            Icon(Icons.Filled.MoreVert, contentDescription = "更多")
        }
    }
}

private fun queryMeta(context: android.content.Context, uri: Uri): Pair<String, Long> {
    context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
        val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
        val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
        if (cursor.moveToFirst()) {
            val name = if (nameIndex >= 0) cursor.getString(nameIndex) else "upload.bin"
            val size = if (sizeIndex >= 0) cursor.getLong(sizeIndex) else 0L
            return (name ?: "upload.bin") to size
        }
    }
    return "upload.bin" to 0L
}
