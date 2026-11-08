package com.haodrive.app.ui

import androidx.compose.foundation.background
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.Canvas
import com.haodrive.app.data.Api
import com.haodrive.app.data.DayTraffic
import com.haodrive.app.data.Stats
import com.haodrive.app.data.formatBytes
import kotlinx.coroutines.launch

@Composable
fun DashboardScreen(api: Api) {
    var stats by remember { mutableStateOf<Stats?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf("") }
    var days by remember { mutableIntStateOf(30) }
    val scope = rememberCoroutineScope()

    suspend fun load() {
        loading = true
        error = ""
        try {
            stats = api.stats(days)
        } catch (e: Exception) {
            error = e.message ?: "加载失败"
        } finally {
            loading = false
        }
    }

    LaunchedEffect(days) { load() }

    val data = stats
    when {
        loading && data == null -> Box(Modifier.fillMaxSize(), Alignment.Center) {
            CircularProgressIndicator()
        }
        error.isNotBlank() && data == null -> Box(Modifier.fillMaxSize(), Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(error, color = MaterialTheme.colorScheme.error)
                TextButton(onClick = { scope.launch { load() } }) { Text("重试") }
            }
        }
        data != null -> LazyColumn(
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("概览", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                    if (loading) {
                        CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                    }
                    IconButton(onClick = { scope.launch { load() } }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "刷新")
                    }
                }
            }

            item {
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    StatBox("已用空间", formatBytes(data.usedBytes),
                        if (data.quotaBytes > 0) "共 ${formatBytes(data.quotaBytes)}" else "Telegram 存储",
                        Modifier.weight(1f))
                    StatBox("文件数量", data.files.toString(), "${data.folders} 个文件夹", Modifier.weight(1f))
                }
            }

            item {
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    StatBox("累计上传", formatBytes(data.up), "${data.upFiles} 个文件", Modifier.weight(1f))
                    StatBox("累计下载", formatBytes(data.down), "${data.downFiles} 个文件", Modifier.weight(1f))
                }
            }

            item {
                StatBox(
                    "服务器磁盘",
                    formatBytes(data.diskTotal - data.diskFree),
                    "剩余 ${formatBytes(data.diskFree)} / 共 ${formatBytes(data.diskTotal)}",
                    Modifier.fillMaxWidth(),
                )
            }

            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("上传 / 下载流量", style = MaterialTheme.typography.titleMedium,
                                modifier = Modifier.weight(1f))
                            listOf(7, 30, 90).forEach { option ->
                                FilterChip(
                                    selected = days == option,
                                    onClick = { days = option },
                                    label = { Text("${option}天") },
                                    modifier = Modifier.padding(start = 6.dp),
                                )
                            }
                        }
                        Spacer(Modifier.height(10.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            LegendDot(MaterialTheme.colorScheme.primary, "上传")
                            Spacer(Modifier.width(14.dp))
                            LegendDot(MaterialTheme.colorScheme.secondary, "下载")
                            Spacer(Modifier.weight(1f))
                            val today = data.days.lastOrNull()
                            if (today != null) {
                                Text(
                                    "今日 ↑${formatBytes(today.up)} ↓${formatBytes(today.down)}",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                        Spacer(Modifier.height(12.dp))
                        if (data.days.all { it.up + it.down == 0L }) {
                            Box(
                                Modifier.fillMaxWidth().height(120.dp)
                                    .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(10.dp)),
                                contentAlignment = Alignment.Center,
                            ) {
                                Text("暂无流量数据", color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        } else {
                            TrafficChart(data.days, Modifier.fillMaxWidth().height(160.dp))
                        }
                    }
                }
            }

            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("文件夹占用", style = MaterialTheme.typography.titleMedium)
                        Spacer(Modifier.height(10.dp))
                        if (data.byFolder.isEmpty()) {
                            Text("还没有数据", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        val maxBytes = (data.byFolder.maxOfOrNull { it.bytes } ?: 1L).coerceAtLeast(1L)
                        data.byFolder.forEach { folder ->
                            Column(Modifier.padding(vertical = 6.dp)) {
                                Row {
                                    Text(folder.name, modifier = Modifier.weight(1f), maxLines = 1)
                                    Text(
                                        formatBytes(folder.bytes),
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                                Spacer(Modifier.height(4.dp))
                                LinearProgressIndicator(
                                    progress = { folder.bytes.toFloat() / maxBytes },
                                    modifier = Modifier.fillMaxWidth().height(6.dp),
                                )
                            }
                        }
                    }
                }
            }

            item {
                Text(
                    "个人兴趣研究项目 · 数据存在你自己的 Telegram 账号里，账号风险自负",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun StatBox(label: String, value: String, sub: String, modifier: Modifier = Modifier) {
    Card(modifier) {
        Column(Modifier.padding(16.dp)) {
            Text(label, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(6.dp))
            Text(value, style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.height(4.dp))
            Text(sub, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
        }
    }
}

@Composable
private fun LegendDot(color: Color, label: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(8.dp).background(color, CircleShape))
        Spacer(Modifier.width(6.dp))
        Text(label, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun TrafficChart(days: List<DayTraffic>, modifier: Modifier = Modifier) {
    val upColor = MaterialTheme.colorScheme.primary
    val downColor = MaterialTheme.colorScheme.secondary
    val gridColor = MaterialTheme.colorScheme.outline.copy(alpha = 0.35f)
    val max = (days.maxOfOrNull { it.up + it.down } ?: 0L).coerceAtLeast(1L)
    Canvas(modifier) {
        if (days.isEmpty()) return@Canvas
        // horizontal guides
        for (i in 1..3) {
            val y = size.height * i / 4f
            drawLine(gridColor, Offset(0f, y), Offset(size.width, y), strokeWidth = 1f)
        }
        val slot = size.width / days.size
        val barWidth = (slot * 0.55f).coerceAtLeast(1.5f)
        days.forEachIndexed { index, day ->
            val total = day.up + day.down
            if (total <= 0) return@forEachIndexed
            val height = size.height * (total.toFloat() / max)
            val x = index * slot + (slot - barWidth) / 2f
            val downHeight = height * (day.down.toFloat() / total)
            val upHeight = height - downHeight
            drawRect(
                color = downColor,
                topLeft = Offset(x, size.height - downHeight),
                size = Size(barWidth, downHeight),
            )
            drawRect(
                color = upColor,
                topLeft = Offset(x, size.height - height),
                size = Size(barWidth, upHeight),
            )
        }
        drawRect(gridColor, size = size, style = Stroke(width = 1f))
    }
}
