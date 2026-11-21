package com.tushar.ytdl

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.JsPromptResult
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import java.net.HttpURLConnection
import java.net.URL

/**
 * Thin shell around the Flask UI that already runs in Termux on 127.0.0.1:5000.
 * Nothing of the web app is bundled here: the WebView always loads the live server.
 */
class MainActivity : Activity() {

    private companion object {
        const val BASE = "http://127.0.0.1:5000"
        const val START_SCRIPT = "/data/data/com.termux/files/home/start_ytdl.sh"
        const val BG = 0xFF0F1115.toInt()
        const val FG = 0xFFE8EAF0.toInt()
        const val ACC = 0xFFFF4D4D.toInt()
        const val REQ_TERMUX = 1001
        const val POLL_TRIES = 50
        const val POLL_MS = 500L
    }

    private lateinit var webView: WebView
    private lateinit var loadingUi: LinearLayout
    private lateinit var statusText: TextView
    private lateinit var retryBtn: Button
    private lateinit var spinner: ProgressBar
    private val ui = Handler(Looper.getMainLooper())
    private val bootLock = Any()
    private var booting = false
    private var pageShown = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()

        if ((applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true)
        }

        if (checkSelfPermission("com.termux.permission.RUN_COMMAND") != PackageManager.PERMISSION_GRANTED) {
            try {
                requestPermissions(arrayOf("com.termux.permission.RUN_COMMAND"), REQ_TERMUX)
            } catch (e: Exception) {
                // Termux not installed: the retry message explains what to install.
            }
        }

        boot()
    }

    // ---------------------------------------------------------------- UI

    private fun buildUi() {
        val root = FrameLayout(this)
        root.setBackgroundColor(BG)

        webView = WebView(this).apply {
            setBackgroundColor(BG)
            visibility = View.GONE
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.setSupportZoom(false)
            settings.builtInZoomControls = false
            addJavascriptInterface(Bridge(), "Android")
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    pageShown = true
                    this@MainActivity.webView.visibility = View.VISIBLE
                    loadingUi.visibility = View.GONE
                }
            }
            webChromeClient = buildChromeClient()
        }
        root.addView(
            webView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )

        loadingUi = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(BG)
            setPadding(56, 56, 56, 56)
        }
        spinner = ProgressBar(this).apply { isIndeterminate = true }
        statusText = TextView(this).apply {
            text = getString(R.string.starting)
            setTextColor(FG)
            textSize = 16f
            gravity = Gravity.CENTER
            setPadding(0, 28, 0, 16)
        }
        retryBtn = Button(this).apply {
            text = getString(R.string.retry)
            setTextColor(ACC)
            visibility = View.GONE
            setOnClickListener { boot() }
        }
        loadingUi.addView(spinner)
        loadingUi.addView(statusText)
        loadingUi.addView(retryBtn)
        root.addView(
            loadingUi,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )

        setContentView(root)
    }

    private fun buildChromeClient(): WebChromeClient {
        return object : WebChromeClient() {
            override fun onJsAlert(
                view: WebView?, url: String?, message: String?, result: JsResult?
            ): Boolean {
                if (message == null || result == null) return false
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setCancelable(false)
                    .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm() }
                    .setOnCancelListener { result.cancel() }
                    .show()
                return true
            }

            override fun onJsConfirm(
                view: WebView?, url: String?, message: String?, result: JsResult?
            ): Boolean {
                if (message == null || result == null) return false
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setCancelable(false)
                    .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm() }
                    .setNegativeButton(android.R.string.cancel) { _, _ -> result.cancel() }
                    .setOnCancelListener { result.cancel() }
                    .show()
                return true
            }

            override fun onJsPrompt(
                view: WebView?,
                url: String?,
                message: String?,
                defaultValue: String?,
                result: JsPromptResult?
            ): Boolean {
                if (message == null || result == null) return false
                val input = EditText(this@MainActivity).apply {
                    setText(defaultValue ?: "")
                    inputType = InputType.TYPE_CLASS_TEXT
                    setSelection(text.length)
                }
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setView(input)
                    .setCancelable(false)
                    .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm(input.text.toString()) }
                    .setNegativeButton(android.R.string.cancel) { _, _ -> result.cancel() }
                    .setOnCancelListener { result.cancel() }
                    .show()
                return true
            }
        }
    }

    // ------------------------------------------------------------ boot flow

    private fun boot() {
        synchronized(bootLock) {
            if (booting) return
            booting = true
        }
        ui.post {
            loadingUi.visibility = View.VISIBLE
            spinner.visibility = View.VISIBLE
            retryBtn.visibility = View.GONE
            statusText.text = getString(R.string.starting)
        }

        Thread {
            var ok = health()
            if (!ok) {
                startTermuxServer()
                var i = 0
                while (i < POLL_TRIES) {
                    Thread.sleep(POLL_MS)
                    if (health()) {
                        ok = true
                        break
                    }
                    i++
                }
            }
            synchronized(bootLock) { booting = false }
            val up = ok
            ui.post {
                if (up) {
                    loadingUi.visibility = View.GONE
                    webView.visibility = View.VISIBLE
                    if (!pageShown || webView.url == null) {
                        webView.loadUrl("$BASE/?v=1")
                    }
                } else {
                    spinner.visibility = View.GONE
                    retryBtn.visibility = View.VISIBLE
                    statusText.text = getString(R.string.start_failed)
                    loadingUi.visibility = View.VISIBLE
                }
            }
        }.start()
    }

    private fun health(): Boolean {
        return try {
            val c = URL("$BASE/api/health").openConnection() as HttpURLConnection
            c.connectTimeout = 1000
            c.readTimeout = 1000
            c.useCaches = false
            val code = c.responseCode
            c.disconnect()
            code == 200
        } catch (e: Exception) {
            false
        }
    }

    private fun startTermuxServer() {
        val i = Intent("com.termux.RUN_COMMAND").apply {
            setClassName("com.termux", "com.termux.app.RunCommandService")
            putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash")
            putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arrayOf(START_SCRIPT))
            putExtra("com.termux.RUN_COMMAND_WORKDIR", "/data/data/com.termux/files/home")
            putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
        }
        try {
            startService(i)
        } catch (e: Exception) {
            ui.post {
                statusText.text = getString(R.string.termux_missing)
                retryBtn.visibility = View.VISIBLE
            }
        }
    }

    inner class Bridge {
        @JavascriptInterface
        fun ensureServer() {
            Thread {
                if (!health()) startTermuxServer()
            }.start()
        }
    }

    // ------------------------------------------------------------ lifecycle

    override fun onResume() {
        super.onResume()
        if (pageShown) {
            Thread {
                if (!health()) startTermuxServer()
            }.start()
        }
    }

    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onBackPressed() {
        if (this::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            // Downloads keep running inside Termux.
            moveTaskToBack(true)
        }
    }

    override fun onDestroy() {
        if (this::webView.isInitialized) {
            webView.destroy()
        }
        super.onDestroy()
    }
}
