#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adds the Android-app support to an existing ytdl.py, without touching anything else.

Usage (on the phone, in Termux):
    python patch_ytdl.py /data/data/com.termux/files/home/ytdl.py

Backend changes:
  * /api/health route
  * request/idle counters + watchdog that exits only when nothing is downloading
  * --no-browser and --idle-shutdown flags (plain `python ytdl.py` behaves as before)

Frontend changes (inside INDEX_HTML, only active when window.Android exists):
  * fetch wrapper that re-starts the server through the APK bridge
  * job id saved in localStorage so a running download can be resumed

The script is idempotent: running it twice changes nothing. It refuses to write
anything and lists the anchors it could not find if the file does not match.
"""

import ast
import os
import shutil
import sys
import time

MARK = "ANDROID-APP PATCH"

IDLE_BLOCK = '''
# ---- {mark}: health route + request counters ----
_IDLE = {{"last": time.time(), "active": 0}}
_IDLE_LOCK = threading.Lock()


@app.before_request
def _req_start():
    with _IDLE_LOCK:
        _IDLE["last"] = time.time()
        _IDLE["active"] += 1


@app.teardown_request
def _req_end(exc=None):
    with _IDLE_LOCK:
        _IDLE["active"] = max(0, _IDLE["active"] - 1)
        _IDLE["last"] = time.time()


@app.route("/api/health")
def api_health():
    return jsonify({{"ok": True}})
'''.format(mark=MARK)

WATCHDOG_BLOCK = '''# ---- {mark}: idle shutdown, never while a download is running ----
def _watchdog(timeout):
    while True:
        time.sleep(5)
        running = any(not j.finished for j in list(JOBS.values()))
        with _IDLE_LOCK:
            if running:
                _IDLE["last"] = time.time()
            idle = time.time() - _IDLE["last"]
            busy = _IDLE["active"] > 0
        if not busy and not running and idle > timeout:
            os._exit(0)


'''.format(mark=MARK)

FETCH_WRAPPER = '''// ---- {mark}: restart the server through the APK bridge when it went idle ----
(function () {{
  if (!window.Android) return;
  const _origFetch = window.fetch.bind(window);
  let lastOk = 0, starting = null;
  async function ping() {{
    try {{ return (await _origFetch('/api/health', {{ cache: 'no-store' }})).ok; }}
    catch (e) {{ return false; }}
  }}
  async function ensureServer() {{
    if (Date.now() - lastOk < 20000) return true;
    if (await ping()) {{ lastOk = Date.now(); return true; }}
    if (starting) return starting;
    starting = (async () => {{
      try {{ window.Android.ensureServer(); }} catch (e) {{}}
      for (let i = 0; i < 50; i++) {{
        await new Promise(r => setTimeout(r, 500));
        if (await ping()) {{ lastOk = Date.now(); return true; }}
      }}
      return false;
    }})();
    const ok = await starting; starting = null; return ok;
  }}
  window.fetch = async (input, init) => {{
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.startsWith('/') && !url.startsWith('/api/health') && !(await ensureServer()))
      throw new Error('Server start failed');
    return _origFetch(input, init);
  }};
}})();

'''.format(mark=MARK)

RESUME_BLOCK = '''try {{
  const j = localStorage.getItem('ytdl_job');
  if (j) {{ jobId = j; $(\"btnStart\").disabled = true; $(\"btnStop\").disabled = false;
           timer = setInterval(poll, 1000); poll(); }}
}} catch (e) {{}}

'''.format(mark=MARK)

MAIN_OLD = "    threading.Thread(target=open_browser, args=(url,), daemon=True).start()"

MAIN_NEW = '''    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--idle-shutdown", type=int, nargs="?", const=300, default=0,
                    help="exit after N idle seconds with no running download (default 300 if used without a value)")
    args = ap.parse_args()
    if not args.no_browser:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    if args.idle_shutdown:
        threading.Thread(target=_watchdog, args=(args.idle_shutdown,), daemon=True).start()'''


def apply(src, old, new, label, missing):
    if old in src:
        return src.replace(old, new, 1)
    missing.append(label)
    return src


def patch(text):
    missing = []
    changed = []

    if MARK in text:
        return text, changed, missing

    # 1. argparse
    if "import argparse" not in text:
        old = "import webbrowser\n"
        new = "import webbrowser\nimport argparse  # " + MARK + "\n"
        if old in text:
            text = text.replace(old, new, 1)
            changed.append("import argparse")
        else:
            missing.append("import webbrowser")

    # 2. health route + idle counters
    old = "app = Flask(__name__)\n"
    if old in text and "_IDLE_LOCK" not in text:
        text = text.replace(old, old + IDLE_BLOCK, 1)
        changed.append("/api/health + idle counters")
    elif "_IDLE_LOCK" not in text:
        missing.append("app = Flask(__name__)")

    # 3. watchdog (needs JOBS, so it goes right before run_cmd)
    old = "def run_cmd(job, cmd):"
    if old in text and "_watchdog" not in text:
        text = text.replace(old, WATCHDOG_BLOCK + old, 1)
        changed.append("idle watchdog")
    elif "_watchdog" not in text:
        missing.append("def run_cmd(job, cmd):")

    # 4. CLI flags in __main__
    if "--idle-shutdown" not in text:
        before = text
        text = apply(text, MAIN_OLD, MAIN_NEW, "cli flags", missing)
        if text != before:
            changed.append("--no-browser / --idle-shutdown")

    # 5. JS fetch wrapper (top of the <script> block)
    old = "const $ = id => document.getElementById(id);"
    if old in text and "window.Android.ensureServer" not in text:
        text = text.replace(old, FETCH_WRAPPER + old, 1)
        changed.append("fetch wrapper")
    elif "window.Android.ensureServer" not in text:
        missing.append("const $ = id => document.getElementById(id);")

    # 6. remember the job id
    old = "  jobId = r.id;\n"
    if old in text and "localStorage.setItem('ytdl_job'" not in text:
        text = text.replace(
            old,
            old + "  try { localStorage.setItem('ytdl_job', jobId); } catch (e) {}\n",
            1,
        )
        changed.append("save job id")

    # 7. clear it when the job errored or finished
    old = "  if (r.error) { clearInterval(timer); return; }"
    if old in text:
        text = text.replace(
            old,
            "  if (r.error) {\n"
            "    clearInterval(timer);\n"
            "    try { localStorage.removeItem('ytdl_job'); } catch (e) {}\n"
            "    $(\"btnStart\").disabled = false; $(\"btnStop\").disabled = true;\n"
            "    return;\n"
            "  }",
            1,
        )
        changed.append("clear job id on error")

    old = "  if (r.finished) {\n    clearInterval(timer);"
    if old in text:
        text = text.replace(
            old,
            "  if (r.finished) {\n    clearInterval(timer);\n"
            "    try { localStorage.removeItem('ytdl_job'); } catch (e) {}",
            1,
        )
        changed.append("clear job id on finish")

    # 8. resume a job a previous session left running
    old = "loadSaved();\nrefresh();"
    if old in text and "localStorage.getItem('ytdl_job')" not in text:
        idx = text.rfind(old)
        if idx >= 0:
            text = text[:idx] + RESUME_BLOCK + text[idx:]
            changed.append("resume running job")

    return text, changed, missing


def main():
    if len(sys.argv) != 2:
        print("usage: python patch_ytdl.py <path to ytdl.py>")
        return 2
    path = sys.argv[1]
    if not os.path.isfile(path):
        print("file not found: " + path)
        return 2

    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    if MARK in original:
        print("already patched, nothing to do.")
        return 0

    new_text, changed, missing = patch(original)

    if missing:
        print("could not find these anchors, nothing was written:")
        for m in missing:
            print("  - " + m)
        return 1

    try:
        ast.parse(new_text)
    except SyntaxError as e:
        print("patched file would not be valid python: %s" % e)
        return 1

    if 'r\"\"\"' in new_text and new_text.count('\"\"\"') != original.count('\"\"\"'):
        print("refusing to write: triple quotes changed")
        return 1

    backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)

    print("patched: " + path)
    print("backup:  " + backup)
    for c in changed:
        print("  + " + c)
    print("")
    print("check it: python -c \"import ast;ast.parse(open('%s').read())\"" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
