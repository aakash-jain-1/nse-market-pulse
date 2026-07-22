"""
Unit tests for nse_quote.py — option math + NextApi/charting parsers.

Pure math: _norm_cdf/_norm_pdf, Black-Scholes greeks (call/put delta signs,
positive gamma/vega, negative call theta, None on bad inputs), max-pain, _num,
the bounded LRU-ish _cache_put, and the baked-epoch roundtrip. Parsers
(get_quote depth, get_chart, get_option_chain analytics, get_option_price,
get_symbol_futures basis math, get_near_future) are driven through a fake session
/ stubbed _call so nothing touches the network.

Run: python test_quote.py   (also works under pytest)
"""

import contextlib
import datetime as dt
import math

from nse_pulse.core import intrabar
from nse_pulse.core import nse_client as nse
from nse_pulse.core import nse_quote as q


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._p = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http %d" % self.status_code)

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._p = payload

    def get(self, *a, **k):
        return _Resp(self._p)


@contextlib.contextmanager
def _reset_block():
    """Clear + restore nse_client's shared WAF-block cooldown around a test."""
    saved = nse._blocked_until
    nse._blocked_until = 0.0
    try:
        yield
    finally:
        nse._blocked_until = saved


@contextlib.contextmanager
def _clean_cache():
    saved = dict(q._cache)
    q._cache.clear()
    try:
        yield
    finally:
        q._cache.clear()
        q._cache.update(saved)


# ---------------------------------------------------------------------------
# normal cdf/pdf
# ---------------------------------------------------------------------------
def test_norm_cdf_pdf():
    assert abs(q._norm_cdf(0.0) - 0.5) < 1e-9
    assert q._norm_cdf(8) > 0.999999 and q._norm_cdf(-8) < 1e-6
    assert abs(q._norm_pdf(0.0) - 1 / math.sqrt(2 * math.pi)) < 1e-9


# ---------------------------------------------------------------------------
# Black-Scholes greeks
# ---------------------------------------------------------------------------
def test_greeks_call_vs_put():
    call = q._bs_greeks(100, 100, 30, 20, True)
    put = q._bs_greeks(100, 100, 30, 20, False)
    assert 0 < call["delta"] < 1
    assert -1 < put["delta"] < 0
    assert abs((call["delta"] - put["delta"]) - 1.0) < 0.01   # put = call - 1
    assert call["gamma"] > 0 and call["vega"] > 0
    assert call["theta"] < 0                                   # long call bleeds time
    assert call["gamma"] == put["gamma"]                       # gamma/vega same for CE/PE
    assert call["vega"] == put["vega"]


def test_greeks_bad_inputs_return_nones():
    for bad in (
        q._bs_greeks(0, 100, 30, 20, True),
        q._bs_greeks(100, 100, 0, 20, True),
        q._bs_greeks(100, 100, 30, 0, True),
        q._bs_greeks(None, 100, 30, 20, True),
    ):
        assert bad == {"delta": None, "gamma": None, "theta": None, "vega": None}


def test_greeks_deep_itm_call_delta_near_one():
    g = q._bs_greeks(200, 100, 30, 20, True)
    assert g["delta"] > 0.95


# ---------------------------------------------------------------------------
# _num
# ---------------------------------------------------------------------------
def test_num_coercion():
    assert q._num("12.5") == 12.5
    assert q._num(5) == 5.0
    assert q._num(None) is None
    assert q._num("abc") is None
    assert q._num("-3.2,") is None   # stray comma → not a float


# ---------------------------------------------------------------------------
# max pain
# ---------------------------------------------------------------------------
def test_max_pain_picks_min_writer_loss():
    rows = [
        {"strike": 100, "ce": {"oi": 0}, "pe": {"oi": 200}},
        {"strike": 110, "ce": {"oi": 100}, "pe": {"oi": 100}},
        {"strike": 120, "ce": {"oi": 300}, "pe": {"oi": 0}},
    ]
    assert q._max_pain(rows) == 110


def test_max_pain_empty():
    assert q._max_pain([]) is None
    assert q._max_pain([{"strike": None, "ce": None, "pe": None}]) is None


# ---------------------------------------------------------------------------
# cache eviction
# ---------------------------------------------------------------------------
def test_cache_put_bounded():
    with _clean_cache(), _patch(q, "_CACHE_MAX", 10):
        for i in range(60):
            q._cache_put(("k", i), i)
        assert len(q._cache) <= 10
        assert ("k", 59) in q._cache        # newest survives
        assert ("k", 0) not in q._cache     # oldest evicted


# ---------------------------------------------------------------------------
# get_quote / get_chart parsers
# ---------------------------------------------------------------------------
def test_get_quote_normalizes_and_builds_depth():
    payload = {"equityResponse": [{
        "metaData": {"companyName": "Acme", "change": 2.5, "pChange": 1.2,
                     "open": 99, "dayHigh": 103, "dayLow": 98,
                     "previousClose": 98.5, "averagePrice": 100.4},
        "tradeInfo": {"lastPrice": 101.0, "totalTradedVolume": 5000,
                      "totalTradedValue": 500000, "deliveryToTradedQuantity": 60},
        "priceInfo": {"yearHigh": 150, "yearLow": 80, "priceBand": "No Band"},
        "orderBook": {f"buyPrice{i}": 100 - i for i in range(1, 6)}
                     | {f"buyQuantity{i}": 10 * i for i in range(1, 6)}
                     | {f"sellPrice{i}": 101 + i for i in range(1, 6)}
                     | {f"sellQuantity{i}": 5 * i for i in range(1, 6)},
        "lastUpdateTime": "now",
    }]}
    with _clean_cache(), _patch(q, "_call", lambda query, symbol: payload):
        out = q.get_quote("acme")
    assert out["symbol"] == "ACME" and out["ltp"] == 101.0
    assert out["vwap"] == 100.4 and out["deliveryPct"] == 60
    assert len(out["depth"]["bids"]) == 5 and len(out["depth"]["asks"]) == 5
    assert out["depth"]["bids"][0] == {"price": 99.0, "qty": 10.0}
    assert out["source"] == "nse"   # provenance chip: dashboard shows where each number came from


def test_get_chart_parses_points():
    payload = {"grapthData": [[1000, 100.5], [2000, 101.0], [3000]],  # 3rd malformed
               "name": "Acme", "closePrice": 99.0}
    with _clean_cache(), _patch(q, "_call", lambda query, symbol: payload):
        out = q.get_chart("acme")
    assert out["prevClose"] == 99.0
    assert out["points"] == [{"t": 1000, "price": 100.5}, {"t": 2000, "price": 101.0}]
    assert out["source"] == "nse"


# ---------------------------------------------------------------------------
# option chain integration (analytics on top of _leg + greeks)
# ---------------------------------------------------------------------------
def _oc_item(strike, ce_oi, pe_oi):
    return {
        "CE": {"strikePrice": strike, "openInterest": ce_oi, "lastPrice": 5,
               "impliedVolatility": 20, "totalTradedVolume": 1},
        "PE": {"strikePrice": strike, "openInterest": pe_oi, "lastPrice": 4,
               "impliedVolatility": 22, "totalTradedVolume": 1},
    }


def test_get_option_chain_analytics():
    payload = {"underlyingValue": 110, "timestamp": "t",
               "data": [_oc_item(100, 0, 200), _oc_item(110, 100, 100),
                        _oc_item(120, 300, 0)]}
    with _clean_cache(), \
         _patch(q, "get_option_expiries", lambda s: ["28-Jul-2026"]), \
         _patch(q, "_oc_warm", lambda s: None), \
         _patch(nse, "get_session", lambda *a, **k: _Session(payload)), \
         _patch(nse, "_days_to_expiry", lambda e: 7), \
         _patch(nse, "get_lot_size", lambda s: 50):
        oc = q.get_option_chain("acme")
    assert oc["pcr"] == 0.75                       # 300 / 400
    assert oc["maxPain"] == 110
    assert oc["atmStrike"] == 110
    assert oc["support"][0]["strike"] == 100       # biggest PUT OI
    assert oc["resistance"][0]["strike"] == 120    # biggest CALL OI
    assert oc["rows"][0]["ce"]["delta"] is not None  # greeks attached
    assert oc["lotSize"] == 50


def test_get_option_price_picks_leg():
    chain = {"rows": [{"strike": 100.0, "ce": {"ltp": 12.5}, "pe": {"ltp": 8.0}}]}
    with _patch(q, "get_option_chain", lambda u, e: chain):
        assert q.get_option_price("X", "exp", 100, "CE") == 12.5
        assert q.get_option_price("X", "exp", 100, "pe") == 8.0
        assert q.get_option_price("X", "exp", 999, "CE") is None


# ---------------------------------------------------------------------------
# futures basis math
# ---------------------------------------------------------------------------
def test_get_symbol_futures_basis():
    payload = {"data": [{
        "instrumentType": "FUTSTK", "underlyingValue": 100, "lastPrice": 101,
        "pchange": 1.0, "changeinOpenInterest": 500, "expiryDate": "31-Jul-2026",
        "openInterest": 1000, "totalTradedVolume": 200,
    }]}
    with _clean_cache(), \
         _patch(q, "_deriv_warm", lambda s: None), \
         _patch(nse, "get_session", lambda *a, **k: _Session(payload)), \
         _patch(nse, "_days_to_expiry", lambda e: 15), \
         _patch(nse, "_oi_signal", lambda rising, pc: ("Long buildup", "buildup")), \
         _patch(nse, "get_lot_size", lambda s: 50):
        out = q.get_symbol_futures("acme")
    fut = out["futures"][0]
    assert fut["basis"] == 1.0 and fut["basisPct"] == 1.0
    assert fut["annualizedPct"] == round(1.0 * 365 / 15, 1)   # 24.3
    assert fut["signalKind"] == "buildup" and out["lotSize"] == 50


def test_get_near_future_wraps():
    with _patch(q, "get_symbol_futures", lambda s: {"futures": [{"ltp": 1}, {"ltp": 2}]}):
        assert q.get_near_future("X") == {"ltp": 1}
    with _patch(q, "get_symbol_futures", lambda s: {"futures": []}):
        assert q.get_near_future("X") is None


# ---------------------------------------------------------------------------
# baked-epoch time helper (must roundtrip through intrabar.candle_dt)
# ---------------------------------------------------------------------------
def test_baked_epoch_roundtrip():
    naive = dt.datetime(2026, 7, 15, 9, 15, 0)
    ms = q._baked_epoch(naive) * 1000
    assert intrabar.candle_dt(ms) == naive


# ---------------------------------------------------------------------------
# WAF-block backoff (shared cooldown from nse_client)
# ---------------------------------------------------------------------------
def test_sget_short_circuits_when_blocked():
    def boom(*a, **k):
        raise AssertionError("must not hit NSE while blocked")

    with _reset_block(), _patch(nse, "get_session", boom):
        nse.note_block("test")
        raised = False
        try:
            q._sget("https://x/api")
        except RuntimeError:
            raised = True
        assert raised


def test_sget_marks_block_on_403():
    calls = {"n": 0}

    class _S:
        def get(self, *a, **k):
            calls["n"] += 1
            return _Resp({}, status=403, text="Access Denied edgesuite.net")

    with _reset_block(), _patch(nse, "get_session", lambda *a, **k: _S()):
        raised = False
        try:
            q._sget("https://x/api")
        except RuntimeError:
            raised = True
        assert raised
        assert calls["n"] == 1                 # one hit, then recorded
        assert nse.blocked_for() > 0


def test_call_does_not_retry_into_a_block():
    """A 403 on the first NextApi GET records the block; _call must NOT do its
    force-rebuild retry (that would fire warm-up + call straight into the block)."""
    calls = {"n": 0}

    class _S:
        def get(self, *a, **k):
            calls["n"] += 1
            return _Resp({}, status=403, text="Access Denied")

    with _reset_block(), _clean_cache(), \
            _patch(q, "_warm", lambda s: None), \
            _patch(nse, "get_session", lambda *a, **k: _S()):
        raised = False
        try:
            q._call("getSymbolData&symbol=ACME", "ACME")
        except Exception:
            raised = True
        assert raised
        assert calls["n"] == 1                  # NO retry into the block
        assert nse.blocked_for() > 0


def test_warm_skipped_during_block():
    def boom(*a, **k):
        raise AssertionError("warm must not hit NSE while blocked")

    with _reset_block(), _patch(nse, "get_session", boom):
        q._warmed.discard("ACME")
        nse.note_block("test")
        q._warm("ACME")                         # returns immediately, no GET
        assert "ACME" not in q._warmed


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} quote tests passed")


if __name__ == "__main__":
    _main()
