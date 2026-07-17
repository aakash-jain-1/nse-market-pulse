"""
Route/wiring tests for app.py — every JSON endpoint via the Flask test client.

test_app.py covers the middleware (CSRF / token / headers / error contract);
this file covers the ROUTE TABLE: each handler forwards to the right backend,
parses its query/body args correctly, and passes the result through jsonify with
the right HTTP status. Backends are stubbed (modules imported inside handlers
resolve to the cached module, so patching module attributes works), so nothing
hits NSE / brokers / the DB.

Run: python test_app_routes.py   (also works under pytest)
"""

import contextlib
import os
import shutil
import tempfile

import app as webapp

client = webapp.app.test_client()


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _patches(*triples):
    with contextlib.ExitStack() as st:
        for obj, name, value in triples:
            st.enter_context(_patch(obj, name, value))
        yield


def _json(path, **kw):
    r = client.get(path, **kw)
    return r.status_code, r.get_json()


# ---------------------------------------------------------------------------
# simple pass-through GET boards
# ---------------------------------------------------------------------------
def test_board_endpoints_passthrough():
    import nse_client as nse
    with _patches(
        (nse, "get_variations", lambda k, limit=20: [{"symbol": k}]),
        (nse, "get_most_active", lambda k, limit=20: [{"active": k}]),
        (nse, "get_volume_gainers", lambda limit=20: [{"vg": 1}]),
        (nse, "get_oi_spurts", lambda limit=20: [{"oi": 1}]),
        (nse, "get_futures", lambda limit=25: [{"fut": 1}]),
        (nse, "get_all_futures", lambda: [{"allfut": 1}]),
        (nse, "get_demand_score", lambda limit=25: [{"demand": 1}]),
        (nse, "get_fno_universe", lambda: {"stocks": ["A"], "count": 1}),
    ):
        assert _json("/api/gainers") == (200, [{"symbol": "gainers"}])
        assert _json("/api/losers") == (200, [{"symbol": "losers"}])
        assert _json("/api/volume") == (200, [{"active": "volume"}])
        assert _json("/api/value") == (200, [{"active": "value"}])
        assert _json("/api/volgainers") == (200, [{"vg": 1}])
        assert _json("/api/oispurts") == (200, [{"oi": 1}])
        assert _json("/api/futures") == (200, [{"fut": 1}])
        assert _json("/api/futures/all") == (200, [{"allfut": 1}])
        assert _json("/api/demand") == (200, [{"demand": 1}])
        assert _json("/api/fno/universe")[1]["count"] == 1


def test_recommendations_fno_arg():
    import nse_client as nse
    seen = {}
    with _patch(nse, "get_recommendations",
                lambda fno_only=False, limit=None: seen.update(fno=fno_only) or {"longs": [], "shorts": []}):
        client.get("/api/recommendations?fno=1")
    assert seen["fno"] is True


# ---------------------------------------------------------------------------
# per-symbol quote / chart / futures / deepdive / option chain
# ---------------------------------------------------------------------------
def test_symbol_quote_chart_futures():
    import nse_client as nse
    import nse_quote as q
    with _patches(
        (q, "get_symbol_futures", lambda s: {"symbol": s, "basis": 1}),
        (nse, "get_stock_deepdive", lambda s: {"symbol": s, "dd": 1}),
        (q, "get_quote", lambda s: {"symbol": s, "ltp": 10}),
        (q, "get_chart", lambda s: {"symbol": s, "points": []}),
        (q, "get_option_chain", lambda s, e: {"symbol": s, "expiry": e}),
        (q, "get_option_summary", lambda s: {"symbol": s, "expiries": []}),
    ):
        assert _json("/api/futures/ACC")[1]["symbol"] == "ACC"
        assert _json("/api/deepdive/ACC")[1]["dd"] == 1
        assert _json("/api/quote/ACC")[1]["ltp"] == 10
        assert _json("/api/chart/ACC")[1]["symbol"] == "ACC"
        assert _json("/api/optionchain/ACC?expiry=31-Jul-2026")[1]["expiry"] == "31-Jul-2026"
        assert _json("/api/optionchain/ACC/summary")[1]["symbol"] == "ACC"


def test_ohlc_arg_parsing():
    import nse_quote as q
    seen = {}

    def fake(sym, **kw):
        seen.update(symbol=sym, **kw)
        return {"points": []}
    with _patch(q, "get_ohlc", fake):
        client.get("/api/ohlc/ACC?interval=5&type=D&days=30&from=100&to=200")
    assert seen["symbol"] == "ACC" and seen["interval"] == 5
    assert seen["chart_type"] == "D" and seen["days"] == 30
    assert seen["from_ts"] == 100 and seen["to_ts"] == 200


def test_depth_splits_symbols():
    import nse_quote as q
    seen = {}
    with _patch(q, "get_book_stats", lambda syms: seen.update(syms=syms) or {"A": {}}):
        st, j = _json("/api/depth?symbols=A,B,C")
    assert st == 200 and j == {"symbols": {"A": {}}}
    assert seen["syms"] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# ideas journal (imported inside the handler)
# ---------------------------------------------------------------------------
def test_ideas_endpoints():
    import ideas_journal as ij
    seen = {}
    with _patches(
        (ij, "history", lambda limit=60: seen.update(hist=limit) or {"days": []}),
        (ij, "day_ideas", lambda date: {"date": date, "count": 0}),
        (ij, "recent", lambda window_min=60, limit=40, min_rating=None:
            seen.update(win=window_min, lim=limit, mr=min_rating) or {"ideas": []}),
    ):
        assert _json("/api/ideas/history?limit=5")[1] == {"days": []}
        assert seen["hist"] == 5
        assert _json("/api/ideas/day?date=2026-07-16")[1]["date"] == "2026-07-16"
        _json("/api/ideas/recent?window=30&limit=7&min=High")
    assert seen["win"] == 30 and seen["lim"] == 7 and seen["mr"] == "High"


def test_ideas_history_bad_limit_defaults():
    import ideas_journal as ij
    seen = {}
    with _patch(ij, "history", lambda limit=60: seen.update(l=limit) or {}):
        client.get("/api/ideas/history?limit=abc")
    assert seen["l"] == 60


# ---------------------------------------------------------------------------
# alerts (notify, imported inside handler)
# ---------------------------------------------------------------------------
def test_alerts_status_and_test():
    import notify
    with _patches(
        (notify, "public_status", lambda: {"configured": False}),
        (notify, "send_test", lambda: {"sent": True}),
    ):
        assert _json("/api/alerts/status")[1] == {"configured": False}
        r = client.post("/api/alerts/test")
        assert r.status_code == 200 and r.get_json() == {"sent": True}


# ---------------------------------------------------------------------------
# live feed (live_feed module reference on webapp)
# ---------------------------------------------------------------------------
def test_live_endpoints():
    import types
    fake = types.SimpleNamespace(
        public_status=lambda: {"provider": "nse", "connected": False},
        set_watch=lambda syms, focus: {"watched": syms, "focus": focus},
        snapshot=lambda ids=None: {"A": {"ltp": 1}},
    )
    import nse_quote as q
    with _patches((webapp, "live_feed", fake),
                  (q, "get_ohlc", lambda s, **k: {"symbol": s, "points": [], "kw": k})):
        assert _json("/api/live/config")[1]["provider"] == "nse"
        r = client.post("/api/live/watch", json={"symbols": ["A", "B"], "focus": "A"})
        assert r.get_json() == {"watched": ["A", "B"], "focus": "A"}
        st, j = _json("/api/live/snapshot?ids=A,B")
        assert j["quotes"] == {"A": {"ltp": 1}} and "status" in j
        # seed: intraday vs daily
        assert _json("/api/live/seed/ACC?interval=5")[1]["symbol"] == "ACC"
        assert _json("/api/live/seed/ACC?interval=D&days=90")[1]["kw"]["chart_type"] == "D"


# ---------------------------------------------------------------------------
# paper trading orders
# ---------------------------------------------------------------------------
def test_paper_portfolio_and_orders():
    import paper
    with _patch(paper, "portfolio", lambda: {"cash": 1000000}):
        assert _json("/api/paper/portfolio")[1]["cash"] == 1000000
    with _patch(paper, "place_option_order",
                lambda u, e, s, ot, side, lots: (True, "ok", {"id": 2})):
        r = client.post("/api/paper/option_order",
                        json={"underlying": "ACC", "expiry": "x", "strike": 100,
                              "optType": "CE", "side": "BUY", "lots": 1})
        assert r.status_code == 200 and r.get_json()["order"]["id"] == 2
    with _patch(paper, "place_futures_order", lambda s, side, lots: (False, "no margin", None)):
        r = client.post("/api/paper/futures_order",
                        json={"symbol": "ACC", "side": "BUY", "lots": 1})
        assert r.status_code == 400 and r.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# sim read endpoints (+ book arg)
# ---------------------------------------------------------------------------
def test_sim_read_endpoints_and_book():
    import sim
    import strategies
    seen = {}
    with _patches(
        (strategies, "strategy_meta", lambda: [{"id": "momentum"}]),
        (sim, "summary", lambda strategy_id=None, book="cash": seen.update(sum_book=book, sid=strategy_id) or {"mode": "overview", "book": book}),
        (sim, "daily_matrix", lambda book="cash": {"rows": []}),
        (sim, "daily_performance", lambda book="cash": {"today": {}}),
        (sim, "day_trades", lambda date, book="cash": {"date": date, "book": book}),
        (sim, "leaderboard_bundle", lambda book="cash": {"regime": {}}),
        (sim, "performance", lambda book="cash": {"rows": [], "book": book}),
        (sim, "analytics", lambda book="cash": {"portfolio": {}}),
        (sim, "current_regime", lambda: {"label": "Range"}),
    ):
        assert _json("/api/sim/strategies")[1]["strategies"][0]["id"] == "momentum"
        assert _json("/api/sim/summary?strategy=momentum&book=fno")[1]["book"] == "fno"
        assert seen["sum_book"] == "fno" and seen["sid"] == "momentum"
        assert _json("/api/sim/daily?book=fno")[1] == {"rows": [], "perf": {"today": {}}}
        assert _json("/api/sim/day?date=2026-07-16")[1]["date"] == "2026-07-16"
        assert _json("/api/sim/leaderboard")[1] == {"regime": {}}
        assert _json("/api/sim/performance?book=fno")[1]["book"] == "fno"
        assert _json("/api/sim/analytics")[1] == {"portfolio": {}}
        assert _json("/api/sim/regime")[1]["label"] == "Range"


def test_sim_write_endpoints():
    import sim
    seen = {}
    with _patches(
        (sim, "take", lambda strategy_ids=None, book="cash": seen.update(ids=strategy_ids, take_book=book) or 3),
        (sim, "summary", lambda strategy_id=None, book="cash": {"book": book, "strategy": strategy_id}),
        (sim, "set_auto", lambda on: bool(on)),
        (sim, "set_entry_mode", lambda m: "open" if m == "open" else "continuous"),
        (sim, "reset", lambda book=None: seen.update(reset_book=book)),
    ):
        r = client.post("/api/sim/take", json={"book": "fno", "strategy": "momentum"})
        j = r.get_json()
        assert j["added"] == 3 and seen["ids"] == ["momentum"] and seen["take_book"] == "fno"
        assert client.post("/api/sim/auto", json={"on": True}).get_json() == {"auto": True}
        assert client.post("/api/sim/mode", json={"entryMode": "open"}).get_json() == {"entryMode": "open"}
        client.post("/api/sim/reset", json={"book": "fno"})
        assert seen["reset_book"] == "fno"


def test_sim_backtest_arg_parsing():
    import backtest_strategies as bt
    import backtest_daily as btd
    seen = {}
    with _patch(bt, "run", lambda **k: seen.update(k) or {"strategies": []}):
        client.get("/api/sim/backtest?days=5&resolve=ltp&maxSessions=2&entryMode=open")
    assert seen["max_sessions"] == 2 and seen["entry_mode"] == "open"
    assert seen["resolve"] == "ltp" and seen["since_day"] is not None

    seen2 = {}
    with _patch(btd, "run", lambda **k: seen2.update(k) or {"mode": "daily"}):
        client.get("/api/sim/backtest_daily?days=10&universe=20&maxHold=4&refresh=1&resolve=intrabar")
    assert seen2["days"] == 10 and seen2["universe_size"] == 20
    assert seen2["max_hold"] == 4 and seen2["force"] is True and seen2["resolve"] == "intrabar"


def test_sim_strategy_of_day():
    import backtest_daily as btd
    seen = {}
    with _patch(btd, "strategy_of_day",
                lambda days=60, universe_size=60: seen.update(days=days, u=universe_size) or {"pick": None}):
        client.get("/api/sim/strategy_of_day?days=30&universe=25")
    assert seen["days"] == 30 and seen["u"] == 25


def test_sim_walkforward_arg_parsing():
    import walkforward as wf
    seen = {}
    with _patch(wf, "run", lambda **k: seen.update(k) or {"ok": True}):
        st, j = _json("/api/sim/walkforward?days=90&universe=50&maxHold=6&folds=5")
    assert st == 200 and j == {"ok": True}
    assert seen["days"] == 90 and seen["universe_size"] == 50
    assert seen["max_hold"] == 6 and seen["folds"] == 5


# ---------------------------------------------------------------------------
# EOD bhavcopy endpoints
# ---------------------------------------------------------------------------
def test_eod_status_price_quote():
    import bhavcopy
    seen = {}
    with _patches(
        (bhavcopy, "status", lambda refresh=False: seen.update(refresh=refresh)
            or {"cmDate": "2026-07-16", "equities": 3, "cached": True}),
        (bhavcopy, "eod_close", lambda s: 123.4),
        (bhavcopy, "eod_quote", lambda s: {"symbol": s.upper(), "close": 123.4}),
    ):
        st, j = _json("/api/eod/status")
        assert st == 200 and j["cmDate"] == "2026-07-16" and seen["refresh"] is False
        _json("/api/eod/status?refresh=1")
        assert seen["refresh"] is True
        st, j = _json("/api/eod/price/reliance")
        assert st == 200 and j == {"symbol": "RELIANCE", "close": 123.4,
                                   "date": "2026-07-16"}
        st, j = _json("/api/eod/quote/tcs")
        assert st == 200 and j["symbol"] == "TCS"


def test_eod_refresh_post():
    import bhavcopy
    seen = {}
    with _patch(bhavcopy, "ingest_db",
                lambda date=None: seen.update(date=date) or {"bars": 3166, "oi": 215}):
        r = client.post("/api/eod/refresh", json={})
        assert r.status_code == 200 and r.get_json()["bars"] == 3166
        assert seen["date"] is None
        client.post("/api/eod/refresh", json={"date": "2026-07-15"})
        assert seen["date"] == "2026-07-15"


# ---------------------------------------------------------------------------
# logger endpoints
# ---------------------------------------------------------------------------
def test_log_endpoints():
    with _patches(
        (webapp.snaplog, "status", lambda: {"totalRows": 5}),
        (webapp.snaplog, "health", lambda: {"healthy": True}),
        (webapp.snaplog, "capture_snapshot", lambda: 7),
        (webapp.snaplog, "capture_iv", lambda: 0),
        (webapp.snaplog, "backtest", lambda view: {"view": view}),
        (webapp.snaplog, "iv_rank", lambda s: {"symbol": s, "rank": 50}),
    ):
        assert _json("/api/log/status")[1]["totalRows"] == 5
        assert _json("/api/log/health")[1]["healthy"] is True
        r = client.post("/api/log/snapshot")
        assert r.get_json()["ok"] is True and r.get_json()["rowsWritten"] == 7
        r = client.post("/api/log/iv")
        assert r.get_json()["ok"] is False   # 0 rows
        assert _json("/api/log/backtest?view=volume")[1]["view"] == "volume"
        assert _json("/api/iv/rank/ACC")[1]["rank"] == 50


def test_log_download_404_when_empty():
    with _patch(webapp.snaplog, "status", lambda: {"totalRows": 0}):
        r = client.get("/api/log/download")
    assert r.status_code == 404 and "error" in r.get_json()


def test_log_download_sends_csv():
    import db
    d = tempfile.mkdtemp(prefix="nse_dl_test_")

    def _export(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("a,b\n1,2\n")
    try:
        with _patches(
            (webapp.snaplog, "status", lambda: {"totalRows": 3}),
            (db, "DATA_DIR", d),
            (db, "export_snapshots_csv", _export),
        ):
            r = client.get("/api/log/download")
        assert r.status_code == 200
        assert "attachment" in (r.headers.get("Content-Disposition") or "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ideas_recent_bad_args_default():
    import ideas_journal as ij
    seen = {}
    with _patch(ij, "recent",
                lambda window_min=60, limit=40, min_rating=None:
                seen.update(w=window_min, l=limit) or {}):
        client.get("/api/ideas/recent?window=abc&limit=xyz")
    assert seen["w"] == 60 and seen["l"] == 40      # both fell back to defaults


def test_backtest_invalid_resolve_normalized():
    import backtest_strategies as bt
    seen = {}
    with _patch(bt, "run", lambda **k: seen.update(k) or {}):
        client.get("/api/sim/backtest?resolve=garbage")
    assert seen["resolve"] == "intrabar"            # unknown → default


# ---------------------------------------------------------------------------
# pure app helpers
# ---------------------------------------------------------------------------
def test_select_live_feed_prefers_configured():
    with _patches((webapp.angel_feed, "is_configured", lambda: False),
                  (webapp.dhan_feed, "is_configured", lambda: True)):
        assert webapp._select_live_feed() is webapp.dhan_feed
    with _patches((webapp.angel_feed, "is_configured", lambda: True),
                  (webapp.dhan_feed, "is_configured", lambda: False)):
        assert webapp._select_live_feed() is webapp.angel_feed
    # neither configured → still Angel (shows the free provider's setup card)
    with _patches((webapp.angel_feed, "is_configured", lambda: False),
                  (webapp.dhan_feed, "is_configured", lambda: False)):
        assert webapp._select_live_feed() is webapp.angel_feed


def test_lan_ip_returns_str_or_none():
    ip = webapp._lan_ip()
    assert ip is None or (isinstance(ip, str) and ip.count(".") == 3)


def test_envflag():
    assert webapp._envflag("MISSING_X", "1") is True
    assert webapp._envflag("MISSING_X", "0") is False


# ---------------------------------------------------------------------------
# index (template render)
# ---------------------------------------------------------------------------
def test_index_renders():
    r = client.get("/")
    assert r.status_code == 200 and r.mimetype == "text/html"


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} app-route tests passed")


if __name__ == "__main__":
    _main()
