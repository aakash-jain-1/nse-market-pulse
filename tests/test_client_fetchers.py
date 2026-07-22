"""
Unit tests for nse_client.py's remaining fetch/normalization surface.

test_client.py covers the signal/idea math; this file covers the raw-payload
PARSERS that turn NSE's JSON/CSV into clean rows — the part most likely to break
when NSE renames a field. Everything is driven through a fake requests.Session
or a stubbed _fetch, so no network is touched:

  get_stock_history      — securityArchives daily bars (field mapping, dedup, sort)
  get_futures_oi_history — foCPV OI rows (oi-None drop, date sort)
  get_fno_universe       — underlying-information → indices/stocks
  get_lot_sizes          — fo_mktlots.csv → {SYMBOL: lot}
  get_recommendations    — long/short split, F&O filter, per-side limit
  _underlying_price_map  — futures-first, gainers/most-active backfill (setdefault)
  _oi_change_map, _mean, _pct — small pure helpers

Run: python test_client_fetchers.py   (also works under pytest)
"""

import contextlib

from nse_pulse.core import nse_client as nse


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class _Resp:
    def __init__(self, payload=None, text=""):
        self._p = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    """Fake requests.Session: every .get() returns the same canned response."""
    def __init__(self, payload=None, text=""):
        self._resp = _Resp(payload, text)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self._resp


@contextlib.contextmanager
def _reset_caches():
    saved = (dict(nse._hist_cache), dict(nse._focpv_cache),
             dict(nse._fno_cache), dict(nse._lots_cache),
             dict(nse._price_cache), dict(nse._reco_cache))
    nse._hist_cache.clear()
    nse._focpv_cache.clear()
    nse._fno_cache.update(ts=0.0, data=None)
    nse._lots_cache.update(ts=0.0, map=None)
    nse._price_cache.update(ts=0.0, map={})
    nse._reco_cache.update(ts=0.0, data=None)
    try:
        yield
    finally:
        nse._hist_cache.clear(); nse._hist_cache.update(saved[0])
        nse._focpv_cache.clear(); nse._focpv_cache.update(saved[1])
        nse._fno_cache.clear(); nse._fno_cache.update(saved[2])
        nse._lots_cache.clear(); nse._lots_cache.update(saved[3])
        nse._price_cache.clear(); nse._price_cache.update(saved[4])
        nse._reco_cache.clear(); nse._reco_cache.update(saved[5])


# ---------------------------------------------------------------------------
# tiny pure helpers
# ---------------------------------------------------------------------------
def test_mean_and_pct():
    assert nse._mean([2, 4, None, 6]) == 4.0
    assert nse._mean([None]) is None
    assert round(nse._pct(110, 100), 6) == 10.0
    assert nse._pct(None, 100) is None
    assert nse._pct(110, 0) is None


# ---------------------------------------------------------------------------
# get_stock_history
# ---------------------------------------------------------------------------
def test_get_stock_history_maps_and_sorts():
    payload = {"data": [
        {"CH_TIMESTAMP": "2026-07-02", "mTIMESTAMP": "02-Jul-2026",
         "CH_OPENING_PRICE": "101", "CH_TRADE_HIGH_PRICE": "103",
         "CH_TRADE_LOW_PRICE": "100", "CH_CLOSING_PRICE": "102",
         "CH_PREVIOUS_CLS_PRICE": "100.5", "CH_TOT_TRADED_QTY": "2000",
         "COP_DELIV_PERC": "60"},
        {"CH_TIMESTAMP": "2026-07-01", "mTIMESTAMP": "01-Jul-2026",
         "CH_OPENING_PRICE": "100", "CH_TRADE_HIGH_PRICE": "101",
         "CH_TRADE_LOW_PRICE": "99", "CH_CLOSING_PRICE": "100.5",
         "CH_PREVIOUS_CLS_PRICE": "100", "CH_TOT_TRADED_QTY": "1000",
         "COP_DELIV_PERC": "55"},
        {"CH_TIMESTAMP": "2026-07-03", "CH_CLOSING_PRICE": None},   # dropped (no close)
    ]}
    with _reset_caches(), _patch(nse, "get_session", lambda: _Session(payload)):
        out = nse.get_stock_history("acc", chunks=1, chunk_days=80)
    assert [b["iso"] for b in out] == ["2026-07-01", "2026-07-02"]   # ascending
    assert out[0]["close"] == 100.5 and out[0]["date"] == "01-Jul-2026"
    assert out[0]["delivPct"] == 55.0 and out[1]["volume"] == 2000.0


# ---------------------------------------------------------------------------
# get_futures_oi_history
# ---------------------------------------------------------------------------
def test_get_futures_oi_history():
    payload = {"data": [
        {"FH_TIMESTAMP": "02-Jul-2026", "FH_CLOSING_PRICE": "205",
         "FH_UNDERLYING_VALUE": "204", "FH_OPEN_INT": "1200",
         "FH_CHANGE_IN_OI": "200", "FH_TOT_TRADED_QTY": "500", "FH_MARKET_LOT": "250"},
        {"FH_TIMESTAMP": "01-Jul-2026", "FH_CLOSING_PRICE": "200",
         "FH_UNDERLYING_VALUE": "199", "FH_OPEN_INT": "1000",
         "FH_CHANGE_IN_OI": "100", "FH_TOT_TRADED_QTY": "400", "FH_MARKET_LOT": "250"},
        {"FH_TIMESTAMP": "03-Jul-2026", "FH_OPEN_INT": None},   # dropped (no OI)
    ]}
    with _reset_caches(), _patch(nse, "get_session", lambda: _Session(payload)):
        out = nse.get_futures_oi_history("ACC", "31-JUL-2026")
    assert [r["date"] for r in out] == ["01-Jul-2026", "02-Jul-2026"]
    assert out[0]["oi"] == 1000.0 and out[1]["changeOi"] == 200.0
    assert nse.get_futures_oi_history("ACC", None) == []   # no expiry → empty


# ---------------------------------------------------------------------------
# get_fno_universe + get_lot_sizes
# ---------------------------------------------------------------------------
def test_get_fno_universe():
    payload = {"data": {"IndexList": [{"symbol": "NIFTY"}, {"symbol": None}],
                        "UnderlyingList": [{"symbol": "RELIANCE"}, {"symbol": "ACC"}]}}
    with _reset_caches(), _patch(nse, "_fetch", lambda *a, **k: payload):
        u = nse.get_fno_universe()
    assert u["indices"] == ["NIFTY"]
    assert u["stocks"] == ["ACC", "RELIANCE"]      # sorted
    assert u["count"] == 3


def test_get_lot_sizes_from_csv():
    csv_text = ("UNDERLYING,SYMBOL,JUL-26,AUG-26,SEP-26\n"
                "ACC LTD,ACC,250,250,250\n"
                "RELIANCE IND,RELIANCE,500,500,500\n"
                ",,,\n")                            # short/blank row → skipped
    with _reset_caches(), _patch(nse, "get_session", lambda: _Session(text=csv_text)):
        lots = nse.get_lot_sizes()
    assert lots == {"ACC": 250, "RELIANCE": 500}


# ---------------------------------------------------------------------------
# get_recommendations
# ---------------------------------------------------------------------------
def test_get_recommendations_split_filter_limit():
    from nse_pulse.sim import ideas_journal
    rows = [
        {"symbol": "A", "direction": "LONG", "conviction": 80, "fno": True},
        {"symbol": "B", "direction": "SHORT", "conviction": 70, "fno": False},
        {"symbol": "C", "direction": "LONG", "conviction": 90, "fno": False},
    ]
    with _reset_caches(), \
         _patch(nse, "get_scanner", lambda limit=250: list(rows)), \
         _patch(nse, "_build_idea", lambda e: dict(e)), \
         _patch(nse, "get_price_map", lambda: {}), \
         _patch(ideas_journal, "enrich", lambda longs, shorts, price_fn=None: (longs, shorts)):
        allrec = nse.get_recommendations()
        assert [i["symbol"] for i in allrec["longs"]] == ["C", "A"]   # by conviction desc
        assert allrec["count"] == 3

        fno = nse.get_recommendations(fno_only=True)     # served from cache, filtered
        assert [i["symbol"] for i in fno["longs"]] == ["A"] and fno["shorts"] == []

        lim = nse.get_recommendations(limit=1)
        assert len(lim["longs"]) == 1 and lim["longs"][0]["symbol"] == "C"


def test_get_recommendations_swr_serves_stale_then_refreshes():
    # Non-blocking: with an expired-but-present cache, the call returns the STALE set
    # immediately and swaps in the fresh one via a background thread (no sweep on the
    # request path — that's what killed the recurring per-poll stalls).
    import time as _t

    from nse_pulse.sim import ideas_journal
    with _reset_caches():
        nse._reco_cache.update(
            data={"longs": [{"symbol": "OLD", "fno": True}], "shorts": [], "generatedAt": "old"},
            ts=_t.time() - (nse._RECO_TTL + 5))
        nse._reco_running = False
        new_rows = [{"symbol": "NEW", "direction": "LONG", "conviction": 90, "fno": True}]
        with _patch(nse, "get_scanner", lambda limit=250: (_t.sleep(0.1), list(new_rows))[1]), \
             _patch(nse, "_build_idea", lambda e: dict(e)), \
             _patch(nse, "get_price_map", lambda: {}), \
             _patch(ideas_journal, "enrich", lambda longs, shorts, price_fn=None: (longs, shorts)):
            out = nse.get_recommendations()
            assert [i["symbol"] for i in out["longs"]] == ["OLD"]     # served stale, instantly
            deadline = _t.time() + 5
            while _t.time() < deadline and (nse._reco_running
                                            or nse._reco_cache["data"]["generatedAt"] == "old"):
                _t.sleep(0.02)
            assert [i["symbol"] for i in nse._reco_cache["data"]["longs"]] == ["NEW"]
        nse._reco_running = False


# ---------------------------------------------------------------------------
# _underlying_price_map + _oi_change_map
# ---------------------------------------------------------------------------
def test_underlying_price_map_precedence():
    fut = {"data": [{"underlying": "A", "pChange": "1.5"}]}
    with _reset_caches(), \
         _patch(nse, "_fetch", lambda *a, **k: fut), \
         _patch(nse, "get_variations",
                lambda kind, limit=500: [{"symbol": "B", "pChange": 2.0}] if kind == "gainers" else []), \
         _patch(nse, "get_most_active",
                lambda kind, limit=100: [{"symbol": "C", "pChange": 3.0}] if kind == "volume" else []):
        pm = nse._underlying_price_map()
    assert pm == {"A": 1.5, "B": 2.0, "C": 3.0}


def test_oi_change_map():
    with _patch(nse, "_fetch", lambda *a, **k: {"data": [{"symbol": "A", "changeInOI": "500"}]}):
        assert nse._oi_change_map() == {"A": 500.0}


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} client-fetcher tests passed")


if __name__ == "__main__":
    _main()
