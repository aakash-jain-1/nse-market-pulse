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
import copy
import math

from nse_pulse.backtest import portfolio_backtest as pb


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
    # costs=False throughout this block: these pin the exact sizing/compounding
    # arithmetic, which is clearest against clean fills. Costs get their own tests.
    r = pb.simulate([_t(entry=100, stop=95, exit_px=110)],
                    start_capital=1_000_000, max_positions=5, risk_pct=1.0,
                    costs=False)
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
                    start_capital=1_000_000, risk_pct=1.0, costs=False)
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
                    risk_pct=1.0, costs=False)
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


def test_simulate_mark_to_market_shows_intratrade_drawdown():
    # A long that dips well below entry mid-hold, then exits a winner. Cost-basis sees
    # a flat curve (no drawdown); mark-to-market must show the mid-trade dip.
    trade = _t(entry=100, stop=90, exit_px=110, status="TARGET",
               opened="2026-07-01", closed="2026-07-05")
    closes = {"ACME": {"2026-07-01": 100, "2026-07-02": 95, "2026-07-03": 92,
                       "2026-07-04": 105}}
    mtm = pb.simulate([trade], start_capital=1_000_000, risk_pct=1.0, closes=closes,
                      costs=False)
    cost = pb.simulate([trade], start_capital=1_000_000, risk_pct=1.0,      # no closes
                       costs=False)
    # Same realized outcome either way (qty 1,000 × +10 = +10,000)…
    assert mtm["endCapital"] == cost["endCapital"] == 1_010_000.0
    # …but only MTM exposes the -8% mark on 2026-07-03 (92 vs 100 entry, 1,000 sh).
    assert mtm["maxDrawdownPct"] == 0.8
    assert cost["maxDrawdownPct"] == 0.0
    # MTM curve walks the intervening trading days; cost-basis only the open+close.
    assert len(mtm["equityCurve"]) == 5 and len(cost["equityCurve"]) == 2
    assert mtm["equityCurve"][2]["equity"] == 992_000.0                     # 2026-07-03


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


def test_run_ranks_same_day_by_conviction_score():
    """run() passes rank_key='score', so when signals contend for one slot the book
    takes the higher-conviction trade (bd attaches `score` to every trade)."""
    fake = {
        "days": 30, "universeWithData": 2, "universeAvailable": 2, "range": {},
        "trades": [
            _t(sym="MEH", strat="gap", score=12, exit_px=90, stop=95, status="STOP",
               opened="2026-07-01", closed="2026-07-05"),
            _t(sym="STRONG", strat="momentum", score=95, exit_px=110, stop=95,
               status="TARGET", opened="2026-07-01", closed="2026-07-05"),
        ],
    }
    with _patch(pb.bd, "run", lambda **k: fake):
        out = pb.run(max_positions=1)
    o = out["overall"]
    assert o["tradesTaken"] == 1 and o["tradesSkippedSlot"] == 1
    assert o["endCapital"] > o["startCapital"]     # took STRONG (the winner), not MEH


def test_run_handles_no_trades():
    with _patch(pb.bd, "run", lambda **k: {"message": "No EOD history ingested yet"}):
        out = pb.run(source="eod")
    assert out["overall"]["tradesTaken"] == 0
    assert out["perStrategy"] == []
    assert out["message"]


# ---------------------------------------------------------------------------
# transaction costs
# ---------------------------------------------------------------------------
def test_cost_schedule_true_false_and_override():
    assert pb.cost_schedule(True) == pb.COSTS
    off = pb.cost_schedule(False)
    assert set(off) == set(pb.COSTS) and not any(off.values())
    assert pb.cost_schedule(None) == off                      # None also means gross
    ov = pb.cost_schedule({"slippagePctPerSide": 0.5, "bogus": 9})
    assert ov["slippagePctPerSide"] == 0.5                    # overridden
    assert ov["sttBuyPct"] == pb.COSTS["sttBuyPct"]           # the rest defaulted
    assert "bogus" not in ov                                  # unknown keys ignored


def test_charges_are_side_aware():
    cs = pb.cost_schedule(True)
    buy = pb.charges(100_000.0, "buy", cs)
    sell = pb.charges(100_000.0, "sell", cs)
    # stamp duty is a buy-side levy; the depository fee is charged on delivery sells
    assert buy["stamp"] > 0 and buy["dp"] == 0.0
    assert sell["dp"] > 0 and sell["stamp"] == 0.0
    # delivery STT hits both legs at the same rate
    assert round(buy["stt"], 2) == round(sell["stt"], 2) == 100.0
    # GST rides on brokerage + exchange + SEBI only (never on STT/stamp)
    assert round(buy["gst"], 2) == round(
        0.18 * (buy["brokerage"] + buy["exchange"] + buy["sebi"]), 2)
    assert pb.charges(0.0, "buy", cs) == {}                   # nothing traded, nothing owed


def test_brokerage_takes_the_cheaper_of_flat_and_pct():
    # "Rs 20 an order or 0.03%, whichever is lower" — the usual discount plan
    cs = pb.cost_schedule({"brokeragePctPerSide": 0.03})
    assert round(pb._brokerage(10_000.0, cs), 2) == 3.0       # 0.03% is cheaper
    assert round(pb._brokerage(10_000_000.0, cs), 2) == 20.0  # flat caps it
    flat_only = pb.cost_schedule(True)                        # pct = 0 in the default
    assert pb._brokerage(10_000.0, flat_only) == 20.0
    free = pb.cost_schedule({"brokerageFlatPerOrder": 0.0, "brokeragePctPerSide": 0.03})
    assert round(pb._brokerage(10_000.0, free), 2) == 3.0     # no flat fee to compare


def test_slippage_always_works_against_you():
    cs = pb.cost_schedule({"slippagePctPerSide": 1.0})
    assert pb.slipped(100.0, "buy", cs) == 101.0              # you buy higher
    assert pb.slipped(100.0, "sell", cs) == 99.0              # and sell lower
    assert pb._sides("LONG") == ("buy", "sell")
    assert pb._sides("SHORT") == ("sell", "buy")              # a short opens by selling


def test_costs_reduce_the_return_and_reconcile_exactly():
    trades = [_t()]
    net, gross = pb.simulate(trades), pb.simulate(trades, costs=False)
    c = net["costs"]
    assert net["totalReturnPct"] < gross["totalReturnPct"]     # the whole point
    # `total` is exactly what the same executed positions gave up
    assert round(c["total"], 2) == round(c["grossEndCapital"] - net["endCapital"], 2)
    assert round(sum(c["breakdown"].values()), 2) == round(c["total"], 2)
    assert c["enabled"] and c["schedule"] and c["turnover"] > 0
    # delivery STT is the single biggest line item, and slippage is charged twice
    assert c["breakdown"]["stt"] == max(c["breakdown"].values())
    assert c["breakdown"]["slippage"] > 0


def test_costs_off_is_a_true_gross_run():
    g = pb.simulate([_t()], costs=False)["costs"]
    assert g["total"] == 0.0 and g["enabled"] is False and g["schedule"] is None
    assert not any(g["breakdown"].values())
    assert g["grossTotalReturnPct"] == pb.simulate([_t()], costs=False)["totalReturnPct"]


def test_costs_can_turn_a_thin_winner_into_a_loser():
    """The reason this exists: a rule whose edge is smaller than its costs looks
    profitable gross and loses money net."""
    thin = [_t(sym=f"S{i}", entry=100.0, stop=99.0, exit_px=100.10,
               opened=f"2026-07-{i+1:02d}", closed=f"2026-07-{i+2:02d}")
            for i in range(8)]
    gross, net = pb.simulate(thin, costs=False), pb.simulate(thin)
    assert gross["totalReturnPct"] > 0                         # edge exists on paper
    assert net["totalReturnPct"] < 0                           # and is eaten by costs
    assert net["costs"]["perTrade"] > 0


def test_short_pays_sell_side_costs_on_the_way_in():
    """A short opens by selling, so the depository fee lands on the OPENING leg and
    the stamp duty on the closing buy — the mirror of a long."""
    s = pb.simulate([_t(direction="SHORT", entry=100.0, stop=105.0, exit_px=90.0)])
    b = s["costs"]["breakdown"]
    assert b["dp"] > 0 and b["stamp"] > 0
    assert s["endCapital"] > s["startCapital"]                 # still a profitable short
    assert s["totalReturnPct"] < pb.simulate(
        [_t(direction="SHORT", entry=100.0, stop=105.0, exit_px=90.0)],
        costs=False)["totalReturnPct"]


def test_entry_costs_cannot_overdraw_cash():
    """Sizing fills the book to the last rupee, so the entry charges have to be paid
    out of something — the position is shrunk (or skipped), never funded on credit."""
    r = pb.simulate([_t(entry=100.0, stop=1.0, exit_px=101.0)],   # stop so wide the
                    start_capital=10_000.0, max_alloc_pct=100.0)  # cap is all the cash
    assert r["tradesTaken"] + r["tradesSkippedCapital"] == 1
    for pt in r["equityCurve"]:
        assert pt["equity"] > 0                                # never went negative


def test_costs_reported_even_with_nothing_to_simulate():
    c = pb.simulate([_t(status="OPEN")])["costs"]
    assert c["total"] == 0.0 and c["perTrade"] == 0.0 and c["pctOfTurnover"] is None


def test_params_records_whether_costs_were_charged():
    assert pb.simulate([_t()])["params"]["costs"] is True
    assert pb.simulate([_t()], costs=False)["params"]["costs"] is False


def test_simulate_never_mutates_the_trades_it_is_given():
    """The guard that keeps costs OUT of the R leaderboards: `backtest_daily` hands the
    same trade dicts to its own scorecards, so slipping fills here must not write back."""
    trades = [_t(sym="A"), _t(sym="B", direction="SHORT", stop=105, exit_px=90)]
    before = copy.deepcopy(trades)
    pb.simulate(trades)
    assert trades == before


def test_run_threads_costs_and_reports_gross_per_strategy():
    fake = {"days": 30, "universeWithData": 2, "universeAvailable": 2, "range": {},
            "trades": [_t(sym="A", strat="momentum", exit_px=110)]}
    with _patch(pb.bd, "run", lambda **k: fake):
        net = pb.run(days=30)
        gross = pb.run(days=30, costs=False)
    assert net["overall"]["totalReturnPct"] < gross["overall"]["totalReturnPct"]
    row = net["perStrategy"][0]
    # turnover differs per strategy, so the table carries both to stay honest
    assert row["grossTotalReturnPct"] > row["totalReturnPct"]
    assert row["costsTotal"] > 0
    assert gross["perStrategy"][0]["costsTotal"] == 0.0


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
