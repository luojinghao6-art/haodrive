package com.haodrive.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.haodrive.app.ui.AppRoot
import com.haodrive.app.ui.HaoDriveTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            HaoDriveTheme {
                AppRoot()
            }
        }
    }
}
