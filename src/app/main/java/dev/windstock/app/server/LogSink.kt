package dev.windstock.app.server

class LogSink {
    @JvmName("onLine")
    fun onLine(line: String) = ServerController.log(line)

    @JvmName("onNotice")
    fun onNotice(line: String) = ServerController.log("[app] $line")
}