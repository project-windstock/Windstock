package dev.windstock.app.ui

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import dev.windstock.app.server.ServerController

class WorldManagerActivity : ComponentActivity() {
    private var web: WebView? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val view = WebView(this)
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true
        view.webViewClient = WebViewClient()
        view.webChromeClient = WebChromeClient()
        web = view
        setContentView(view)
        view.loadUrl("http://127.0.0.1:8080")
        ServerController.log("[app] World Manager opened")
    }

    override fun onResume() {
        super.onResume()
        web?.loadUrl("http://127.0.0.1:8080")
        web?.onResume()
    }

    override fun onPause() {
        super.onPause()
        web?.onPause()
    }

    override fun onBackPressed() {
        if (web?.canGoBack() == true) web?.goBack() else super.onBackPressed()
    }
}