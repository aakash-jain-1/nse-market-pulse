"""
Unit tests for sim.py's DB/JSON-backed read + settings surface.

Complements test_sim.py (pure math) and test_take.py (take()): here we drive the
aggregation/reporting views off a throwaway ledger — performance() (all-time,
ranked by expectancy + profit factor), daily_performance() (date-wise P&L +
today card), day_trades() (drill-down), analytics() (equity curve/drawdown/
R-histogram + per-regime), _by_regime_r, _prior_day_move, current_regime (stubbed
index), and the settings round-trip (set_auto/get_auto/set_entry_mode/reset).

Uses a temp market.db + temp sim_state.json and stubs the network, so nothing
touches NSE.

Run: python test_sim_views.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile

import db
import sim


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _temp_sim():
    d = tempfile.mkdtemp(prefix="nse_simv_test_")
    saved_db = (db.DATA_DIR, db.DB_FILE, db._initialized)
    saved_state, saved_mig, saved_ens = sim.STATE_FILE, sim._migrated, sim._ensure_migrated
    db.DATA_DIR = d
    db.DB_FILE = os.path.join(d, "market.db")
    db._initialized = False
    db.init()
    sim.STATE_FILE = os.path.join(d, "sim_state.json")
    sim._migrated = True                      # skip legacy migration
    sim._ensure_migrated = lambda: None
    sim._regime_cache = None                  # SWR regime cache: start each test cold
    sim._regime_ts = 0.0
    sim._regime_running = False
    try:
        yield
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved_db
        sim.STATE_FILE, sim._migrated, sim._ensure_migrated = saved_state, saved_mig, saved_ens
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)


def _t(tid, strategy="momentum", status="TARGET", pnl=4000.0, r=2.0, book="cash",
       opened="2026-07-15", closed_day="2026-07-15", regime="Trend-Up", **kw):
    d = {"id": tid, "book": book, "strategy": strategy, "symbol": "X",
         "direction": "LONG", "status": status, "pnl": pnl, "rMultiple": r,
         "pnlPct": 0.0, "risk": 2000.0, "qty": 1000.0, "entry": 100.0,
         "stop": 98.0, "target": 104.0, "openedAt": opened + " 09:20:00",
         "openedDate": opened,
         "closedAt": (closed_day + " 15:00:00") if closed_day else None,
         "closedDay": closed_day, "regimeAtEntry": regime, "minsToExit": 60,
         "reasons": [], "fno": False}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# performance (all-time)
# ---------------------------------------------------------------------------
def test_performance_ranked_by_expectancy():
    with _temp_sim():
        db.sim_insert_trades([
            _t("1", "momentum", "TARGET", 4000.0, 2.0),
            _t("2", "momentum", "STOP", -2000.0, -1.0),
            _t("3", "vwap", "EXPIRED", 500.0, 0.25),
        ])
        perf = sim.performance()
    ranked = [r for r in perf["rows"] if r["closed"] > 0]
    assert ranked[0]["id"] == "momentum" and ranked[0]["expectancyR"] == 0.5
    assert ranked[0]["profitFactor"] == 2.0          # 4000 / 2000
    assert ranked[0]["tradingDays"] == 1
    assert ranked[1]["id"] == "vwap"
    assert perf["totals"]["closed"] == 3 and perf["tradeCount"] == 3


# ---------------------------------------------------------------------------
# daily_performance + day_trades
# ---------------------------------------------------------------------------
def test_daily_performance_buckets():
    with _temp_sim():
        db.sim_insert_trades([
            _t("1", status="TARGET", pnl=4000.0, r=2.0),
            _t("2", status="STOP", pnl=-2000.0, r=-1.0),
            _t("3", status="OPEN", pnl=300.0, r=0.15, closed_day=None),
        ])
        dp = sim.daily_performance()
    row = next(r for r in dp["rows"] if r["date"] == "2026-07-15")
    assert row["opened"] == 3 and row["closed"] == 2
    assert row["wins"] == 1 and row["stops"] == 1 and row["winRate"] == 50.0
    assert row["realized"] == 2000.0
    assert dp["today"]["openNow"] == 1 and dp["today"]["unrealized"] == 300.0


def test_day_trades_drilldown():
    with _temp_sim():
        db.sim_insert_trades([
            _t("1", status="TARGET", pnl=4000.0),
            _t("2", status="OPEN", pnl=300.0, closed_day=None),
            _t("3", status="STOP", pnl=-2000.0, closed_day="2026-07-14"),  # other day
        ])
        out = sim.day_trades("2026-07-15")
    assert out["closedTotal"] == 1 and out["openTotal"] == 1
    assert out["closed"][0]["symbol"] == "X"
    assert out["closed"][0]["strategyName"] == "Multi-Signal Momentum"


# ---------------------------------------------------------------------------
# analytics + _by_regime_r
# ---------------------------------------------------------------------------
def test_analytics_equity_and_hist():
    with _temp_sim():
        db.sim_insert_trades([
            _t("1", "momentum", "TARGET", 4000.0, 2.0),
            _t("2", "momentum", "STOP", -2000.0, -1.0),
        ])
        a = sim.analytics()
    mom = next(s for s in a["strategies"] if s["id"] == "momentum")
    assert mom["stats"]["closed"] == 2 and mom["stats"]["finalR"] == 1.0
    assert mom["expectancyR"] == 0.5 and mom["stats"]["winRate"] == 50.0
    assert sum(b["count"] for b in mom["hist"]) == 2
    assert a["portfolio"]["closed"] == 2


def test_by_regime_r():
    closed = [
        _t("1", regime="Trend-Up", status="TARGET", r=2.0),
        _t("2", regime="Trend-Up", status="STOP", r=-1.0),
        _t("3", regime="Range", status="TARGET", r=1.0),
    ]
    out = sim._by_regime_r(closed)
    by = {r["regime"]: r for r in out}
    assert by["Trend-Up"]["closed"] == 2 and by["Trend-Up"]["expectancyR"] == 0.5
    assert by["Trend-Up"]["winRate"] == 50.0
    assert by["Range"]["expectancyR"] == 1.0


# ---------------------------------------------------------------------------
# regime helpers
# ---------------------------------------------------------------------------
def test_prior_day_move():
    state = {"daily": {"2026-07-10": {"niftyPct": -2.0},
                       "2026-07-14": {"niftyPct": 1.5}}}
    with _patch(sim, "_today", lambda: "2026-07-16"):
        assert sim._prior_day_move(state) == 1.5      # most recent day < today
        assert sim._prior_day_move({"daily": {}}) is None


def _await_regime(timeout=3.0):
    """current_regime() is now non-blocking (stale-while-revalidate): the real
    regime is computed on a background thread. Kick it, then wait for the cache."""
    import time as _t
    sim.current_regime()                          # kicks the bg compute
    end = _t.time() + timeout
    while _t.time() < end:
        with sim._regime_gate:
            if sim._regime_cache is not None:
                return sim._regime_cache
        _t.sleep(0.01)
    raise AssertionError("regime cache not populated in time")


def test_current_regime_from_index():
    with _temp_sim(), _patch(sim.nse, "get_index_snapshot",
                             lambda: {"NIFTY": {"pChange": 1.0, "advances": 100,
                                                "declines": 50}}):
        r = _await_regime()
    assert r["label"] == "Trend-Up" and r["niftyPct"] == 1.0


def test_current_regime_surfaces_volatility():
    idx = {"NIFTY": {"pChange": 1.0, "advances": 100, "declines": 50},
           "INDIAVIX": {"last": 20.0, "yearLow": 10.0, "yearHigh": 30.0}}
    with _temp_sim(), _patch(sim.nse, "get_index_snapshot", lambda: idx):
        r = _await_regime()
    assert r["label"] == "Trend-Up"          # direction unaffected by the vol axis
    assert r["vix"] == 20.0 and r["volState"] == "Elevated" and r["vixPctile"] == 50.0


def test_current_regime_nonblocking_on_cold_start():
    # A cold call must NOT block on a slow index fetch: it returns a neutral regime
    # at once and computes the real one in the background (this was the residual
    # "first SIM/F&O call won't load" — the last synchronous NSE hop in summary()).
    import time as _t
    hit = {"v": False}

    def _slow_snap():
        hit["v"] = True
        _t.sleep(1.0)
        return {"NIFTY": {"pChange": 1.0, "advances": 100, "declines": 50}}

    with _temp_sim(), _patch(sim.nse, "get_index_snapshot", _slow_snap):
        t0 = _t.time()
        r = sim.current_regime()                  # returns immediately (fallback)
        assert (_t.time() - t0) < 0.5
        assert isinstance(r, dict) and "label" in r
        end = _t.time() + 3.0
        while _t.time() < end and sim._regime_cache is None:
            _t.sleep(0.01)
        assert hit["v"] and sim._regime_cache["niftyPct"] == 1.0


def test_current_regime_serves_stale_then_refreshes():
    # A stale cache is served instantly while a background refresh recomputes it.
    import time as _t
    with _temp_sim():
        sim._regime_cache = {"label": "Range", "niftyPct": 0.0}
        sim._regime_ts = 0.0                       # force stale
        with _patch(sim.nse, "get_index_snapshot",
                    lambda: {"NIFTY": {"pChange": 1.0, "advances": 100,
                                       "declines": 50}}):
            r = sim.current_regime()              # instant: the stale value
            assert r["label"] == "Range"
            end = _t.time() + 3.0
            while _t.time() < end and sim._regime_cache.get("label") != "Trend-Up":
                _t.sleep(0.01)
            assert sim._regime_cache["label"] == "Trend-Up"


# ---------------------------------------------------------------------------
# settings + reset
# ---------------------------------------------------------------------------
def test_settings_roundtrip():
    with _temp_sim():
        assert sim.set_auto(False) is False and sim.get_auto() is False
        assert sim.set_auto(True) is True and sim.get_auto() is True
        assert sim.set_entry_mode("open") == "open"
        assert sim.set_entry_mode("weird") == "continuous"


def test_reset_book_vs_all():
    with _temp_sim():
        db.sim_insert_trades([_t("1", book="cash"), _t("2", book="fno")])
        sim.set_auto(False)
        sim.reset(book="fno")                          # clears only fno
        assert db.sim_trade_count(book="cash") == 1 and db.sim_trade_count(book="fno") == 0
        sim.reset()                                    # wipe all + reset settings
        assert db.sim_trade_count() == 0
        assert sim.get_auto() is True                  # default restored


# ---------------------------------------------------------------------------
# pure trade-list views (trades passed in → no DB needed)
# ---------------------------------------------------------------------------
def test_regime_leaderboard_best_by_avg_pct():
    trades = [
        _t("1", "momentum", "TARGET", pnl=4000, r=2.0, regime="Trend-Up"),
        _t("2", "momentum", "TARGET", pnl=4000, r=2.0, regime="Trend-Up"),
        _t("3", "momentum", "STOP", pnl=-2000, r=-1.0, regime="Trend-Up"),
    ]
    for x, pct in zip(trades, (6.0, 4.0, -3.0)):
        x["pnlPct"] = pct
    lb = sim.regime_leaderboard(trades=trades)
    row = next(r for r in lb["rows"] if r["regime"] == "Trend-Up")
    cell = row["cells"]["momentum"]
    assert cell["closed"] == 3 and cell["winRate"] == 66.7
    assert cell["avgPnlPct"] == round((6 + 4 - 3) / 3, 2)
    assert row["best"] == "momentum"


def test_strategy_of_the_day_history_and_fit():
    lb = {"rows": [{"regime": "Trend-Up", "best": "momentum", "cells": {
        "momentum": {"closed": 5, "open": 0, "winRate": 60.0,
                     "avgPnlPct": 3.5, "totalPnl": 10000},
    }}]}
    pick = sim.strategy_of_the_day("Trend-Up", lb=lb)["pick"]
    assert pick["id"] == "momentum" and pick["basis"] == "history"
    # a regime with no history → falls back to an a-priori regimeFit strategy
    fit = sim.strategy_of_the_day("Range", lb={"rows": []})["pick"]
    assert fit is not None and fit["basis"] == "fit"


def test_equity_curves_cumulative():
    trades = [
        _t("1", "momentum", "TARGET", pnl=4000, r=2.0, closed_day="2026-07-15"),
        _t("2", "momentum", "STOP", pnl=-1500, r=-0.75, closed_day="2026-07-16"),
    ]
    trades[0]["closedAt"] = "2026-07-15 15:00:00"
    trades[1]["closedAt"] = "2026-07-16 15:00:00"
    ec = sim.equity_curves(trades=trades)["momentum"]
    assert ec["points"] == [4000.0, 2500.0] and ec["final"] == 2500.0 and ec["n"] == 2


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} sim-view tests passed")


if __name__ == "__main__":
    _main()
