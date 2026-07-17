"""
Unit tests for portfolio_backtest.py — replaying trades through a real book.

Everything that matters is PURE, so it's driven with hand-built trade dicts (the
shape backtest_daily emits): _usable filtering, direction-aware pnl/move, position
sizing (risk vs equal, capped by max-alloc + cash), drawdown / Sharpe, and the full
simulate() book (compounding, slot gating, capital gating, equity curve, shorts).
run() is exercised against a stubbed backtest_daily.run so nothing touches NSE/DB.

Run: python test_portfolio_backtest.py   (also works under pytest)
"""

import contextlib
import math

import portfolio_backtest as pb


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _t(sym="ACME", strat="momentum", direction="LONG", entry=100.0, stop=95.0,
       exit_px=110.0, status="TARGET", opened="2026-07-01", closed="2026-07-03",
       hold=2, **extra):
    t = {"symbol": sym, "strategy": strat, "direction": direction, "entry": entry,
         "stop": stop, "target": entry * 1.1, "exitPrice": exit_px, "status": status,
         "openedDate": opened, "closedDate": closed, "holdDays": hold}
    t.update(extra)
    return t


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_usable_filter():
    assert pb._usable(_t())
    assert not pb._usable(_t(status="OPEN"))                     # not closed
    assert not pb._usable(_t(exit_px=None))                      # no exit price
    assert not pb._usable(_t(entry=100, stop=100))              # degenerate stop
    assert not pb._usable(_t(opened="bad-date"))                # unparseable date
    assert not pb._usable(_t(opened="2026-07-05", closed="2026-07-01"))  # close < open


def test_move_and_pnl_direction_aware():
    assert round(pb._move_pct("LONG", 100, 110), 2) == 10.0
    assert round(pb._move_pct("SHORT", 100, 90), 2) == 11.11
    assert pb._pnl("LONG", 100, 110, 10) == 100
    assert pb._pnl("SHORT", 100, 90, 10) == 100                 # short profits when px falls


def test_max_drawdown():
    assert pb._max_drawdown([100, 120, 90, 130]) == 25.0        # 120 -> 90
    assert pb._max_drawdown([100, 101, 102]) == 0.0             # monotonic up
    assert pb._max_drawdown([]) == 0.0


def test_sharpe():
    assert pb._sharpe([0.01], 252) is None                      # < 2 points
    assert pb._sharpe([0.01, 0.01, 0.01], 252) is None          # zero variance
    s = pb._sharpe([0.01, -0.005, 0.02, 0.0], 252)
    assert isinstance(s, float) and s > 0


def test_size_risk_and_caps():
    # risk 1% of 1,000,000 = 10,000; stop 5 pts away → 2,000 shares of a ₹100 stock
    assert pb._size("risk", 1_000_000, 1_000_000, 100, 95, 5, 25, 1.0) == 2000
    # tight stop would want a huge position → capped at 25% alloc (250,000 → 2,500)
    assert pb._size("risk", 1_000_000, 1_000_000, 100, 99, 5, 25, 1.0) == 2500
    # low cash caps it (only 50,000 free → 500 shares)
    assert pb._size("risk", 1_000_000, 50_000, 100, 95, 5, 25, 1.0) == 500
    # degenerate stop → 0
    assert pb._size("risk", 1_000_000, 1_000_000, 100, 100, 5, 25, 1.0) == 0


def test_size_equal():
    # equal-weight: equity / max_positions = 200,000 → 2,000 shares of ₹100
    assert pb._size("equal", 1_000_000, 1_000_000, 100, 95, 5, 25, 1.0) == 2000
    # can't afford even one share
    assert pb._size("equal", 100, 100, 1000, 900, 5, 25, 1.0) == 0


# ---------------------------------------------------------------------------
# simulate — the book
# ---------------------------------------------------------------------------
def test_simulate_empty():
    r = pb.simulate([])
    assert r["tradesTaken"] == 0 and r["equityCurve"] == [] and r["note"]
    assert r["endCapital"] == r["startCapital"] == 1_000_000.0


def test_simulate_single_winner_compounds():
    r = pb.simulate([_t(entry=100, stop=95, exit_px=110)],
                    start_capital=1_000_000, max_positions=5, risk_pct=1.0)
    # 2,000 shares * (110-100) = +20,000
    assert r["tradesTaken"] == 1 and r["closedTrades"] == 1
    assert r["endCapital"] == 1_020_000.0
    assert r["totalReturnPct"] == 2.0
    assert r["winRate"] == 100.0
    assert r["maxDrawdownPct"] == 0.0
    assert len(r["equityCurve"]) == 2                           # open day + close day
    assert r["equityCurve"][-1]["equity"] == 1_020_000.0


def test_simulate_single_loser_and_drawdown():
    r = pb.simulate([_t(entry=100, stop=95, exit_px=95, status="STOP")],
                    start_capital=1_000_000, risk_pct=1.0)
    # 2,000 * (95-100) = -10,000  → exactly the 1% risk we sized for
    assert r["endCapital"] == 990_000.0
    assert r["totalReturnPct"] == -1.0
    assert r["winRate"] == 0.0
    assert r["maxDrawdownPct"] == 1.0


def test_simulate_slot_gating():
    # 3 signals the same day, but only 2 slots → 1 skipped for lack of a slot
    trades = [_t(sym=f"S{i}", opened="2026-07-01", closed="2026-07-10") for i in range(3)]
    r = pb.simulate(trades, max_positions=2)
    assert r["tradesTaken"] == 2 and r["tradesSkippedSlot"] == 1
    assert r["maxConcurrent"] == 2


def test_simulate_capital_gating():
    # ₹50 of capital can't afford a single ₹100 share → skipped for capital
    r = pb.simulate([_t(entry=100)], start_capital=50)
    assert r["tradesTaken"] == 0 and r["tradesSkippedCapital"] == 1
    assert r["endCapital"] == 50


def test_simulate_short_profits_on_drop():
    r = pb.simulate([_t(direction="SHORT", entry=100, stop=105, exit_px=90, status="TARGET")],
                    risk_pct=1.0)
    # risk 1% with a 5-pt stop → 2,000 sh; short gains (100-90)*2000 = +20,000
    assert r["endCapital"] == 1_020_000.0 and r["winRate"] == 100.0
    assert r["avgWinPct"] > 0


def test_simulate_capital_frees_up_for_reuse():
    # Two trades on the SAME name/slot but non-overlapping in time; with 1 slot the
    # second still fits because the first freed its capital when it closed.
    trades = [
        _t(sym="A", opened="2026-07-01", closed="2026-07-03", entry=100, stop=95, exit_px=110),
        _t(sym="A", opened="2026-07-04", closed="2026-07-06", entry=100, stop=95, exit_px=110),
    ]
    r = pb.simulate(trades, max_positions=1)
    assert r["tradesTaken"] == 2 and r["tradesSkippedSlot"] == 0


def test_simulate_rank_key_prefers_higher():
    # Same day, one slot; the higher-conviction trade should be the one taken.
    trades = [
        _t(sym="LOW", conviction=10, exit_px=90, stop=95, status="STOP",
           opened="2026-07-01", closed="2026-07-05"),
        _t(sym="HIGH", conviction=90, exit_px=110, stop=95, status="TARGET",
           opened="2026-07-01", closed="2026-07-05"),
    ]
    r = pb.simulate(trades, max_positions=1, rank_key="conviction")
    assert r["tradesTaken"] == 1 and r["closedTrades"] == 1
    assert r["endCapital"] > r["startCapital"]                  # took HIGH (the winner)


# ---------------------------------------------------------------------------
# run — impure, stubbed backtest_daily
# ---------------------------------------------------------------------------
def test_run_wires_backtest_and_per_strategy():
    fake = {
        "days": 60, "universeWithData": 120, "universeAvailable": 2400,
        "range": {"from": "2026-04-01", "to": "2026-07-01"},
        "trades": [
            _t(sym="A", strat="momentum", exit_px=110),
            _t(sym="B", strat="meanrev", exit_px=95, stop=95, status="STOP"),
        ],
    }
    with _patch(pb.bd, "run", lambda **k: fake):
        out = pb.run(days=60, universe_size=120, source="eod")
    assert out["source"] == "eod"
    assert out["window"]["trades"] == 2
    assert out["overall"]["tradesTaken"] == 2
    ids = [r["id"] for r in out["perStrategy"]]
    assert set(ids) == {"momentum", "meanrev"}
    # momentum won, meanrev lost → momentum ranks first
    assert out["perStrategy"][0]["id"] == "momentum"
    assert out["perStrategy"][0]["name"] == "Multi-Signal Momentum"


def test_run_handles_no_trades():
    with _patch(pb.bd, "run", lambda **k: {"message": "No EOD history ingested yet"}):
        out = pb.run(source="eod")
    assert out["overall"]["tradesTaken"] == 0
    assert out["perStrategy"] == []
    assert out["message"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print("ok  ", fn.__name__)
        except Exception as e:
            fails += 1
            print("FAIL", fn.__name__, "->", repr(e))
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
