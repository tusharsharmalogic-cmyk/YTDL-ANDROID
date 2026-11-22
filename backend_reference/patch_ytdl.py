#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adds Android-app support to an existing ytdl.py, without touching anything else.

Usage (on the phone, in Termux):
    python patch_ytdl.py /data/data/com.termux/files/home/ytdl.py

The script is idempotent and layered:

  v1: /api/health, request/idle counters, an idle watchdog that never exits
      while a download is running, --no-browser / --idle-shutdown CLI flags,
      a JS fetch wrapper that revives the server via the APK bridge, and
      per-job resume on page reload (single-job UI).

  v2: persistent job state (~/.ytdl_jobs.json, snapshotted every 2s), a new
      /api/jobs endpoint that returns the full active + history list, and a
      frontend bootstrap that syncs the download list from the server on
      reconnect instead of relying on localStorage alone. Multiple active
      jobs are preserved; the UI resumes the newest one and leaves the
      others running on the server untouched.

Both layers are skipped if their MARK is already present. If a new layer is
added but its anchors are not found in the target file, nothing is written
for that layer and the missing anchors are listed.
"""

import ast
import os
import shutil
import sys
import time

MARK = "ANDROID-APP PATCH"
MARK_V2 = "ANDROID-APP PATCH v2"

# --------------------------------------------------------------------------- v1

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
  if (j) {{ jobId = j; $("btnStart").disabled = true; $("btnStop").disabled = false;
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

# --------------------------------------------------------------------------- v2

V2_STATE_BLOCK = '''
# ---- {mark}: persistent job state + /api/jobs ----
import json as _v2_json
_V2_STATE_FILE = os.path.expanduser("~/.ytdl_jobs.json")
_V2_STATE_LOCK = threading.Lock()

def _v2_safe(v):
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, (list, dict)):
        try:
            _v2_json.dumps(v)
            return v
        except (TypeError, ValueError):
            return None
    return None

def _v2_job_to_dict(j):
    try:
        d = {{}}
        for k, v in vars(j).items():
            if k.startswith("_"):
                continue
            sv = _v2_safe(v)
            if sv is not None or v is None:
                d[k] = sv
        return d
    except Exception:
        return {{}}

def _v2_load_state():
    try:
        with open(_V2_STATE_FILE, "r") as f:
            return _v2_json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {{}}

def _v2_save_state():
    try:
        with _V2_STATE_LOCK:
            snap = {{}}
            for jid, j in list(JOBS.items()):
                snap[str(jid)] = _v2_job_to_dict(j)
            tmp = _V2_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                _v2_json.dump(snap, f)
            os.replace(tmp, _V2_STATE_FILE)
    except Exception:
        pass

def _v2_persist_loop():
    while True:
        time.sleep(2)
        _v2_save_state()

@app.route("/api/jobs")
def api_jobs():
    out = []
    for jid, j in list(JOBS.items()):
        d = _v2_job_to_dict(j)
        d["id"] = str(jid)
        out.append(d)
    return jsonify(out)

def _v2_restore_missing():
    # Re-seed JOBS from disk for entries lost by a server crash.
    # A job that was still running when the server died cannot be resumed
    # (its yt-dlp subprocess died with the server). It is surfaced as an
    # error so the UI shows it in history instead of pretending it is live.
    saved = _v2_load_state()
    for jid, rec in saved.items():
        if jid in JOBS:
            continue
        if isinstance(rec, dict) and not rec.get("finished") and not rec.get("error"):
            rec = dict(rec)
            rec["error"] = "interrupted by server restart"
        # Best-effort placeholder so history survives a restart.
        try:
            class _Ghost:
                def __init__(self, d):
                    self.__dict__.update(d)
                    self.finished = True
            JOBS[jid] = _Ghost(rec)
        except Exception:
            pass
'''.format(mark=MARK_V2)

# JOBS is defined later in ytdl.py than the idle lock, so the startup calls
# must go right after "JOBS = {}" instead of inside the state block.
V2_START_BLOCK = '''_v2_restore_missing()  # ANDROID-APP PATCH v2
threading.Thread(target=_v2_persist_loop, daemon=True).start()
'''

RESUME_BLOCK_V2 = '''// ---- {mark}: restore the full download list from the server on reconnect ----
(async function restoreJobs() {{
  try {{
    const r = await fetch('/api/jobs', {{ cache: 'no-store' }});
    if (!r.ok) return;
    const jobs = await r.json();
    if (!Array.isArray(jobs) || jobs.length === 0) return;
    const active = jobs.filter(j => !j.finished && !j.error);
    if (active.length > 0) {{
      const pick = active[active.length - 1];
      jobId = pick.id;
      try {{ localStorage.setItem('ytdl_job', jobId); }} catch (e) {{}}
      $("btnStart").disabled = true;
      $("btnStop").disabled = false;
      timer = setInterval(poll, 1000);
      poll();
    }} else {{
      try {{ localStorage.removeItem('ytdl_job'); }} catch (e) {{}}
    }}
  }} catch (e) {{}}
}})();

'''.format(mark=MARK_V2)

# --------------------------------------------------------------------------- apply


def _replace_once(text, old, new, label, missing, changed):
    if old in text:
        return text.replace(old, new, 1)
    missing.append(label)
    return text


def apply_v1(text):
    """Apply the v1 layer. Returns (text, changed, missing)."""
    changed = []
    missing = []

    if "import argparse" not in text:
        old = "import webbrowser\n"
        new = "import webbrowser\nimport argparse  # " + MARK + "\n"
        if old in text:
            text = text.replace(old, new, 1)
            changed.append("import argparse")
        else:
            missing.append("import webbrowser")

    old = "app = Flask(__name__)\n"
    if old in text and "_IDLE_LOCK" not in text:
        text = text.replace(old, old + IDLE_BLOCK, 1)
        changed.append("/api/health + idle counters")
    elif "_IDLE_LOCK" not in text:
        missing.append("app = Flask(__name__)")

    old = "def run_cmd(job, cmd):"
    if old in text and "_watchdog" not in text:
        text = text.replace(old, WATCHDOG_BLOCK + old, 1)
        changed.append("idle watchdog")
    elif "_watchdog" not in text:
        missing.append("def run_cmd(job, cmd):")

    if "--idle-shutdown" not in text:
        before = text
        text = _replace_once(text, MAIN_OLD, MAIN_NEW, "cli flags", missing, changed)
        if text != before:
            changed.append("--no-browser / --idle-shutdown")

    old = "const $ = id => document.getElementById(id);"
    if old in text and "window.Android.ensureServer" not in text:
        text = text.replace(old, FETCH_WRAPPER + old, 1)
        changed.append("fetch wrapper")
    elif "window.Android.ensureServer" not in text:
        missing.append("const $ = id => document.getElementById(id);")

    old = "  jobId = r.id;\n"
    if old in text and "localStorage.setItem('ytdl_job'" not in text:
        text = text.replace(
            old,
            old + "  try { localStorage.setItem('ytdl_job', jobId); } catch (e) {}\n",
            1,
        )
        changed.append("save job id")

    old = "  if (r.error) { clearInterval(timer); return; }"
    if old in text:
        new = (
            "  if (r.error) {\n"
            "    clearInterval(timer);\n"
            "    try { localStorage.removeItem('ytdl_job'); } catch (e) {}\n"
            "    $(\"btnStart\").disabled = false; $(\"btnStop\").disabled = true;\n"
            "    return;\n"
            "  }"
        )
        if old in text and new not in text:
            text = text.replace(old, new, 1)
            changed.append("clear job id on error")

    old = "  if (r.finished) {\n    clearInterval(timer);"
    if old in text:
        new = (
            "  if (r.finished) {\n    clearInterval(timer);\n"
            "    try { localStorage.removeItem('ytdl_job'); } catch (e) {}"
        )
        if new not in text:
            text = text.replace(old, new, 1)
            changed.append("clear job id on finish")

    old = "loadSaved();\nrefresh();"
    if old in text and "localStorage.getItem('ytdl_job')" not in text:
        idx = text.rfind(old)
        if idx >= 0:
            text = text[:idx] + RESUME_BLOCK + text[idx:]
            changed.append("resume running job")

    return text, changed, missing


def apply_v2(text):
    """Apply the v2 layer. Returns (text, changed, missing)."""
    changed = []
    missing = []

    # 1. backend: state block goes right after v1's idle lock definition.
    anchor = "_IDLE_LOCK = threading.Lock()\n"
    if anchor in text and "_v2_persist_loop" not in text:
        text = text.replace(anchor, anchor + V2_STATE_BLOCK, 1)
        jobs_anchor = "JOBS = {}\n"
        if jobs_anchor in text:
            text = text.replace(jobs_anchor, jobs_anchor + V2_START_BLOCK, 1)
            changed.append("persistent state + /api/jobs")
        else:
            missing.append(jobs_anchor.strip())
    elif "_v2_persist_loop" not in text:
        missing.append(anchor.strip())

    # 2. frontend: swap the single-job resume for a list-aware one.
    if "restoreJobs()" not in text and RESUME_BLOCK in text:
        text = text.replace(RESUME_BLOCK, RESUME_BLOCK_V2, 1)
        changed.append("restore list from /api/jobs")
    elif "restoreJobs()" not in text and RESUME_BLOCK not in text:
        # v1 was not applied here — v2 needs its frontend hooks, skip cleanly.
        missing.append("v1 resume block")

    return text, changed, missing


def patch(text):
    changed = []
    missing = []

    if MARK not in text:
        text, c, m = apply_v1(text)
        if m:
            return text, changed, missing + ["v1: " + x for x in m]
        changed.extend(c)
    else:
        # v1 markers already present, but its frontend hooks may be missing
        # on a partially-patched file. Re-run the frontend-only sub-steps.
        for old, label in [
            ("  jobId = r.id;\n", "save job id"),
        ]:
            pass  # no-op: v1 hooks are considered settled once MARK is present.

    if MARK_V2 not in text:
        text, c, m = apply_v2(text)
        if m:
            return text, changed, missing + ["v2: " + x for x in m]
        changed.extend(c)

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

    if MARK in original and MARK_V2 in original:
        print("already fully patched (v1 + v2), nothing to do.")
        return 0

    new_text, changed, missing = patch(original)

    if missing:
        print("could not find these anchors, nothing was written:")
        for m in missing:
            print("  - " + m)
        return 1

    if not changed:
        print("no changes applied (target already up to date).")
        return 0

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