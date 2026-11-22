#!/data/data/com.termux/files/usr/bin/bash
# Started by the YTDL APK through Termux RUN_COMMAND.
# Set APP_FILE to the full path of your ytdl.py on the phone.
APP_FILE="/data/data/com.termux/files/home/ytdl.py"

cd "$(dirname "$APP_FILE")" || exit 1
termux-wake-lock 2>/dev/null
nohup python "$APP_FILE" --no-browser --idle-shutdown 30 > /data/data/com.termux/files/home/ytdl_server.log 2>&1 &
SRV=$!
( while kill -0 "$SRV" 2>/dev/null; do sleep 2; done; termux-wake-unlock ) >/dev/null 2>&1 &
