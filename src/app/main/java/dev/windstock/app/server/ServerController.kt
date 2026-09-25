package dev.windstock.app.server

import android.app.Application
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class Phase { IDLE, PREPARING, RUNNING, STOPPING }

enum class RunMode { LOCAL, LAN }

enum class LaunchTarget { WINDSTOCK, FOLDER, SCRIPT }

data class Snapshot(
    val phase: Phase = Phase.IDLE,
    val mode: RunMode = RunMode.LOCAL,
    val port: Int = 443,
    val detail: String = "",
    val error: String? = null,
    val target: LaunchTarget = LaunchTarget.WINDSTOCK,
    val scriptPath: String = "",
)

object ServerController {

    lateinit var app: android.app.Application
        private set

    private val _snapshot = MutableStateFlow(Snapshot())
    val snapshot: StateFlow<Snapshot> = _snapshot.asStateFlow()

    private val _logs = MutableStateFlow<List<String>>(emptyList())
    val logs: StateFlow<List<String>> = _logs.asStateFlow()

    private const val LOG_LIMIT = 400
    const val BUNDLED_DIR = "windstock"
    const val PROJECTS_DIR = "projects"
    const val SCRIPTS_DIR = "scripts"
    const val DEFAULT_PORT = 443
    const val ADMIN_PORT = 8080

    val projectDir: java.io.File
        get() {
            val custom = prefs().getString("project", null)
            val dir = if (custom.isNullOrBlank()) java.io.File(app.filesDir, BUNDLED_DIR)
            else java.io.File(custom)
            dir.mkdirs()
            return dir
        }

    fun init(application: Application) {
        app = application
    }

    private fun prefs() = app.getSharedPreferences("windstock", 0)

    fun setProjectDir(dir: java.io.File) = prefs().edit().putString("project", dir.absolutePath).apply()

    fun log(line: String) {
        synchronized(_logs) {
            val next = (_logs.value + line).takeLast(LOG_LIMIT)
            _logs.value = next
        }
    }

    fun clearLogs() {
        _logs.value = emptyList()
    }

    fun update(snapshot: Snapshot) {
        _snapshot.value = snapshot
    }

    fun caCertFile(): java.io.File = java.io.File(projectDir, "certs/ca.crt")

    fun ensureBundled() {
        if (!prefs().getString("project", null).isNullOrBlank()) return
        val marker = java.io.File(projectDir, ".bundled")
        val version = try {
            app.packageManager.getPackageInfo(app.packageName, 0).versionName ?: "0"
        } catch (e: Exception) { "0" }

        val bundled = java.io.File(projectDir, "windstock/__init__.py").exists()
        if (bundled && marker.readText().trim() == version) return

        log("[app] extracting bundled Windstock into ${projectDir.name} ...")
        copyAssets(BUNDLED_DIR, projectDir, overwrite = false)
        copyAssets("$BUNDLED_DIR/certs", java.io.File(projectDir, "certs"), overwrite = false)
        marker.writeText(version)
        log("[app] server files ready at ${projectDir.absolutePath}")
    }

    private fun copyAssets(assetPath: String, dest: java.io.File, overwrite: Boolean) {
        val am = app.assets
        val children = try { am.list(assetPath) } catch (e: Exception) { null } ?: return
        for (child in children) {
            val childAsset = "$assetPath/$child"
            val childOut = java.io.File(dest, child)
            val sub = try { am.list(childAsset) } catch (e: Exception) { null }
            if (sub != null && sub.isNotEmpty()) {
                childOut.mkdirs()
                copyAssets(childAsset, childOut, overwrite)
            } else {
                if (childOut.exists() && !overwrite) continue
                childOut.parentFile?.mkdirs()
                am.open(childAsset).use { input ->
                    java.io.FileOutputStream(childOut).use { output -> input.copyTo(output) }
                }
            }
        }
    }

    fun replaceProject(from: java.io.File, keepData: Boolean = true) {
        val dataDir = java.io.File(projectDir, "data")
        val tempData = java.io.File(app.cacheDir, "keepdata")
        if (keepData && dataDir.exists()) {
            tempData.deleteRecursively()
            dataDir.renameTo(tempData)
        }
        from.walkTopDown().filter { it.isFile }.forEach { src ->
            val rel = src.relativeTo(from).path
            val dst = java.io.File(projectDir, rel)
            dst.parentFile?.mkdirs()
            src.copyTo(dst, overwrite = true)
        }
        if (keepData && tempData.exists()) {
            tempData.walkTopDown().filter { it.isFile }.forEach { src ->
                val rel = src.relativeTo(tempData).path
                val dst = java.io.File(dataDir, rel)
                dst.parentFile?.mkdirs()
                if (!dst.exists()) src.copyTo(dst)
            }
            tempData.deleteRecursively()
        }
        java.io.File(projectDir, "certs").mkdirs()
        copyAssets("$BUNDLED_DIR/certs", java.io.File(projectDir, "certs"), overwrite = false)
        log("[app] project refreshed (${from.name})")
    }

    fun startService() {
        ServerService.start(app)
    }

    fun stopService() {
        ServerService.stop(app)
    }

    fun setMode(mode: RunMode) {
        if (_snapshot.value.phase != Phase.IDLE) return
        _snapshot.value = _snapshot.value.copy(mode = mode)
    }

    fun setTarget(target: LaunchTarget) {
        if (_snapshot.value.phase != Phase.IDLE) return
        _snapshot.value = _snapshot.value.copy(target = target)
        log("[app] payload: ${target.name.lowercase()}")
    }

    fun runScriptFrom(uri: android.net.Uri, onDone: (String) -> Unit) {
        log("[app] importing python script ...")
        Thread {
            try {
                val name = queryDisplayName(uri) ?: "script.py"
                val safe = sanitize(name).let {
                    if (it.endsWith(".py")) it else "$it.py"
                }
                val scriptsDir = java.io.File(app.filesDir, SCRIPTS_DIR)
                scriptsDir.mkdirs()
                var out = java.io.File(scriptsDir, safe)
                var suffix = 1
                while (out.exists()) {
                    val base = safe.removeSuffix(".py")
                    out = java.io.File(scriptsDir, "${base}_$suffix.py")
                    suffix++
                }
                app.contentResolver.openInputStream(uri)?.use { input ->
                    java.io.FileOutputStream(out).use { input.copyTo(it) }
                }
                setProjectDir(scriptsDir)
                update(_snapshot.value.copy(target = LaunchTarget.SCRIPT, scriptPath = out.absolutePath))
                log("[app] script ready: ${out.name}")
                onDone("script: ${out.name}")
            } catch (e: Exception) {
                log("[error] script import failed: ${e.message}")
                onDone("import failed: ${e.message}")
            }
        }.start()
    }

    private fun queryDisplayName(uri: android.net.Uri): String? {
        return try {
            app.contentResolver.query(
                uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        val idx = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                        if (idx >= 0) cursor.getString(idx) else null
                    } else null
                }
        } catch (e: Exception) { null }
    }

    fun resetProject() {
        prefs().edit().remove("project").apply()
        ensureBundled()
        update(_snapshot.value.copy(target = LaunchTarget.WINDSTOCK, scriptPath = ""))
        log("[app] switched to bundled Windstock project")
    }

    fun importZip(uri: android.net.Uri, onDone: (String) -> Unit) {
        log("[app] importing asset zip ...")
        Thread {
            try {
                var count = 0
                app.contentResolver.openInputStream(uri)?.use { stream ->
                    java.util.zip.ZipInputStream(stream).use { zip ->
                        var entry = zip.nextEntry
                        while (entry != null) {
                            if (!entry.isDirectory) {
                                val name = entry.name.removePrefix("assets/")
                                val target = java.io.File(projectDir, name)
                                if (target.absolutePath.startsWith(projectDir.absolutePath + java.io.File.separator)) {
                                    target.parentFile?.mkdirs()
                                    java.io.FileOutputStream(target).use { zip.copyTo(it) }
                                    count++
                                }
                            }
                            zip.closeEntry()
                            entry = zip.nextEntry
                        }
                    }
                }
                log("[app] imported $count files")
                onDone("imported $count files")
            } catch (e: Exception) {
                log("[error] import failed: ${e.message}")
                onDone("import failed: ${e.message}")
            }
        }.start()
    }

    fun importFolder(uri: android.net.Uri, onDone: (String) -> Unit) {
        log("[app] importing folder project ...")
        Thread {
            try {
                try {
                    app.contentResolver.takePersistableUriPermission(
                        uri, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
                } catch (e: Exception) { /* not required */ }
                val root = androidx.documentfile.provider.DocumentFile.fromTreeUri(app, uri)
                if (root == null) {
                    onDone("could not read that folder")
                    return@Thread
                }
                val projectRoot = java.io.File(app.filesDir, PROJECTS_DIR)
                var dest = java.io.File(projectRoot, sanitize(root.name ?: "project"))
                var suffix = 1
                while (dest.exists()) {
                    dest = java.io.File(projectRoot, "${sanitize(root.name ?: "project")}_$suffix")
                    suffix++
                }
                dest.mkdirs()

                var count = 0
                fun copyTree(node: androidx.documentfile.provider.DocumentFile, into: java.io.File) {
                    node.listFiles().forEach { child ->
                        val name = child.name ?: return@forEach
                        if (child.isDirectory) {
                            val dir = java.io.File(into, name)
                            dir.mkdirs()
                            copyTree(child, dir)
                        } else {
                            app.contentResolver.openInputStream(child.uri)?.use { input ->
                                java.io.FileOutputStream(java.io.File(into, name))
                                    .use { it.write(input.readBytes()) }
                                count++
                            }
                        }
                    }
                }
                copyTree(root, dest)
                setProjectDir(dest)
                update(_snapshot.value.copy(target = LaunchTarget.FOLDER, scriptPath = ""))
                log("[app] folder imported as '${dest.name}' ($count files)")
                onDone("project: ${dest.name} ($count files)")
            } catch (e: Exception) {
                log("[error] folder import failed: ${e.message}")
                onDone("import failed: ${e.message}")
            }
        }.start()
    }

    fun fetchWindstock(onProgress: (String) -> Unit, onDone: (String) -> Unit) {
        log("[app] fetching Windstock from GitHub ...")
        Thread {
            try {
                val zipFile = java.io.File(app.cacheDir, "windstock-src.zip")
                zipFile.delete()
                java.net.URI("https://codeload.github.com/project-windstock/windstock/zip/refs/heads/main")
                    .toURL().openStream().use { input ->
                        java.io.FileOutputStream(zipFile).use { input.copyTo(it) }
                    }
                onProgress("downloaded, unpacking ...")
                log("[app] downloaded archive (${zipFile.length() / 1024} KiB)")

                val unzipDir = java.io.File(app.cacheDir, "windstock-src")
                unzipDir.deleteRecursively()
                unzipDir.mkdirs()
                java.util.zip.ZipInputStream(java.io.FileInputStream(zipFile)).use { zip ->
                    var entry = zip.nextEntry
                    while (entry != null) {
                        if (!entry.isDirectory) {
                            val out = java.io.File(unzipDir, entry.name)
                            out.parentFile?.mkdirs()
                            java.io.FileOutputStream(out).use { zip.copyTo(it) }
                        }
                        zip.closeEntry()
                        entry = zip.nextEntry
                    }
                }
                zipFile.delete()

                val top = unzipDir.listFiles()?.firstOrNull() ?: unzipDir
                val serverDir = java.io.File(top, "src/server")
                if (!serverDir.exists()) {
                    onDone("expected src/server inside the archive")
                    return@Thread
                }
                prefs().edit().remove("project").apply()
                replaceProject(serverDir)
                java.io.File(projectDir, ".bundled").delete()
                update(_snapshot.value.copy(target = LaunchTarget.WINDSTOCK, scriptPath = ""))
                log("[app] Windstock updated from GitHub")
                onDone("Windstock updated from GitHub")
            } catch (e: Exception) {
                log("[error] fetch failed: ${e.message}")
                onDone("fetch failed: ${e.message}")
            }
        }.start()
    }

    fun installCa(context: android.content.Context): Boolean {
        val ca = caCertFile()
        if (!ca.exists()) return false
        try {
            val uri = androidx.core.content.FileProvider.getUriForFile(
                context, "${context.packageName}.fileprovider", ca)
            val intent = android.content.Intent(android.content.Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/x-x509-ca-cert")
                .addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
            context.startActivity(intent)
            return true
        } catch (e: Exception) {
            log("[error] hiding the CA file from the certificate installer: ${e.message}")
            return false
        }
    }

    private fun sanitize(name: String): String =
        name.replace(Regex("[^A-Za-z0-9._-]"), "_")
}