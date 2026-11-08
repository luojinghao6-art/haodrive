package com.haodrive.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val Accent = Color(0xFF4F8CFF)
private val AccentDark = Color(0xFF2F6FE4)

private val DarkColors = darkColorScheme(
    primary = Accent,
    onPrimary = Color.White,
    secondary = Color(0xFF6FE3B0),
    background = Color(0xFF0E1014),
    onBackground = Color(0xFFE8ECF3),
    surface = Color(0xFF161A21),
    onSurface = Color(0xFFE8ECF3),
    surfaceVariant = Color(0xFF1E242E),
    onSurfaceVariant = Color(0xFF9BA6B8),
    outline = Color(0xFF2A3140),
    error = Color(0xFFFF6B6B),
)

private val LightColors = lightColorScheme(
    primary = AccentDark,
    onPrimary = Color.White,
    secondary = Color(0xFF128A5B),
    background = Color(0xFFF5F7FB),
    onBackground = Color(0xFF121722),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF121722),
    surfaceVariant = Color(0xFFEDF1F7),
    onSurfaceVariant = Color(0xFF5A6577),
    outline = Color(0xFFD6DEEA),
    error = Color(0xFFD33A3A),
)

@Composable
fun HaoDriveTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    MaterialTheme(colorScheme = if (dark) DarkColors else LightColors, content = content)
}
