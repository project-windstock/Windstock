package dev.windstock.app

import android.Manifest
import android.app.Activity.RESULT_OK
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.core.content.ContextCompat.startForegroundService
import dev.windstock.app.server.LaunchTarget
import dev.windstock.app.server.Phase
import dev.windstock.app.server.RunMode
import dev.windstock.app.server.ServerController
import dev.windstock.app.ui.WorldManagerActivity
import dev.windstock.app.vpn.PogoVpnService

private val FrostyColors = lightColorScheme(
    primary = Color(0xFF1FA6C9),
    onPrimary = Color(0xFFFFFFFF),
    secondary = Color(0xFF0FA3C7),
    onSecondary = Color(0xFFFFFFFF),
    tertiary = Color(0xFF5A7A90),
    background = Color(0xFFF6FBFF),
    onBackground = Color(0xFF0B2038),
    surface = Color(0xFFFFFFFF),
    surfaceVariant = Color(0xFFE7F3FA),
    onSurface = Color(0xFF0B2038),
    onSurfaceVariant = Color(0xFF5A7A90),
    error = Color(0xFFE5484D),
    onError = Color(0xFFFFFFFF),
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = FrostyColors) {
                MainScreen()
            }
        }
    }
}

@Composable
fun MainScreen() {
    val context = LocalContext.current
    val snapshot by ServerController.snapshot.collectAsState()
    val logs by ServerController.logs.collectAsState()
    val listState = rememberLazyListState()
    var tunnelOn by remember { mutableStateOf(false) }
    var confirmFetch by remember { mutableStateOf(false) }
    var confirmReset by remember { mutableStateOf(false) }

    val toast = { message: String ->
        Toast.makeText(context, message, Toast.LENGTH_LONG).show()
    }

    val notificationsPerm = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            notificationsPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    val zipPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        uri?.let { ServerController.importZip(it, toast) }
    }
    val folderPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree()) { uri ->
        uri?.let { ServerController.importFolder(it, toast) }
    }
    val scriptPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        uri?.let { ServerController.runScriptFrom(it, toast) }
    }
    val vpnReady = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == RESULT_OK) startTunnel(context) { tunnelOn = true }
    }

    val startServer = {
        ServerController.ensureBundled()
        ServerController.startService()
    }

    val stopServer = { ServerController.stopService() }

    val toggleVpn = {
        if (tunnelOn) {
            startForegroundService(context,
                Intent(context, PogoVpnService::class.java).setAction(PogoVpnService.ACTION_STOP))
            tunnelOn = false
        } else {
            val intent = VpnService.prepare(context)
            if (intent != null) vpnReady.launch(intent)
            else startTunnel(context) { tunnelOn = true }
        }
    }

    val openPogo = {
        val pm = context.packageManager
        val found = listOf(
            "com.nianticlabs.pokemongo",
            "com.nianticproject.pokemongo",
        ).firstOrNull { pm.getLaunchIntentForPackage(it) != null }
        if (found != null) {
            context.startActivity(pm.getLaunchIntentForPackage(found))
        } else {
            toast("Pokémon GO isn't installed")
        }
    }

    val installCa = {
        if (!ServerController.installCa(context)) {
            toast("Run the server once so the certificate is unpacked")
        }
    }

    LaunchedEffect(logs.size) {
        if (logs.isNotEmpty()) listState.animateScrollToItem(logs.size - 1)
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(listOf(Color(0xFFF8FCFF), Color(0xFFE1F0F9)))
            )
    ) {
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            Header(snapshot.target, snapshot.scriptPath, snapshot.mode, snapshot.phase)
            Spacer(Modifier.height(12.dp))

            PowerButton(
                running = snapshot.phase == Phase.RUNNING,
                enabled = snapshot.phase == Phase.IDLE || snapshot.phase == Phase.RUNNING,
                target = snapshot.target,
                onToggle = { if (snapshot.phase == Phase.RUNNING) stopServer() else startServer() }
            )

            Spacer(Modifier.height(12.dp))

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ActionCard(modifier = Modifier.weight(1f), title = "Pokémon GO",
                    subtitle = "${snapshot.port}", enabled = true,
                    locked = snapshot.phase != Phase.RUNNING) { openPogo() }
                ActionCard(modifier = Modifier.weight(1f), title = "Tunnel",
                    subtitle = if (tunnelOn) "ON" else "OFF",
                    enabled = true, locked = false) { toggleVpn() }
                ActionCard(modifier = Modifier.weight(1f), title = "World Manager",
                    subtitle = "web",
                    enabled = snapshot.phase == Phase.RUNNING, locked = false) {
                    context.startActivity(Intent(context, WorldManagerActivity::class.java))
                }
            }

            Spacer(Modifier.height(8.dp))

            if (snapshot.target == LaunchTarget.WINDSTOCK) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ModeChip("Local", snapshot.mode == RunMode.LOCAL) {
                        ServerController.setMode(RunMode.LOCAL)
                    }
                    ModeChip("LAN", snapshot.mode == RunMode.LAN) {
                        ServerController.setMode(RunMode.LAN)
                    }
                }
                Spacer(Modifier.height(8.dp))
            }

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ActionCard(modifier = Modifier.weight(1f), title = "Import .zip",
                    subtitle = "assets", enabled = true, locked = false) {
                    zipPicker.launch(arrayOf("application/zip", "*/*"))
                }
                ActionCard(modifier = Modifier.weight(1f), title = "Use folder",
                    subtitle = "project", enabled = true, locked = false) {
                    folderPicker.launch(null)
                }
                ActionCard(modifier = Modifier.weight(1f), title = "Fetch repo",
                    subtitle = "GitHub", enabled = true, locked = false) {
                    confirmFetch = true
                }
            }

            Spacer(Modifier.height(8.dp))

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ActionCard(modifier = Modifier.weight(1f), title = "Run .py",
                    subtitle = "script", enabled = true, locked = false) {
                    scriptPicker.launch(arrayOf("text/x-python", "application/octet-stream", "*/*"))
                }
                ActionCard(modifier = Modifier.weight(1f), title = "Install CA",
                    subtitle = "trust", enabled = true, locked = false) { installCa() }
                ActionCard(modifier = Modifier.weight(1f), title = "Reset bundle",
                    subtitle = "built-in", enabled = true, locked = false) {
                    confirmReset = true
                }
            }

            Spacer(Modifier.height(12.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "console",
                    color = MaterialTheme.colorScheme.secondary,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                    letterSpacing = 2.sp,
                    modifier = Modifier.weight(1f),
                )
                if (logs.isNotEmpty()) {
                    TextButton(onClick = { ServerController.clearLogs() }) {
                        Text("clear", fontSize = 11.sp)
                    }
                }
            }
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = Color(0x1A607E96))
            ) {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxWidth().height(240.dp).padding(10.dp),
                ) {
                    if (logs.isEmpty()) {
                        item { ConsoleLine("— start the server for live output —", Color(0xFF6B87A0)) }
                    }
                    items(logs.size) { index ->
                        val line = logs[index]
                        ConsoleLine(
                            line,
                            if (line.startsWith("[error]") || line.startsWith("[fatal]"))
                                MaterialTheme.colorScheme.error
                            else if (line.startsWith("[app]") || line.startsWith("[boot]"))
                                MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
        }
    }

    if (confirmFetch) {
        AlertDialog(
            onDismissRequest = { confirmFetch = false },
            title = { Text("Fetch Windstock updates?") },
            text = { Text("Re-downloads the server source from GitHub and replaces the project files. Your saved data and certificates are kept.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmFetch = false
                    ServerController.fetchWindstock(toast, toast)
                }) { Text("Fetch") }
            },
            dismissButton = {
                TextButton(onClick = { confirmFetch = false }) { Text("Cancel") }
            },
        )
    }

    if (confirmReset) {
        AlertDialog(
            onDismissRequest = { confirmReset = false },
            title = { Text("Reset to bundled Windstock?") },
            text = { Text("Switches back to the preloaded Windstock project, ignoring any folder project you selected.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmReset = false
                    ServerController.resetProject()
                }) { Text("Reset") }
            },
            dismissButton = {
                TextButton(onClick = { confirmReset = false }) { Text("Cancel") }
            },
        )
    }
}

private fun startTunnel(context: Context, onStarted: () -> Unit) {
    startForegroundService(context,
        Intent(context, PogoVpnService::class.java).setAction(PogoVpnService.ACTION_START))
    onStarted()
}

@Composable
private fun Header(target: LaunchTarget, scriptPath: String, mode: RunMode, phase: Phase) {
    val (title, subtitle) = when (target) {
        LaunchTarget.WINDSTOCK ->
            "WINDSTOCK" to "0.29 private server · runs on this phone"
        LaunchTarget.FOLDER ->
            "FOLDER PYTHON" to "runs the project entry point ❄️"
        LaunchTarget.SCRIPT ->
            "RUN SCRIPT" to "python · ${scriptPath.substringAfterLast('/')}"
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(
                title,
                color = Color(0xFF0FA3C7),
                fontSize = 28.sp,
                fontWeight = FontWeight.Black,
                letterSpacing = 3.sp,
            )
            Text(
                subtitle,
                color = Color(0xFF5A7A90),
                fontSize = 12.sp,
            )
        }
        Column(horizontalAlignment = Alignment.End) {
            StatusPill(phase)
            Spacer(Modifier.height(2.dp))
            Text(
                if (target == LaunchTarget.WINDSTOCK) mode.name.lowercase() else target.name.lowercase(),
                color = MaterialTheme.colorScheme.secondary,
                fontSize = 11.sp,
                letterSpacing = 1.5.sp,
            )
        }
    }
}

@Composable
private fun StatusPill(phase: Phase) {
    val (label, color) = when (phase) {
        Phase.IDLE -> "IDLE" to Color(0xFF5A7A90)
        Phase.PREPARING -> "STARTING..." to Color(0xFFFFB054)
        Phase.RUNNING -> "LIVE" to Color(0xFF2FBF71)
        Phase.STOPPING -> "STOPPING" to Color(0xFFFFB054)
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.background(color.copy(alpha = 0.15f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
    ) {
        Box(
            Modifier.size(8.dp).background(color)
        )
        Spacer(Modifier.width(6.dp))
        Text(label, color = color, fontSize = 11.sp, fontWeight = FontWeight.Bold,
            letterSpacing = 1.sp)
    }
}

@Composable
private fun PowerButton(running: Boolean, enabled: Boolean, target: LaunchTarget,
                        onToggle: () -> Unit) {
    val bg = if (running) Color(0xFFFF6B76) else Color(0xFF1FA6C9)
    val fg = Color(0xFFFFFFFF)
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(enabled = enabled, onClick = onToggle),
        colors = CardDefaults.cardColors(containerColor = bg),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(vertical = 18.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            val start = if (target == LaunchTarget.WINDSTOCK) "▶  START SERVER" else "▶  RUN PYTHON"
            val stop = if (target == LaunchTarget.WINDSTOCK) "■  STOP SERVER" else "■  STOP"
            Text(
                if (running) stop else start,
                color = fg,
                fontSize = 20.sp,
                fontWeight = FontWeight.Black,
                letterSpacing = 1.sp,
            )
            if (!running) {
                Text(
                    when (target) {
                        LaunchTarget.WINDSTOCK ->
                            "extracts files, installs dependencies, serves on port 443"
                        LaunchTarget.FOLDER ->
                            "detects the entry point and runs it right here 🧊"
                        LaunchTarget.SCRIPT ->
                            "runs the chosen .py file natively in CPython 3.11"
                    },
                    color = fg.copy(alpha = 0.85f),
                    fontSize = 11.sp,
                )
            }
        }
    }
}

@Composable
private fun ActionCard(modifier: Modifier = Modifier, title: String, subtitle: String,
                       enabled: Boolean, locked: Boolean, onClick: () -> Unit) {
    Card(
        modifier = modifier.clickable(enabled = enabled, onClick = onClick),
        colors = CardDefaults.cardColors(
            containerColor = if (enabled and !locked) MaterialTheme.colorScheme.surfaceVariant
            else Color(0xFFE3EDF4)),
    ) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 12.dp)) {
            Text(
                title + (if (locked) " 🔒" else ""),
                color = if (enabled) MaterialTheme.colorScheme.onSurface
                else Color(0xFF9FB3C3),
                fontSize = 13.sp,
                fontWeight = FontWeight.Bold,
            )
            Text(
                subtitle,
                color = if (enabled) MaterialTheme.colorScheme.primary
                else Color(0xFF9FB3C3),
                fontSize = 11.sp,
            )
        }
    }
}

@Composable
private fun ModeChip(label: String, selected: Boolean, onClick: () -> Unit) {
    val color = if (selected) Color(0xFF1FA6C9) else Color(0xFFD4E8F2)
    val text = if (selected) Color(0xFFFFFFFF) else Color(0xFF5A7A90)
    Box(
        Modifier.clickable(enabled = ServerController.snapshot.value.phase == Phase.IDLE,
                onClick = onClick)
            .background(color)
            .padding(horizontal = 16.dp, vertical = 6.dp),
    ) {
        Text(label, color = text, fontSize = 12.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun ConsoleLine(line: String, color: Color) {
    Text(
        line,
        color = color,
        fontFamily = FontFamily.Monospace,
        fontSize = 11.sp,
        lineHeight = 15.sp,
    )
}