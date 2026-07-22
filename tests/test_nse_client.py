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
import os
import threading
import time as _time
import types

import requests

from nse_pulse.core import nse_client as nse


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


@contextlib.contextmanager
def _fresh_ep(clock=None):
    """Isolate the per-endpoint request-budget log (optionally on a fake clock)."""
    saved_calls = list(nse._ep_calls)
    saved_time = nse.time
    if clock is not None:
        nse.time = clock
    nse._ep_calls.clear()
    try:
        yield clock
    finally:
        nse.time = saved_time
        nse._ep_calls.clear()
        nse._ep_calls.extend(saved_calls)


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
    saved_imp = nse._impersonate_profile
    requests.Session.send = fake_send
    nse._pace = lambda: None
    nse._impersonate_profile = lambda: None    # force the pure-requests transport
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
        nse._impersonate_profile = saved_imp


# --- Phase 2: optional curl_cffi TLS-fingerprint impersonation -------------

class _FakeCffiSession:
    """Stand-in for curl_cffi.requests.Session: records the impersonate profile and
    every GET, and returns a duck-typed response — so the cffi branch of
    _build_session is testable without the real (optional) dependency or network."""
    def __init__(self, impersonate=None):
        self.impersonate = impersonate
        self.headers = {}
        self.gets = []

    def get(self, url, **kw):
        self.gets.append(url)
        return types.SimpleNamespace(status_code=200, text="{}",
                                     json=lambda: {}, raise_for_status=lambda: None)


@contextlib.contextmanager
def _fake_cffi(profile="chrome124"):
    """Pretend curl_cffi is installed + enabled: swap in a fake _cffi, point
    _PacedCffiSession at the fake session class, set the env profile, and no-op the
    pacer. profile=None leaves NSE_TLS_IMPERSONATE unset (tests the built-in default).
    Everything is restored afterwards."""
    saved = (nse._cffi, nse._PacedCffiSession, nse._pace,
             os.environ.get("NSE_TLS_IMPERSONATE"))
    nse._cffi = types.SimpleNamespace(Session=_FakeCffiSession)
    nse._PacedCffiSession = _FakeCffiSession
    nse._pace = lambda: None
    if profile is None:
        os.environ.pop("NSE_TLS_IMPERSONATE", None)
    else:
        os.environ["NSE_TLS_IMPERSONATE"] = profile
    try:
        yield
    finally:
        nse._cffi, nse._PacedCffiSession, nse._pace = saved[0], saved[1], saved[2]
        if saved[3] is None:
            os.environ.pop("NSE_TLS_IMPERSONATE", None)
        else:
            os.environ["NSE_TLS_IMPERSONATE"] = saved[3]


def test_impersonate_profile_none_without_dep():
    """No curl_cffi installed → impersonation is off no matter what the env says."""
    saved = (nse._cffi, os.environ.get("NSE_TLS_IMPERSONATE"))
    nse._cffi = None
    os.environ["NSE_TLS_IMPERSONATE"] = "chrome124"
    try:
        assert nse._impersonate_profile() is None
    finally:
        nse._cffi = saved[0]
        if saved[1] is None:
            os.environ.pop("NSE_TLS_IMPERSONATE", None)
        else:
            os.environ["NSE_TLS_IMPERSONATE"] = saved[1]


def test_impersonate_profile_default_is_auto_off_until_blocks():
    """Default policy is `auto`: dep present but no blocks yet → stay on plain requests
    (impersonation only escalates on repeat WAF blocks). _fresh_block guarantees a clean
    ladder regardless of what ran before."""
    with _fake_cffi(profile=None), _fresh_block(_Clock()):   # env unset → 'auto', no blocks
        assert nse._impersonate_mode() == "auto"
        assert nse._impersonate_profile() is None


def test_impersonate_profile_env_toggle():
    with _fake_cffi(profile="chrome120"):
        assert nse._impersonate_profile() == "chrome120"
    for off in ("off", "none", "0", "false", "no", ""):
        with _fake_cffi(profile=off):
            assert nse._impersonate_profile() is None, off


def test_pacer_stats_exposes_impersonate_field():
    # Key is always present (None without the dep) so /api/health has a stable shape.
    assert "impersonate" in nse.pacer_stats()
    with _fake_cffi(profile="chrome124"):
        assert nse.pacer_stats()["impersonate"] == "chrome124"


def test_pacer_stats_reports_impersonate_mode():
    assert "impersonateMode" in nse.pacer_stats()          # stable shape
    with _fake_cffi(profile="auto"), _fresh_block(_Clock()):   # clean ladder → not armed
        st = nse.pacer_stats()
        assert st["impersonateMode"] == "auto"
        assert st["impersonate"] is None                   # policy set, not yet in effect
    with _fake_cffi(profile="chrome124"):
        assert nse.pacer_stats()["impersonateMode"] == "chrome124"
    with _fake_cffi(profile="off"):
        assert nse.pacer_stats()["impersonateMode"] == "off"


# --- Phase 2: auto-failover (impersonate only after repeat WAF blocks) ------

def _arm_blocks(clk, n):
    """Drive n consecutive FRESH WAF blocks on the fake clock so _block_count climbs to
    n — advancing just past each escalating cooldown but staying inside the reset gap."""
    for _ in range(n):
        if nse._blocked_until > clk.t:
            clk.t = nse._blocked_until + 1.0     # jump past the active cooldown
        nse.note_block("t")


def test_auto_failover_arms_after_threshold_blocks():
    clk = _Clock(t=2000.0)
    with _fake_cffi(profile="auto"), _fresh_block(clk):
        assert nse._impersonate_profile() is None                 # cold → plain requests
        _arm_blocks(clk, nse._AUTO_FAILOVER_AT - 1)
        assert nse._impersonate_profile() is None                 # below threshold
        _arm_blocks(clk, 1)                                       # crosses the threshold
        assert nse._block_count >= nse._AUTO_FAILOVER_AT
        assert nse._impersonate_profile() == nse._AUTO_PROFILE    # now impersonating


def test_auto_failover_reverts_after_clean_window():
    clk = _Clock(t=2000.0)
    with _fake_cffi(profile="auto"), _fresh_block(clk):
        _arm_blocks(clk, nse._AUTO_FAILOVER_AT)
        assert nse._impersonate_profile() == nse._AUTO_PROFILE
        # advance well past the ladder's reset gap → disarms itself (no manual toggle)
        clk.t = nse._last_block_ts + nse._prev_cooldown + nse._BLOCK_COOLDOWN + 1
        assert nse._auto_failover_armed() is False
        assert nse._impersonate_profile() is None


def test_auto_mode_off_never_impersonates_despite_blocks():
    clk = _Clock(t=2000.0)
    with _fake_cffi(profile="off"), _fresh_block(clk):
        _arm_blocks(clk, nse._AUTO_FAILOVER_AT + 1)
        assert nse._impersonate_profile() is None


def test_explicit_profile_impersonates_regardless_of_blocks():
    clk = _Clock(t=2000.0)
    with _fake_cffi(profile="chrome124"), _fresh_block(clk):
        assert nse._impersonate_profile() == "chrome124"          # armed with zero blocks


def test_build_session_auto_failover_switches_transport():
    """auto mode: _build_session serves plain requests until repeat blocks, then the
    impersonated transport — the whole point of self-healing failover."""
    hits = []

    def fake_send(self, request, **kw):
        hits.append(request.url)
        r = requests.models.Response()
        r.status_code = 200
        r._content = b"{}"
        r.url = request.url
        return r

    saved_send = requests.Session.send
    requests.Session.send = fake_send
    clk = _Clock(t=2000.0)
    try:
        with _fake_cffi(profile="auto"), _fresh_block(clk):
            assert isinstance(nse._build_session(), nse._PacedSession)   # cold → requests
            _arm_blocks(clk, nse._AUTO_FAILOVER_AT)
            assert isinstance(nse._build_session(), _FakeCffiSession)    # armed → impersonate
    finally:
        requests.Session.send = saved_send


def test_build_session_prefers_cffi_when_enabled():
    """When impersonation is on, _build_session returns the impersonated transport,
    warmed with Referer + exactly the two cookie GETs — never touching requests."""
    with _fake_cffi(profile="chrome124"):
        s = nse._build_session()
        assert isinstance(s, _FakeCffiSession)
        assert s.impersonate == "chrome124"
        assert s.headers.get("Referer")
        assert s.gets == [nse.BASE, nse.BASE + "/market-data/live-equity-market"]


def test_build_session_falls_back_to_requests_when_disabled():
    """curl_cffi importable but disabled (NSE_TLS_IMPERSONATE=off) → pure-requests
    paced session, not the cffi one."""
    hits = []

    def fake_send(self, request, **kw):
        hits.append(request.url)
        r = requests.models.Response()
        r.status_code = 200
        r._content = b"{}"
        r.url = request.url
        return r

    saved_send = requests.Session.send
    requests.Session.send = fake_send
    try:
        with _fake_cffi(profile="off"):
            s = nse._build_session()
            assert isinstance(s, nse._PacedSession)
            assert not isinstance(s, _FakeCffiSession)
            assert len(hits) == 2                          # still warms cookies
    finally:
        requests.Session.send = saved_send


def test_paced_cffi_session_wraps_real_dep_when_present():
    """Structural check of the REAL override — only when curl_cffi is actually
    installed (auto-passes otherwise, since the class is None without the dep)."""
    if nse._cffi is None or nse._PacedCffiSession is None:
        return
    assert issubclass(nse._PacedCffiSession, nse._cffi.Session)
    assert "request" in nse._PacedCffiSession.__dict__     # request() is overridden


# --- per-endpoint request budget ------------------------------------------

def test_endpoint_key_buckets_by_path():
    k = nse._endpoint_key
    # query dropped → gainers + losers collapse into one endpoint bucket
    assert k(nse.BASE + "/api/live-analysis-variations?index=gainers") == \
        "/api/live-analysis-variations"
    assert k(nse.BASE + "/api/live-analysis-variations?index=loosers") == \
        "/api/live-analysis-variations"
    # non-main host is prefixed so charting is distinguishable from www
    assert k("https://charting.nseindia.com/Charts/ChartData?index=TCSEQN") == \
        "charting.nseindia.com/Charts/ChartData"
    assert k(None) == "?" and k("") == "?"


def test_endpoint_budget_counts_last_min_and_hour():
    clk = _Clock(t=10000.0)
    with _fresh_ep(clk):
        nse._record_endpoint(nse.BASE + "/api/x")
        nse._record_endpoint(nse.BASE + "/api/x")
        nse._record_endpoint(nse.BASE + "/api/y")
        clk.t += 1800                                  # 30 min later (in-hour, out-of-minute)
        nse._record_endpoint(nse.BASE + "/api/x")
        b = {e["endpoint"]: e for e in nse.endpoint_budget()}
        assert b["/api/x"]["lastHour"] == 3 and b["/api/x"]["lastMin"] == 1
        assert b["/api/y"]["lastHour"] == 1 and b["/api/y"]["lastMin"] == 0
        # ranked by hourly volume: the heavier endpoint comes first
        assert nse.endpoint_budget()[0]["endpoint"] == "/api/x"


def test_endpoint_budget_prunes_beyond_hour():
    clk = _Clock(t=10000.0)
    with _fresh_ep(clk):
        nse._record_endpoint(nse.BASE + "/api/old")
        clk.t += 3700                                  # >1h later → old drops out
        nse._record_endpoint(nse.BASE + "/api/new")
        b = {e["endpoint"]: e for e in nse.endpoint_budget()}
        assert "/api/old" not in b and b["/api/new"]["lastHour"] == 1


def test_paced_session_records_endpoint():
    """send() tags the budget so /api/health can show per-endpoint volume."""
    def fake_send(self, request, **kw):
        r = requests.models.Response()
        r.status_code = 200
        r._content = b"{}"
        r.url = request.url
        return r

    saved_send, saved_pace = requests.Session.send, nse._pace
    requests.Session.send = fake_send
    nse._pace = lambda: None
    try:
        with _fresh_ep():
            req = requests.Request("GET", nse.BASE + "/api/marketStatus").prepare()
            nse._PacedSession().send(req)
            b = {e["endpoint"]: e for e in nse.endpoint_budget()}
            assert b["/api/marketStatus"]["lastMin"] == 1
    finally:
        requests.Session.send, nse._pace = saved_send, saved_pace


def test_pacer_stats_includes_endpoint_budget():
    st = nse.pacer_stats()
    assert isinstance(st["endpoints"], list)           # stable shape for /api/health


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} nse_client resilience tests passed")


if __name__ == "__main__":
    _main()
