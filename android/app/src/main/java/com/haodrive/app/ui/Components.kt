package com.haodrive.app.ui

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import com.haodrive.app.data.Api

/** Small image loader that reuses the logged-in OkHttp client (cookies included). */
@Composable
fun RemoteImage(
    api: Api,
    url: String,
    modifier: Modifier = Modifier,
    contentScale: ContentScale = ContentScale.Crop,
    emoji: String = "🖼️",
) {
    var bitmap by remember(url) { mutableStateOf<ImageBitmap?>(null) }
    var failed by remember(url) { mutableStateOf(false) }

    LaunchedEffect(url) {
        failed = false
        val bytes = api.fetchBytes(url)
        bitmap = bytes?.let {
            runCatching { BitmapFactory.decodeByteArray(it, 0, it.size)?.asImageBitmap() }.getOrNull()
        }
        failed = bitmap == null
    }

    Box(
        modifier.background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(10.dp)),
        contentAlignment = Alignment.Center,
    ) {
        val image = bitmap
        if (image != null) {
            Image(
                bitmap = image,
                contentDescription = null,
                modifier = Modifier.fillMaxSize(),
                contentScale = contentScale,
            )
        } else {
            Text(if (failed) "🗻" else emoji)
        }
    }
}

fun kindEmoji(kind: String): String = when (kind) {
    "image" -> "🖼️"
    "video" -> "🎬"
    "audio" -> "🎵"
    "pdf" -> "📕"
    "text" -> "📄"
    else -> "📦"
}
