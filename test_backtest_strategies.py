"""
Unit tests for backtest_strategies.py — the archived-context replay backtester.

All the scoring/exit math is pure, so we test it without touching the DB or NSE:
the baked-UTC epoch (_epoch_s), the per-cycle price map (quotes over boards),
signed %-move, the coarse single-price _resolve (target/stop/expiry), the
even/odd _median, the scorecard, the equity curve, the regime × strategy
_leaderboard, the chronological LTP fallback resolver (_resolve_ltp), and the
Pass-1 entry taker (_take_entries) with strat.generate stubbed + per-day dedup.

Run: python test_backtest_strategies.py   (also works under pytest)
"""

import contextlib
from datetime import datetime, timezone

import backtest_strategies as bs


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------
def test_epoch_s_bakes_wall_clock_as_utc():
    want = int(datetime(2026, 7, 8, 9, 15, tzinfo=timezone.utc).timestamp())
    assert bs._epoch_s("2026-07-08 09:15:00") == want
    # a tz-aware string keeps the same wall clock (offset dropped, not applied)
    assert bs._epoch_s("2026-07-08T09:15:00+05:30") == want


def test_price_map_quotes_win():
    ctx = {"scanner": [{"symbol": "A", "ltp": 100}, {"symbol": "B", "ltp": 50}],
           "gainers": [{"symbol": "C", "ltp": 10}],
           "quotes": {"A": {"ltp": 101.5}}}     # quote overrides the board
    m = bs._price_map(ctx)
    assert m == {"A": 101.5, "B": 50, "C": 10}


def test_move_pct():
    assert bs._move_pct("LONG", 100, 105) == 5.0
    assert bs._move_pct("SHORT", 100, 95) == 5.0


def test_median_even_odd():
    assert bs._median([1, 2, 3]) == 2
    assert bs._median([1, 2, 3, 4]) == 2.5      # averages the two middles
    assert bs._median([None]) is None


# ---------------------------------------------------------------------------
# _resolve — coarse single-price exit
# ---------------------------------------------------------------------------
def _t(direction="LONG", entry=100.0, stop=97.0, target=106.0, qty=1000.0,
       max_sessions=3):
    return {"direction": direction, "entry": entry, "stop": stop, "target": target,
            "qty": qty, "maxSessions": max_sessions, "status": "OPEN", "ltp": entry,
            "pnl": 0.0, "pnlPct": 0.0, "rMultiple": 0.0}


def test_resolve_target():
    t = _t()
    assert bs._resolve(t, 107.0, 1) is True
    assert t["status"] == "TARGET" and t["exitPrice"] == 106.0
    assert t["pnl"] == 6000.0 and t["rMultiple"] == 3.0


def test_resolve_stop():
    t = _t()
    assert bs._resolve(t, 96.0, 1) is True
    assert t["status"] == "STOP" and t["exitPrice"] == 97.0
    assert t["pnl"] == -3000.0


def test_resolve_expiry_and_open():
    t = _t()
    # between levels but past the horizon → time-expire at the sample price
    assert bs._resolve(t, 101.0, 5) is True
    assert t["status"] == "EXPIRED" and t["exitPrice"] == 101.0
    t2 = _t()
    # between levels, within the horizon → stays open
    assert bs._resolve(t2, 101.0, 1) is False
    assert t2["status"] == "OPEN"


# ---------------------------------------------------------------------------
# scorecard / equity / leaderboard
# ---------------------------------------------------------------------------
def _closed(status, pnl, r, pct, regime="Trend-Up", ts="2026-07-10 10:00:00"):
    return {"status": status, "pnl": pnl, "rMultiple": r, "pnlPct": pct,
            "regimeAtEntry": regime, "closedTs": ts, "mfePct": 1.0, "maePct": -1.0,
            "minsToExit": 30}


def test_scorecard():
    trades = [_closed("TARGET", 6000, 3.0, 6.0), _closed("STOP", -3000, -1.5, -3.0),
              _closed("EXPIRED", 500, 0.25, 0.5),
              {"status": "OPEN", "pnl": 0, "rMultiple": 0, "pnlPct": 0}]
    sc = bs._scorecard(trades)
    assert sc["closed"] == 3 and sc["open"] == 1
    assert sc["target"] == 1 and sc["stop"] == 1 and sc["expired"] == 1
    assert sc["winRate"] == round(1 / 3 * 100, 1)
    assert sc["totalR"] == 1.75 and sc["realizedPnl"] == 3500.0
    assert sc["medianMinsToExit"] == 30


def test_equity_curve():
    trades = [_closed("TARGET", 6000, 3.0, 6.0, ts="2026-07-10 10:00:00"),
              _closed("STOP", -3000, -1.5, -3.0, ts="2026-07-11 10:00:00")]
    eq = bs._equity(trades)
    assert eq["points"] == [6000.0, 3000.0] and eq["final"] == 3000.0 and eq["n"] == 2


def test_leaderboard_best_by_avg_pct():
    books = {"momentum": [_closed("TARGET", 6000, 3.0, 6.0),
                          _closed("TARGET", 6000, 3.0, 4.0)],
             "meanrev": [_closed("STOP", -3000, -1.5, -3.0)]}
    lb = bs._leaderboard(books, ["momentum", "meanrev"],
                         {"momentum": "Momentum", "meanrev": "Mean-Rev"})
    row = next(r for r in lb["rows"] if r["regime"] == "Trend-Up")
    assert row["best"] == "momentum"            # +5% avg beats −3%
    assert row["cells"]["momentum"]["avgPnlPct"] == 5.0
    assert row["cells"]["meanrev"]["winRate"] == 0.0


# ---------------------------------------------------------------------------
# _resolve_ltp — chronological fallback resolver
# ---------------------------------------------------------------------------
def test_resolve_ltp_hits_target():
    t = _t()
    t.update(openedTs="2026-07-10 09:20:00", openedDay="2026-07-10", closedTs=None,
             closedDay=None)
    series = [("2026-07-10 09:15:00", "2026-07-10", 99.0),   # before entry → skipped
              ("2026-07-10 12:00:00", "2026-07-10", 107.0)]  # target
    bs._resolve_ltp(t, series, {"2026-07-10": 0})
    assert t["status"] == "TARGET" and t["closedTs"] == "2026-07-10 12:00:00"


# ---------------------------------------------------------------------------
# _take_entries — Pass 1 with strat.generate stubbed
# ---------------------------------------------------------------------------
def test_take_entries_dedup_per_day():
    idea = {"symbol": "A", "direction": "LONG", "entry": 100.0, "stop": 98.0,
            "target": 106.0, "conviction": 80}
    cycles = [
        {"day": "2026-07-10", "ts": "2026-07-10 09:20:00", "regime": "Trend-Up",
         "ctx": {"scanner": [{"symbol": "A", "ltp": 100.0}]}},
        {"day": "2026-07-10", "ts": "2026-07-10 09:25:00", "regime": "Trend-Up",
         "ctx": {"scanner": [{"symbol": "A", "ltp": 101.0}]}},
    ]
    with _patch(bs.strat, "generate", lambda sid, ctx: [dict(idea)]):
        books, series = bs._take_entries(cycles, ["momentum"], {"2026-07-10": 0},
                                         "continuous", 3, 10)
    # same symbol+direction twice in one day → a single entry (live dedup)
    assert len(books["momentum"]) == 1
    assert books["momentum"][0]["entry"] == 100.0
    assert len(series["A"]) == 2               # both cycles feed the LTP series


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} backtest_strategies tests passed")


if __name__ == "__main__":
    _main()
