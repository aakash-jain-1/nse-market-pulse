"""
Unit tests for backtest_daily.py — the daily-bar historical backtester.

Every money-critical helper is pure (given bars in hand), so we exercise the
whole reconstruction pipeline offline: date parsing (_clean_date/_dmy_to_iso),
OI% map, freshness TTL, per-bar features, the close-independent _signals, the
daily stop-first exit walk (_resolve, incl. straddle + expiry), a full _trade,
the _backtest_symbol integration, the equal-weight regime proxy
(_classify_regime/_regime_map), the regime × strategy leaderboard, the a-priori
regimeFit gate, the scorecard, and strategy_of_day (regime + leaderboard stubbed).

No network: everything is fed hand-built bars.

Run: python test_backtest_daily.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile

import backtest_daily as bd
import db
import strategies as strat


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ---------------------------------------------------------------------------
# date / small parsers
# ---------------------------------------------------------------------------
def test_clean_date_and_dmy():
    assert bd._clean_date({"date": "08-Jul-2026"}) == "2026-07-08"
    assert bd._clean_date({"date": "bad", "iso": "2026-07-08T00:00:00"}) == "2026-07-08"
    assert bd._dmy_to_iso("31-Jul-2026") == "2026-07-31"
    assert bd._dmy_to_iso("nope") is None


def test_oi_map_from_rows():
    rows = [{"d": "2026-07-01", "oi": 110, "changeOi": 10},   # prev 100 → +10%
            {"d": "2026-07-02", "oi": 90, "changeOi": None},  # skipped
            {"d": None, "oi": 5, "changeOi": 1}]              # skipped
    m = bd._oi_map_from_rows(rows)
    assert m == {"2026-07-01": 10.0}


def test_fresh_ttl():
    assert bd._fresh(bd._now()) is True
    assert bd._fresh("2000-01-01 00:00:00") is False
    assert bd._fresh("garbage") is False
    assert bd._fresh(None) is False


def test_median():
    assert bd._median([3, 1, 2]) == 2          # middle of the sorted list
    assert bd._median([1, None, 3]) == 3        # upper-middle of [1,3]
    assert bd._median([]) is None


# ---------------------------------------------------------------------------
# features + signals
# ---------------------------------------------------------------------------
def _bar(d, c, h, l, v, prev=None, deliv=None):
    return {"d": d, "symbol": "X", "close": c, "high": h, "low": l,
            "volume": v, "prevClose": prev, "delivPct": deliv}


def test_features_second_bar():
    bars = [_bar("d0", 100, 101, 99, 1000, prev=100),
            _bar("d1", 110, 112, 100, 3000, prev=100),
            _bar("d2", 108, 113, 107, 1500, prev=110)]
    f = bd._features(bars)[1]
    assert round(f["ret1"], 2) == 10.0          # 110 vs prev 100
    assert f["volMult"] == 3.0                  # 3000 / mean([1000])
    assert f["hi20"] == 101 and f["lo20"] == 99
    assert f["hh"] == 112 and f["ll"] == 99
    assert round(f["rngPos"], 3) == round((110 - 100) / (112 - 100), 3)


def test_signals_momentum_and_delivery():
    f = {"ret1": 4.5, "volMult": 2.0, "rngPos": 0.8, "delivPct": 70}
    assert set(bd._signals(f)) == {("momentum", "LONG"), ("delivery", "LONG")}
    f2 = {"ret1": -5.0, "volMult": None, "rngPos": 0.2, "delivPct": None}
    assert bd._signals(f2) == [("meanrev", "LONG")]      # fade the drop, no vm→no momentum
    f3 = {"ret1": 6.0, "volMult": None, "rngPos": 0.5, "delivPct": None}
    assert ("meanrev", "SHORT") in bd._signals(f3)


# ---------------------------------------------------------------------------
# _resolve — daily stop-first exit walk
# ---------------------------------------------------------------------------
def _rb(h, l, c=None, d="d"):
    return {"high": h, "low": l, "close": c if c is not None else (h + l) / 2, "d": d}


def test_resolve_target_stop_straddle_expire():
    entry = 100.0
    # LONG stop 97 / target 106
    tgt = bd._resolve("LONG", entry, 97, 106, [_rb(100, 100), _rb(107, 101)], 0, 5)
    assert tgt[0] == "TARGET" and tgt[1] == 106
    stop = bd._resolve("LONG", entry, 97, 106, [_rb(100, 100), _rb(101, 96)], 0, 5)
    assert stop[0] == "STOP" and stop[1] == 97
    # a bar piercing both → STOP wins (conservative)
    both = bd._resolve("LONG", entry, 97, 106, [_rb(100, 100), _rb(107, 96)], 0, 5)
    assert both[0] == "STOP"
    # never hit within the hold → EXPIRED at the last close
    exp = bd._resolve("LONG", entry, 97, 106,
                      [_rb(100, 100), _rb(101, 99, c=100.5), _rb(102, 99, c=101)], 0, 5)
    assert exp[0] == "EXPIRED" and exp[1] == 101
    # only the entry bar available → still OPEN
    assert bd._resolve("LONG", entry, 97, 106, [_rb(100, 100)], 0, 5)[0] == "OPEN"


def test_resolve_short_symmetric():
    # SHORT entry 100, stop 103, target 94: a bar low<=94 → TARGET
    r = bd._resolve("SHORT", 100.0, 103, 94, [_rb(100, 100), _rb(99, 93)], 0, 5)
    assert r[0] == "TARGET" and r[1] == 94


# ---------------------------------------------------------------------------
# _trade
# ---------------------------------------------------------------------------
def test_trade_long_target_pnl():
    bars = [_rb(100, 100, c=100, d="d0"), _rb(112, 106, c=110, d="d1")]
    bars[0]["symbol"] = "X"
    t = bd._trade("momentum", "LONG", bars, None, 0, 5)   # _trade ignores feats
    assert t["status"] == "TARGET"
    assert t["entry"] == 100 and t["target"] == 106   # +6% target
    assert t["pnlPct"] == 6.0 and t["rMultiple"] == 2.0
    assert t["holdDays"] == 1


def test_backtest_symbol_fires_one_momentum():
    bars = [_bar("2026-07-01", 100, 101, 99, 1000, prev=100),
            _bar("2026-07-02", 100, 101, 99, 1000, prev=100),
            _bar("2026-07-03", 104.5, 105.5, 100, 1800, prev=100),   # ret+4.5, vm 1.8
            _bar("2026-07-04", 111, 112, 105, 1500, prev=104.5)]      # tags target
    trades = bd._backtest_symbol(bars, {}, "2026-01-01", 5)
    assert len(trades) == 1
    assert trades[0]["strategy"] == "momentum" and trades[0]["direction"] == "LONG"
    assert trades[0]["status"] == "TARGET"


# ---------------------------------------------------------------------------
# new EOD signals: gap / squeeze / rel_strength
# ---------------------------------------------------------------------------
def _fb(d, o, c, h, l, v=1000, prev=None, deliv=None):
    """Full daily bar incl. open (needed by gap)."""
    return {"d": d, "symbol": "X", "open": o, "close": c, "high": h, "low": l,
            "volume": v, "prevClose": prev if prev is not None else c, "delivPct": deliv}


def test_strats_include_new_eod():
    ids = [s["id"] for s in bd.STRATS]
    assert {"rel_strength", "gap", "squeeze"} <= set(ids)
    assert len(ids) == 9


def test_backtest_gap_and_go_long():
    bars = [
        _fb("2026-07-01", 100, 100, 101, 99, prev=100),
        _fb("2026-07-02", 103, 104, 104.5, 102.5, prev=100),   # +3% gap, close holds open
        _fb("2026-07-03", 106, 109, 109.5, 105, prev=104),     # resolve: +4% target hit
    ]
    dr = {"2026-07-02": {"label": "Trend-Up", "mktPct": 0.2}}   # trend → gap-and-go
    trades = bd._backtest_symbol(bars, {}, "2026-01-01", 5, dr)
    gaps = [t for t in trades if t["strategy"] == "gap"]
    assert gaps and gaps[0]["direction"] == "LONG"


def test_backtest_gap_fade_short():
    bars = [
        _fb("2026-07-01", 100, 100, 101, 99, prev=100),
        _fb("2026-07-02", 103, 101, 103.5, 100.5, prev=100),   # +3% gap but close rejects open
        _fb("2026-07-03", 99, 96, 100, 95, prev=101),          # resolve down → target
    ]
    dr = {"2026-07-02": {"label": "Range", "mktPct": 0.0}}      # quiet → fade
    trades = bd._backtest_symbol(bars, {}, "2026-01-01", 5, dr)
    gaps = [t for t in trades if t["strategy"] == "gap"]
    assert gaps and gaps[0]["direction"] == "SHORT"


def test_backtest_squeeze_breakout_long():
    wide = [_fb(f"2026-06-{d:02d}", 100, 100, 105, 95, prev=100) for d in range(1, 7)]
    nr7 = _fb("2026-06-08", 100, 100, 100.5, 99.5, prev=100)        # tightest range (NR7)
    today = _fb("2026-06-09", 100.5, 101, 101.5, 100.4, prev=100)   # close breaks NR7 high
    resolve = _fb("2026-06-10", 101, 107, 108, 100.5, prev=101)     # target
    bars = wide + [nr7, today, resolve]
    trades = bd._backtest_symbol(bars, {}, "2026-01-01", 5, None)
    sq = [t for t in trades if t["strategy"] == "squeeze"]
    assert sq and sq[0]["direction"] == "LONG"


def test_backtest_rel_strength_long():
    closes = [100, 100, 101, 102, 103, 104, 105]                    # +5% over 5 sessions
    bars = [_fb(f"2026-07-{i + 1:02d}", c, c, c + 0.5, c - 0.5,
                prev=(closes[i - 1] if i else 100)) for i, c in enumerate(closes)]
    bars.append(_fb("2026-07-08", 105, 108, 112, 105, prev=105))    # resolve target
    dr = {b["d"]: {"label": "Trend-Up", "mktPct": 0.0} for b in bars}   # flat market
    trades = bd._backtest_symbol(bars, {}, "2026-01-01", 5, dr)
    rs = [t for t in trades if t["strategy"] == "rel_strength"]
    assert rs and rs[0]["direction"] == "LONG"


# ---------------------------------------------------------------------------
# regime proxy
# ---------------------------------------------------------------------------
def test_classify_regime_branches():
    assert bd._classify_regime(None, 0, 0, None) == "Unknown"
    assert bd._classify_regime(0.5, 5, 1, -1.5) == "Recovery"
    assert bd._classify_regime(-0.5, 1, 5, 1.5) == "Pullback"
    assert bd._classify_regime(0.8, 10, 3, None) == "Trend-Up"
    assert bd._classify_regime(-0.8, 3, 10, None) == "Trend-Down"
    assert bd._classify_regime(0.2, 5, 5, None) == "Range"
    assert bd._classify_regime(0.5, 5, 5, None) == "Mixed"


def test_regime_map():
    hist = {"X": [{"d": "d1", "close": 100}, {"d": "d2", "close": 102},
                  {"d": "d3", "close": 101}]}
    rm = bd._regime_map(hist)
    assert rm["d2"]["label"] == "Trend-Up"     # +2%, adv breadth, no prior
    assert rm["d3"]["label"] == "Pullback"     # prior +2% then a drop


# ---------------------------------------------------------------------------
# volatility axis (realized-vol proxy) — orthogonal to the regime label
# ---------------------------------------------------------------------------
def test_stdev():
    assert bd._stdev([]) is None
    assert bd._stdev([5]) is None              # needs >=2 points
    assert bd._stdev([1, None]) is None        # only 1 real point after filtering None
    assert bd._stdev([3, None, 3]) == 0.0      # two identical real points → 0
    assert abs(bd._stdev([2, 4, 4, 4, 5, 5, 7, 9]) - 2.13809) < 1e-4  # sample stdev


def test_vol_state_pct_buckets():
    ranked = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert bd._vol_state_pct(1, ranked) == "Calm"        # 0th pct
    assert bd._vol_state_pct(5, ranked) == "Normal"      # 40th pct
    assert bd._vol_state_pct(10, ranked) == "Elevated"   # 90th pct
    assert bd._vol_state_pct(None, ranked) is None
    assert bd._vol_state_pct(5, []) is None


def test_regime_map_has_vol_axis():
    # rising then choppy so realized vol is defined on the later days
    closes = [100, 101, 103, 100, 104, 99, 105]
    hist = {"X": [{"d": f"d{i}", "close": c} for i, c in enumerate(closes)]}
    rm = bd._regime_map(hist)
    days = sorted(rm)
    # every mapped day still carries a directional label
    assert all(rm[d]["label"] for d in days)
    # volState + realVol appear once >=2 daily moves exist in the window
    assert any(rm[d]["volState"] in bd._VOL_ORDER for d in days)
    assert any(rm[d]["realVol"] is not None for d in days)


def _closedv(strategy, status, r, pct, vol="Calm"):
    t = _closed(strategy, status, r, pct)
    t["volAtEntry"] = vol
    return t


def test_vol_leaderboard_buckets_by_volatility():
    trades = [_closedv("momentum", "TARGET", 2.0, 6.0, "Elevated"),
              _closedv("momentum", "TARGET", 2.0, 6.0, "Elevated"),
              _closedv("momentum", "STOP", -1.0, -3.0, "Elevated"),
              _closedv("meanrev", "TARGET", 1.0, 3.0, "Calm")]
    lb = bd._vol_leaderboard(trades)
    # rows keyed by volState, ordered Calm..Normal..Elevated (present buckets only)
    assert [r["volState"] for r in lb["rows"]] == ["Calm", "Elevated"]
    ev = next(r for r in lb["rows"] if r["volState"] == "Elevated")
    assert ev["best"] == "momentum" and ev["cells"]["momentum"]["closed"] == 3
    assert ev["cells"]["momentum"]["expectancyR"] == 1.0


# ---------------------------------------------------------------------------
# vol-conditioned selection: blend regime + vol expectancy
# ---------------------------------------------------------------------------
def test_blend_r():
    assert bd._blend_r(None, None) is None
    assert bd._blend_r(0.5, None) == 0.5            # vol missing → regime only
    assert bd._blend_r(None, 0.5) == 0.5            # regime missing → vol only
    assert abs(bd._blend_r(1.0, 0.0) - 0.6) < 1e-9  # 0.6*reg + 0.4*vol
    assert abs(bd._blend_r(0.0, 1.0) - 0.4) < 1e-9


def test_vol_cells_lookup():
    vol_lb = {"order": ["m"], "rows": [
        {"volState": "Calm", "cells": {"m": {"expectancyR": 0.2, "closed": 5}}},
        {"volState": "Elevated", "cells": {"m": {"expectancyR": -0.1, "closed": 8}}}]}
    assert bd._vol_cells(vol_lb, "Elevated")["m"]["expectancyR"] == -0.1
    assert bd._vol_cells(vol_lb, "Calm")["m"]["closed"] == 5
    assert bd._vol_cells(vol_lb, "Normal") == {}     # bucket not present
    assert bd._vol_cells(None, "Calm") == {} and bd._vol_cells(vol_lb, None) == {}


# ---------------------------------------------------------------------------
# leaderboard / gate / scorecard
# ---------------------------------------------------------------------------
def _closed(strategy, status, r, pct, regime="Trend-Up"):
    return {"strategy": strategy, "status": status, "rMultiple": r, "pnlPct": pct,
            "pnl": r * bd.RISK_PER_TRADE, "regimeAtEntry": regime,
            "closedDate": "2026-07-10", "holdDays": 2,
            "mfePct": 1.0, "maePct": -1.0, "minsToExit": None}


def test_regime_leaderboard_best_pick():
    trades = [_closed("momentum", "TARGET", 2.0, 6.0),
              _closed("momentum", "TARGET", 2.0, 6.0),
              _closed("momentum", "STOP", -1.0, -3.0)]
    lb = bd._regime_leaderboard(trades)
    row = lb["rows"][0]
    assert row["regime"] == "Trend-Up" and row["best"] == "momentum"
    cell = row["cells"]["momentum"]
    assert cell["closed"] == 3 and cell["winRate"] == 66.7
    assert cell["expectancyR"] == 1.0


def test_regime_fit_and_gate():
    assert "Trend-Up" in bd._regime_fit("momentum")
    by = {"momentum": [_closed("momentum", "TARGET", 2.0, 6.0, regime="Trend-Up"),
                       _closed("momentum", "STOP", -1.0, -3.0, regime="Range")]}
    g = bd._gated(by)
    row = next(r for r in g["perStrategy"] if r["id"] == "momentum")
    assert row["all"]["closed"] == 2 and row["gated"]["closed"] == 1   # Range dropped


def test_scorecard_daily():
    trades = [_closed("m", "TARGET", 2.0, 6.0), _closed("m", "STOP", -1.0, -3.0),
              _closed("m", "EXPIRED", 0.5, 1.5),
              {"strategy": "m", "status": "OPEN", "rMultiple": 0, "pnlPct": 0,
               "pnl": 0, "regimeAtEntry": "X", "closedDate": None}]
    sc = bd._scorecard(trades)
    assert sc["closed"] == 3 and sc["open"] == 1
    assert sc["target"] == 1 and sc["stop"] == 1 and sc["expired"] == 1
    assert sc["winRate"] == round(1 / 3 * 100, 1)
    assert sc["totalR"] == 1.5 and sc["expectancyR"] == 0.5
    assert sc["equity"]["n"] == 3


# ---------------------------------------------------------------------------
# strategy_of_day — live regime + leaderboard both stubbed
# ---------------------------------------------------------------------------
def _wf_report(**verdicts):
    """Minimal walk-forward report: {strategy_id: verdict} → perStrategy rows."""
    return {"ok": True, "trainCut": "2026-06-01", "testN": 40, "days": 120,
            "perStrategy": [{"id": sid, "verdict": v} for sid, v in verdicts.items()]}


def test_strategy_of_day_history_pick():
    fake_lb = {"regimeLeaderboard": {"order": ["momentum", "meanrev"], "rows": [
        {"regime": "Trend-Up", "best": "momentum", "cells": {
            "momentum": {"closed": 8, "winRate": 62.5, "avgPnlPct": 2.1, "expectancyR": 0.9},
            "meanrev": None}}]},
        "regimeDist": {"Trend-Up": 12}, "days": 60, "range": None, "universeWithData": 40}
    with _patch(bd.sim, "current_regime", lambda: {"label": "Trend-Up"}), \
         _patch(bd, "cached_regime_leaderboard", lambda **k: fake_lb), \
         _patch(bd, "cached_walkforward", lambda **k: _wf_report(momentum="robust")):
        out = bd.strategy_of_day()
    assert out["basis"] == "history"
    assert out["pick"]["id"] == "momentum" and out["pick"]["closed"] == 8
    assert out["pick"]["robustness"] == "robust"
    assert out["walkForward"]["ok"] is True and out["skippedOverfit"] is None


def test_strategy_of_day_prefers_robust_over_overfit():
    # momentum has the higher in-sample edge but is OVERFIT; meanrev is robust →
    # the pick should skip momentum and take meanrev, recording skippedOverfit.
    fake_lb = {"regimeLeaderboard": {"order": ["momentum", "meanrev"], "rows": [
        {"regime": "Trend-Up", "best": "momentum", "cells": {
            "momentum": {"closed": 10, "winRate": 60.0, "avgPnlPct": 2.0, "expectancyR": 1.2},
            "meanrev": {"closed": 9, "winRate": 55.0, "avgPnlPct": 1.4, "expectancyR": 0.6}}}]},
        "regimeDist": {"Trend-Up": 12}, "days": 60, "range": None, "universeWithData": 40}
    with _patch(bd.sim, "current_regime", lambda: {"label": "Trend-Up"}), \
         _patch(bd, "cached_regime_leaderboard", lambda **k: fake_lb), \
         _patch(bd, "cached_walkforward",
                lambda **k: _wf_report(momentum="overfit", meanrev="robust")):
        out = bd.strategy_of_day()
    assert out["pick"]["id"] == "meanrev" and out["pick"]["robustness"] == "robust"
    assert out["skippedOverfit"]["id"] == "momentum"


def test_strategy_of_day_vol_conditioned_flip():
    # On Trend-Up days momentum edges meanrev in-sample, but the CURRENT vol bucket
    # (Elevated) strongly favours meanrev → the blended pick flips to meanrev.
    fake_lb = {
        "regimeLeaderboard": {"order": ["momentum", "meanrev"], "rows": [
            {"regime": "Trend-Up", "best": "momentum", "cells": {
                "momentum": {"closed": 10, "winRate": 60.0, "avgPnlPct": 2.0, "expectancyR": 0.30},
                "meanrev": {"closed": 10, "winRate": 55.0, "avgPnlPct": 1.4, "expectancyR": 0.20}}}]},
        "volLeaderboard": {"order": ["momentum", "meanrev"], "rows": [
            {"volState": "Elevated", "best": "meanrev", "cells": {
                "momentum": {"closed": 10, "winRate": 40.0, "avgPnlPct": -1.0, "expectancyR": -0.20},
                "meanrev": {"closed": 10, "winRate": 65.0, "avgPnlPct": 2.5, "expectancyR": 0.60}}}]},
        "regimeDist": {"Trend-Up": 12}, "volDist": {"Elevated": 8},
        "days": 60, "range": None, "universeWithData": 40}
    regime = {"label": "Trend-Up", "volState": "Elevated"}
    with _patch(bd.sim, "current_regime", lambda: regime), \
         _patch(bd, "cached_regime_leaderboard", lambda **k: fake_lb), \
         _patch(bd, "cached_walkforward",
                lambda **k: _wf_report(momentum="robust", meanrev="robust")):
        out = bd.strategy_of_day()
    # blended: momentum 0.6*0.30+0.4*(-0.20)=0.10 ; meanrev 0.6*0.20+0.4*0.60=0.36
    assert out["pick"]["id"] == "meanrev"
    assert out["pick"]["volExpectancyR"] == 0.60
    assert abs(out["pick"]["blendedR"] - 0.36) < 1e-9
    assert [r["id"] for r in out["ranked"]][0] == "meanrev"   # re-ranked by blend
    assert out["sample"]["volDays"] == 8


def test_strategy_of_day_no_vol_overlay_keeps_regime_pick():
    # Same regime edges, but NO vol leaderboard → blended == regime, momentum wins.
    fake_lb = {"regimeLeaderboard": {"order": ["momentum", "meanrev"], "rows": [
        {"regime": "Trend-Up", "best": "momentum", "cells": {
            "momentum": {"closed": 10, "winRate": 60.0, "avgPnlPct": 2.0, "expectancyR": 0.30},
            "meanrev": {"closed": 10, "winRate": 55.0, "avgPnlPct": 1.4, "expectancyR": 0.20}}}]},
        "regimeDist": {"Trend-Up": 12}, "days": 60, "range": None, "universeWithData": 40}
    with _patch(bd.sim, "current_regime", lambda: {"label": "Trend-Up", "volState": "Calm"}), \
         _patch(bd, "cached_regime_leaderboard", lambda **k: fake_lb), \
         _patch(bd, "cached_walkforward", lambda **k: _wf_report(momentum="robust")):
        out = bd.strategy_of_day()
    assert out["pick"]["id"] == "momentum"
    assert out["pick"]["volExpectancyR"] is None
    assert out["pick"]["blendedR"] == 0.30           # falls back to regime R


def test_strategy_of_day_fit_fallback():
    empty = {"regimeLeaderboard": {"order": [], "rows": []}, "regimeDist": {},
             "days": 60, "range": None, "universeWithData": 0}
    with _patch(bd.sim, "current_regime", lambda: {"label": "Trend-Up"}), \
         _patch(bd, "cached_regime_leaderboard", lambda **k: empty):
        out = bd.strategy_of_day()
    assert out["basis"] == "fit" and out["pick"]["fits"] is True


# ---------------------------------------------------------------------------
# walk-forward robustness overlay helpers
# ---------------------------------------------------------------------------
def test_robustness_map():
    assert bd.robustness_map(_wf_report(a="robust", b="overfit")) == {"a": "robust", "b": "overfit"}
    assert bd.robustness_map({"ok": False}) == {}
    assert bd.robustness_map(None) == {}


def test_prefer_robust_skips_overfit():
    ranked = [{"id": "a", "expectancyR": 1.0}, {"id": "b", "expectancyR": 0.5}]
    chosen, skipped = bd._prefer_robust(ranked, {"a": "overfit", "b": "robust"})
    assert chosen["id"] == "b" and skipped["id"] == "a"


def test_prefer_robust_no_overlay_keeps_top():
    ranked = [{"id": "a", "expectancyR": 1.0}, {"id": "b", "expectancyR": 0.5}]
    chosen, skipped = bd._prefer_robust(ranked, {})     # no walk-forward yet
    assert chosen["id"] == "a" and skipped is None


def test_prefer_robust_all_untrusted_keeps_top():
    ranked = [{"id": "a", "expectancyR": 1.0}, {"id": "b", "expectancyR": 0.5}]
    chosen, skipped = bd._prefer_robust(ranked, {"a": "overfit", "b": "no-edge"})
    assert chosen["id"] == "a" and skipped is None      # nothing trusted → keep top


# ---------------------------------------------------------------------------
# run() wiring — vol leaderboard + volAtEntry tagging (hermetic: temp DB + stubs)
# ---------------------------------------------------------------------------
def test_run_includes_vol_axis():
    firing = [_bar("2026-07-01", 100, 101, 99, 1000, prev=100),
              _bar("2026-07-02", 100, 101, 99, 1000, prev=100),
              _bar("2026-07-03", 104.5, 105.5, 100, 1800, prev=100),
              _bar("2026-07-04", 111, 112, 105, 1500, prev=104.5)]
    tmp = tempfile.mkdtemp(prefix="nse_bd_vol_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = tmp
    db.DB_FILE = os.path.join(tmp, "market.db")
    db._initialized = False
    try:
        with _patch(bd, "_universe", lambda n: ["X"]), \
             _patch(bd, "_cached_bars", lambda *a, **k: (firing, True)), \
             _patch(bd, "_near_expiry", lambda: None):
            out = bd.run(days=5, universe_size=5, include_oi=False,
                         resolve="daily", _collect=True)
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved[0], saved[1], False
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)
    assert set(out["volLeaderboard"]) == {"order", "rows"}
    assert "volDist" in out
    # the momentum setup fires, and every collected trade is tagged (value may be None)
    assert out["trades"] and all("volAtEntry" in t for t in out["trades"])
    assert any(t["strategy"] == "momentum" for t in out["trades"])


# ---------------------------------------------------------------------------
# Full-market EOD source: read the whole ingested bhavcopy universe from SQLite
# (no network) and run the same pipeline. Hermetic: temp DB seeded with bars.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    tmp = tempfile.mkdtemp(prefix="nse_bd_eodsrc_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = tmp
    db.DB_FILE = os.path.join(tmp, "market.db")
    db._initialized = False
    db.init()
    try:
        yield
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved[0], saved[1], False
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)


def _recent_iso(n):
    """`n` recent weekday ISO dates (oldest first) so the EOD source's rolling
    `since` window (now − ~months) always covers them regardless of the clock."""
    from datetime import timedelta
    out, cur = [], bd.datetime.now(bd.IST).date()
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur -= timedelta(days=1)
    return list(reversed(out))


def _seed_eod(sym, dates, o, c, h, l, v, value=1e8):
    rows = []
    for i, d in enumerate(dates):
        rows.append({"symbol": sym, "d": d, "date": d, "iso": d,
                     "open": o[i], "high": h[i], "low": l[i], "close": c[i],
                     "prevClose": c[i - 1] if i else c[i], "volume": v[i],
                     "value": value})
    db.eod_bars_put_bulk(rows)


def _firing_eod(sym, value=1e8):
    """A 4-bar momentum setup (ret+4.5%, vol 1.8x, closes near the high) that
    fires momentum LONG on bar 2 and tags the +6% target on bar 3."""
    d = _recent_iso(4)
    _seed_eod(sym, d,
              o=[100, 100, 100, 105], c=[100, 100, 104.5, 111],
              h=[101, 101, 105.5, 112], l=[99, 99, 100, 105],
              v=[1000, 1000, 1800, 1500], value=value)
    return d


def test_load_eod_filters_and_ranks():
    with _temp_db():
        d = _recent_iso(4)
        _firing_eod("LIQBIG", value=5e8)     # ₹50 cr turnover, ~₹100 stock
        _firing_eod("LIQSML", value=2e7)     # ₹2 cr turnover, ~₹100 stock
        # PENNY: liquid turnover but sub-₹20 price → dropped by the price floor.
        _seed_eod("PENNY", d, o=[5] * 4, c=[5] * 4, h=[5.2] * 4, l=[4.8] * 4,
                  v=[9e6] * 4, value=5e8)
        # ILLIQ: fine price but ₹0.1 cr turnover → dropped by the value floor.
        _seed_eod("ILLIQ", d, o=[300] * 4, c=[300] * 4, h=[303] * 4, l=[297] * 4,
                  v=[10] * 4, value=1e6)
        hist, ois, meta = bd._load_eod(universe_size=10, days=30, include_oi=False,
                                       min_price=20.0, min_value_cr=1.0)
        assert set(hist) == {"LIQBIG", "LIQSML"}      # PENNY (price) + ILLIQ (value) dropped
        assert meta["universeAvailable"] == 4         # all seeded names counted
        # cap keeps the most-liquid names first
        hist2, _o, _m = bd._load_eod(universe_size=1, days=30, include_oi=False,
                                     min_price=20.0, min_value_cr=1.0)
        assert set(hist2) == {"LIQBIG"}               # highest turnover wins the single slot


def test_load_eod_builds_oi_series():
    with _temp_db():
        d = _firing_eod("FUT1")
        # two expiries (a rollover) → one continuous OI% series
        db.eod_oi_put("FUT1", "2026-07-31",
                      [{"d": d[0], "oi": 110, "changeOi": 10},
                       {"d": d[1], "oi": 120, "changeOi": 10}])
        db.eod_oi_put("FUT1", "2026-08-28",
                      [{"d": d[2], "oi": 130, "changeOi": 10},
                       {"d": d[3], "oi": 140, "changeOi": 10}])
        hist, ois, meta = bd._load_eod(universe_size=10, days=60, include_oi=True,
                                       min_price=20.0, min_value_cr=1.0)
        assert "FUT1" in ois
        assert ois["FUT1"][d[0]] == 10.0              # 110 vs prev 100 → +10%
        assert len(ois["FUT1"]) == 4


def test_run_source_eod_end_to_end():
    with _temp_db():
        _firing_eod("X")
        out = bd.run(days=60, source="eod", include_oi=False, _collect=True)
    assert out.get("source") == "eod"
    assert out["universeWithData"] == 1
    assert out["resolve"] == "daily"
    assert {"regimeLeaderboard", "volLeaderboard", "strategies"} <= set(out)
    assert any(t["strategy"] == "momentum" for t in out["trades"])


def test_run_source_eod_empty_store_message():
    with _temp_db():
        out = bd.run(days=30, source="eod")
    assert out.get("source") == "eod"
    assert "message" in out and "store" in out           # helpful "load bhavcopy first"


def test_run_eod_forces_daily_resolve():
    # intrabar re-resolution needs per-symbol NSE fetches → forced off for EOD.
    with _temp_db():
        _firing_eod("X")
        out = bd.run(days=60, source="eod", include_oi=False, resolve="intrabar")
    assert out["resolve"] == "daily"


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} backtest_daily tests passed")


if __name__ == "__main__":
    _main()
