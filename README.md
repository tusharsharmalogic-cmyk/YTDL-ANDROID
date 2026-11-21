# YTDL-ANDROID

YouTube video and audio downloader and more.

Android shell (WebView) for the YTDL Flask app that runs inside Termux. The APK
does not contain the downloader itself: it starts `start_ytdl.sh` in Termux, waits
for `http://127.0.0.1:5000/api/health`, and then shows the live web UI.

## Build

Every push to `main` runs `.github/workflows/build.yml`. The APK is attached to the
run as the **YTDL-apk** artifact (`app/build/outputs/apk/debug/app-debug.apk`).

Locally: `gradle assembleDebug` with JDK 17.

## Fixed signing key (so updates install over the old app)

Add two repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `KEYSTORE_B64` | `base64 -w 0 ytdl.p12` |
| `KEYSTORE_PASSWORD` | content of the pass file (hex) |

`app/build.gradle` uses the key only when `ytdl.p12` exists and the password is set.
The keystore itself is never committed.

## Backend (`backend_reference/`)

`patch_ytdl.py` adds the app support to your `ytdl.py` (health route, idle
shutdown that never kills a running download, `--no-browser`, `--idle-shutdown`,
WebView fetch wrapper, job resume). Run it once in Termux:

```
python patch_ytdl.py /data/data/com.termux/files/home/ytdl.py
```

`start_ytdl.sh` is the script the APK launches (set `APP_FILE` at the top, then
`chmod +x`).

## Icon

Placeholder only. Replace `app/src/main/res/drawable/ic_launcher_foreground.xml`
and `app/src/main/res/values/ic_launcher_background.xml` when the real icon is ready.
