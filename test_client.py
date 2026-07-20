"""
Unit tests for nse_client.py — normalization + signal/idea math.

Pure logic: _num, _oi_signal (all four buildup/unwind branches + the neutral
no-price case), _days_to_expiry, and the crown jewel _build_idea (LONG/SHORT
scoring, conviction bands, risk plan, and the many "no clean edge → None"
rejections). List getters (variations / most-active / volume-gainers / index /
oi-spurts / futures) are driven through a stubbed _fetch; the aggregate builders
(scanner, demand score, price map, get_price, lot size) via stubbed sub-getters.
No network.

Run: python test_client.py   (also works under pytest)
"""

import contextlib
import time
from datetime import datetime, timedelta

import nse_client as nse


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _reset_block():
    """Clear + restore the WAF-block cooldown (incl. the escalation ladder) and
    session pointers around a test, so each starts from a clean 'block #1' state."""
    saved = (nse._blocked_until, nse._session, nse._session_ts,
             nse._block_count, nse._last_block_ts, nse._prev_cooldown)
    nse._blocked_until = 0.0
    nse._block_count = 0
    nse._last_block_ts = 0.0
    nse._prev_cooldown = 0.0
    try:
        yield
    finally:
        (nse._blocked_until, nse._session, nse._session_ts,
         nse._block_count, nse._last_block_ts, nse._prev_cooldown) = saved


@contextlib.contextmanager
def _reset_fetch_cache():
    saved = dict(nse._fetch_cache)
    nse._fetch_cache.clear()
    try:
        yield
    finally:
        nse._fetch_cache.clear()
        nse._fetch_cache.update(saved)


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = {} if json_data is None else json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("HTTP %d" % self.status_code)

    def json(self):
        return self._json


class _Sess:
    """Fake session that returns a canned response and counts .get() calls."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return self._resp


@contextlib.contextmanager
def _reset_cache(name):
    """Snapshot & restore one of the module's dict caches around a test."""
    cache = getattr(nse, name)
    saved = dict(cache)
    cache.update(ts=0.0)
    if "map" in cache:
        cache["map"] = {}
    if "data" in cache:
        cache["data"] = None
    try:
        yield
    finally:
        cache.clear()
        cache.update(saved)


# ---------------------------------------------------------------------------
# _num / _oi_signal / _days_to_expiry
# ---------------------------------------------------------------------------
def test_num():
    assert nse._num("3.5") == 3.5 and nse._num(2) == 2.0
    assert nse._num(None) is None and nse._num("x") is None


def test_oi_signal_branches():
    assert nse._oi_signal(True, 1.0) == ("Long buildup", "buildup")
    assert nse._oi_signal(True, -1.0) == ("Short buildup", "short")
    assert nse._oi_signal(False, 1.0) == ("Short covering", "buildup")
    assert nse._oi_signal(False, -1.0) == ("Long unwinding", "short")
    assert nse._oi_signal(True, None) == ("OI Rising", "neutral")
    assert nse._oi_signal(False, None) == ("OI Falling", "neutral")


def test_days_to_expiry():
    assert nse._days_to_expiry(None) is None
    assert nse._days_to_expiry("garbage") is None
    exp = (datetime.now().date() + timedelta(days=10)).strftime("%d-%b-%Y")
    assert nse._days_to_expiry(exp) == 10


# ---------------------------------------------------------------------------
# _build_idea (pure)
# ---------------------------------------------------------------------------
def test_build_idea_long_high_conviction():
    e = {"symbol": "A", "ltp": 100, "pChange": 5.0, "oiSignal": "Long buildup",
         "volMult": 6, "tags": ["💰 Money flow"], "listCount": 3, "fno": True}
    idea = nse._build_idea(e)
    assert idea["direction"] == "LONG"
    # bull = 5 (mom) + 6 (buildup) + 2 (vol boost) + 1.5 (breadth) = 14.5 → 66 → High
    assert idea["conviction"] == 66 and idea["rating"] == "High"
    assert idea["stopPct"] == 3.0 and idea["targetPct"] == 6.0 and idea["rr"] == 2.0
    assert idea["stop"] == 97.0 and idea["target"] == 106.0
    assert idea["fno"] is True


def test_build_idea_short():
    e = {"symbol": "B", "ltp": 200, "pChange": -5.0, "oiSignal": "Short buildup",
         "volMult": 0, "tags": [], "listCount": 1}
    idea = nse._build_idea(e)
    assert idea["direction"] == "SHORT" and idea["conviction"] == 50
    assert idea["stop"] == 206.0 and idea["target"] == 188.0


def test_build_idea_short_covering_is_bullish():
    e = {"symbol": "C", "ltp": 100, "pChange": None, "oiSignal": "Short covering"}
    idea = nse._build_idea(e)
    assert idea["direction"] == "LONG"
    assert idea["stopPct"] == 1.0        # no pChange → move defaults to 1.0
    assert idea["targetPct"] == 2.0


def test_build_idea_conviction_capped_99():
    e = {"symbol": "D", "ltp": 100, "pChange": 8.0, "oiSignal": "Long buildup",
         "volMult": 30, "tags": [], "listCount": 6}
    assert nse._build_idea(e)["conviction"] == 99


def test_build_idea_rejects_no_edge():
    assert nse._build_idea({"symbol": "X", "ltp": None}) is None
    assert nse._build_idea({"symbol": "X", "ltp": 0, "pChange": 5}) is None
    # net move < 2 and nothing else → None
    assert nse._build_idea({"symbol": "X", "ltp": 100, "pChange": 0.5}) is None
    # no reasons at all → None
    assert nse._build_idea({"symbol": "X", "ltp": 100, "pChange": None}) is None


# ---------------------------------------------------------------------------
# list normalizers (stubbed _fetch)
# ---------------------------------------------------------------------------
def test_get_variations_allsec_and_limit():
    payload = {"allSec": {"data": [
        {"symbol": "A", "ltp": "100", "perChange": "2.5", "prev_price": "98", "trade_quantity": "10"},
        {"symbol": "B", "ltp": "50", "perChange": "-1", "prev_price": "51", "trade_quantity": "5"},
    ]}}
    with _patch(nse, "_fetch", lambda path, **k: payload):
        out = nse.get_variations("gainers", limit=1)
    assert len(out) == 1
    assert out[0] == {"symbol": "A", "ltp": 100.0, "pChange": 2.5,
                      "prevClose": 98.0, "volume": 10.0}


def test_get_variations_nifty_fallback():
    payload = {"NIFTY": {"data": [{"symbol": "Z", "ltp": "10", "perChange": "1"}]}}
    with _patch(nse, "_fetch", lambda path, **k: payload):
        out = nse.get_variations("gainers")
    assert out[0]["symbol"] == "Z" and out[0]["ltp"] == 10.0


def test_get_most_active():
    payload = {"data": [{"symbol": "A", "lastPrice": "100", "pChange": "1",
                         "totalTradedVolume": "999", "totalTradedValue": "12345"}]}
    with _patch(nse, "_fetch", lambda path, **k: payload):
        out = nse.get_most_active("value")
    assert out[0]["volume"] == 999.0 and out[0]["value"] == 12345.0


def test_get_volume_gainers():
    payload = {"data": [{"symbol": "A", "ltp": "100", "pChange": "3",
                         "volume": "1000", "week1AvgVolume": "100", "week1volChange": "10"}]}
    with _patch(nse, "_fetch", lambda path, **k: payload):
        out = nse.get_volume_gainers()
    assert out[0]["week1volChange"] == 10.0


def test_get_index_snapshot_maps_names():
    payload = {"data": [
        {"index": "NIFTY 50", "last": "22000", "percentChange": "0.8",
         "previousClose": "21800", "advances": "30", "declines": "20", "unchanged": "0"},
        {"index": "NIFTY IT", "last": "1", "percentChange": "0"},   # ignored
    ]}
    with _reset_cache("_index_cache"), _patch(nse, "_fetch", lambda path, **k: payload):
        out = nse.get_index_snapshot()
    assert set(out) == {"NIFTY"}
    assert out["NIFTY"]["pChange"] == 0.8 and out["NIFTY"]["advances"] == 30.0


def test_get_index_snapshot_includes_india_vix():
    payload = {"data": [
        {"index": "NIFTY 50", "last": "22000", "percentChange": "0.8",
         "previousClose": "21800", "advances": "30", "declines": "20"},
        {"index": "INDIA VIX", "last": "13.27", "percentChange": "3.02",
         "previousClose": "12.88", "yearHigh": "28.91", "yearLow": "8.72"},
    ]}
    with _reset_cache("_index_cache"), _patch(nse, "_fetch", lambda path, **k: payload):
        out = nse.get_index_snapshot()
    assert set(out) == {"NIFTY", "INDIAVIX"}
    vx = out["INDIAVIX"]
    assert vx["last"] == 13.27 and vx["yearHigh"] == 28.91 and vx["yearLow"] == 8.72
    # equity indices also carry the (harmless) yearHigh/yearLow keys now
    assert "yearHigh" in out["NIFTY"]


def test_get_oi_spurts_pct_and_signal():
    payload = {"data": [{"symbol": "A", "latestOI": "1200", "prevOI": "1000",
                         "changeInOI": "200", "underlyingValue": "100", "volume": "50"}]}
    with _patch(nse, "_fetch", lambda path, **k: payload), \
         _patch(nse, "_underlying_price_map", lambda: {"A": 2.0}):
        out = nse.get_oi_spurts()
    r = out[0]
    assert r["oiPctChange"] == 20.0
    assert r["signal"] == "Long buildup" and r["signalKind"] == "buildup"


def test_get_futures_basis_and_oi_na():
    payload = {"data": [
        {"underlying": "A", "lastPrice": "101", "underlyingValue": "100",
         "pChange": "1", "expiryDate": "31-Jul-2026", "openInterest": "1000", "volume": "200"},
        {"underlying": "B", "lastPrice": "50", "underlyingValue": "50",
         "pChange": "0", "expiryDate": "31-Jul-2026"},
    ]}
    with _patch(nse, "_fetch", lambda path, **k: payload), \
         _patch(nse, "_oi_change_map", lambda: {"A": 500.0}), \
         _patch(nse, "_days_to_expiry", lambda e: 15):
        out = nse.get_futures()
    a = next(r for r in out if r["symbol"] == "A")
    b = next(r for r in out if r["symbol"] == "B")
    assert a["basis"] == 1.0 and a["basisPct"] == 1.0
    assert a["annualizedPct"] == round(1.0 * 365 / 15, 1)
    assert a["signalKind"] == "buildup"
    assert b["signal"] == "OI n/a" and b["signalKind"] == "neutral"   # no OI change known


# ---------------------------------------------------------------------------
# scanner + aggregate builders (stubbed sub-getters)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _scanner_sources():
    """Symbol A appears across vol/value/volume/gainers/oi/futures → multi-signal."""
    with _patch(nse, "get_volume_gainers",
                lambda limit=40: [{"symbol": "A", "ltp": 100, "pChange": 5, "week1volChange": 30}]), \
         _patch(nse, "get_most_active",
                lambda kind, limit=25: {
                    "value": [{"symbol": "A", "ltp": 100, "pChange": 5, "value": 1e8}],
                    "volume": [{"symbol": "A", "ltp": 100, "pChange": 5, "volume": 999}],
                }[kind]), \
         _patch(nse, "get_variations",
                lambda kind, limit=25: [{"symbol": "A", "ltp": 100, "pChange": 5}]
                if kind == "gainers" else []), \
         _patch(nse, "get_oi_spurts",
                lambda limit=40: [{"symbol": "A", "ltp": 100, "pChange": 5,
                                   "signal": "Long buildup", "signalKind": "buildup",
                                   "changeInOI": 500}]), \
         _patch(nse, "get_futures",
                lambda limit=40: [{"symbol": "A", "spot": 100, "pChange": 5, "basisPct": 1.0}]):
        yield


def test_scanner_aggregates_multi_signal():
    with _scanner_sources():
        rows = nse.get_scanner(limit=10)
    assert len(rows) == 1
    a = rows[0]
    assert a["symbol"] == "A" and a["fno"] is True
    assert a["oiKind"] == "buildup" and a["listCount"] >= 3
    assert "⭐ Multi-signal" in a["tags"]


def test_scanner_filters():
    with _scanner_sources():
        assert nse.get_scanner(direction="down") == []          # A is +5%
        assert nse.get_scanner(oi="short") == []                # A is buildup
        assert nse.get_scanner(min_vol_mult=50) == []           # A volMult 30
        assert len(nse.get_scanner(fno_only=True)) == 1         # A is F&O


def test_demand_score():
    with _patch(nse, "get_volume_gainers",
                lambda limit=30: [{"symbol": "A", "week1volChange": 30, "ltp": 100, "pChange": 5}]), \
         _patch(nse, "get_most_active",
                lambda kind, limit=20: [{"symbol": "A", "value": 1e8, "ltp": 100, "pChange": 5}]), \
         _patch(nse, "get_variations",
                lambda kind, limit=20: [{"symbol": "A", "pChange": 5, "ltp": 100}]):
        out = nse.get_demand_score()
    assert out[0]["symbol"] == "A"
    assert out[0]["score"] == 25.5      # 3 (vol) + 20 (value rank0) + 2.5 (gain/2)
    assert out[0]["signalCount"] == 3


def test_price_map_and_get_price():
    stub = lambda *a, **k: [{"symbol": "A", "ltp": 100}, {"symbol": "B", "ltp": 200}]
    with _reset_cache("_ltp_cache"), \
         _patch(nse, "get_most_active", lambda kind, n=50: stub()), \
         _patch(nse, "get_volume_gainers", stub), \
         _patch(nse, "get_variations", lambda kind, n=50: stub()), \
         _patch(nse, "get_oi_spurts", stub):
        pmap = nse.get_price_map()
        assert pmap == {"A": 100, "B": 200}
        with _patch(nse, "get_price_map", lambda: {"A": 100}):
            assert nse.get_price("a") == 100        # case-insensitive hit
            import nse_quote
            with _patch(nse_quote, "get_ltp", lambda s: 55):
                assert nse.get_price("ghost") == 55  # fallback to per-stock quote
    assert nse.get_price(None) is None


def test_get_lot_size():
    with _patch(nse, "get_lot_sizes", lambda: {"NIFTY": 50}):
        assert nse.get_lot_size("nifty") == 50
        assert nse.get_lot_size("nope") is None
        assert nse.get_lot_size(None) is None


# ---------------------------------------------------------------------------
# Akamai / WAF block backoff
# ---------------------------------------------------------------------------
def test_note_block_and_blocked_for():
    with _reset_block():
        assert nse.blocked_for() == 0.0
        nse.note_block("test")
        assert 0 < nse.blocked_for() <= nse._BLOCK_COOLDOWN


def test_is_blocked_response():
    assert nse.is_blocked_response(_Resp(403))
    assert nse.is_blocked_response(_Resp(401))
    # a WAF page can arrive with a non-401/403 status — sniff the body markers
    assert nse.is_blocked_response(_Resp(503, text="... Access Denied  Reference #18.x"))
    assert nse.is_blocked_response(_Resp(500, text="blocked by edgesuite.net"))
    # a genuine miss / real payload is NOT a block
    assert not nse.is_blocked_response(_Resp(404, text="not found"))
    assert not nse.is_blocked_response(_Resp(200, json_data={"ok": 1}))


def test_fetch_marks_block_on_403_without_rebuild():
    """A 403 records the block and must NOT trigger a force-rebuild retry (the
    rebuild itself would fire two more GETs into the block)."""
    sess = _Sess(_Resp(403, text="Access Denied edgesuite.net"))
    seen = {"n": 0, "force": []}

    def fake_gs(force=False):
        seen["n"] += 1
        seen["force"].append(force)
        return sess

    with _reset_block(), _reset_fetch_cache(), _patch(nse, "get_session", fake_gs):
        raised = False
        try:
            nse._fetch("/api/x", ttl=0)
        except Exception:
            raised = True
        assert raised
        assert nse.blocked_for() > 0          # block recorded
        assert seen["n"] == 1                  # only the initial (non-forced) session
        assert seen["force"] == [False]        # never rebuilt into the block
        assert sess.calls == 1                 # exactly one NSE hit


def test_fetch_short_circuits_when_blocked_serving_stale():
    """While blocked, a stale cache entry is served WITHOUT touching NSE."""
    def boom(*a, **k):
        raise AssertionError("must not hit NSE while blocked")

    with _reset_block(), _reset_fetch_cache(), _patch(nse, "get_session", boom):
        nse._fetch_cache["/api/y"] = (time.time() - 9999, {"cached": True})  # stale
        nse.note_block("test")
        assert nse._fetch("/api/y") == {"cached": True}


def test_fetch_blocked_no_cache_raises_without_hitting_nse():
    def boom(*a, **k):
        raise AssertionError("must not hit NSE while blocked")

    with _reset_block(), _reset_fetch_cache(), _patch(nse, "get_session", boom):
        nse.note_block("test")
        raised = False
        try:
            nse._fetch("/api/z")
        except RuntimeError:
            raised = True
        assert raised


def test_get_session_skips_warmup_during_block():
    built = {"n": 0}

    def fake_build():
        built["n"] += 1
        return "FRESH"

    with _reset_block(), _patch(nse, "_build_session", fake_build):
        with nse._lock:                       # pretend we have an expired session
            nse._session = "STALE"
            nse._session_ts = time.time() - 10_000
        nse.note_block("test")
        # even force=True must reuse the stale session, not warm up into the block
        assert nse.get_session(force=True) == "STALE"
        assert built["n"] == 0


def test_get_session_blocked_no_session_raises():
    with _reset_block(), _patch(nse, "_build_session", lambda: "FRESH"):
        with nse._lock:
            nse._session = None
            nse._session_ts = 0.0
        nse.note_block("test")
        raised = False
        try:
            nse.get_session(force=True)
        except RuntimeError:
            raised = True
        assert raised


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} client tests passed")


if __name__ == "__main__":
    _main()
