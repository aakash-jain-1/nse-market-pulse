"""
Unit tests for the backtest engines' exit + aggregation math (AUDIT.md L8).

  - backtest_daily._resolve   — daily high/low bars, STOP-first tie-break, MFE/MAE,
                                time-expiry at close.
  - backtest_strategies._resolve/_scorecard/_median/_equity — coarse single-price
                                exit (shared intrabar.resolve_point) + aggregation.

These are the look-ahead-safe rules the historical scorecards depend on, so they
must not drift. Run: python test_backtest.py   (also works under pytest)
"""

import backtest_daily as bd
import backtest_strategies as bs

RISK = bs.RISK_PER_TRADE


def _approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def _bar(h, l, c):
    return {"high": h, "low": l, "close": c}


# ---------------------------------------------------------------------------
# backtest_daily._resolve — daily-bar resolution
# ---------------------------------------------------------------------------
def test_daily_long_target():
    bars = [_bar(100, 100, 100), _bar(105, 99.5, 104)]
    status, px, idx, mfe, mae = bd._resolve("LONG", 100, 98, 104, bars, 0, 5)
    assert (status, px, idx) == ("TARGET", 104, 1)
    assert _approx(mfe, 5.0) and _approx(mae, -0.5)


def test_daily_stop_first_tie():
    # A single bar that pierces BOTH the stop and the target counts as a STOP
    # (conservative, no intrabar order known).
    bars = [_bar(100, 100, 100), _bar(105, 97, 100)]
    status, px, idx, mfe, mae = bd._resolve("LONG", 100, 98, 104, bars, 0, 5)
    assert (status, px, idx) == ("STOP", 98, 1)
    assert _approx(mfe, 5.0) and _approx(mae, -3.0)


def test_daily_expiry_at_close():
    bars = [_bar(100, 100, 100), _bar(102, 99, 101), _bar(103, 100, 102)]
    status, px, idx, mfe, mae = bd._resolve("LONG", 100, 90, 110, bars, 0, 2)
    assert (status, px, idx) == ("EXPIRED", 102, 2)
    assert _approx(mfe, 3.0) and _approx(mae, -1.0)


def test_daily_short_target():
    bars = [_bar(100, 100, 100), _bar(101, 95, 96)]
    status, px, idx, _, _ = bd._resolve("SHORT", 100, 102, 96, bars, 0, 5)
    assert (status, px, idx) == ("TARGET", 96, 1)


def test_daily_short_stop_first_tie():
    bars = [_bar(100, 100, 100), _bar(103, 95, 100)]   # hits stop 102 and target 96
    status, px, idx, _, _ = bd._resolve("SHORT", 100, 102, 96, bars, 0, 5)
    assert (status, px, idx) == ("STOP", 102, 1)


def test_daily_open_when_no_bars_after_entry():
    bars = [_bar(100, 100, 100)]        # only the entry bar exists
    status, px, idx, _, _ = bd._resolve("LONG", 100, 98, 104, bars, 0, 5)
    assert (status, px, idx) == ("OPEN", None, None)


def test_daily_skips_none_bars():
    bars = [_bar(100, 100, 100), _bar(None, None, None), _bar(105, 99, 104)]
    status, px, idx, _, _ = bd._resolve("LONG", 100, 98, 104, bars, 0, 5)
    assert (status, px, idx) == ("TARGET", 104, 2)


# ---------------------------------------------------------------------------
# backtest_strategies._resolve — coarse single-price exit
# ---------------------------------------------------------------------------
def _bt(direction="LONG", entry=100.0, stop=98.0, target=104.0, qty=1000.0,
        max_sessions=3):
    return {"direction": direction, "entry": entry, "stop": stop, "target": target,
            "qty": qty, "maxSessions": max_sessions, "status": "OPEN",
            "exitPrice": None, "pnl": 0.0, "pnlPct": 0.0, "rMultiple": 0.0}


def test_bt_resolve_target():
    t = _bt()
    assert bs._resolve(t, 104.5, 1) is True
    assert t["status"] == "TARGET" and t["exitPrice"] == 104.0
    assert t["pnl"] == 4000.0 and t["rMultiple"] == 2.0


def test_bt_resolve_stop():
    t = _bt()
    assert bs._resolve(t, 97.0, 1) is True
    assert t["status"] == "STOP" and t["exitPrice"] == 98.0 and t["pnl"] == -2000.0


def test_bt_resolve_time_expiry():
    t = _bt()
    assert bs._resolve(t, 101.0, 4) is True     # 4 > maxSessions 3, no level hit
    assert t["status"] == "EXPIRED" and t["exitPrice"] == 101.0 and t["pnl"] == 1000.0


def test_bt_resolve_stays_open():
    t = _bt()
    assert bs._resolve(t, 101.0, 2) is False    # within horizon, no hit
    assert t["status"] == "OPEN"


# ---------------------------------------------------------------------------
# backtest_strategies aggregation
# ---------------------------------------------------------------------------
def test_bt_median():
    assert bs._median([3, 1, 2]) == 2
    assert bs._median([1, 2, 3, 4]) == 2.5
    assert bs._median([]) is None
    assert bs._median([None, 5]) == 5


def _c(status, pnl, r, pct, mfe, mae, mins):
    return {"status": status, "pnl": pnl, "rMultiple": r, "pnlPct": pct,
            "mfePct": mfe, "maePct": mae, "minsToExit": mins}


def test_bt_scorecard():
    trades = [
        _c("TARGET", 4000.0, 2.0, 4.0, 5.0, -1.0, 30),
        _c("STOP", -2000.0, -1.0, -2.0, 1.0, -3.0, 20),
        _c("EXPIRED", 500.0, 0.25, 0.5, 2.0, -1.0, 100),
        {"status": "OPEN", "pnl": 0.0, "pnlPct": 0.0},
    ]
    sc = bs._scorecard(trades)
    assert sc["trades"] == 4 and sc["closed"] == 3 and sc["open"] == 1
    assert sc["target"] == 1 and sc["stop"] == 1 and sc["expired"] == 1
    assert sc["winRate"] == 33.3
    assert sc["totalR"] == 1.25
    assert sc["expectancyR"] == 0.42
    assert sc["realizedPnl"] == 2500.0
    assert sc["avgPnlPct"] == 0.83          # (4 - 2 + 0.5) / 3
    assert sc["avgMfePct"] == 2.67          # (5 + 1 + 2) / 3
    assert sc["avgMaePct"] == -1.67         # (-1 - 3 - 1) / 3
    assert sc["medianMinsToExit"] == 30


def test_bt_scorecard_empty():
    sc = bs._scorecard([])
    assert sc["closed"] == 0 and sc["winRate"] is None and sc["expectancyR"] is None


def test_bt_equity_cumulative():
    trades = [
        {"status": "TARGET", "pnl": 100.0, "closedTs": "2026-01-01T10:00:00"},
        {"status": "STOP", "pnl": -50.0, "closedTs": "2026-01-02T10:00:00"},
        {"status": "OPEN", "pnl": 999.0},         # excluded (no close)
    ]
    eq = bs._equity(trades)
    assert eq["points"] == [100, 50]
    assert eq["final"] == 50 and eq["n"] == 2


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} backtest tests passed")


if __name__ == "__main__":
    _main()
