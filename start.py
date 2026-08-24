#!/usr/bin/env python
"""Robust launcher for the NSE Market Pulse dashboard.

Does the startup hygiene that a bare ``python app.py`` doesn't:

1. **Kill stale instances** — any process LISTENING on the target port, plus any
   Python process whose command line is running this repo's ``app.py`` (a common
   mess after reloader restarts or forgotten background runs, which then fight over
   the port / hammer NSE from several processes at once).
2. **Preflight** — reuse the very interpreter that launched this script (sidesteps
   the Windows "python is the Store shim" trap noted in AGENTS.md), confirm the port
   is actually free again, ensure ``data/`` exists, and sanity-check core deps import.
3. **Launch** — start ``app.py`` in the foreground so the banner + access log stream
   to your terminal (Ctrl+C stops it). ``--background`` detaches instead.
4. **Supervise** (foreground) — keep the server up. Werkzeug's reloader only restarts
   on its own "file changed" exit code, so an error raised while *importing* kills the
   parent too and the server stays down — showing a traceback and then nothing. This
   relaunches it: a crash before it finished starting waits for you to fix the file,
   a crash after it had been serving backs off and retries.

Usage:
    python start.py                     # port 5055 (or $PORT), foreground, supervised
    python start.py --port 5060
    PORT=5060 python start.py
    python start.py --dry-run           # show what it WOULD kill/preflight; don't touch anything
    python start.py --kill-only         # kill stale instances and exit (no launch)
    python start.py --no-kill           # skip the kill step
    python start.py --no-supervise      # don't relaunch on a crash
    python start.py --background        # detach; print the child PID and exit (unsupervised)

Any extra args after ``--`` are forwarded to app.py's process environment untouched.
"""

import argparse
import os
import socket
import subprocess
import sys
import time

# Windows consoles default to cp1252, which crashes on non-ASCII prints (AGENTS.md:
# "startup banner crashed on non-UTF-8 stdout"). Best-effort switch to UTF-8; the
# messages below are ASCII anyway, so this is just belt-and-suspenders.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"
DEFAULT_PORT = int(os.environ.get("PORT", "5055"))


def log(msg):
    print(f"[start] {msg}", flush=True)


# ---------------------------------------------------------------------------
# stale-instance discovery
# ---------------------------------------------------------------------------
def _parse_listening_pids(netstat_output, port):
    """PIDs LISTENING on `port` from `netstat -ano` text (pure; unit-tested).

    Matches both IPv4/IPv6 local-address forms (0.0.0.0:P, [::]:P, 127.0.0.1:P).
    """
    pids = set()
    want = str(port)
    for line in netstat_output.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if "LISTENING" not in parts:
            continue
        local, pid = parts[1], parts[-1]
        if local.rsplit(":", 1)[-1] == want and pid.isdigit():
            pids.add(int(pid))
    return pids


def listening_pids(port):
    """PIDs currently LISTENING on `port` (Windows netstat / POSIX lsof)."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True).stdout
            return _parse_listening_pids(out, port)
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        return {int(x) for x in out.split() if x.strip().isdigit()}
    except Exception as e:
        log(f"port scan failed: {e}")
        return set()


def apppy_pids():
    """PIDs of *python* processes whose command line runs this repo's app.py.

    Excludes this launcher (start.py doesn't match ``\\bapp\\.py``) and our own PID.
    """
    pids = set()
    try:
        if IS_WINDOWS:
            ps = (r"Get-CimInstance Win32_Process | "
                  r"Where-Object { $_.CommandLine -match '\bapp\.py' -and "
                  r"$_.Name -match 'python' } | "
                  r"Select-Object -ExpandProperty ProcessId")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True).stdout
            pids = {int(x) for x in out.split() if x.strip().isdigit()}
        else:
            out = subprocess.run(["pgrep", "-f", r"app\.py"],
                                 capture_output=True, text=True).stdout
            pids = {int(x) for x in out.split() if x.strip().isdigit()}
    except Exception as e:
        log(f"process scan failed: {e}")
    pids.discard(os.getpid())
    return pids


def kill_pids(pids):
    """Force-kill the given PIDs (best-effort). Returns the set actually signalled."""
    killed = set()
    for pid in sorted(pids):
        try:
            if IS_WINDOWS:
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, text=True)
                ok, why = r.returncode == 0, (r.stderr.strip() or "not found")
            else:
                import signal
                os.kill(pid, signal.SIGKILL)
                ok, why = True, ""
            if ok:
                killed.add(pid)
                log(f"killed stale pid {pid}")
            else:
                log(f"could not kill pid {pid}: {why}")
        except Exception as e:
            log(f"could not kill pid {pid}: {e}")
    return killed


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
def port_is_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def wait_port_free(port, host="127.0.0.1", tries=20, delay=0.25):
    for _ in range(tries):
        if port_is_free(port, host):
            return True
        time.sleep(delay)
    return port_is_free(port, host)


def deps_ok(python):
    """Quick sanity check that the target interpreter has the core deps."""
    r = subprocess.run([python, "-c", "import flask, requests, tabulate"],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr.strip().splitlines() or [""])[-1]


# ---------------------------------------------------------------------------
# supervisor
#
# Werkzeug's reloader only survives crashes that happen *after* the app imports.
# Its parent loop restarts the child solely on exit code 3 (its "file changed"
# signal); any other non-zero exit is returned, so the parent exits too. An error
# raised while importing — a typo, or a half-finished save, e.g. a module-level SQL
# constant referenced by db.init() after being cut from one place but not yet pasted
# in another — therefore takes the whole server down and it STAYS down. The terminal
# shows a traceback and nothing more, which is easy to miss when detached.
#
# So: relaunch it. But blindly retrying an import error just spins, because nothing
# has changed. The runtime before the crash tells the two cases apart — a startup
# error dies almost instantly, whereas a crash after minutes of serving is a runtime
# fault worth retrying — so a fast death waits for the source to be edited (exactly
# what the reloader would have done had it survived) and a late one backs off.
# ---------------------------------------------------------------------------
_FAST_CRASH_SEC = 5.0     # died this fast => it never finished starting up
_MAX_RETRIES = 5          # consecutive late crashes before giving up
_POLL_SEC = 0.5

# Files whose edit should trigger a relaunch: the app's own code, plus the template
# (harmless to include — it's re-read per request, so a change there just costs a
# no-op restart, whereas MISSING a .py would leave the server down).
_WATCH_EXT = (".py", ".html")


def source_snapshot(root, exts=_WATCH_EXT):
    """{path: mtime} for the app's source, for change detection while it's down."""
    snap = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "data", "logs", ".pytest_cache",
                                "node_modules", ".venv", "venv"}]
        for f in files:
            if f.endswith(exts):
                p = os.path.join(base, f)
                try:
                    snap[p] = os.stat(p).st_mtime
                except OSError:
                    pass
    return snap


def plan_restart(returncode, ran_secs, consecutive, max_retries=_MAX_RETRIES,
                 fast_crash_sec=_FAST_CRASH_SEC):
    """What to do after the server process exits (pure; unit-tested).

    Returns ``(action, why)`` where action is ``stop`` | ``wait`` | ``retry``:
      * ``stop``  — it exited cleanly (or we're out of retries); leave it alone.
      * ``wait``  — it died before it could start serving, so the code is broken:
                    wait for an edit rather than spinning on the same failure.
      * ``retry`` — it served for a while then fell over; back off and relaunch.
    """
    if returncode == 0:
        return "stop", "exited cleanly"
    if ran_secs < fast_crash_sec:
        return "wait", (f"crashed after {ran_secs:.1f}s (exit {returncode}) - it never "
                        f"finished starting, so this is a code error")
    if consecutive >= max_retries:
        return "stop", (f"crashed {consecutive}x in a row (exit {returncode}); "
                        f"giving up - fix the error and relaunch")
    return "retry", f"crashed after {ran_secs:.0f}s (exit {returncode})"


def wait_for_source_change(root, before, poll=_POLL_SEC, timeout=None):
    """Block until any watched source file is added/removed/modified.

    Returns the changed path, or None on timeout. Compares against a `before`
    snapshot so an edit made *while the process was dying* is not missed.
    """
    waited = 0.0
    while timeout is None or waited < timeout:
        now = source_snapshot(root)
        for p, m in now.items():
            if before.get(p) != m:
                return p
        for p in before:
            if p not in now:
                return p
        time.sleep(poll)
        waited += poll
    return None


def supervise(cmd, cwd, env, root, max_retries=_MAX_RETRIES):
    """Run the server, relaunching it if it dies. Returns the final exit code.

    Ctrl+C stops for good (that's the user talking, not a fault).
    """
    consecutive = 0
    while True:
        before = source_snapshot(root)
        started = time.time()
        try:
            rc = subprocess.run(cmd, cwd=cwd, env=env).returncode
        except KeyboardInterrupt:
            return 0
        ran = time.time() - started
        action, why = plan_restart(rc, ran, consecutive, max_retries=max_retries)
        if action == "stop":
            if rc:
                log(f"server {why}")
            return rc
        log(f"server {why}")
        if action == "wait":
            log("waiting for a source edit to relaunch... (Ctrl+C to quit)")
            try:
                changed = wait_for_source_change(root, before)
            except KeyboardInterrupt:
                return rc
            log(f"detected change in {os.path.relpath(changed, root)} - relaunching")
            consecutive = 0          # a human intervened; the clock resets
        else:
            backoff = min(2 ** consecutive, 30)
            consecutive += 1
            log(f"relaunching in {backoff}s (attempt {consecutive}/{max_retries})")
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                return rc


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Launch NSE Market Pulse with a clean slate.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port to run/clean (default {DEFAULT_PORT} or $PORT)")
    ap.add_argument("--host", default=os.environ.get("HOST", ""),
                    help="bind host (passed to app.py via $HOST)")
    ap.add_argument("--no-kill", action="store_true", help="don't kill stale instances")
    ap.add_argument("--kill-only", action="store_true", help="kill stale instances, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be killed/preflighted; change nothing")
    ap.add_argument("--background", action="store_true",
                    help="detach the server (print child PID and exit)")
    ap.add_argument("--no-supervise", action="store_true",
                    help="don't relaunch the server if it crashes (foreground only)")
    args = ap.parse_args(argv)

    python = sys.executable or "python"
    port = args.port
    log(f"interpreter: {python}")
    log(f"target: http://127.0.0.1:{port}  (repo: {ROOT})")

    # 1) discover + kill stale instances --------------------------------------
    on_port = listening_pids(port)
    stray = apppy_pids()
    stale = on_port | stray
    if stale:
        log(f"stale instances -> on port {port}: {sorted(on_port) or '-'}; "
            f"running app.py: {sorted(stray) or '-'}")
    else:
        log("no stale instances found")

    if args.dry_run:
        free = port_is_free(port)
        ok, err = deps_ok(python)
        log(f"[dry-run] would kill: {sorted(stale) or '-'}")
        log(f"[dry-run] port {port} free now: {free}")
        log(f"[dry-run] deps import: {'ok' if ok else 'MISSING - ' + err}")
        log("[dry-run] would then launch app.py")
        return 0

    if stale and not args.no_kill:
        kill_pids(stale)
    elif stale and args.no_kill:
        log("--no-kill: leaving stale instances running")

    if args.kill_only:
        log("--kill-only: done")
        return 0

    # 2) preflight ------------------------------------------------------------
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    if not wait_port_free(port):
        log(f"WARN: port {port} is STILL in use after the kill step - "
            f"pick another with --port, or investigate the listener.")
        return 1
    ok, err = deps_ok(python)
    if not ok:
        log(f"WARN: core deps failed to import with this interpreter ({err}). "
            f"Is the right venv active? Continuing - app.py will report the real error.")

    # 3) launch ---------------------------------------------------------------
    env = dict(os.environ, PORT=str(port))
    if args.host:
        env["HOST"] = args.host
    app = os.path.join(ROOT, "app.py")
    log(f"launching: {python} app.py  (PORT={port}"
        + (f", HOST={args.host}" if args.host else "") + ")")

    if args.background:
        cread = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        proc = subprocess.Popen([python, app], cwd=ROOT, env=env,
                                creationflags=cread) if IS_WINDOWS else \
               subprocess.Popen([python, app], cwd=ROOT, env=env,
                                start_new_session=True)
        log(f"detached - child PID {proc.pid}. Stop it with: python start.py --kill-only --port {port}")
        log("note: detached runs are NOT supervised - a bad save can leave it down")
        return 0

    if args.no_supervise:
        try:
            return subprocess.run([python, app], cwd=ROOT, env=env).returncode
        except KeyboardInterrupt:
            return 0
    return supervise([python, app], ROOT, env, ROOT)


if __name__ == "__main__":
    sys.exit(main())
