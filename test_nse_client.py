"""
Unit tests for nse_client's Akamai-resilience layer:

  * the GLOBAL request pacer (_pace + _PacedSession) that smooths the bursty
    6-8 worker fan-outs into a steady, browser-like stream — min-gap between
    starts, a soft per-minute ceiling, and a bounded in-flight concurrency;
  * the ESCALATING WAF-block cooldown (note_block doubles the pause on repeat
    blocks, capped, and resets after a clean gap);
  * the enriched browser HEADERS / _NAV_HEADERS (client hints + Sec-Fetch-*);
  * pacer_stats() (the /api/health payload) and _build_session() wiring.

Everything runs offline: a fake clock replaces time so there are no real
sleeps, requests.Session.send is stubbed so nothing touches the network, and
each test saves/restores the module globals it perturbs.

Run: python test_nse_client.py   (also works under pytest)
"""

import contextlib
import threading
import time as _time
import types

import requests

import nse_client as nse


class _Clock:
    """Deterministic stand-in for the `time` module: sleep() just advances the
    clock and records the duration, so pacing math is testable without waiting."""
    def __init__(self, t=10000.0):
        self.t = float(t)
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += max(0.0, s)


@contextlib.contextmanager
def _fresh_pace(clock):
    """Isolate the pacer: fake clock, zero jitter, empty window; restore after."""
    saved = (nse.time, nse.random, nse._last_start, list(nse._req_calls))
    nse.time = clock
    nse.random = types.SimpleNamespace(uniform=lambda a, b: 0.0)
    nse._last_start = 0.0
    nse._req_calls.clear()
    try:
        yield clock
    finally:
        nse.time, nse.random, nse._last_start = saved[0], saved[1], saved[2]
        nse._req_calls.clear()
        nse._req_calls.extend(saved[3])


@contextlib.contextmanager
def _fresh_block(clock):
    """Isolate the block-cooldown ladder on a fake clock; restore after."""
    saved = (nse.time, nse._blocked_until, nse._block_count,
             nse._last_block_ts, nse._prev_cooldown)
    nse.time = clock
    nse._blocked_until = 0.0
    nse._block_count = 0
    nse._last_block_ts = 0.0
    nse._prev_cooldown = 0.0
    try:
        yield clock
    finally:
        (nse.time, nse._blocked_until, nse._block_count,
         nse._last_block_ts, nse._prev_cooldown) = saved


# --- pacer: min-gap + soft-RPM --------------------------------------------

def test_pace_enforces_min_gap_between_starts():
    clk = _Clock()
    with _fresh_pace(clk):
        nse._pace()                       # first start: huge gap → no sleep
        assert clk.sleeps == []
        nse._pace()                       # immediately after → sleep exactly min-gap
        assert len(clk.sleeps) == 1
        assert abs(clk.sleeps[0] - nse._NSE_MIN_GAP) < 1e-6
        assert len(nse._req_calls) == 2   # both starts recorded in the window


def test_pace_soft_rpm_waits_for_window_room():
    clk = _Clock(t=1000.0)
    with _fresh_pace(clk):
        for _ in range(nse._NSE_SOFT_RPM):    # window already at the ceiling, all "now"
            nse._req_calls.append(1000.0)
        nse._last_start = 1000.0
        nse._pace()
        # Must wait until the oldest start ages out of the 60s window (+ tiny slack),
        # not just the 0.2s min-gap.
        assert any(abs(s - 60.05) < 0.01 for s in clk.sleeps)
        assert clk.t >= 1060.0


def test_pace_no_rpm_wait_below_ceiling():
    clk = _Clock(t=500.0)
    with _fresh_pace(clk):
        for _ in range(nse._NSE_SOFT_RPM - 1):     # one slot short of the cap
            nse._req_calls.append(500.0)
        nse._last_start = 500.0
        nse._pace()
        assert all(s < 1.0 for s in clk.sleeps)    # only the min-gap, no 60s wait


# --- pacer: bounded in-flight concurrency ----------------------------------

def test_paced_session_caps_concurrency():
    """8 threads through one _PacedSession must never have more than
    _NSE_MAX_CONCURRENCY inside the underlying send() at once."""
    peak = [0]
    cur = [0]
    lock = threading.Lock()
    gate_open = threading.Event()

    def fake_send(self, request, **kw):
        with lock:
            cur[0] += 1
            peak[0] = max(peak[0], cur[0])
        gate_open.wait(3)                 # hold the slot until the test releases us
        with lock:
            cur[0] -= 1
        return "ok"

    saved_send = requests.Session.send
    saved_pace = nse._pace
    saved_gate = nse._NSE_GATE
    requests.Session.send = fake_send
    nse._pace = lambda: None              # take timing out of the picture
    nse._NSE_GATE = threading.BoundedSemaphore(nse._NSE_MAX_CONCURRENCY)
    threads = []
    try:
        s = nse._PacedSession()
        threads = [threading.Thread(target=lambda: s.send(object())) for _ in range(8)]
        for t in threads:
            t.start()
        deadline = _time.time() + 3
        while peak[0] < nse._NSE_MAX_CONCURRENCY and _time.time() < deadline:
            _time.sleep(0.01)
        # Exactly the cap got in; the other 4 are parked on the semaphore.
        assert peak[0] == nse._NSE_MAX_CONCURRENCY
        with lock:
            assert cur[0] == nse._NSE_MAX_CONCURRENCY
    finally:
        gate_open.set()
        for t in threads:
            t.join(3)
        requests.Session.send = saved_send
        nse._pace = saved_pace
        nse._NSE_GATE = saved_gate


# --- escalating cooldown ---------------------------------------------------

def test_cooldown_for_ladder_and_cap():
    assert nse._cooldown_for(0) == nse._BLOCK_COOLDOWN
    assert nse._cooldown_for(1) == nse._BLOCK_COOLDOWN
    assert nse._cooldown_for(2) == nse._BLOCK_COOLDOWN * 2
    assert nse._cooldown_for(3) == nse._BLOCK_COOLDOWN * 4
    assert nse._cooldown_for(99) == nse._BLOCK_MAX          # capped


def test_note_block_escalates_then_resets():
    clk = _Clock(t=10000.0)
    with _fresh_block(clk):
        nse.note_block("t")                                # block #1
        assert nse._block_count == 1
        assert abs(nse.blocked_for() - nse._BLOCK_COOLDOWN) < 1.5

        clk.t = nse._blocked_until + 1                     # re-block right after it clears
        nse.note_block("t")                                # block #2 → doubled
        assert nse._block_count == 2
        assert abs(nse.blocked_for() - nse._BLOCK_COOLDOWN * 2) < 2

        clk.t = nse._blocked_until + nse._BLOCK_COOLDOWN * 10   # long clean run
        nse.note_block("t")                                # gap big enough → ladder resets
        assert nse._block_count == 1
        assert abs(nse.blocked_for() - nse._BLOCK_COOLDOWN) < 2


def test_note_block_during_cooldown_extends_without_climbing():
    clk = _Clock(t=5000.0)
    with _fresh_block(clk):
        nse.note_block("t")
        assert nse._block_count == 1
        until1 = nse._blocked_until
        clk.t += 10                                        # still inside the cooldown
        nse.note_block("t")                                # straggler hit
        assert nse._block_count == 1                       # did NOT escalate
        assert nse._blocked_until >= until1                # cooldown not shortened


# --- headers ---------------------------------------------------------------

def test_headers_mimic_modern_chrome():
    for k in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
              "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site",
              "Accept-Encoding", "Connection"):
        assert k in nse.HEADERS, "missing browser header %s" % k
    # The client-hint version must match the UA major so they don't contradict.
    assert "124" in nse.HEADERS["User-Agent"]
    assert "124" in nse.HEADERS["sec-ch-ua"]
    assert nse.HEADERS["Sec-Fetch-Mode"] == "cors"        # API calls are XHRs
    assert "gzip" in nse.HEADERS["Accept-Encoding"]


def test_nav_headers_are_navigation_shaped():
    assert nse._NAV_HEADERS["Sec-Fetch-Mode"] == "navigate"
    assert nse._NAV_HEADERS["Sec-Fetch-Dest"] == "document"
    assert "text/html" in nse._NAV_HEADERS["Accept"]
    assert nse._NAV_HEADERS["Upgrade-Insecure-Requests"] == "1"


# --- pacer_stats + _build_session wiring -----------------------------------

def test_pacer_stats_shape():
    saved = (nse._blocked_until, nse._block_count, list(nse._req_calls))
    nse._blocked_until = 0.0
    nse._block_count = 0
    nse._req_calls.clear()
    try:
        st = nse.pacer_stats()
        assert set(st) >= {"blockedForSec", "blockCount", "cooldownSec",
                           "reqLastMin", "concurrency", "minGap", "softRpm"}
        assert st["blockedForSec"] == 0.0
        assert st["blockCount"] == 0
        assert st["cooldownSec"] == 0            # no blocks → no advertised cooldown
        assert st["reqLastMin"] == 0
        assert st["concurrency"] == nse._NSE_MAX_CONCURRENCY
        assert st["softRpm"] == nse._NSE_SOFT_RPM
    finally:
        nse._blocked_until, nse._block_count = saved[0], saved[1]
        nse._req_calls.clear()
        nse._req_calls.extend(saved[2])


def test_pacer_stats_reports_block_after_note_block():
    clk = _Clock(t=2000.0)
    with _fresh_block(clk):
        nse.note_block("t")
        st = nse.pacer_stats()
        assert st["blockCount"] == 1
        assert st["blockedForSec"] > 0
        assert st["cooldownSec"] == nse._BLOCK_COOLDOWN


def test_build_session_is_paced_with_two_warmups():
    """_build_session must return a _PacedSession, apply the enriched headers, and
    warm up cookies with exactly the two navigation GETs — all without network."""
    hits = []

    def fake_send(self, request, **kw):
        hits.append(request.url)
        r = requests.models.Response()
        r.status_code = 200
        r._content = b"{}"
        r.url = request.url
        return r

    saved_send = requests.Session.send
    saved_pace = nse._pace
    requests.Session.send = fake_send
    nse._pace = lambda: None
    try:
        s = nse._build_session()
        assert isinstance(s, nse._PacedSession)
        assert s.headers.get("sec-ch-ua")                  # enriched headers applied
        assert len(hits) == 2                              # homepage + market page
        assert hits[0] == nse.BASE + "/"
        assert "live-equity-market" in hits[1]
    finally:
        requests.Session.send = saved_send
        nse._pace = saved_pace


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} nse_client resilience tests passed")


if __name__ == "__main__":
    _main()
