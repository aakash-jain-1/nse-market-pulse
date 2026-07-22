"""
Unit tests for the sim's financial math (AUDIT.md L8).

Covers the pure, money-critical helpers so a refactor can't silently change the
numbers: risk-based sizing, %-move, the business-day hold horizon, the coarse
single-price exit path (incl. the M4 "expire even with no live price" fix) and
the scorecard aggregation (win%, total R, expectancy).

Run: python test_sim.py   (also works under pytest)
"""

import sim

RISK = sim.RISK_PER_TRADE  # 2000.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _patch(attr, value):
    """Swap a module global, returning a restore() callable."""
    orig = getattr(sim, attr)
    setattr(sim, attr, value)
    return lambda: setattr(sim, attr, orig)


def _trade(direction="LONG", entry=100.0, stop=98.0, target=104.0, qty=1000.0,
           opened_date="2026-07-16", status="OPEN", ltp=100.0, risk=RISK):
    return {
        "symbol": "X", "direction": direction, "entry": entry, "stop": stop,
        "target": target, "qty": qty, "risk": risk, "status": status,
        "ltp": ltp, "mfePct": 0.0, "maePct": 0.0, "pnl": 0.0, "pnlPct": 0.0,
        "rMultiple": 0.0, "openedDate": opened_date, "openedAt": opened_date + " 09:20:00",
        "exitPrice": None, "closedAt": None, "closedDay": None, "minsToExit": None,
    }


# ---------------------------------------------------------------------------
# size_position — risk-based sizing
# ---------------------------------------------------------------------------
def test_size_position_basic():
    qty, notional = sim.size_position(100.0, 98.0, risk=2000.0)
    assert qty == 1000.0            # 2000 / |100-98|
    assert notional == 100000.0     # 1000 * 100


def test_size_position_notional_cap():
    # A very tight stop would size huge; MAX_NOTIONAL caps it.
    qty, notional = sim.size_position(100.0, 99.9, risk=2000.0)
    assert qty == sim.MAX_NOTIONAL / 100.0     # 5000, capped
    assert notional == sim.MAX_NOTIONAL         # 500000


def test_size_position_no_stop_falls_back_to_notional():
    qty, notional = sim.size_position(100.0, None)
    assert qty == sim.NOTIONAL / 100.0          # flat notional fallback
    assert notional == round(qty * 100.0, 2)


def test_size_position_bad_entry():
    assert sim.size_position(0, 98) == (0.0, 0.0)
    assert sim.size_position(-5, 98) == (0.0, 0.0)


def test_size_position_short_symmetric():
    # Sizing is direction-agnostic (uses |entry-stop|): a short with the same
    # per-share risk gets the same quantity as the long.
    assert sim.size_position(100.0, 102.0, risk=2000.0)[0] == 1000.0


# ---------------------------------------------------------------------------
# _move_pct
# ---------------------------------------------------------------------------
def test_move_pct_long_short():
    assert sim._move_pct(_trade("LONG"), 104.0) == 4.0
    assert sim._move_pct(_trade("LONG"), 95.0) == -5.0
    assert sim._move_pct(_trade("SHORT"), 96.0) == 4.0   # down move favors a short
    assert sim._move_pct(_trade("SHORT"), 104.0) == -4.0


# ---------------------------------------------------------------------------
# _sessions_elapsed — business-day horizon (AUDIT.md L6)
# ---------------------------------------------------------------------------
def test_sessions_elapsed_business_days():
    restore = _patch("_today", lambda: "2026-07-16")  # a Thursday
    try:
        assert sim._sessions_elapsed({}, "2026-07-16") == 1        # same day
        assert sim._sessions_elapsed({}, "2026-07-15") == 2        # Wed..Thu
        assert sim._sessions_elapsed({}, "2026-07-13") == 4        # Mon..Thu
        # Fri 10th -> Thu 16th skips Sat/Sun: Fri,Mon,Tue,Wed,Thu = 5
        assert sim._sessions_elapsed({}, "2026-07-10") == 5
        # opened in the future -> clamp to 1
        assert sim._sessions_elapsed({}, "2026-07-20") == 1
    finally:
        restore()


def test_sessions_elapsed_independent_of_logged_days():
    # The old impl counted only days the app logged; the new one counts weekdays,
    # so an empty daily-log still advances the horizon.
    restore = _patch("_today", lambda: "2026-07-16")
    try:
        assert sim._sessions_elapsed({"daily": {}}, "2026-07-13") == 4
    finally:
        restore()


# ---------------------------------------------------------------------------
# _refresh_trade — coarse single-price exit + M4 (expire w/o a price)
# ---------------------------------------------------------------------------
def test_refresh_trade_target():
    t = _trade("LONG")
    restore = _patch("_price", lambda s: 104.5)
    try:
        sim._refresh_trade({"maxSessions": 3}, t)
    finally:
        restore()
    assert t["status"] == "TARGET"
    assert t["exitPrice"] == 104.0          # closes AT the level, not the overshoot
    assert t["pnl"] == 4000.0               # 1000 * (104-100)
    assert t["rMultiple"] == 2.0


def test_refresh_trade_stop():
    t = _trade("LONG")
    restore = _patch("_price", lambda s: 97.0)
    try:
        sim._refresh_trade({"maxSessions": 3}, t)
    finally:
        restore()
    assert t["status"] == "STOP"
    assert t["exitPrice"] == 98.0
    assert t["pnl"] == -2000.0
    assert t["rMultiple"] == -1.0


def test_refresh_trade_open_marks_to_market():
    t = _trade("LONG")
    restore = _patch("_price", lambda s: 101.0)   # between stop and target
    restore2 = _patch("_today", lambda: "2026-07-16")
    try:
        # opened today -> 1 session, below the 3-session horizon -> stays open
        t["openedDate"] = "2026-07-16"
        sim._refresh_trade({"maxSessions": 3}, t)
    finally:
        restore2(); restore()
    assert t["status"] == "OPEN"
    assert t["ltp"] == 101.0
    assert t["pnl"] == 1000.0               # mark-to-market, not realized
    assert t["rMultiple"] == 0.5


def test_refresh_trade_expires_without_price():
    # AUDIT.md M4: the symbol fell out of the hot list (_price -> None) but the
    # hold horizon is exceeded, so the trade must still close (at last mark) and
    # never hang OPEN forever.
    t = _trade("LONG", opened_date="2026-07-13")   # Mon
    t["ltp"] = 100.0
    restore = _patch("_price", lambda s: None)
    restore2 = _patch("_today", lambda: "2026-07-16")   # Thu -> 4 sessions > 3
    try:
        sim._refresh_trade({"maxSessions": 3}, t)
    finally:
        restore2(); restore()
    assert t["status"] == "EXPIRED"
    assert t["exitPrice"] == 100.0
    assert t["closedAt"] is not None


def test_refresh_trade_no_price_within_horizon_stays_open():
    t = _trade("LONG", opened_date="2026-07-16")
    restore = _patch("_price", lambda s: None)
    restore2 = _patch("_today", lambda: "2026-07-16")
    try:
        sim._refresh_trade({"maxSessions": 3}, t)
    finally:
        restore2(); restore()
    assert t["status"] == "OPEN"            # no price, still within horizon -> untouched


# ---------------------------------------------------------------------------
# _scorecard — aggregation (win%, total R, expectancy)
# ---------------------------------------------------------------------------
def _closed(status, pnl, r, pct=0.0):
    return {"status": status, "pnl": pnl, "rMultiple": r, "pnlPct": pct,
            "risk": RISK, "closedAt": "2026-01-01 10:00:00"}


def test_scorecard_math():
    restore = _patch("_today", lambda: "2026-07-16")
    try:
        trades = [
            _closed("TARGET", 4000.0, 2.0),
            _closed("STOP", -2000.0, -1.0),
            _closed("EXPIRED", 500.0, 0.25),
            {"status": "OPEN", "pnl": 300.0, "rMultiple": 0.15, "pnlPct": 0.0, "risk": RISK},
        ]
        sc = sim._scorecard(trades)
    finally:
        restore()
    assert sc["closed"] == 3
    assert sc["open"] == 1
    assert sc["target"] == 1 and sc["stop"] == 1 and sc["expired"] == 1
    assert sc["winRate"] == round(1 / 3 * 100, 1)      # 33.3
    assert sc["totalR"] == 1.25                        # 2 - 1 + 0.25
    assert sc["expectancyR"] == round(1.25 / 3, 2)     # 0.42
    assert sc["realizedPnl"] == 2500.0                 # 4000 - 2000 + 500
    assert sc["unrealizedPnl"] == 300.0


def test_scorecard_empty():
    sc = sim._scorecard([])
    assert sc["closed"] == 0
    assert sc["winRate"] is None
    assert sc["expectancyR"] is None
    assert sc["totalR"] == 0.0


def test_request_stop_halts_intrabar_fetch():
    # Graceful shutdown: with the stop flag set, the minute-candle fetch must bail
    # (return None) BEFORE opening a ThreadPoolExecutor, so it can't race the
    # interpreter teardown. Cleared afterwards so other tests still fetch.
    sim.request_stop()
    try:
        open_trades = [{"symbol": "X", "status": "OPEN",
                        "openedAt": "2026-07-20T09:20:00"}]
        assert sim._intrabar_fetch(open_trades) is None
    finally:
        sim._STOPPING.clear()


# ---------------------------------------------------------------------------
# update() throttle + parallel reprice (stops SIM/F&O piling up NSE calls)
# ---------------------------------------------------------------------------
def test_resolve_prices_fans_out_all_symbols():
    # Parallel price resolution returns one entry per symbol; the empty and
    # single-symbol shortcuts work too. get_price_map is warmed once (stubbed here
    # so the test never touches the network).
    restore = _patch("_price", lambda s: 100.0 + len(s))
    orig_gpm = sim.nse.get_price_map
    sim.nse.get_price_map = lambda: {}
    try:
        assert sim._resolve_prices(set()) == {}
        assert sim._resolve_prices({"Z"}) == {"Z": 101.0}
        assert sim._resolve_prices({"AAA", "BB"}) == {"AAA": 103.0, "BB": 102.0}
    finally:
        sim.nse.get_price_map = orig_gpm
        restore()


def test_update_throttles_nse_reprice():
    # summary() calls update() on every poll; the per-symbol NSE fan-out must run at
    # most once per _UPDATE_TTL (repeated polls reuse the last reprice), while
    # force=True bypasses it. Guards the fix for SIM/F&O piling up NSE requests.
    import db
    calls = {"n": 0}

    def _fake_resolve(symbols):
        calls["n"] += 1
        return {}

    restores = [
        _patch("_resolve_prices", _fake_resolve),
        _patch("_intrabar_fetch", lambda seed: None),
        _patch("_intrabar_apply", lambda *a, **k: None),
        _patch("_load", lambda: {"daily": {}}),
        _patch("_last_update", 0.0),
        _patch("_reprice_running", False),
    ]
    orig_open, orig_ins = db.sim_open_trades, db.sim_insert_trades
    db.sim_open_trades = lambda *a, **k: []
    db.sim_insert_trades = lambda *a, **k: None
    try:
        sim.update()             # cold -> reprices
        sim.update()             # within TTL -> skipped, serves last reprice
        assert calls["n"] == 1
        sim.update(force=True)   # force -> reprices again
        assert calls["n"] == 2
    finally:
        db.sim_open_trades, db.sim_insert_trades = orig_open, orig_ins
        for r in reversed(restores):
            r()


def test_maybe_reprice_async_is_nonblocking_and_reprices():
    # summary() must NOT block on the fan-out: the async kick runs the reprice on a
    # background thread and returns immediately. force=True bypasses the TTL.
    import threading as _th
    done = _th.Event()
    restores = [
        _patch("_reprice_open_trades", lambda: done.set()),
        _patch("_last_update", 0.0),
        _patch("_reprice_running", False),
    ]
    try:
        sim._maybe_reprice_async(force=True)   # returns at once
        assert done.wait(timeout=5)            # background pass actually ran
    finally:
        for r in reversed(restores):
            r()


def test_maybe_reprice_async_skips_when_fresh():
    # A reprice within _UPDATE_TTL is a no-op (repeated polls reuse the last one).
    import time as _t
    calls = {"n": 0}
    restores = [
        _patch("_reprice_open_trades", lambda: calls.__setitem__("n", calls["n"] + 1)),
        _patch("_last_update", _t.time()),     # just repriced
        _patch("_reprice_running", False),
    ]
    try:
        sim._maybe_reprice_async()
        _t.sleep(0.2)
        assert calls["n"] == 0
    finally:
        for r in reversed(restores):
            r()


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} sim tests passed")


if __name__ == "__main__":
    _main()
