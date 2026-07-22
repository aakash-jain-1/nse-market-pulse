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

Usage:
    python start.py                     # port 5055 (or $PORT), foreground
    python start.py --port 5060
    PORT=5060 python start.py
    python start.py --dry-run           # show what it WOULD kill/preflight; don't touch anything
    python start.py --kill-only         # kill stale instances and exit (no launch)
    python start.py --no-kill           # skip the kill step
    python start.py --background        # detach; print the child PID and exit

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
        return 0

    try:
        return subprocess.run([python, app], cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
