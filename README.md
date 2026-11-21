YTDL-ANDROID
============

YouTube video and audio downloader for Android.

This is a thin WebView shell that launches the YTDL Flask backend running
inside Termux. The APK does not contain any downloader code. It starts the
server on the phone, waits for it to become ready, and then loads the live
web UI. Downloads happen on the backend, not in the app, so closing or
restarting the app does not interrupt a running download.


FEATURES

- Live web UI inside a WebView. The downloader, playlist tools, job history
  and settings all live in your existing YTDL Flask app. The APK is just a
  launcher.
- Automatic server startup. On first launch the app asks Termux to run
  start_ytdl.sh, then polls /api/health until the server is ready.
- Restart-safe downloads. The backend keeps running in Termux independently
  of the app. Restarting the app reconnects to the same server and restores
  the full list of jobs including progress, speed, size and status.
- Multiple concurrent jobs. The frontend reconnects to the newest active job
  for live polling and leaves every other job the backend is running alone.
- Idle shutdown. The backend exits automatically after a period of no
  requests and no downloads, so Termux does not keep a Python process alive
  forever. A running download is never interrupted.
- Signed releases. GitHub Actions builds and optionally signs the APK using a
  keystore stored as a repository secret.


ARCHITECTURE

The Android app hosts a WebView and talks to the Flask backend over HTTP on
127.0.0.1:5000. On first launch it sends a RUN_COMMAND intent to Termux,
which runs start_ytdl.sh. The script launches ytdl.py with --no-browser and
--idle-shutdown. The app polls /api/health until the server responds, then
loads the web UI.

- Frontend: app/src/main/java/com/tushar/ytdl/MainActivity.kt. A single
  Activity with a WebView that boots the server, waits for health, then loads
  http://127.0.0.1:5000/.
- Backend: your existing ytdl.py plus the patches from
  backend_reference/patch_ytdl.py. The patches add a health route, an idle
  watchdog, CLI flags, persistent job state and an /api/jobs endpoint.
- Launcher: backend_reference/start_ytdl.sh. The script Termux runs.
- CI: .github/workflows/build.yml builds the debug APK on every push to main
  and uploads it as the YTDL-apk artifact.


REQUIREMENTS

- Phone running Android 7.0 or later (minSdk 24).
- Termux installed from F-Droid. The Play Store build does not work.
- Termux packages: python, ffmpeg, and whatever your ytdl.py needs such as
  yt-dlp and flask.
- Termux config: ~/.termux/termux.properties must contain the line
  allow-external-apps = true.
- Grant the com.termux.permission.RUN_COMMAND permission when the APK prompts.

Install Termux packages once:

  pkg update
  pkg install python ffmpeg
  pip install flask yt-dlp

Enable external commands once:

  mkdir -p ~/.termux
  grep -q allow-external-apps ~/.termux/termux.properties 2>/dev/null \
    || echo "allow-external-apps = true" >> ~/.termux/termux.properties


INSTALLATION

1. Install Termux from F-Droid and open it once.
2. Put your ytdl.py at /data/data/com.termux/files/home/ytdl.py.
3. Apply the Android patches. This step is idempotent.

     cd /data/data/com.termux/files/home
     python patch_ytdl.py ytdl.py

   The patcher creates a timestamped .bak backup first and refuses to write
   if it cannot find an anchor it needs.
4. Install the launcher script.

     cp start_ytdl.sh ~/start_ytdl.sh
     chmod +x ~/start_ytdl.sh

   Edit APP_FILE at the top of start_ytdl.sh if your ytdl.py is not at
   ~/ytdl.py.
5. Install the APK. Grab it from the latest CI run under Actions, then
   Build APK, then the YTDL-apk artifact.
6. On first launch, accept the Termux permission prompt. The app will start
   the server and open the UI.


BUILDING THE APK

GitHub Actions

Every push to main triggers .github/workflows/build.yml, which:

1. checks out the repo,
2. sets up JDK 17 and Gradle 8.5,
3. if KEYSTORE_B64 is set, decodes it to ytdl.p12,
4. runs gradle assembleDebug,
5. verifies the signature with apksigner verify,
6. uploads app/build/outputs/apk/debug/app-debug.apk as the YTDL-apk
   artifact.

Signing secrets go under Settings, Secrets and variables, Actions:

  KEYSTORE_B64       output of base64 -w 0 ytdl.p12 as a single line
  KEYSTORE_PASSWORD  the keystore password

If KEYSTORE_B64 is empty the workflow still succeeds and produces an unsigned
APK, so forks work out of the box. The keystore and its password are never
committed. app/build.gradle only enables the signing config when ytdl.p12
exists and KEYSTORE_PASSWORD is set.

Local build

  gradle assembleDebug
  # optional: cp ytdl.p12 . && KEYSTORE_PASSWORD=... gradle assembleDebug

Requires JDK 17 (Temurin) and Gradle 8.5.


USAGE

- Open the YTDL app. The Starting YTDL server screen is shown until
  /api/health responds, usually 5 to 30 seconds on a cold start.
- Once the server is up the web UI loads. Paste a URL, pick quality, and
  start the download.
- Closing the app or tapping Back does not stop downloads. The server keeps
  running in Termux.
- Reopening the app reconnects to the existing server and restores every job
  in the list. If the server went idle and shut down, the app restarts it
  automatically.
- When the server has been idle with no downloads for five minutes, it exits
  to save battery. The next app launch or the frontend fetch wrapper brings
  it back.

Manual or headless use

The same backend works without the APK:

  python ytdl.py --no-browser
  python ytdl.py --no-browser --idle-shutdown 300


TROUBLESHOOTING

App is stuck on Starting YTDL server
- Confirm Termux is installed from F-Droid, not the Play Store build.
- Open Termux once and check that ~/.termux/termux.properties contains
  allow-external-apps = true.
- Confirm you granted the Termux permission the APK requested. If you denied
  it, uninstall and reinstall the app or grant it from Android settings.
- Look at the server log with: cat ~/ytdl_server.log
- Tap Retry on the loading screen. The app re-probes and relaunches the
  server.

Server did not start after 60 seconds
- The 60-second budget may be tight on low-end devices if Python imports are
  slow. Start the server manually once in Termux to warm the Python cache:

    python ~/ytdl.py --no-browser &

  Then tap Retry.

Termux is not installed or is blocking the command
- Termux is either missing, was installed from the Play Store, or does not
  have allow-external-apps = true.

Downloads disappear after restarting the app
- Verify the patcher applied both layers:

    grep -c "ANDROID-APP PATCH" ~/ytdl.py      expect at least 1
    grep -c "ANDROID-APP PATCH v2" ~/ytdl.py   expect 1

- Check that ~/.ytdl_jobs.json exists and is updated while downloads run.
- Verify /api/jobs responds:

    curl -s http://127.0.0.1:5000/api/jobs | head

Downloads show interrupted by server restart
- The server was killed, for example by the OOM killer, a force-stop or
  battery optimization, while a download was running. yt-dlp died with it
  and cannot be resumed. Restart the download. To reduce the chance of this
  happening, whitelist Termux from battery optimization in Android settings.

apksigner verify fails in CI
- The decoded ytdl.p12 must be a PKCS12 keystore with alias ytdl and a
  password equal to KEYSTORE_PASSWORD, used for both store and key password.
  Re-encode with base64 -w 0 ytdl.p12 and paste it into KEYSTORE_B64 with no
  line breaks.


REPOSITORY LAYOUT

  .github/workflows/build.yml     CI: build and optionally sign the APK
  app/                            Android app
    build.gradle                  signing config, env-gated
    src/main/AndroidManifest.xml
    src/main/java/com/tushar/ytdl/MainActivity.kt
    src/main/res/                 resources
  backend_reference/
    patch_ytdl.py                 patches ytdl.py, idempotent and layered
    start_ytdl.sh                 launcher script run by Termux
  build.gradle
  settings.gradle
  gradle.properties
  README.md


LICENSE

See the upstream YTDL project for licensing.