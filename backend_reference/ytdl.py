#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube Downloader (Web UI) - By Tushar - v4.0
Termux me chalane ke liye:
    pkg install python ffmpeg aria2 -y
    pip install flask yt-dlp
    python app.py
Server start hote hi browser khud khul jayega (http://127.0.0.1:5000)
"""

import os
import re
import sys
import json
import time
import uuid
import shlex
import shutil
import threading
import subprocess
import webbrowser

from flask import Flask, request, jsonify, Response

# ===================== CONFIG =====================
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "5000"))

BASE_DIR = "/storage/emulated/0/Download/NA"
STATE_DIR = "/sdcard"  # playlists + history yahin save hongi (purani script jaisi)

# Storage fallback (script jaisa)
try:
    os.makedirs(BASE_DIR, exist_ok=True)
except Exception:
    BASE_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(BASE_DIR, exist_ok=True)

if not (os.path.isdir(STATE_DIR) and os.access(STATE_DIR, os.W_OK)):
    STATE_DIR = os.path.expanduser("~")

SAVED_PLAYLISTS_FILE = os.path.join(STATE_DIR, ".ytdl_playlists.txt")
# Format: nickname|url|default_limit|default_quality

if shutil.which("yt-dlp"):
    YTDLP = ["yt-dlp"]
else:
    YTDLP = [sys.executable, "-m", "yt_dlp"]

app = Flask(__name__)


# ===================== HELPERS =====================
def history_file(nick):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", nick or "")
    return os.path.join(STATE_DIR, ".ytdl_history_%s.txt" % safe)


def history_count(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
    except Exception:
        pass
    return 0


def load_playlists():
    items = []
    if os.path.isfile(SAVED_PLAYLISTS_FILE):
        with open(SAVED_PLAYLISTS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                p = line.split("|")
                while len(p) < 4:
                    p.append("")
                items.append({
                    "nick": p[0],
                    "url": p[1],
                    "limit": int(p[2]) if p[2].isdigit() else 0,
                    "quality": p[3],
                })
    return items


def write_playlists(items):
    with open(SAVED_PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        for it in items:
            f.write("%s|%s|%s|%s\n" % (it["nick"], it["url"], it["limit"] or 0, it["quality"] or ""))


def safe_name(s, maxlen=100):
    s = re.sub(r"[\\/:|]", "-", s or "")
    s = re.sub(r'[*?"<>\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen].strip(" .") or "chapter"


def esc_tpl(s):
    """yt-dlp output template me % ko escape karo."""
    return s.replace("%", "%%")


def fmt_t(t):
    t = float(t)
    if t == int(t):
        return str(int(t))
    return ("%.3f" % t).rstrip("0").rstrip(".")


def fetch_total(url):
    try:
        r = subprocess.run(
            YTDLP + ["--flat-playlist", "--print", "%(playlist_count)s",
                     "--playlist-items", "1", "--no-warnings", url],
            capture_output=True, text=True, timeout=120)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def fetch_chapters(url):
    best = None
    last_err = "yt-dlp error"
    attempts = [[], ["--extractor-args", "youtube:player_client=android,web"]]
    for extra in attempts:
        try:
            r = subprocess.run(
                YTDLP + ["--no-playlist", "--skip-download", "--dump-single-json",
                         "--no-warnings"] + extra + [url],
                capture_output=True, text=True, timeout=180)
        except Exception as e:
            last_err = str(e)
            continue
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            dur = data.get("duration")
            chs = []
            for c in data.get("chapters") or []:
                s = c.get("start_time") or 0
                e = c.get("end_time")
                if e is None:
                    e = dur
                if e is None:
                    continue
                chs.append({
                    "title": c.get("title") or ("Chapter %d" % (len(chs) + 1)),
                    "start": s,
                    "end": e,
                })
            res = {"title": data.get("title") or "video", "duration": dur, "chapters": chs}
            if chs:
                return res
            if best is None:
                best = res
        else:
            err = (r.stderr or "").strip().splitlines()
            if err:
                last_err = err[-1]
    if best is not None:
        return best
    raise RuntimeError(last_err)


# ===================== JOBS =====================
JOBS = {}

PROG_RE = re.compile(r"\[download\]\s+([\d.]+)%")
ITEM_RE = re.compile(r"Downloading (?:item|video) (\d+) of (\d+)")
DEST_RE = re.compile(r"\[download\] Destination: (.+)")


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:8]
        self.status = "running"
        self.finished = False
        self.log = []
        self.percent = 0.0
        self.file_pct = 0.0
        self.current = ""
        self.msg = ""
        self.proc = None
        self.stop = False
        self.item_i = 0
        self.item_n = 0
        self.ch_i = 0
        self.ch_n = 0
        self.last_prog = False

    def add(self, line, prog=False):
        if prog and self.last_prog and self.log:
            self.log[-1] = line
        else:
            self.log.append(line)
        self.last_prog = prog
        if len(self.log) > 600:
            del self.log[:100]

    def recalc(self):
        if self.ch_n:
            p = ((self.ch_i + self.file_pct / 100.0) / self.ch_n) * 100
        elif self.item_n:
            p = ((self.item_i - 1 + self.file_pct / 100.0) / self.item_n) * 100
        else:
            p = self.file_pct
        self.percent = max(0.0, min(100.0, p))


def run_cmd(job, cmd):
    shown = " ".join(shlex.quote(c) for c in cmd)
    job.add("$ " + (shown if len(shown) < 400 else shown[:400] + " ..."))
    job.file_pct = 0.0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)
    job.proc = proc
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        m = ITEM_RE.search(line)
        if m:
            job.item_i, job.item_n = int(m.group(1)), int(m.group(2))
            job.file_pct = 0.0
            job.current = "Video %s / %s" % (m.group(1), m.group(2))
        m = DEST_RE.search(line)
        if m and not job.ch_n:
            job.current = os.path.basename(m.group(1))[:80]
        m = PROG_RE.search(line)
        if m:
            job.file_pct = float(m.group(1))
            job.recalc()
            job.add(line, True)
        else:
            job.add(line)
    proc.wait()
    job.proc = None
    return proc.returncode


def fix_mp4(job, path, dur=None):
    """Cut hue mp4 ko ffmpeg se dobara 'clean' mp4 banata hai (re-encode nahi),
    taaki player me duration sahi dikhe aur seek karne par peeche na jaye."""
    if not shutil.which("ffmpeg") or not os.path.isfile(path):
        return False
    tmp = path[:-4] + ".fix.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy",
           "-avoid_negative_ts", "make_zero", "-fflags", "+genpts",
           "-movflags", "+faststart"]
    if dur and dur > 0:
        cmd += ["-t", fmt_t(dur)]
    cmd.append(tmp)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            job.add("🔧 Duration fix: %s" % os.path.basename(path))
            return True
        job.add("⚠️ Duration fix fail: %s" % ((r.stderr or "").strip()[-150:]))
    except Exception as e:
        job.add("⚠️ Duration fix error: %s" % e)
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


def build_base(o, aria_args):
    ex = o.get("extras") or {}
    cmd = YTDLP + ["--progress", "--newline", "-c", "--no-warnings",
                   "--extractor-args", "youtube:player_client=android,web"]
    if ex.get("aria2"):
        cmd += ["--downloader", "aria2c", "--downloader-args", aria_args]
    if ex.get("subs"):
        cmd += ["--write-subs", "--write-auto-subs", "--embed-subs", "--sub-langs", "en,hi"]
    if ex.get("sponsor"):
        cmd += ["--sponsorblock-remove", "all"]
    if ex.get("thumb"):
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    if ex.get("meta"):
        cmd += ["--add-metadata", "--embed-metadata"]
    if ex.get("cookies"):
        cf = os.path.join(os.path.expanduser("~"), "cookies.txt")
        if os.path.isfile(cf):
            cmd += ["--cookies", cf]
    return cmd


def media_args(o, for_chapter=False):
    """Audio/Video ke format wale arguments. (args, suffix) return karta hai."""
    if o["type"] == "audio":
        args = ["-x", "--audio-format", o.get("audio_format", "mp3")]
        if o.get("audio_quality"):
            args += ["--audio-quality", o["audio_quality"]]
        return args, ""
    res = int(o.get("res") or 720)
    if o.get("video_mode") == "advance":
        fps = int(o.get("fps") or 30)
        fmt = "bv*[height<=%d][fps<=%d]+ba" % (res, fps)
        suffix = "_%dp_%dfps" % (res, fps)
    else:
        fmt = "best[height<=%d]" % res
        suffix = "_%dp" % res
    return ["-f", fmt, "--merge-output-format", "mp4", "--remux-video", "mp4"], suffix


def ensure_aria2(job, o):
    ex = o.get("extras") or {}
    if ex.get("aria2") and not shutil.which("aria2c"):
        job.add("⚠️ aria2c nahi mila, install kar raha hu...")
        try:
            subprocess.run(["pkg", "install", "aria2", "-y"], capture_output=True, timeout=300)
        except Exception:
            pass
        if not shutil.which("aria2c"):
            job.add("❌ aria2 install nahi hua, normal download chalega")
            ex["aria2"] = False


def finish(job, status, msg):
    job.status = status
    job.msg = msg
    job.finished = True
    job.proc = None


def rc_result(job, rc):
    if job.stop:
        finish(job, "stopped", "⏹ Stopped")
    elif rc == 0:
        finish(job, "done", "✅ Download complete!")
    elif rc == 101:
        finish(job, "done", "✅ Session cap tak download ho gaya")
    else:
        finish(job, "error", "⚠️ Download ended (error ya kuch videos fail hue)")


# ---------- Chapters mode ----------
def run_chapters(job, o):
    url = o["url"]
    chs = o["chapters"]
    vtitle = safe_name(o.get("video_title") or "video")
    folder = os.path.join(BASE_DIR, vtitle)
    os.makedirs(folder, exist_ok=True)

    job.ch_n = len(chs)
    used = {}
    ok = 0
    ext_hint = o.get("audio_format", "mp3") if o["type"] == "audio" else "mp4"
    aria = "aria2c:-x4 -s4 -k1M" if o["type"] == "audio" else "aria2c:-x16 -s16 -k1M"

    for i, ch in enumerate(chs):
        if job.stop:
            break
        job.ch_i = i
        name = safe_name(ch["title"])
        key = name.lower()
        used[key] = used.get(key, 0) + 1
        if used[key] > 1:
            name = "%s (%d)" % (name, used[key])

        job.current = "Chapter %d/%d: %s" % (i + 1, len(chs), name)
        job.add("━━ %s ━━" % job.current)

        margs, _suffix = media_args(o, True)
        cmd = build_base(o, aria)
        cmd += margs
        cmd += ["--no-playlist",
                "--download-sections", "*%s-%s" % (fmt_t(ch["start"]), fmt_t(ch["end"]))]
        if o.get("accurate"):
            cmd += ["--force-keyframes-at-cuts"]
        out = os.path.join(esc_tpl(folder), esc_tpl(name)) + ".%(ext)s"
        cmd += ["-o", out, url]

        rc = run_cmd(job, cmd)
        if rc == 0:
            ok += 1
            if o["type"] == "video":
                job.current = "Duration fix: %s" % name
                fix_mp4(job, os.path.join(folder, name + ".mp4"),
                        None if o.get("accurate") else ch["end"] - ch["start"])
            job.add("✅ %s.%s" % (name, ext_hint))
        elif not job.stop:
            job.add("❌ Chapter fail hua: %s" % name)
        job.ch_i = i + 1
        job.file_pct = 0.0
        job.recalc()

    if job.stop:
        finish(job, "stopped", "⏹ Stopped (%d/%d chapters ho chuke)" % (ok, len(chs)))
    elif ok == len(chs):
        finish(job, "done", "✅ %d/%d chapters download ho gaye\n📁 %s" % (ok, len(chs), folder))
    else:
        finish(job, "error", "⚠️ %d/%d chapters download hue\n📁 %s" % (ok, len(chs), folder))
    job.percent = 100.0 if ok == len(chs) else job.percent


# ---------- Normal / Playlist mode ----------
def run_normal(job, o):
    url = o["url"]
    is_pl = "list=" in url
    pl = o.get("playlist") or {}
    nick = o.get("nick") or ""
    hist = history_file(nick) if (is_pl and nick) else None
    total = None
    range_opts = []
    cap = 0

    if is_pl:
        mode = pl.get("mode", "normal")
        if mode == "reverse":
            pool = max(1, int(pl.get("pool") or 1))
            range_opts = ["--playlist-reverse", "--playlist-end", str(pool)]
        elif mode == "range":
            ps = max(1, int(pl.get("start") or 1))
            pe = max(ps, int(pl.get("end") or ps))
            range_opts = ["--playlist-start", str(ps), "--playlist-end", str(pe)]
        elif mode == "smart":
            job.current = "Playlist count fetch ho raha hai..."
            total = fetch_total(url)
            if total is None:
                finish(job, "error", "❌ Total count fetch nahi hua, Smart mode nahi chal sakta")
                return
            done = history_count(hist)
            if not hist:
                job.add("ℹ️ Playlist saved nahi hai, history 0 maani gayi")
            remaining = total - done
            if remaining <= 0:
                finish(job, "done", "✅ Sab videos already download ho chuke hain (%d/%d)" % (done, total))
                job.percent = 100.0
                return
            limit = max(1, int(pl.get("limit") or 1))
            start = remaining
            end = max(1, remaining - limit + 1)
            range_opts = ["--playlist-items", "%d:%d:-1" % (start, end)]
            job.add("🤖 Smart: Total %d | Done %d | Remaining %d" % (total, done, remaining))
            job.add("   Video #%d se #%d tak (%d videos, reverse)" % (start, end, start - end + 1))
        if mode != "smart":
            cap = max(0, int(pl.get("cap") or 0))

    margs, suffix = media_args(o)
    aria = "aria2c:-x4 -s4 -k1M" if o["type"] == "audio" else "aria2c:-x16 -s16 -k1M"
    cmd = build_base(o, aria)
    cmd += margs

    # custom trim (audio ya advance video)
    ts, te = o.get("trim_start"), o.get("trim_end")
    if ts is not None and te is not None:
        cmd += ["--download-sections", "*%s-%s" % (fmt_t(ts), fmt_t(te))]

    cmd += range_opts
    if is_pl:
        if hist:
            cmd += ["--download-archive", hist]
        if cap > 0:
            cmd += ["--max-downloads", str(cap)]
        tpl = os.path.join(esc_tpl(BASE_DIR), "%(playlist_title)s", "%(title).100B" + suffix + ".%(ext)s")
    else:
        tpl = os.path.join(esc_tpl(BASE_DIR), "%(title).100B" + suffix + ".%(ext)s")
    cmd += ["-o", tpl, url]

    job.current = "Download shuru..."
    t0 = time.time() - 2
    rc = run_cmd(job, cmd)
    if o["type"] == "video" and ts is not None and te is not None and not job.stop:
        for root, _d, files in os.walk(BASE_DIR):
            for fn in files:
                p = os.path.join(root, fn)
                if fn.lower().endswith(".mp4") and not fn.endswith(".fix.mp4") \
                        and os.path.getmtime(p) >= t0:
                    fix_mp4(job, p, te - ts)
    if hist and os.path.isfile(hist):
        job.add("📋 Total downloaded (all time): %d%s" % (
            history_count(hist), ("/%d" % total) if total else ""))
    rc_result(job, rc)
    if job.status == "done":
        job.percent = 100.0
        job.msg += "\n📁 " + BASE_DIR


def job_main(job, o):
    try:
        ensure_aria2(job, o)
        if o.get("chapters"):
            run_chapters(job, o)
        else:
            run_normal(job, o)
    except Exception as e:
        job.add("❌ %s" % e)
        finish(job, "error", "❌ Error: %s" % e)
    finally:
        job.finished = True


# ===================== API =====================
@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/playlists", methods=["GET", "POST"])
def api_playlists():
    if request.method == "GET":
        return jsonify(load_playlists())
    d = request.get_json(force=True) or {}
    nick = (d.get("nick") or "").replace("|", "").strip() or ("playlist_%d" % int(time.time()))
    url = (d.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Valid URL daalo"}), 400
    limit = d.get("limit")
    limit = int(limit) if str(limit).isdigit() else 0
    quality = str(d.get("quality") or "")
    if quality and not quality.isdigit():
        quality = ""
    items = [p for p in load_playlists() if p["nick"] != nick]
    items.append({"nick": nick, "url": url, "limit": limit, "quality": quality})
    write_playlists(items)
    return jsonify({"ok": True, "nick": nick})


@app.route("/api/playlists/delete", methods=["POST"])
def api_playlists_delete():
    d = request.get_json(force=True) or {}
    nick = d.get("nick") or ""
    items = [p for p in load_playlists() if p["nick"] != nick]
    write_playlists(items)
    return jsonify({"ok": True})


@app.route("/api/playlist_info", methods=["POST"])
def api_playlist_info():
    d = request.get_json(force=True) or {}
    url = (d.get("url") or "").strip()
    nick = d.get("nick") or ""
    total = fetch_total(url)
    done = history_count(history_file(nick)) if nick else 0
    return jsonify({"total": total, "done": done})


@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    d = request.get_json(force=True) or {}
    url = (d.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Valid URL daalo (http:// ya https://)"}), 400
    try:
        return jsonify(fetch_chapters(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    o = request.get_json(force=True) or {}
    url = (o.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Valid URL daalo"}), 400
    if o.get("type") not in ("audio", "video"):
        return jsonify({"error": "Audio ya Video chuno"}), 400
    o["url"] = url
    chs = []
    for c in o.get("chapters") or []:
        try:
            chs.append({"title": str(c["title"]), "start": float(c["start"]), "end": float(c["end"])})
        except Exception:
            return jsonify({"error": "Chapter data galat hai"}), 400
    o["chapters"] = chs
    for k in ("trim_start", "trim_end"):
        if o.get(k) is not None:
            try:
                o[k] = float(o[k])
            except Exception:
                return jsonify({"error": "Time galat hai"}), 400
    if o.get("trim_start") is not None and o.get("trim_end") is not None:
        if o["trim_end"] <= o["trim_start"]:
            return jsonify({"error": "End time start se bada hona chahiye"}), 400
    job = Job()
    JOBS[job.id] = job
    threading.Thread(target=job_main, args=(job, o), daemon=True).start()
    return jsonify({"id": job.id})


@app.route("/api/job/<jid>")
def api_job(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify({"error": "job nahi mila"}), 404
    return jsonify({
        "status": j.status,
        "finished": j.finished,
        "percent": j.percent,
        "current": j.current,
        "msg": j.msg,
        "log": j.log[-250:],
    })


@app.route("/api/job/<jid>/stop", methods=["POST"])
def api_job_stop(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify({"error": "job nahi mila"}), 404
    j.stop = True
    p = j.proc
    if p:
        try:
            p.terminate()
        except Exception:
            pass
    return jsonify({"ok": True})


# ===================== FRONTEND =====================
INDEX_HTML = r"""<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Downloader</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--bd:#2a2f3a;--tx:#e8eaf0;--mut:#8a92a5;--ac:#ff4d4d;--ok:#3ddc84;--wn:#ffc857;--in:#0f1218}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:12px 12px 80px}
h1{font-size:20px;margin:6px 0 12px}
h1 small{display:block;color:var(--mut);font-weight:400;font-size:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;margin-bottom:12px}
.card h2{font-size:15px;margin:0 0 10px;color:#fff}
textarea{font-family:inherit;resize:vertical}
input[type=text],input[type=number],select,textarea{width:100%;background:var(--in);color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:10px;font-size:15px;margin:4px 0}
button{background:#2b3140;color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:10px 14px;font-size:15px;cursor:pointer}
button:disabled{opacity:.5}
button.pri{background:var(--ac);border-color:var(--ac);color:#fff;font-weight:600}
button.danger{background:#3a2226;border-color:#5a2b31}
.row{display:flex;gap:8px;align-items:center}
.row>*{flex:1}.row>button{flex:0 0 auto}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
label.ck{display:flex;align-items:center;gap:8px;padding:6px 0}
label.ck input{width:20px;height:20px;flex:0 0 auto}
.hidden{display:none!important}
.mut{color:var(--mut);font-size:13px}
.note{background:#2a2410;border:1px solid #5a4a14;color:var(--wn);padding:8px;border-radius:8px;font-size:13px;margin:6px 0}
.chrow{display:flex;gap:10px;align-items:center;padding:9px 8px;border:1px solid var(--bd);border-radius:8px;margin:5px 0;background:var(--in)}
.chrow input{width:22px;height:22px;flex:0 0 auto}
.chrow .nm{flex:1;word-break:break-word}
.chrow .tm{color:var(--mut);font-size:12px;white-space:nowrap}
.bar{height:14px;background:var(--in);border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin:8px 0 4px}
.bar>div{height:100%;width:0;background:linear-gradient(90deg,#ff4d4d,#ff9a4d);transition:width .3s}
pre#log{background:#0a0c10;border:1px solid var(--bd);border-radius:8px;padding:8px;height:220px;overflow:auto;font-size:11.5px;white-space:pre-wrap;word-break:break-all;margin:8px 0 0}
#msg{white-space:pre-wrap;margin-top:6px}
.sticky{position:fixed;left:0;right:0;bottom:0;background:#0f1115ee;border-top:1px solid var(--bd);padding:10px 12px}
.sticky .in{max-width:720px;margin:0 auto;display:flex;gap:8px}
.sticky button{flex:1}
</style>
</head>
<body>
<div class="wrap">
<h1>▶ YouTube Downloader <small>Web UI · By Tushar · v4.0</small></h1>

<!-- SOURCE -->
<div class="card">
  <h2>1️⃣ Source</h2>
  <div class="row">
    <select id="saved"><option value="">— Saved playlist chuno —</option></select>
    <button id="btnLoad">Load</button>
    <button id="btnDel" class="danger">🗑</button>
  </div>
  <input type="text" id="url" placeholder="👉 YouTube URL daalo (video ya playlist)" inputmode="url" autocomplete="off">
  <div id="saveBox" class="hidden">
    <label class="ck"><input type="checkbox" id="saveCk"> 💾 Ye playlist save karni hai</label>
    <div id="saveFields" class="hidden">
      <input type="text" id="saveNick" placeholder="📛 Playlist ka nickname">
      <div class="grid2">
        <input type="number" id="saveLimit" min="0" placeholder="🔢 Default limit (0 = no limit)">
        <select id="saveQual">
          <option value="">Quality: No default</option>
          <option>144</option><option>240</option><option>360</option><option>480</option>
          <option>720</option><option>1080</option><option>1440</option><option>2160</option>
        </select>
      </div>
      <button id="btnSave" style="width:100%;margin-top:6px">💾 Save karo</button>
    </div>
  </div>
</div>

<!-- CHAPTERS -->
<div class="card">
  <h2>⏱ Timestamps (Chapters)</h2>
  <button id="btnCh" style="width:100%">🔍 Timestamps fetch karo</button>
  <div id="chMsg" class="mut" style="margin-top:8px"></div>
  <div id="chTools" class="hidden">
    <div class="row" style="margin:8px 0 4px">
      <button id="chAll">✔ Select All</button>
      <button id="chNone">✖ Clear</button>
    </div>
    <div class="grid2" style="margin:6px 0">
      <button id="cpNames" style="width:100%;min-width:0;padding:10px 4px;font-size:13px">📄 Copy naam</button>
      <button id="cpTimes" style="width:100%;min-width:0;padding:10px 4px;font-size:13px">📄 Copy time+naam</button>
    </div>
    <div id="cpRes" class="mut"></div>
    <div id="chCount" class="mut"></div>
    <textarea id="pasteBox" rows="5" placeholder="AI se mile topics/names yahan paste karo (har line me ek). Number ya timestamp saath ho to bhi chalega."></textarea>
    <button id="btnPaste" style="width:100%;margin-top:6px">📋 Paste ke names ko auto-tick karo</button>
    <div id="pasteRes" class="mut" style="margin-top:6px;white-space:pre-wrap"></div>
  </div>
  <div id="chList"></div>
  <div id="chTime" class="mut" style="margin-top:8px"></div>
  <div id="chOpts" class="hidden">
    <label class="ck"><input type="checkbox" id="accurate"> 🎯 Accurate cut (slow, re-encode hoga)</label>
    <div class="mut">Har selected timestamp alag file me save hoga, file ka naam = timestamp ka naam.</div>
  </div>
</div>

<!-- PLAYLIST -->
<div id="plCard" class="card hidden">
  <h2>📋 Smart Playlist Mode</h2>
  <div id="plInfo" class="mut">URL daalo, phir info aayegi.</div>
  <button id="btnPlInfo" style="width:100%;margin:6px 0">🔄 Playlist info refresh</button>
  <div id="plChNote" class="note hidden">Timestamps select hain, isliye ye video single mana jayega (playlist options ignore honge).</div>
  <div id="plOpts">
    <select id="plMode">
      <option value="normal">1️⃣ Normal (start se end tak)</option>
      <option value="reverse">2️⃣ Reverse (last video se)</option>
      <option value="range">3️⃣ Specific range</option>
      <option value="smart">4️⃣ 🤖 Smart Auto (history se, reverse)</option>
    </select>
    <div id="fReverse" class="hidden"><input type="number" id="revPool" min="1" placeholder="Last kitne videos me se kaam karna hai? (e.g. 50)"></div>
    <div id="fRange" class="hidden grid2">
      <input type="number" id="rStart" min="1" placeholder="Start #">
      <input type="number" id="rEnd" min="1" placeholder="End #">
    </div>
    <div id="fSmart" class="hidden"><input type="number" id="smartLimit" min="1" placeholder="Kitne videos download karne hain?"></div>
    <div id="smartPrev" class="mut"></div>
    <div id="fCap"><input type="number" id="cap" min="0" placeholder="🔢 Session cap (0/khali = no limit)"></div>
  </div>
</div>

<!-- TYPE -->
<div class="card">
  <h2>🎬 Kya download karna hai?</h2>
  <select id="type">
    <option value="video">🎥 Video</option>
    <option value="audio">🎵 Audio only</option>
  </select>

  <div id="audioBox" class="hidden">
    <div class="grid2">
      <select id="aFmt"><option value="mp3">MP3 (Universal)</option><option value="m4a">M4A (Best quality)</option></select>
      <select id="aQual">
        <option value="">Best (default)</option><option value="320K">320 kbps</option>
        <option value="256K">256 kbps</option><option value="192K">192 kbps</option><option value="128K">128 kbps</option>
      </select>
    </div>
    <div class="trimWrap">
      <label class="ck"><input type="checkbox" id="aCustom"> ✂️ Custom start/end time</label>
      <div id="aTrim" class="hidden grid2">
        <input type="text" id="aS" placeholder="Start (hh:mm:ss)">
        <input type="text" id="aE" placeholder="End (hh:mm:ss)">
      </div>
    </div>
  </div>

  <div id="videoBox">
    <select id="vMode">
      <option value="normal">Normal (Simple)</option>
      <option value="advance">Advance (FPS + trim, best quality)</option>
    </select>
    <select id="res">
      <option value="144">144p</option><option value="240">240p</option><option value="360">360p</option>
      <option value="480">480p</option><option value="720" selected>720p (HD)</option>
      <option value="1080">1080p (Full HD)</option><option value="1440">1440p (2K)</option><option value="2160">2160p (4K)</option>
    </select>
    <div id="advBox" class="hidden">
      <select id="fps"><option>24</option><option selected>30</option><option>48</option><option>60</option></select>
      <div class="trimWrap">
        <label class="ck"><input type="checkbox" id="vCustom"> ✂️ Custom start/end time</label>
        <div id="vTrim" class="hidden grid2">
          <input type="text" id="vS" placeholder="Start (hh:mm:ss)">
          <input type="text" id="vE" placeholder="End (hh:mm:ss)">
        </div>
      </div>
    </div>
  </div>
  <div id="trimNote" class="note hidden">Timestamps select hain, isliye custom trim ignore hoga.</div>
</div>

<!-- EXTRA -->
<div class="card">
  <h2>⚙️ Extra Options</h2>
  <label class="ck"><input type="checkbox" id="xA2"> ⚡ Aria2 fast download</label>
  <label class="ck"><input type="checkbox" id="xSub"> 📝 Subtitles</label>
  <label class="ck"><input type="checkbox" id="xSp"> 🚫 SponsorBlock remove</label>
  <label class="ck"><input type="checkbox" id="xTh"> 🖼 Thumbnail embed</label>
  <label class="ck"><input type="checkbox" id="xMd"> ℹ Metadata embed</label>
  <label class="ck"><input type="checkbox" id="xCk"> 🔐 Cookies (~/cookies.txt, private video)</label>
</div>

<!-- PROGRESS -->
<div class="card">
  <h2>📊 Progress</h2>
  <div id="cur" class="mut">—</div>
  <div class="bar"><div id="bar"></div></div>
  <div id="pct" class="mut">0%</div>
  <div id="msg"></div>
  <pre id="log"></pre>
</div>
</div>

<div class="sticky"><div class="in">
  <button id="btnStart" class="pri">🚀 Download</button>
  <button id="btnStop" disabled>⏹ Stop</button>
</div></div>

<script>
const $ = id => document.getElementById(id);
let chapters = [], videoTitle = "", videoDuration = null, curNick = "", saved = [], jobId = null, timer = null;
let plTotal = null, plDone = 0;

async function api(path, body) {
  const opt = body !== undefined
    ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}
    : {};
  const r = await fetch(path, opt);
  return r.json();
}
const isPl = () => /list=/.test($("url").value);
function fmtT(t) {
  t = Math.round(t);
  const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  const p = n => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}
function parseT(s) {
  s = (s || "").trim();
  if (!s) return null;
  const p = s.split(":").map(Number);
  if (p.some(isNaN)) return NaN;
  return p.reduce((a, b) => a * 60 + b, 0);
}
function selectedCh() {
  return [...document.querySelectorAll(".chk:checked")].map(c => chapters[+c.dataset.i]);
}

/* ---------- UI refresh ---------- */
function refresh() {
  const pl = isPl();
  const nSel = selectedCh().length;
  $("saveBox").classList.toggle("hidden", !pl);
  $("plCard").classList.toggle("hidden", !pl);
  $("plChNote").classList.toggle("hidden", !(pl && nSel));
  $("plOpts").classList.toggle("hidden", pl && nSel > 0);

  const mode = $("plMode").value;
  $("fReverse").classList.toggle("hidden", mode !== "reverse");
  $("fRange").classList.toggle("hidden", mode !== "range");
  $("fSmart").classList.toggle("hidden", mode !== "smart");
  $("fCap").classList.toggle("hidden", mode === "smart");
  renderSmart();

  const audio = $("type").value === "audio";
  $("audioBox").classList.toggle("hidden", !audio);
  $("videoBox").classList.toggle("hidden", audio);
  $("advBox").classList.toggle("hidden", $("vMode").value !== "advance");
  $("aTrim").classList.toggle("hidden", !$("aCustom").checked);
  $("vTrim").classList.toggle("hidden", !$("vCustom").checked);
  document.querySelectorAll(".trimWrap").forEach(e => e.classList.toggle("hidden", nSel > 0));
  $("trimNote").classList.toggle("hidden", !nSel);
  $("saveFields").classList.toggle("hidden", !$("saveCk").checked);
  $("chCount").textContent = nSel + " / " + chapters.length + " selected";
  $("chOpts").classList.toggle("hidden", !chapters.length);
  updateChTime();
}

function fmtDur(sec) {
  sec = Math.round(sec);
  if (sec < 0) sec = 0;
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  const p = n => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

function updateChTime() {
  const el = $("chTime");
  if (!el) return;
  if (!chapters.length) { el.textContent = ""; return; }
  const sel = selectedCh();
  let sum = 0;
  sel.forEach(c => { sum += Math.max(0, (c.end || 0) - (c.start || 0)); });
  const total = (videoDuration != null && videoDuration > 0)
    ? videoDuration
    : (chapters.length ? chapters[chapters.length - 1].end : 0);
  let txt = `⏱ Selected: ${fmtDur(sum)}`;
  if (sel.length) txt += ` (${sel.length} chapter${sel.length > 1 ? "s" : ""})`;
  if (total > 0) txt += `  ·  🎬 Video: ${fmtDur(total)}`;
  el.textContent = txt;
}

function renderSmart() {
  const el = $("smartPrev");
  if ($("plMode").value !== "smart" || plTotal === null) { el.textContent = ""; return; }
  const lim = parseInt($("smartLimit").value) || 0;
  const rem = plTotal - plDone;
  if (rem <= 0) { el.textContent = "✅ Sab videos already download ho chuke hain."; return; }
  if (lim < 1) { el.textContent = "Remaining: " + rem; return; }
  const end = Math.max(1, rem - lim + 1);
  el.textContent = `Download hoga: video #${rem} se #${end} tak (${rem - end + 1} videos, reverse)`;
}

/* ---------- Saved playlists ---------- */
async function loadSaved() {
  saved = await api("/api/playlists");
  const sel = $("saved");
  sel.innerHTML = '<option value="">— Saved playlist chuno —</option>';
  saved.forEach((p, i) => {
    const o = document.createElement("option");
    o.value = i;
    o.textContent = p.nick + (p.limit ? "  [Limit " + p.limit + "]" : "") + (p.quality ? "  [" + p.quality + "p]" : "");
    sel.appendChild(o);
  });
}
$("btnLoad").onclick = () => {
  const i = $("saved").value;
  if (i === "") return alert("Pehle playlist chuno");
  const p = saved[+i];
  $("url").value = p.url;
  curNick = p.nick;
  if (p.limit > 0) { $("plMode").value = "smart"; $("smartLimit").value = p.limit; }
  if (p.quality) { $("res").value = p.quality; }
  clearChapters();
  refresh();
  plInfo();
};
$("btnDel").onclick = async () => {
  const i = $("saved").value;
  if (i === "") return;
  if (!confirm("'" + saved[+i].nick + "' delete karni hai?")) return;
  await api("/api/playlists/delete", {nick: saved[+i].nick});
  if (curNick === saved[+i].nick) curNick = "";
  loadSaved();
};
async function savePlaylist() {
  const nick = $("saveNick").value.trim();
  const r = await api("/api/playlists", {
    nick, url: $("url").value.trim(),
    limit: $("saveLimit").value, quality: $("saveQual").value
  });
  if (r.error) { alert(r.error); return false; }
  curNick = r.nick;
  $("saveNick").value = r.nick;
  await loadSaved();
  return true;
}
$("btnSave").onclick = async () => { if (await savePlaylist()) alert("✅ Saved: " + curNick); };

/* ---------- Playlist info ---------- */
async function plInfo() {
  if (!isPl()) return;
  $("plInfo").textContent = "⏳ Playlist ka count fetch ho raha hai...";
  try {
    const r = await api("/api/playlist_info", {url: $("url").value.trim(), nick: curNick});
    plTotal = r.total; plDone = r.done || 0;
    $("plInfo").textContent = plTotal === null
      ? "⚠️ Total count fetch nahi hua"
      : `📋 History: ${plDone}/${plTotal} downloaded · Remaining: ${Math.max(0, plTotal - plDone)}`
        + (curNick ? "" : "  (playlist saved nahi, history nahi banegi)");
  } catch (e) { $("plInfo").textContent = "❌ " + e; }
  renderSmart();
}
$("btnPlInfo").onclick = plInfo;

/* ---------- Chapters ---------- */
function clearChapters() {
  chapters = []; videoTitle = ""; videoDuration = null;
  $("chList").innerHTML = ""; $("chMsg").textContent = "";
  $("chTools").classList.add("hidden");
  const ct = $("chTime"); if (ct) ct.textContent = "";
}
$("btnCh").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) return alert("Pehle URL daalo");
  clearChapters();
  $("chMsg").textContent = "⏳ Timestamps fetch ho rahe hain...";
  $("btnCh").disabled = true;
  try {
    const r = await api("/api/chapters", {url});
    if (r.error) { $("chMsg").textContent = "❌ " + r.error; return; }
    if (!r.chapters.length) { $("chMsg").textContent = "ℹ️ Is video me timestamps nahi hain."; refresh(); return; }
    chapters = r.chapters; videoTitle = r.title; videoDuration = r.duration || null;
    $("chMsg").textContent = "🎞 " + r.title + " — " + chapters.length + " timestamps mile";
    const box = $("chList");
    chapters.forEach((c, i) => {
      const lab = document.createElement("label");
      lab.className = "chrow";
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.className = "chk"; cb.dataset.i = i;
      cb.onchange = refresh;
      const nm = document.createElement("span");
      nm.className = "nm"; nm.textContent = c.title;
      const tm = document.createElement("span");
      tm.className = "tm"; tm.textContent = fmtT(c.start) + " – " + fmtT(c.end);
      lab.append(cb, nm, tm);
      box.appendChild(lab);
    });
    $("chTools").classList.remove("hidden");
  } catch (e) {
    $("chMsg").textContent = "❌ " + e;
  } finally {
    $("btnCh").disabled = false;
    refresh();
  }
};
$("chAll").onclick = () => { document.querySelectorAll(".chk").forEach(c => c.checked = true); refresh(); };
$("chNone").onclick = () => { document.querySelectorAll(".chk").forEach(c => c.checked = false); refresh(); };

/* ---------- Copy fetched topics ---------- */
async function copyText(txt) {
  try {
    await navigator.clipboard.writeText(txt);
    return true;
  } catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e2) { return false; }
  }
}
async function copyTopics(withTime) {
  if (!chapters.length) return;
  const txt = chapters.map(c => (withTime ? fmtT(c.start) + " " : "") + c.title).join("\n");
  const ok = await copyText(txt);
  $("cpRes").textContent = ok
    ? `✅ ${chapters.length} topics copy ho gaye`
    : "❌ Copy nahi hua, neeche wale box me se manually copy karo";
  if (!ok) { $("pasteBox").value = txt; }
}
$("cpNames").onclick = () => copyTopics(false);
$("cpTimes").onclick = () => copyTopics(true);

/* ---------- Paste names -> auto tick ---------- */
function normTxt(s) {
  s = (s || "").replace(/^\s*(?:[-*•]\s*)?(?:\(?\d{1,2}(?::\d{2}){1,2}\)?\s*[-–—:]?\s*)?(?:\d+\s*[.)]\s+)?/, "");
  return s.toLowerCase().replace(/[^\p{L}\p{N}\p{M}]+/gu, " ").trim();
}
function lineTime(s) {
  const m = (s || "").match(/^\s*[-*•]?\s*\(?(\d{1,2}(?::\d{2}){1,2})\)?/);
  return m ? parseT(m[1]) : null;
}
function tokSim(a, b) {
  const A = new Set(a.split(" ")), B = new Set(b.split(" "));
  let i = 0; A.forEach(x => { if (B.has(x)) i++; });
  const u = A.size + B.size - i;
  return u ? i / u : 0;
}
function findChapter(raw, norms) {
  const n = normTxt(raw);
  if (n) {
    let k = norms.indexOf(n);
    if (k >= 0) return k;
    let best = -1, bl = 0;
    norms.forEach((c, i) => {
      if (c.length >= 3 && n.length >= 3 && (c.includes(n) || n.includes(c))) {
        const l = Math.min(c.length, n.length);
        if (l > bl) { bl = l; best = i; }
      }
    });
    if (best >= 0) return best;
    let bs = 0;
    norms.forEach((c, i) => { const s = tokSim(n, c); if (s > bs) { bs = s; best = i; } });
    if (bs >= 0.6) return best;
  }
  const t = lineTime(raw);
  if (t !== null && !isNaN(t)) {
    const k = chapters.findIndex(c => Math.abs(c.start - t) < 1);
    if (k >= 0) return k;
  }
  return -1;
}
$("btnPaste").onclick = () => {
  if (!chapters.length) { $("pasteRes").textContent = "Pehle timestamps fetch karo."; return; }
  const lines = $("pasteBox").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  if (!lines.length) { $("pasteRes").textContent = "Pehle names paste karo."; return; }
  const norms = chapters.map(c => normTxt(c.title));
  const boxes = document.querySelectorAll(".chk");
  const hit = new Set(), miss = [];
  lines.forEach(l => {
    const k = findChapter(l, norms);
    if (k >= 0) { boxes[k].checked = true; hit.add(k); } else miss.push(l);
  });
  refresh();
  let msg = `✅ ${hit.size} timestamps tick ho gaye (${lines.length} lines me se)`;
  if (miss.length) msg += `\n⚠️ Match nahi hue (${miss.length}):\n- ` + miss.join("\n- ");
  $("pasteRes").textContent = msg;
  if (hit.size) boxes[Math.min(...hit)].scrollIntoView({block: "center", behavior: "smooth"});
};

/* ---------- Download ---------- */
async function start() {
  const url = $("url").value.trim();
  if (!/^https?:\/\//.test(url)) return alert("Valid URL daalo (http:// ya https://)");
  const sel = selectedCh();
  const type = $("type").value;

  if (isPl() && $("saveCk").checked && !curNick) {
    if (!(await savePlaylist())) return;
  }

  const o = {
    url, nick: curNick, type,
    audio_format: $("aFmt").value, audio_quality: $("aQual").value,
    video_mode: $("vMode").value, res: $("res").value, fps: $("fps").value,
    extras: {
      aria2: $("xA2").checked, subs: $("xSub").checked, sponsor: $("xSp").checked,
      thumb: $("xTh").checked, meta: $("xMd").checked, cookies: $("xCk").checked
    },
    chapters: sel, video_title: videoTitle, accurate: $("accurate").checked,
    trim_start: null, trim_end: null,
    playlist: {
      mode: $("plMode").value, pool: $("revPool").value,
      start: $("rStart").value, end: $("rEnd").value,
      limit: $("smartLimit").value, cap: $("cap").value
    }
  };

  if (!sel.length) {
    let cs = null, ce = null, on = false;
    if (type === "audio" && $("aCustom").checked) { on = true; cs = parseT($("aS").value); ce = parseT($("aE").value); }
    if (type === "video" && $("vMode").value === "advance" && $("vCustom").checked) { on = true; cs = parseT($("vS").value); ce = parseT($("vE").value); }
    if (on) {
      if (cs === null) cs = 0;
      if (ce === null || isNaN(cs) || isNaN(ce)) return alert("Time sahi daalo (hh:mm:ss)");
      if (ce <= cs) return alert("End time start se bada hona chahiye");
      o.trim_start = cs; o.trim_end = ce;
    }
    if (isPl()) {
      const m = o.playlist.mode;
      if (m === "reverse" && !(+o.playlist.pool > 0)) return alert("Reverse ke liye number daalo");
      if (m === "range") {
        if (!(+o.playlist.start > 0) || !(+o.playlist.end >= +o.playlist.start)) return alert("Range sahi daalo");
      }
      if (m === "smart" && !(+o.playlist.limit > 0)) return alert("Smart mode ke liye video count daalo");
    }
  }

  $("btnStart").disabled = true; $("btnStop").disabled = false;
  $("log").textContent = ""; $("msg").textContent = ""; $("bar").style.width = "0%"; $("pct").textContent = "0%";
  const r = await api("/api/download", o);
  if (r.error) {
    alert(r.error);
    $("btnStart").disabled = false; $("btnStop").disabled = true;
    return;
  }
  jobId = r.id;
  clearInterval(timer);
  timer = setInterval(poll, 1000);
  poll();
}
async function poll() {
  if (!jobId) return;
  let r;
  try { r = await api("/api/job/" + jobId); } catch (e) { return; }
  if (r.error) { clearInterval(timer); return; }
  $("bar").style.width = r.percent + "%";
  $("pct").textContent = r.percent.toFixed(1) + "%";
  $("cur").textContent = r.current || "—";
  const lg = $("log");
  lg.textContent = r.log.join("\n");
  lg.scrollTop = lg.scrollHeight;
  if (r.finished) {
    clearInterval(timer);
    $("btnStart").disabled = false; $("btnStop").disabled = true;
    $("msg").textContent = r.msg;
    if (plTotal !== null || isPl()) plInfo();
  }
}
$("btnStart").onclick = start;
$("btnStop").onclick = async () => { if (jobId) { await api("/api/job/" + jobId + "/stop", {}); } };

/* ---------- wiring ---------- */
["type", "vMode", "plMode", "aCustom", "vCustom", "saveCk"].forEach(id => $(id).addEventListener("change", refresh));
$("smartLimit").addEventListener("input", renderSmart);
$("url").addEventListener("input", () => { curNick = ""; clearChapters(); refresh(); });
$("url").addEventListener("change", () => { if (isPl()) plInfo(); });

loadSaved();
refresh();
</script>
</body>
</html>
"""


# ===================== BROWSER AUTO-OPEN =====================
def open_browser(url):
    time.sleep(1.5)
    try:
        if shutil.which("termux-open-url"):
            subprocess.Popen(["termux-open-url", url])
            return
        if shutil.which("am"):
            subprocess.Popen(["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    url = "http://%s:%d" % (HOST, PORT)
    print("=" * 40)
    print("  YouTube Downloader (Web) - v4.0")
    print("  By Tushar")
    print("  Open: %s" % url)
    print("  Save folder: %s" % BASE_DIR)
    print("  Band karne ke liye: Ctrl + C")
    print("=" * 40)
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
