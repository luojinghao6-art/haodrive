package com.haodrive.app.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import com.haodrive.app.data.Api
import com.haodrive.app.data.Prefs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

@Composable
fun AppRoot() {
    val context = LocalContext.current
    val prefs = remember { Prefs(context) }
    val api = remember { Api(prefs) }
    var checking by remember { mutableStateOf(true) }
    var authed by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        authed = withContext(Dispatchers.IO) {
            runCatching { api.session() }.getOrDefault(false)
        }
        checking = false
    }

    when {
        checking -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
        !authed -> LoginScreen(
            initialServer = prefs.baseUrl,
            onLogin = { server, password ->
                api.login(server, password)
                authed = true
            },
        )
        else -> MainShell(
            api = api,
            onLoggedOut = {
                prefs.clearSession()
                authed = false
            },
        )
    }
}

@Composable
private fun MainShell(api: Api, onLoggedOut: () -> Unit) {
    var tab by remember { mutableIntStateOf(0) }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = tab == 0,
                    onClick = { tab = 0 },
                    icon = { Icon(Icons.Filled.List, contentDescription = null) },
                    label = { Text("文件") },
                )
                NavigationBarItem(
                    selected = tab == 1,
                    onClick = { tab = 1 },
                    icon = { Icon(Icons.Filled.Info, contentDescription = null) },
                    label = { Text("概览") },
                )
                NavigationBarItem(
                    selected = tab == 2,
                    onClick = { tab = 2 },
                    icon = { Icon(Icons.Filled.Settings, contentDescription = null) },
                    label = { Text("设置") },
                )
            }
        },
    ) { padding ->
        Box(Modifier.padding(padding).statusBarsPadding().fillMaxSize()) {
            when (tab) {
                0 -> FilesScreen(api)
                1 -> DashboardScreen(api)
                else -> SettingsScreen(api, onLoggedOut)
            }
        }
    }
}
