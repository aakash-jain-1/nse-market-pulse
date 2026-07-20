"""
Unit tests for snapshot_logger.py — capture loop helpers + analysis.

is_market_hours (weekday + 09:15-15:30 IST boundaries), _rows_for_snapshot /
capture_snapshot (against stubbed NSE getters + a temp DB), health() across the
running/stalled/idle states, status(), the forward-return backtest(), iv_rank()
percentile math, _trim_context whitelist, and _to_float. No network.

Run: python test_logger.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile
import types
from datetime import datetime

import db
import snapshot_logger as sl


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _globals(**kw):
    """Temporarily set snapshot_logger module globals."""
    saved = {k: getattr(sl, k) for k in kw}
    for k, v in kw.items():
        setattr(sl, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(sl, k, v)


@contextlib.contextmanager
def _temp_db():
    d = tempfile.mkdtemp(prefix="nse_logger_test_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = d
    db.DB_FILE = os.path.join(d, "market.db")
    db._initialized = False
    db.init()
    try:
        yield
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# is_market_hours
# ---------------------------------------------------------------------------
def test_market_hours_weekday_window():
    thu = datetime(2026, 7, 16, 10, 0)      # Thursday, midday
    assert sl.is_market_hours(thu) is True
    assert sl.is_market_hours(datetime(2026, 7, 16, 9, 15)) is True    # open edge
    assert sl.is_market_hours(datetime(2026, 7, 16, 15, 30)) is True   # close edge
    assert sl.is_market_hours(datetime(2026, 7, 16, 9, 14)) is False   # pre-open
    assert sl.is_market_hours(datetime(2026, 7, 16, 15, 31)) is False  # post-close


def test_market_hours_weekend():
    assert sl.is_market_hours(datetime(2026, 7, 18, 10, 0)) is False   # Saturday
    assert sl.is_market_hours(datetime(2026, 7, 19, 10, 0)) is False   # Sunday


# ---------------------------------------------------------------------------
# _rows_for_snapshot / capture_snapshot
# ---------------------------------------------------------------------------
def test_rows_for_snapshot_shape():
    with _patch(sl.nse, "get_demand_score",
                lambda n=25: [{"symbol": "A", "ltp": 100, "pChange": 2,
                               "score": 9, "signalCount": 3, "volMult": 10}]), \
         _patch(sl.nse, "get_volume_gainers",
                lambda n=25: [{"symbol": "B", "ltp": 50, "pChange": 1,
                               "week1volChange": 5, "volume": 999}]):
        rows = sl._rows_for_snapshot()
    views = {r["view"] for r in rows}
    assert views == {"demand", "volgainers"}
    demand = next(r for r in rows if r["view"] == "demand")
    assert demand["symbol"] == "A" and demand["rank"] == 1


def test_capture_snapshot_writes_and_empty():
    with _temp_db():
        with _patch(sl.nse, "get_demand_score",
                    lambda n=25: [{"symbol": "A", "ltp": 100}]), \
             _patch(sl.nse, "get_volume_gainers", lambda n=25: []):
            assert sl.capture_snapshot() == 1
        # both sources empty → nothing written, error noted
        with _patch(sl.nse, "get_demand_score", lambda n=25: []), \
             _patch(sl.nse, "get_volume_gainers", lambda n=25: []):
            assert sl.capture_snapshot() == 0
            assert sl._last_error == "No data returned from NSE"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_health_not_running():
    with _globals(_running=False, _thread=None, _watchdog=None), \
         _patch(sl, "is_market_hours", lambda: False):
        h = sl.health()
    assert h["healthy"] is False and h["running"] is False and h["marketHours"] is False


def test_health_running_and_ticking():
    live = types.SimpleNamespace(is_alive=lambda: True)
    import time
    with _globals(_running=True, _thread=live, _watchdog=live, _last_tick=time.time()), \
         _patch(sl, "is_market_hours", lambda: True):
        h = sl.health()
    assert h["healthy"] is True and h["stalled"] is False


def test_health_stalled_when_no_recent_tick():
    live = types.SimpleNamespace(is_alive=lambda: True)
    with _globals(_running=True, _thread=live, _watchdog=live, _last_tick=0.0), \
         _patch(sl, "is_market_hours", lambda: True):
        h = sl.health()
    assert h["stalled"] is True and h["healthy"] is False


def test_status_keys():
    with _temp_db():
        st = sl.status()
        assert st["db"] == db.DB_FILE       # points at the temp DB while active
    assert "running" in st and "health" in st and isinstance(st["totalRows"], int)


# ---------------------------------------------------------------------------
# backtest (forward-return analysis)
# ---------------------------------------------------------------------------
def test_backtest_forward_returns():
    with _temp_db():
        db.insert_snapshots([
            {"ts": "2026-07-16 09:20:00", "view": "demand", "rank": 1, "symbol": "A", "ltp": "100"},
            {"ts": "2026-07-16 15:20:00", "view": "demand", "rank": 2, "symbol": "A", "ltp": "110"},
            {"ts": "2026-07-16 09:20:00", "view": "demand", "rank": 3, "symbol": "B", "ltp": "200"},
            {"ts": "2026-07-16 15:20:00", "view": "demand", "rank": 4, "symbol": "B", "ltp": "180"},
        ])
        out = sl.backtest("demand")
    syms = {r["symbol"]: r for r in out["symbols"]}
    assert syms["A"]["forwardReturnPct"] == 10.0
    assert syms["B"]["forwardReturnPct"] == -10.0
    assert out["summary"]["hitRatePct"] == 50.0
    assert out["summary"]["bestSymbol"] == "A" and out["summary"]["worstSymbol"] == "B"


def test_backtest_empty():
    with _temp_db():
        out = sl.backtest("demand")
    assert out["symbols"] == [] and out["summary"] is None and out["message"]


# ---------------------------------------------------------------------------
# iv_rank
# ---------------------------------------------------------------------------
def test_iv_rank_math():
    with _temp_db():
        db.insert_iv([
            {"ts": "t1", "symbol": "NIFTY", "atmIV": 10.0},
            {"ts": "t2", "symbol": "NIFTY", "atmIV": 20.0},
            {"ts": "t3", "symbol": "NIFTY", "atmIV": 15.0},
        ])
        out = sl.iv_rank("nifty")
    assert out["enough"] is True and out["samples"] == 3
    assert out["currentIV"] == 15.0 and out["ivRank"] == 50.0
    assert out["ivPercentile"] == 66.7      # 2 of 3 <= 15


def test_iv_rank_not_enough():
    with _temp_db():
        db.insert_iv([{"ts": "t1", "symbol": "NIFTY", "atmIV": 10.0}])
        out = sl.iv_rank("NIFTY")
    assert out["enough"] is False and out["samples"] == 1


# ---------------------------------------------------------------------------
# _trim_context / _to_float
# ---------------------------------------------------------------------------
def test_trim_context_whitelist():
    ctx = {
        "scanner": [{"symbol": "A", "ltp": 100, "SECRET": "drop-me"}],
        "gainers": [{"symbol": "B", "ltp": 50, "extra": 1}],
        "index": {"NIFTY": {"last": 22000, "pChange": 0.8, "junk": 9},
                  "SGXNIFTY": {"last": 1}},   # non-whitelisted index dropped
        "niftyPcr": 0.95,
        "candles": {"A": [1, 2, 3]},           # deliberately not stored
    }
    out = sl._trim_context(ctx)
    assert out["scanner"][0] == {"symbol": "A", "ltp": 100}
    assert "SECRET" not in out["scanner"][0]
    assert set(out["index"]) == {"NIFTY"} and "junk" not in out["index"]["NIFTY"]
    assert out["niftyPcr"] == 0.95
    assert "candles" not in out


def test_to_float():
    assert sl._to_float("3.5") == 3.5
    assert sl._to_float(None) is None
    assert sl._to_float("x") is None


# ---------------------------------------------------------------------------
# capture_context / _note_error
# ---------------------------------------------------------------------------
def test_capture_context_stores_trimmed_cycle():
    ctx = {"regime": {"label": "Trend-Up", "niftyPct": 0.8},
           "scanner": [{"symbol": "A", "ltp": 100, "SECRET": "x"}],
           "candles": {"A": [1, 2, 3]}}          # dropped by _trim_context
    with _temp_db():
        n = sl.capture_context(ctx)
        cycles = db.context_cycles()
        assert n and len(cycles) == 1
        c0 = cycles[0]
        assert c0["regime"] == "Trend-Up" and c0["niftyPct"] == 0.8
        assert c0["ctx"]["scanner"][0] == {"symbol": "A", "ltp": 100}
        assert "candles" not in c0["ctx"]
    assert sl.capture_context(None) == 0        # empty context → no-op


def test_note_error_sets_state():
    with _globals(_last_error=None, _last_error_at=None):
        sl._note_error("boom")
        assert sl._last_error == "boom" and sl._last_error_at is not None


def test_env_int_parsing_and_floor():
    assert sl._env_int("NSE_TEST_MISSING_XYZ", 90, 30) == 90          # missing → default
    with _patch(os, "environ", dict(os.environ, NSE_TEST_INT="45")):
        assert sl._env_int("NSE_TEST_INT", 90, 30) == 45             # valid override
    with _patch(os, "environ", dict(os.environ, NSE_TEST_INT="5")):
        assert sl._env_int("NSE_TEST_INT", 90, 30) == 30             # below floor → clamped
    with _patch(os, "environ", dict(os.environ, NSE_TEST_INT="abc")):
        assert sl._env_int("NSE_TEST_INT", 90, 30) == 90            # garbage → default
    with _patch(os, "environ", dict(os.environ, NSE_TEST_INT="   ")):
        assert sl._env_int("NSE_TEST_INT", 90, 30) == 90            # blank → default


def test_default_cadences_are_trimmed():
    # The source-trim: snapshot cadence raised 60 → 90, and "stalled" scales with it
    # so a longer interval isn't mis-flagged unhealthy.
    assert sl.INTERVAL == 90 and sl.INTERVAL >= 30
    assert sl.STALE_AFTER >= sl.INTERVAL * 2


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} logger tests passed")


if __name__ == "__main__":
    _main()
