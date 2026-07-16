"""
Extra unit tests for nse_quote.py — the fetch/parse surface test_quote.py skips.

Covers the raw-payload parsers + thin wrappers, all stubbed (no network):
  _leg              — one NSE option-chain leg dict → clean keys
  get_ltp           — get_quote wrapper (+ swallow errors → None)
  get_token         — symbolsDynamic search: exact -EQ pref, prefix fallback, cache
  get_ohlc          — charting candles parse (+ token-not-found error path)
  get_option_expiries / get_option_summary — dropdown + cross-expiry rollup
  _baked_epoch / _now_ist_naive / _ist_session_start_epoch — the IST-as-UTC clock

Run: python test_quote_more.py   (also works under pytest)
"""

import contextlib
import datetime as _dt

import nse_quote as q


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._p = payload

    def get(self, *a, **k):
        return _Resp(self._p)


@contextlib.contextmanager
def _clean():
    saved_cache, saved_tok = dict(q._cache), dict(q._token_cache)
    q._cache.clear()
    q._token_cache.clear()
    try:
        yield
    finally:
        q._cache.clear(); q._cache.update(saved_cache)
        q._token_cache.clear(); q._token_cache.update(saved_tok)


# ---------------------------------------------------------------------------
# _leg
# ---------------------------------------------------------------------------
def test_leg_maps_fields():
    leg = q._leg({"openInterest": "1000", "changeinOpenInterest": "50",
                  "pchangeinOpenInterest": "5", "lastPrice": "12.5",
                  "change": "1.2", "impliedVolatility": "18.4",
                  "totalTradedVolume": "3000", "buyPrice1": "12.4", "sellPrice1": "12.6"})
    assert leg["oi"] == 1000.0 and leg["chgOi"] == 50.0 and leg["iv"] == 18.4
    assert leg["ltp"] == 12.5 and leg["bid"] == 12.4 and leg["ask"] == 12.6


# ---------------------------------------------------------------------------
# get_ltp
# ---------------------------------------------------------------------------
def test_get_ltp_wraps_quote():
    with _patch(q, "get_quote", lambda s: {"ltp": 123.4}):
        assert q.get_ltp("ACC") == 123.4

    def _boom(s):
        raise RuntimeError("down")
    with _patch(q, "get_quote", _boom):
        assert q.get_ltp("ACC") is None


# ---------------------------------------------------------------------------
# get_token
# ---------------------------------------------------------------------------
def test_get_token_prefers_exact_eq_then_prefix():
    data = {"data": [{"symbol": "ACC-BE", "scripcode": 111},
                     {"symbol": "ACC-EQ", "scripcode": 222}]}
    with _clean(), _patch(q, "_charting_get", lambda p: data):
        assert q.get_token("acc") == 222        # exact -EQ wins
        assert q._token_cache["ACC"] == 222      # cached

    prefix_only = {"data": [{"symbol": "XYZ-BE", "scripcode": 999}]}
    with _clean(), _patch(q, "_charting_get", lambda p: prefix_only):
        assert q.get_token("XYZ") == 999        # prefix fallback

    with _clean(), _patch(q, "_charting_get", lambda p: {"data": []}):
        assert q.get_token("NOPE") is None


# ---------------------------------------------------------------------------
# get_ohlc
# ---------------------------------------------------------------------------
def test_get_ohlc_parses_candles():
    candles = {"data": [{"time": 1700000000, "open": "10", "high": "11",
                         "low": "9", "close": "10.5", "volume": "1000"}]}
    with _clean(), _patch(q, "get_token", lambda s: "TKN"), \
         _patch(q, "_charting_get", lambda p: candles):
        out = q.get_ohlc("ACC", interval=1, from_ts=1699990000, to_ts=1700000000)
    assert out["token"] == "TKN" and len(out["points"]) == 1
    pt = out["points"][0]
    assert pt["o"] == 10.0 and pt["c"] == 10.5 and pt["v"] == 1000.0


def test_get_ohlc_token_not_found():
    with _clean(), _patch(q, "get_token", lambda s: None):
        out = q.get_ohlc("ACC")
    assert out["error"] == "token-not-found" and out["points"] == []


# ---------------------------------------------------------------------------
# get_option_expiries / get_option_summary
# ---------------------------------------------------------------------------
def test_get_option_expiries():
    with _patch(q, "_oc_warm", lambda s: None), \
         _patch(q.nse, "get_session", lambda: _Session({"expiryDates": ["28-Jul-2026", "25-Aug-2026"]})):
        exps = q.get_option_expiries("acc")
    assert exps == ["28-Jul-2026", "25-Aug-2026"]


def test_get_option_summary_rolls_up_expiries():
    with _patch(q, "get_option_expiries", lambda s: ["28-Jul-2026", "25-Aug-2026"]), \
         _patch(q, "get_option_chain",
                lambda s, exp: {"underlying": 200.0, "pcr": 1.1, "maxPain": 200,
                                "ceTotOI": 1000, "peTotOI": 1100}):
        out = q.get_option_summary("ACC")
    assert out["underlying"] == 200.0 and len(out["expiries"]) == 2
    assert out["expiries"][0]["expiry"] == "28-Jul-2026" and out["expiries"][0]["pcr"] == 1.1


# ---------------------------------------------------------------------------
# IST-as-UTC clock helpers
# ---------------------------------------------------------------------------
def test_baked_epoch_and_session_start():
    dt = _dt.datetime(2026, 7, 8, 9, 15, 0)
    assert q._baked_epoch(dt) == int(dt.replace(tzinfo=_dt.timezone.utc).timestamp())
    # session start is 09:15 of "today" in the IST wall clock, baked as UTC
    start = q._ist_session_start_epoch()
    naive = q._now_ist_naive()
    expect = q._baked_epoch(naive.replace(hour=9, minute=15, second=0, microsecond=0))
    assert start == expect
    assert q._baked_now() >= start or naive.hour < 9   # now is at/after session start


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} quote-more tests passed")


if __name__ == "__main__":
    _main()
