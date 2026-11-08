import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing reads android/keystore.properties (gitignored). Without it the
// release build still works, it just produces an unsigned APK.
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}
val releaseStoreFile = keystoreProps.getProperty("storeFile")?.let { file(it) }
val releaseStorePass = keystoreProps.getProperty("storePassword")
val releaseKeyAlias = keystoreProps.getProperty("keyAlias") ?: "haodrive"
val releaseKeyPass = keystoreProps.getProperty("keyPassword")
val releaseReady = releaseStoreFile?.exists() == true &&
    !releaseStorePass.isNullOrBlank() && !releaseKeyPass.isNullOrBlank()

android {
    namespace = "com.haodrive.app"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.haodrive.app"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "1.0.0"
    }
    buildFeatures {
        compose = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }
    signingConfigs {
        if (releaseReady) {
            create("release") {
                storeFile = releaseStoreFile
                storePassword = releaseStorePass
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPass
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            if (releaseReady) signingConfig = signingConfigs.getByName("release")
        }
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2026.09.00"))
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("androidx.media3:media3-exoplayer:1.3.1")
    implementation("androidx.media3:media3-ui:1.3.1")
}
