package com.tushar.ytdl

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.util.Log
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
 * Thin shell around the Flask UI that runs in Termux on 127.0.0.1:5000.
 *
 * Startup contract:
 *  - Probe /api/health first. If it responds, the server is already up (the
 *    user brought the app back to the foreground after a previous session,
 *    or Termux is still running it) and we go straight to the WebView.
 *  - Otherwise ask Termux to run start_ytdl.sh and poll /api/health until it
 *    responds or the boot budget (~60s) runs out.
 *  - The whole boot is wrapped in try/finally so the UI always ends in a
 *    usable state, even if the thread dies mid-way. Previously an exception
 *    inside the boot thread left `booting=true` forever, freezing the
 *    "Starting YTDL server…" screen with no retry button.
 *  - The Termux permission dialog is awaited before boot(), so we never race
 *    startTermuxServer() against a permission that has not been granted yet.
 */
class MainActivity : Activity() {

    private companion object {
        const val TAG = "YTDL"
        const val BASE = "http://127.0.0.1:5000"
        const val START_SCRIPT = "/data/data/com.termux/files/home/start_ytdl.sh"
        const val BG = 0xFF0F1115.toInt()
        const val FG = 0xFFE8EAF0.toInt()
        const val ACC = 0xFFFF4D4D.toInt()
        const val REQ_TERMUX = 1001
        // 120 * 500ms = 60s — Termux + Python cold start can easily take 20-40s.
        const val POLL_TRIES = 120
        const val POLL_MS = 500L
        // Extra pause so Termux has actually spawned the process before we probe.
        const val TERMUX_WARMUP_MS = 1200L
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

        val granted = checkSelfPermission("com.termux.permission.RUN_COMMAND") ==
                PackageManager.PERMISSION_GRANTED
        if (!granted) {
            try {
                requestPermissions(arrayOf("com.termux.permission.RUN_COMMAND"), REQ_TERMUX)
            } catch (e: Exception) {
                // Termux is not installed, or the permission is unknown.
                // Boot anyway — the loading screen will surface the message.
                Log.w(TAG, "requestPermissions failed", e)
                boot()
            }
        } else {
            boot()
        }
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

                override fun onReceivedError(
                    view: WebView?,
                    errorCode: Int,
                    description: String?,
                    failingUrl: String?
                ) {
                    super.onReceivedError(view, errorCode, description, failingUrl)
                    Log.w(TAG, "WebView error $errorCode: $description ($failingUrl)")
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
            setOnClickListener {
                Log.i(TAG, "retry tapped")
                boot()
            }
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

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_TERMUX) {
            val granted = grantResults.isNotEmpty() &&
                    grantResults[0] == PackageManager.PERMISSION_GRANTED
            Log.i(TAG, "RUN_COMMAND permission granted=$granted")
            // Boot now that the permission state is settled. If the user
            // denied, the loading screen will explain what to fix and Retry
            // stays available.
            boot()
        }
    }

    private fun boot() {
        synchronized(bootLock) {
            if (booting) {
                Log.d(TAG, "boot ignored: already booting")
                return
            }
            booting = true
        }
        Log.i(TAG, "boot: probing /api/health")

        ui.post {
            loadingUi.visibility = View.VISIBLE
            spinner.visibility = View.VISIBLE
            retryBtn.visibility = View.GONE
            statusText.text = getString(R.string.starting)
            if (!pageShown) {
                webView.visibility = View.GONE
            }
        }

        Thread {
            var up = false
            try {
                up = health()
                Log.d(TAG, "initial health=$up")
                if (!up) {
                    startTermuxServer()
                    // Give Termux a moment to actually spawn the process.
                    sleepQuietly(TERMUX_WARMUP_MS)
                    var i = 0
                    while (i < POLL_TRIES) {
                        if (health()) {
                            up = true
                            Log.d(TAG, "health up after ${i * POLL_MS}ms")
                            break
                        }
                        sleepQuietly(POLL_MS)
                        i++
                    }
                }
            } catch (t: Throwable) {
                // Any unexpected throwable (Interrupted, OOM, IllegalState…)
                // must not leave the UI frozen on the "Starting" overlay.
                Log.e(TAG, "boot failed", t)
                up = false
            } finally {
                synchronized(bootLock) { booting = false }
                val finalUp = up
                ui.post { onBootResult(finalUp) }
            }
        }.start()
    }

    private fun onBootResult(up: Boolean) {
        Log.i(TAG, "onBootResult up=$up pageShown=$pageShown")
        if (up) {
            spinner.visibility = View.GONE
            retryBtn.visibility = View.GONE
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

    private fun sleepQuietly(ms: Long) {
        try {
            Thread.sleep(ms)
        } catch (_: InterruptedException) {
        }
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

    /**
     * Builds a one-shot bash command that unpacks the bundled (already patched)
     * ytdl.py from the APK assets into Termux's home and starts the server.
     * The file travels inside the intent as gzip+base64, so no script has to be
     * copied into Termux by hand. Falls back to ~/start_ytdl.sh if the asset
     * cannot be read.
     */
    private fun buildBootCommand(): Array<String> {
        return try {
            val raw = assets.open("ytdl.py").use { it.readBytes() }
            val bos = java.io.ByteArrayOutputStream()
            java.util.zip.GZIPOutputStream(bos).use { it.write(raw) }
            val b64 = android.util.Base64.encodeToString(bos.toByteArray(), android.util.Base64.NO_WRAP)
            val home = "/data/data/com.termux/files/home"
            val cmd = "cd $home || exit 1; " +
                "echo '$b64' | base64 -d | gzip -dc > ytdl.py.tmp && mv ytdl.py.tmp ytdl.py || exit 1; " +
                "termux-wake-lock 2>/dev/null; " +
                "nohup python $home/ytdl.py --no-browser --idle-shutdown 300 > $home/ytdl_server.log 2>&1 & " +
                "( while pgrep -f $home/ytdl.py >/dev/null; do sleep 5; done; termux-wake-unlock ) >/dev/null 2>&1 &"
            Log.i(TAG, "bundled ytdl.py: ${raw.size} bytes, command ${cmd.length} chars")
            arrayOf("-c", cmd)
        } catch (e: Exception) {
            Log.w(TAG, "asset ytdl.py unavailable, falling back to $START_SCRIPT", e)
            arrayOf(START_SCRIPT)
        }
    }

    private fun startTermuxServer() {
        Log.i(TAG, "startTermuxServer: bundled backend")
        val i = Intent("com.termux.RUN_COMMAND").apply {
            setClassName("com.termux", "com.termux.app.RunCommandService")
            putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash")
            putExtra("com.termux.RUN_COMMAND_ARGUMENTS", buildBootCommand())
            putExtra("com.termux.RUN_COMMAND_WORKDIR", "/data/data/com.termux/files/home")
            putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
        }
        try {
            startService(i)
        } catch (e: Exception) {
            Log.w(TAG, "startService(RUN_COMMAND) failed", e)
            ui.post {
                statusText.text = getString(R.string.termux_missing)
                spinner.visibility = View.GONE
                retryBtn.visibility = View.VISIBLE
            }
        }
    }

    inner class Bridge {
        @JavascriptInterface
        fun ensureServer() {
            Log.d(TAG, "JS requested ensureServer")
            Thread {
                if (!health()) startTermuxServer()
            }.start()
        }
    }

    // ------------------------------------------------------------ lifecycle

    override fun onResume() {
        super.onResume()
        // If the server died since we were last visible (e.g. the idle
        // watchdog fired while nothing was downloading), bring it back so
        // the user sees a live UI rather than a blank page.
        if (pageShown) {
            Thread {
                if (!health()) {
                    Log.i(TAG, "onResume: server down, re-booting")
                    ui.post { boot() }
                }
            }.start()
        }
    }

    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onBackPressed() {
        if (this::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            // Downloads keep running inside Termux; do not kill them.
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