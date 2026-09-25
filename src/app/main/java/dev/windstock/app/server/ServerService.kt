package dev.windstock.app.server

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import dev.windstock.app.R

class ServerService : Service() {

    companion object {
        private const val CHANNEL = "windstock_server"
        private const val NOTIFICATION_ID = 443

        fun start(context: Context) {
            val intent = Intent(context, ServerService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ServerService::class.java))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForegroundCompat()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Thread { runServer() }.start()
        return START_NOT_STICKY
    }

    private fun startForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.server_channel_name),
                    NotificationManager.IMPORTANCE_LOW)
            )
        }
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.server_notification_title))
            .setContentText(getString(R.string.server_notification_text))
            .setOngoing(true)
            .build()
        startForeground(NOTIFICATION_ID, notification)
    }

    private fun runServer() {
        ServerController.ensureBundled()
        ServerController.update(
            ServerController.snapshot.value.copy(phase = Phase.PREPARING, error = null))

        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            val boot = Python.getInstance().getModule("windstock_android.boot")
            val sink = LogSink()

            boot.callAttr("set_sink", sink)
            val s = ServerController.snapshot.value
            boot.callAttr(
                "configure",
                ServerController.projectDir.absolutePath,
                s.mode.name.lowercase(),
                s.port,
                s.target.name.lowercase(),
                s.scriptPath,
            )
            val prep = boot.callAttr("prepare").toString()
            ServerController.log("[boot] $prep")

            val result = boot.callAttr("run").toString()
            if (result == "started") {
                ServerController.update(
                    ServerController.snapshot.value.copy(
                        phase = Phase.RUNNING,
                        detail = when (ServerController.snapshot.value.target) {
                            LaunchTarget.WINDSTOCK ->
                                "https://0.0.0.0:${ServerController.snapshot.value.port}"
                            LaunchTarget.FOLDER -> "running folder project"
                            LaunchTarget.SCRIPT -> "running python script"
                        }))
            } else {
                fail(result)
            }
        } catch (e: Exception) {
            fail(e.message ?: e.toString())
        }
    }

    private fun fail(message: String) {
        ServerController.log("[error] $message")
        ServerController.update(
            ServerController.snapshot.value.copy(phase = Phase.IDLE, error = message))
        stopSelf()
    }

    override fun onDestroy() {
        try {
            val boot = Python.getInstance().getModule("windstock_android.boot")
            boot.callAttr("stop")
        } catch (e: Exception) {
            ServerController.log("[app] shutdown: ${e.message}")
        }
        ServerController.update(
            ServerController.snapshot.value.copy(phase = Phase.IDLE, detail = ""))
        super.onDestroy()
    }
}