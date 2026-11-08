package com.haodrive.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.haodrive.app.data.Api
import com.haodrive.app.data.TgStatus
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(api: Api, onLoggedOut: () -> Unit) {
    var status by remember { mutableStateOf<TgStatus?>(null) }
    var error by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        try {
            status = api.status()
        } catch (e: Exception) {
            error = e.message ?: "无法获取状态"
        }
    }

    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        modifier = Modifier.fillMaxSize(),
    ) {
        item { Text("设置", style = MaterialTheme.typography.titleLarge) }

        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("服务器", style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(4.dp))
                    Text(api.base(), style = MaterialTheme.typography.bodyMedium)
                    Spacer(Modifier.height(12.dp))
                    Text("Telegram", style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(4.dp))
                    val tg = status
                    Text(
                        when {
                            tg == null && error.isNotBlank() -> error
                            tg == null -> "读取中…"
                            tg.state == "ready" -> "已连接" + if (tg.user.isNotBlank()) " · ${tg.user}" else ""
                            else -> tg.state
                        },
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
        }

        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("会话", style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(8.dp))
                    OutlinedButton(
                        onClick = {
                            scope.launch {
                                runCatching { api.logout() }
                                onLoggedOut()
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("退出登录") }
                    Spacer(Modifier.height(8.dp))
                    TextButton(
                        onClick = {
                            api.prefs.clearSession()
                            onLoggedOut()
                        },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("更换服务器") }
                }
            }
        }

        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("说明", style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "这是 HaoDrive 的原生安卓客户端，直接调用你自己的服务器。文件仍然存在你的 Telegram 私有频道里。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "个人兴趣研究项目：把第三方服务当存储可能违反其服务条款，账号存在被封风险。重要数据请另做备份。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
        }
    }
}
