// ZettaBridge launcher. Generated native/runtime inputs come only from build/launcher.
// Plain Java, UI built in code, no AndroidX.
import java.io.File
import java.util.Properties

plugins {
    id("com.android.application")
}

// The release key lives outside the repository: ~/.zettabridge/signing.properties names the
// keystore and its passwords. Without that file the release build stays unsigned, so a clone
// without the key still builds.
val signingProps = Properties()
val signingPropsFile = File(System.getProperty("user.home"), ".zettabridge/signing.properties")
if (signingPropsFile.isFile) {
    signingPropsFile.inputStream().use { stream -> signingProps.load(stream) }
}
val releaseKeystore: File? = signingProps.getProperty("storeFile")?.let { path -> File(path) }
val hasReleaseKey = releaseKeystore != null && releaseKeystore.isFile

// The obfuscated APK replaces the one already installed on the phone, and Android refuses to
// upgrade an app whose signature changed (INSTALL_FAILED_UPDATE_INCOMPATIBLE). The debug key is
// therefore reused deliberately: same key, same upgrade path, obfuscated contents.
val debugKeystore = File(System.getProperty("user.home"), ".android/debug.keystore")

android {
    namespace = "com.zettabridge.launcher"
    compileSdk = 35

    defaultConfig {
        // The installed identity of THIS build, and the only package name a reverse engineer sees
        // without unpacking the dex: the app is called "Translator" on the home screen and installs
        // as com.chucny.translator.bridge. The Java namespace underneath stays
        // com.zettabridge.launcher, because two things fix it from outside this module's source:
        // libzbridge.so exports Java_com_zettabridge_core_ZBridge_* (the class name is half of the
        // symbol), and PluginClassLoader routes a plugin's requests for "com.zettabridge.core."
        // to the host's bridge classes. Neither can be renamed without rebuilding the runtime.
        //
        // Nothing in the core keys off the applicationId: Process::add_path_alias registers the
        // /data/data/<pkg> and /data/user/0/<pkg> spellings of whatever package the process
        // actually runs as, and the guest's own System.load path goes through
        // ProxyRuntime::load_guest_library, which keeps only the file name. Verified on the device
        // for the previous rename, and again for this one (README-OBFUSCATION.md).
        applicationId = "com.chucny.translator.bridge"
        minSdk = 26
        // targetSdk is deliberately not the newest, and not lower either: a plugin's Java code runs
        // in this process, and the framework keys its compatibility changes on the *process*
        // targetSdk, not on the plugin's own.
        //
        // Above 30 breaks 2016 code. com.nianticlabs.nia.location.LocationManagerProvider calls
        // LocationManager.addGpsStatusListener, which throws UnsupportedOperationException under
        // compat change BLOCK_GPS_STATUS_USAGE (144027538, "enableSinceTargetSdk=31", platform
        // message: "threshold (30)") on any modern device, killing the :guest process from a
        // ContextService handler thread. The same key decides PendingIntent mutability flags,
        // runtime-receiver export flags and foreground service types, all of which plugin code
        // uses in the pre-S form. The platform also refuses to override such a change for an app
        // whose own targetSdk is above the threshold, so this is the only lever.
        //
        // Below 28 breaks the launcher: Android 16's ro.build.version.min_supported_target_sdk is
        // 28, and a lower targetSdk makes the framework put a modal "This app was built for an
        // older version of Android" dialog in front of every activity launch. That dialog blocks
        // the :guest process's first window, which then never finishes its Unity start-up.
        // 28 is therefore the lowest value that works, and the oldest era a modern device allows.
        targetSdk = 28
        versionCode = 1
        versionName = "0.1.0"
        ndk { abiFilters += "arm64-v8a" }
        // The runtime and the bundled client travel as two encrypted payload containers, staged by
        // build/pack_payloads.py into build/payloads/assets. They are already compressed inside and
        // already incompressible outside, so aapt is told to store them: re-deflating 60 MB of
        // ciphertext would cost build time and buy nothing.
        // The decoy layer's extensions are listed as well: those files are random bytes (nothing for
        // aapt to gain by deflating 1600 of them, and every one aapt stores as-is costs less than a
        // deflate wrapper would).
        androidResources {
            noCompress += listOf("bin", "dat", "pkg", "map", "sig", "idx", "cfg", "ko")
        }
    }

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = releaseKeystore
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
            }
        }
        create("obfuscated") {
            storeFile = debugKeystore
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }
    buildTypes {
        debug {
            // The debuggable variant, built by project2/build-debug.sh. It is the release build
            // with the opposite of everything R8 is asked to do, plus the verbose DebugLog that
            // lives in app/src/debug: readable names and line numbers, android:debuggable="true",
            // no shrinking, no resource shrinking, and an app log file that explains every window
            // that closes and every process that dies.
            //
            // The applicationId is deliberately NOT suffixed. Keeping it means this APK installs
            // over the obfuscated one as an upgrade (same key, same package), so the imported
            // plugin, its extracted arm32 libraries and the whole /data/data tree survive the
            // switch - the debug build can then reproduce the crash it is meant to explain,
            // instead of starting from an empty library.
            isDebuggable = true
            isMinifyEnabled = false
            isShrinkResources = false
            ndk { debugSymbolLevel = "NONE" }
            signingConfig = if (hasReleaseKey) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("obfuscated")
            }
        }
        release {
            // The whole point of project2: R8 renames and shrinks the module's dex, and the
            // unused resources go with it. proguard-rules.pro keeps exactly the names the JVM
            // and libzbridge.so resolve by string; everything else is renamed out of meaning.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            // No debug-symbol level at all: the packaged .so files are stripped, and no symbol
            // zip is produced for this variant.
            ndk { debugSymbolLevel = "NONE" }
            signingConfig = if (hasReleaseKey) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("obfuscated")
            }
        }
    }
    // This artifact must build without network. lintVital runs for release builds by default but
    // needs com.android.tools.lint:lint-gradle, which is not in the offline cache, so the build
    // failed at `lintVitalAnalyzeRelease` before packaging. Lint is a static-analysis gate that
    // cannot change the APK, so it is switched off for the release variant rather than dragging a
    // dependency in; the launcher's own contract tests are run instead (README-OBFUSCATION.md).
    lint {
        checkReleaseBuilds = false
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    sourceSets.getByName("main") {
        // Paths are one level shallower than upstream's android/launcher: this copy of the module
        // lives at project2/launcher, so the staged inputs are under project2/build, not ../../build.
        //
        // Everything the app ships as data comes from build/payloads, which build/pack_payloads.py
        // writes: two encrypted containers (the arm32 runtime bundle, and the bundled client APK with
        // its descriptor), the bridge library under a generated name, and the generated Keys class
        // holding those names and the payload key. The raw staged runtime and the raw bundled APK are
        // deliberately NOT asset sources any more - shipping them was what made "unzip and take the
        // .so" work. pack_payloads.py runs before Gradle (see build-obfuscated.sh), and this module
        // will not compile without its generated Keys.java.
        assets.srcDir(rootProject.file("../build/payloads/assets"))
        jniLibs.srcDir(rootProject.file("../build/payloads/jniLibs"))
        java.srcDir(rootProject.file("../build/payloads/src"))
    }
}

dependencies {
    // Apache-2.0. Lifts hidden API restrictions for the framework hooks in the :guest process.
    implementation("org.lsposed.hiddenapibypass:hiddenapibypass:6.1")
}
