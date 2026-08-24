"""
Unit tests for start.py — the clean-slate launcher.

We test the parts that are pure or purely local (no process spawning): the
netstat-output parser that decides which PIDs to kill, and the port-free probe.
The kill/launch paths just shell out to taskkill/lsof/subprocess and aren't
exercised here (they'd touch real processes).
"""

import os
import socket
import tempfile
import shutil
import threading
import time

import start


_NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:5055           0.0.0.0:0              LISTENING       4152
  TCP    127.0.0.1:5056         0.0.0.0:0              LISTENING       6104
  TCP    [::]:5055              [::]:0                 LISTENING       4152
  TCP    192.168.1.20:52344     140.82.112.21:443      ESTABLISHED     9000
  TCP    0.0.0.0:5055           0.0.0.0:0              TIME_WAIT       0
  UDP    0.0.0.0:5353           *:*                                    2200
"""


def test_parse_listening_pids_matches_port_across_bind_forms():
    # Both the 0.0.0.0 and [::] LISTENING rows for 5055 map to PID 4152 (deduped).
    assert start._parse_listening_pids(_NETSTAT, 5055) == {4152}


def test_parse_listening_pids_is_port_specific():
    assert start._parse_listening_pids(_NETSTAT, 5056) == {6104}
    assert start._parse_listening_pids(_NETSTAT, 9999) == set()


def test_parse_listening_pids_ignores_non_listening_and_udp():
    # ESTABLISHED, TIME_WAIT and the UDP row must never be treated as a listener.
    pids = start._parse_listening_pids(_NETSTAT, 443)
    assert pids == set()
    # 5353 is UDP only → no TCP listener.
    assert start._parse_listening_pids(_NETSTAT, 5353) == set()


def test_parse_listening_pids_handles_empty():
    assert start._parse_listening_pids("", 5055) == set()


def test_port_is_free_true_when_nothing_bound():
    # Grab an ephemeral port, close it, and confirm it reads as free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert start.port_is_free(port) is True


def test_port_is_free_false_while_listener_up():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert start.port_is_free(port) is False
    finally:
        s.close()
    assert start.port_is_free(port) is True


# ---------------------------------------------------------------------------
# supervisor — keeps the server up when the reloader can't
# ---------------------------------------------------------------------------
def test_plan_restart_distinguishes_a_startup_error_from_a_runtime_crash():
    """The whole point of the runtime heuristic: an import error must NOT be retried
    on a loop (nothing changed, it will fail identically), while a process that served
    for a while and then fell over should come back."""
    # clean exit (incl. Ctrl+C, which main() turns into 0) => leave it alone
    assert start.plan_restart(0, 0.1, 0)[0] == "stop"
    assert start.plan_restart(0, 900.0, 4)[0] == "stop"
    # died before it could serve => broken code; wait for a human edit
    assert start.plan_restart(1, 0.4, 0)[0] == "wait"
    assert start.plan_restart(1, 4.9, 3)[0] == "wait"      # still under the threshold
    # ran, then crashed => transient; retry
    assert start.plan_restart(1, 5.1, 0)[0] == "retry"
    assert start.plan_restart(70, 600.0, 2)[0] == "retry"
    # ...but not forever
    assert start.plan_restart(1, 600.0, 5, max_retries=5)[0] == "stop"
    assert "giving up" in start.plan_restart(1, 600.0, 5, max_retries=5)[1]


def test_plan_restart_explains_itself():
    """The reason is printed to the terminal, so it has to say something useful."""
    for rc, ran, n in ((1, 0.3, 0), (1, 88.0, 1), (1, 88.0, 9)):
        action, why = start.plan_restart(rc, ran, n)
        assert why and action in ("stop", "wait", "retry")
        assert "exit 1" in why


def test_source_snapshot_watches_code_and_skips_noise():
    root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(root, "pkg"))
        os.makedirs(os.path.join(root, "__pycache__"))
        os.makedirs(os.path.join(root, "data"))
        for rel in ("app.py", "pkg/mod.py", "pkg/page.html",
                    "__pycache__/mod.cpython-313.pyc", "data/market.db", "notes.txt"):
            p = os.path.join(root, rel)
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
        snap = start.source_snapshot(root)
        rels = {os.path.relpath(p, root).replace("\\", "/") for p in snap}
        assert rels == {"app.py", "pkg/mod.py", "pkg/page.html"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_wait_for_source_change_detects_edit_add_and_delete():
    root = tempfile.mkdtemp()
    try:
        target = os.path.join(root, "m.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("a = 1")
        before = start.source_snapshot(root)

        # a modification (bump mtime explicitly — a same-second write can tie)
        os.utime(target, (time.time() + 10, time.time() + 10))
        assert start.wait_for_source_change(root, before, poll=0.01, timeout=1) == target

        # a NEW file counts too (you might fix an import by adding the module)
        before = start.source_snapshot(root)
        added = os.path.join(root, "new.py")
        with open(added, "w", encoding="utf-8") as f:
            f.write("b = 2")
        assert start.wait_for_source_change(root, before, poll=0.01, timeout=1) == added

        # so does a deletion
        before = start.source_snapshot(root)
        os.remove(added)
        assert start.wait_for_source_change(root, before, poll=0.01, timeout=1) == added

        # and nothing happening returns None rather than blocking forever
        before = start.source_snapshot(root)
        assert start.wait_for_source_change(root, before, poll=0.01, timeout=0.05) is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_wait_for_source_change_catches_an_edit_made_while_it_was_dying():
    """The snapshot is taken BEFORE the run, so an edit landing during the crash
    (a very likely race — you save, it dies, you're already fixing it) still counts."""
    root = tempfile.mkdtemp()
    try:
        target = os.path.join(root, "m.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("a = 1")
        before = start.source_snapshot(root)          # "before the run"
        os.utime(target, (time.time() + 10, time.time() + 10))   # edited mid-crash
        # already stale on the very first poll -> returns immediately
        assert start.wait_for_source_change(root, before, poll=5, timeout=0.001) == target
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_supervise_relaunches_after_an_edit_then_stops_on_a_clean_exit():
    """End-to-end over the real loop with a fake 'server': crash fast (as an import
    error does), wait for the edit, relaunch, then exit 0 and stay stopped."""
    root = tempfile.mkdtemp()
    try:
        target = os.path.join(root, "m.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("a = 1")
        runs = []

        def fake_run(cmd, cwd=None, env=None):
            runs.append(cmd)
            if len(runs) == 1:                       # first boot: instant crash
                # simulate the user fixing the file while it's down
                threading.Timer(0.05, lambda: os.utime(
                    target, (time.time() + 10, time.time() + 10))).start()
                return type("R", (), {"returncode": 1})()
            return type("R", (), {"returncode": 0})()   # after the fix: clean run

        orig = start.subprocess.run
        start.subprocess.run = fake_run
        try:
            rc = start.supervise(["python", "app.py"], root, {}, root)
        finally:
            start.subprocess.run = orig
        assert rc == 0
        assert len(runs) == 2, f"expected one relaunch, got {len(runs)} run(s)"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_supervise_gives_up_instead_of_looping_forever():
    """A repeatedly-crashing server that DID manage to serve each time must not spin
    indefinitely — bounded attempts, and no hang."""
    root = tempfile.mkdtemp()
    try:
        runs = []

        def fake_run(cmd, cwd=None, env=None):
            runs.append(cmd)
            return type("R", (), {"returncode": 1})()

        orig_run, orig_sleep = start.subprocess.run, start.time.sleep
        start.subprocess.run = fake_run
        start.time.sleep = lambda s: None            # don't actually back off
        # force the "retry" branch: pretend every run lasted well past the threshold
        orig_plan = start.plan_restart
        start.plan_restart = lambda rc, ran, n, **kw: orig_plan(rc, 60.0, n, **kw)
        try:
            rc = start.supervise(["python", "app.py"], root, {}, root, max_retries=3)
        finally:
            start.plan_restart = orig_plan
            start.subprocess.run, start.time.sleep = orig_run, orig_sleep
        assert rc == 1
        assert len(runs) == 4, f"3 retries after the first run, got {len(runs)}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_supervise_stops_on_ctrl_c():
    """Ctrl+C is the user talking, not a fault — never relaunch through it."""
    root = tempfile.mkdtemp()
    try:
        def fake_run(cmd, cwd=None, env=None):
            raise KeyboardInterrupt

        orig = start.subprocess.run
        start.subprocess.run = fake_run
        try:
            assert start.supervise(["python", "app.py"], root, {}, root) == 0
        finally:
            start.subprocess.run = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
    sys.exit(0)
