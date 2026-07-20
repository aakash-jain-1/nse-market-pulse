"""
Unit tests for strategies.py — the strategy library + regime engine.

Covers the pure math (rating bands, _mk_idea risk plan for LONG & SHORT,
conviction clamp), regime detection across every label branch, all ten idea
generators driven by synthetic contexts (momentum, OI smart-money, mean-reversion,
volume breakout, 52w-high, VWAP, delivery, opening-range breakout, intraday-VWAP,
regime-adaptive), the regime-playbook pick (history + a-priori fit), and the
regime-conditioned position-sizing multipliers.

No network: nse.get_lot_size / nse._build_idea and backtest_daily's leaderboard
are stubbed; candle strategies get hand-built minute bars.

Run: python test_strategies.py   (also works under pytest)
"""

import contextlib
from datetime import datetime, timezone

import strategies as S


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ts(h, m, day=15):
    """Epoch-ms whose UTC wall-clock is day/Jul/2026 h:m — matches candle_dt()."""
    return int(datetime(2026, 7, day, h, m, tzinfo=timezone.utc).timestamp() * 1000)


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _fno(names=("A", "B", "ACME", "X", "NIFTY")):
    """Stub _is_fno's lot-size lookup so given names read as F&O."""
    names = set(names)
    with _patch(S.nse, "get_lot_size", lambda s: 50 if s in names else None):
        yield


# ---------------------------------------------------------------------------
# _rate + _mk_idea
# ---------------------------------------------------------------------------
def test_rate_bands():
    assert S._rate(66) == "High"
    assert S._rate(65) == "Medium"
    assert S._rate(40) == "Medium"
    assert S._rate(39) == "Low"


def test_mk_idea_long_risk_plan():
    idea = S._mk_idea("X", "LONG", 100.0, 50, ["r"], stop_pct=2, tgt_pct=4)
    assert idea["stop"] == 98.0 and idea["target"] == 104.0
    assert idea["entry"] == 100.0 and idea["rr"] == 2.0
    assert idea["rating"] == "Medium" and idea["conviction"] == 50


def test_mk_idea_short_risk_plan():
    idea = S._mk_idea("X", "SHORT", 100.0, 70, ["r"], stop_pct=2, tgt_pct=4)
    assert idea["stop"] == 102.0 and idea["target"] == 96.0
    assert idea["rating"] == "High"


def test_mk_idea_conviction_clamped():
    assert S._mk_idea("X", "LONG", 10, 500, [], 1, 2)["conviction"] == 99
    assert S._mk_idea("X", "LONG", 10, -5, [], 1, 2)["conviction"] == 1


def test_mk_idea_rejects_bad_ltp():
    assert S._mk_idea("X", "LONG", None, 50, [], 1, 2) is None
    assert S._mk_idea("X", "LONG", 0, 50, [], 1, 2) is None


# ---------------------------------------------------------------------------
# detect_regime
# ---------------------------------------------------------------------------
def _idx(nifty_pct, adv=0, dec=0):
    return {"index": {"NIFTY": {"pChange": nifty_pct, "advances": adv, "declines": dec},
                      "BANKNIFTY": {"pChange": nifty_pct}}}


def test_regime_trend_up():
    r = S.detect_regime(_idx(1.0, adv=100, dec=50))
    assert r["label"] == "Trend-Up" and r["niftyPct"] == 1.0


def test_regime_trend_down():
    assert S.detect_regime(_idx(-1.0, adv=50, dec=100))["label"] == "Trend-Down"


def test_regime_range():
    assert S.detect_regime(_idx(0.2, adv=60, dec=60))["label"] == "Range"


def test_regime_recovery():
    assert S.detect_regime(_idx(1.0, adv=90, dec=40), prior_day_move=-2.0)["label"] == "Recovery"


def test_regime_pullback():
    assert S.detect_regime(_idx(-1.0, adv=40, dec=90), prior_day_move=2.0)["label"] == "Pullback"


def test_regime_mixed():
    # 0.5% up but breadth negative → neither Trend-Up nor Range
    assert S.detect_regime(_idx(0.5, adv=40, dec=90))["label"] == "Mixed"


def test_regime_unknown_without_index():
    r = S.detect_regime({})
    assert r["label"] == "Unknown" and r["niftyPct"] is None


def test_regime_note_contains_breadth():
    r = S.detect_regime(_idx(1.0, adv=100, dec=50))
    assert "breadth 100:50" in r["note"] and "NIFTY +1.00%" in r["note"]


# ---------------------------------------------------------------------------
# volatility axis (India VIX) — orthogonal to the directional label
# ---------------------------------------------------------------------------
def _idx_vix(nifty_pct, vix, lo=8.0, hi=30.0, adv=100, dec=50):
    ix = _idx(nifty_pct, adv=adv, dec=dec)
    ix["index"]["INDIAVIX"] = {"last": vix, "yearLow": lo, "yearHigh": hi}
    return ix


def test_vol_state_bands():
    assert S._vol_state(11.0) == "Calm"
    assert S._vol_state(13.0) == "Normal"      # boundary is inclusive of Normal
    assert S._vol_state(15.0) == "Normal"
    assert S._vol_state(18.0) == "Elevated"    # >=18 elevated
    assert S._vol_state(24.0) == "Elevated"
    assert S._vol_state(None) is None


def test_vix_pctile_math_and_guards():
    assert S._vix_pctile(20.0, 10.0, 30.0) == 50.0
    assert S._vix_pctile(10.0, 10.0, 30.0) == 0.0
    assert S._vix_pctile(40.0, 10.0, 30.0) == 100.0   # clamped
    assert S._vix_pctile(20.0, None, 30.0) is None
    assert S._vix_pctile(20.0, 30.0, 30.0) is None     # hi<=lo guard
    assert S._vix_pctile(None, 10.0, 30.0) is None


def test_detect_regime_carries_vol_axis():
    r = S.detect_regime(_idx_vix(1.0, 12.0))
    assert r["label"] == "Trend-Up"            # direction unchanged by vol
    assert r["vix"] == 12.0 and r["volState"] == "Calm"
    assert r["vixPctile"] == round((12.0 - 8.0) / (30.0 - 8.0) * 100, 1)
    assert "VIX 12.00" in r["note"] and "Calm" in r["note"]


def test_detect_regime_vol_absent_is_none():
    r = S.detect_regime(_idx(1.0, adv=100, dec=50))
    assert r["vix"] is None and r["volState"] is None and r["vixPctile"] is None
    assert "VIX" not in r["note"]              # no VIX bits when unavailable


# ---------------------------------------------------------------------------
# gen_momentum (delegates to nse._build_idea)
# ---------------------------------------------------------------------------
def test_gen_momentum_passthrough_and_filter():
    ctx = {"scanner": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]}
    # B yields no idea (None) → filtered out.
    fake = lambda e: None if e["symbol"] == "B" else {"symbol": e["symbol"], "direction": "LONG"}
    with _patch(S.nse, "_build_idea", fake):
        out = S.gen_momentum(ctx)
    assert [i["symbol"] for i in out] == ["A", "C"]


# ---------------------------------------------------------------------------
# gen_oi_smartmoney
# ---------------------------------------------------------------------------
def test_gen_oi_buildup_is_long():
    ctx = {"oispurts": [{"symbol": "A", "signalKind": "buildup", "ltp": 100,
                         "oiPctChange": 20, "pChange": 2, "signal": "Long buildup"}]}
    out = S.gen_oi_smartmoney(ctx)
    assert len(out) == 1
    i = out[0]
    assert i["direction"] == "LONG" and i["fno"] is True
    assert i["oiSignal"] == "Long buildup"
    # conv = min(20*1.5,80)=30 + confirm min(2*3,19)=6 = 36
    assert i["conviction"] == 36


def test_gen_oi_short_is_short_and_dedupes_neutral_skipped():
    ctx = {"oispurts": [
        {"symbol": "B", "signalKind": "short", "ltp": 50, "oiPctChange": 10, "pChange": -1},
        {"symbol": "B", "signalKind": "short", "ltp": 50, "oiPctChange": 10},   # dup symbol
        {"symbol": "C", "signalKind": "neutral", "ltp": 50, "oiPctChange": 10}, # neutral skip
        {"symbol": "D", "signalKind": "buildup", "ltp": None},                  # no ltp skip
    ]}
    out = S.gen_oi_smartmoney(ctx)
    assert [i["symbol"] for i in out] == ["B"]
    assert out[0]["direction"] == "SHORT"


# ---------------------------------------------------------------------------
# gen_meanrev
# ---------------------------------------------------------------------------
def test_gen_meanrev_oversold_long_and_extended_short():
    ctx = {
        "losers": [{"symbol": "A", "pChange": -3.0, "ltp": 100}],
        "gainers": [{"symbol": "B", "pChange": 5.0, "ltp": 200}],
        "scannerSyms": {"A", "B"},
        "regime": {"label": "Range"},
        "oispurts": [],
    }
    out = S.gen_meanrev(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_meanrev_liquidity_filter():
    ctx = {
        "losers": [{"symbol": "ILLIQ", "pChange": -5.0, "ltp": 100}],
        "gainers": [],
        "scannerSyms": {"OTHER"},          # ILLIQ not liquid → dropped
        "regime": {"label": "Range"},
        "oispurts": [],
    }
    assert S.gen_meanrev(ctx) == []


def test_gen_meanrev_threshold_guards():
    # loser only -1% (needs <= -2), gainer only +2% (needs >= 4) → nothing
    ctx = {
        "losers": [{"symbol": "A", "pChange": -1.0, "ltp": 100}],
        "gainers": [{"symbol": "B", "pChange": 2.0, "ltp": 100}],
        "scannerSyms": set(),
        "regime": {"label": "Mixed"},
        "oispurts": [],
    }
    assert S.gen_meanrev(ctx) == []


# ---------------------------------------------------------------------------
# gen_vol_breakout
# ---------------------------------------------------------------------------
def test_gen_vol_breakout_direction_and_guards():
    ctx = {"volgainers": [
        {"symbol": "A", "week1volChange": 10, "pChange": 2.0, "ltp": 100},   # LONG
        {"symbol": "B", "week1volChange": 8, "pChange": -1.0, "ltp": 100},   # SHORT
        {"symbol": "C", "week1volChange": 3, "pChange": 5.0, "ltp": 100},    # vm<5 skip
        {"symbol": "D", "week1volChange": 10, "pChange": 0.2, "ltp": 100},   # |pc|<0.5 skip
    ]}
    out = S.gen_vol_breakout(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}
    a = next(i for i in out if i["symbol"] == "A")
    assert a["conviction"] == min(10 * 2, 60) + min(2.0 * 3, 39)   # 20 + 6 = 26


# ---------------------------------------------------------------------------
# gen_high52w / gen_vwap / gen_delivery (quote-driven)
# ---------------------------------------------------------------------------
def test_gen_high52w_long_near_high_short_near_low():
    ctx = {"quotes": {
        "A": {"ltp": 95, "yearHigh": 100, "yearLow": 40, "pChange": 1.0},   # near high LONG
        "B": {"ltp": 102, "yearHigh": 400, "yearLow": 100, "pChange": -0.2}, # near low SHORT
    }}
    with _fno():
        out = S.gen_high52w(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_vwap_above_below():
    ctx = {"quotes": {
        "A": {"ltp": 101, "vwap": 100, "pChange": 1.0},    # above → LONG
        "B": {"ltp": 99, "vwap": 100, "pChange": -1.0},    # below → SHORT
        "C": {"ltp": 100.1, "vwap": 100, "pChange": 1.0},  # within 0.2% → skip
    }}
    with _fno():
        out = S.gen_vwap(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_delivery_accumulation_distribution():
    ctx = {"quotes": {
        "A": {"ltp": 100, "deliveryPct": 75, "pChange": 2.0},   # accumulation LONG
        "B": {"ltp": 100, "deliveryPct": 70, "pChange": -2.0},  # distribution SHORT
        "C": {"ltp": 100, "deliveryPct": 40, "pChange": 2.0},   # dp<55 skip
    }}
    with _fno():
        out = S.gen_delivery(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


# ---------------------------------------------------------------------------
# gen_orb / gen_ivwap (candle-driven)
# ---------------------------------------------------------------------------
def test_gen_orb_breakout_up():
    pts = [
        {"t": _ts(9, 15), "o": 101, "h": 105, "l": 100, "c": 102, "v": 1000},
        {"t": _ts(9, 20), "o": 102, "h": 106, "l": 101, "c": 104, "v": 1000},
        {"t": _ts(9, 35), "o": 106, "h": 110, "l": 106, "c": 109, "v": 3000},
        {"t": _ts(9, 40), "o": 109, "h": 111, "l": 108, "c": 110, "v": 3000},
    ]
    with _fno():
        out = S.gen_orb({"candles": {"A": pts}})
    assert len(out) == 1 and out[0]["direction"] == "LONG"
    assert any("opening range" in r for r in out[0]["reasons"])


def test_gen_orb_breakdown_down():
    pts = [
        {"t": _ts(9, 15), "o": 101, "h": 105, "l": 100, "c": 102, "v": 1000},
        {"t": _ts(9, 20), "o": 102, "h": 106, "l": 101, "c": 104, "v": 1000},
        {"t": _ts(9, 35), "o": 100, "h": 100, "l": 94, "c": 96, "v": 3000},
        {"t": _ts(9, 40), "o": 96, "h": 97, "l": 92, "c": 95, "v": 3000},
    ]
    with _fno():
        out = S.gen_orb({"candles": {"A": pts}})
    assert len(out) == 1 and out[0]["direction"] == "SHORT"


def test_gen_orb_needs_enough_bars():
    pts = [{"t": _ts(9, 15), "h": 105, "l": 100, "c": 102, "v": 1000}]
    assert S.gen_orb({"candles": {"A": pts}}) == []


def test_gen_ivwap_long_and_short():
    up = [{"t": _ts(9, 15 + i), "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000}
          for i, c in enumerate([98, 99, 100, 101, 102, 103])]
    down = [{"t": _ts(9, 15 + i), "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000}
            for i, c in enumerate([103, 102, 101, 100, 99, 98])]
    with _fno():
        long_out = S.gen_ivwap({"candles": {"A": up}})
        short_out = S.gen_ivwap({"candles": {"A": down}})
    assert long_out and long_out[0]["direction"] == "LONG"
    assert short_out and short_out[0]["direction"] == "SHORT"


def test_gen_ivwap_needs_six_bars():
    pts = [{"t": _ts(9, 15 + i), "h": 100, "l": 99, "c": 100, "v": 1000} for i in range(5)]
    assert S.gen_ivwap({"candles": {"A": pts}}) == []


# ---------------------------------------------------------------------------
# gen_fut_basis (futures basis / carry)
# ---------------------------------------------------------------------------
def test_gen_fut_basis_premium_long_discount_short():
    ctx = {"futures": [
        {"symbol": "A", "spot": 100, "basisPct": 1.0, "changeInOI": 5000, "pChange": 1.0},  # premium+OI↑ LONG
        {"symbol": "B", "spot": 200, "basisPct": -0.8, "changeInOI": 4000, "pChange": -1.0}, # discount+OI↑ SHORT
    ]}
    out = S.gen_fut_basis(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}
    assert all(i["fno"] for i in out)
    assert next(i for i in out if i["symbol"] == "A")["basisPct"] == 1.0


def test_gen_fut_basis_guards():
    # premium but OI falling → skip; tiny basis → skip; missing spot → skip
    ctx = {"futures": [
        {"symbol": "A", "spot": 100, "basisPct": 1.0, "changeInOI": -10},   # OI down
        {"symbol": "B", "spot": 100, "basisPct": 0.1, "changeInOI": 999},   # basis too small
        {"symbol": "C", "spot": None, "basisPct": 1.0, "changeInOI": 999},  # no spot
    ]}
    assert S.gen_fut_basis(ctx) == []


# ---------------------------------------------------------------------------
# gen_rel_strength (relative strength vs NIFTY)
# ---------------------------------------------------------------------------
def test_gen_rel_strength_leader_long_laggard_short():
    ctx = {
        "index": {"NIFTY": {"pChange": 0.5}},
        "scanner": [
            {"symbol": "A", "pChange": 2.5, "ltp": 100},   # rs +2.0 leader LONG
            {"symbol": "B", "pChange": -1.5, "ltp": 100},  # rs -2.0 laggard SHORT
            {"symbol": "C", "pChange": 1.0, "ltp": 100},   # rs +0.5 too small skip
        ],
        "scannerSyms": {"A", "B", "C"},
    }
    with _fno():
        out = S.gen_rel_strength(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_rel_strength_needs_market():
    ctx = {"index": {}, "scanner": [{"symbol": "A", "pChange": 5, "ltp": 100}],
           "scannerSyms": {"A"}}
    assert S.gen_rel_strength(ctx) == []


# ---------------------------------------------------------------------------
# gen_squeeze (NR7 volatility squeeze)
# ---------------------------------------------------------------------------
def _bar(h, l):
    return {"high": h, "low": l, "close": (h + l) / 2}


def test_gen_squeeze_breakout_up_and_down():
    # 6 wide bars then a tight NR7 last bar (range 1) → breakout on today's ltp
    wide = [_bar(110, 100) for _ in range(6)]
    nr7 = _bar(105.5, 104.5)                       # range 1 = tightest
    ctx = {
        "daily": {"A": wide + [nr7], "B": wide + [nr7]},
        "quotes": {"A": {"ltp": 106}, "B": {"ltp": 104}},   # A breaks up, B breaks down
    }
    with _fno():
        out = S.gen_squeeze(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_squeeze_not_nr7_skips():
    # last bar is NOT the tightest → no squeeze setup
    bars = [_bar(101, 100) for _ in range(6)] + [_bar(110, 100)]
    ctx = {"daily": {"A": bars}, "quotes": {"A": {"ltp": 120}}}
    with _fno():
        assert S.gen_squeeze(ctx) == []


def test_gen_squeeze_needs_seven_bars():
    ctx = {"daily": {"A": [_bar(101, 100) for _ in range(6)]},
           "quotes": {"A": {"ltp": 200}}}
    assert S.gen_squeeze(ctx) == []


# ---------------------------------------------------------------------------
# gen_gap (gap-and-go / fade, regime-tilted)
# ---------------------------------------------------------------------------
def test_gen_gap_go_on_trend():
    ctx = {
        "regime": {"label": "Trend-Up"},
        "quotes": {
            "A": {"open": 103, "prevClose": 100, "ltp": 104},   # +3% gap, holding → LONG
            "B": {"open": 97, "prevClose": 100, "ltp": 96},     # -3% gap, holding → SHORT
        },
    }
    with _fno():
        out = S.gen_gap(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_gap_fade_on_range():
    ctx = {
        "regime": {"label": "Range"},
        "quotes": {
            "A": {"open": 103, "prevClose": 100, "ltp": 101.5},  # gap up rejecting → SHORT
            "B": {"open": 97, "prevClose": 100, "ltp": 98.5},    # gap down recovering → LONG
        },
    }
    with _fno():
        out = S.gen_gap(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "SHORT", "B": "LONG"}


def test_gen_gap_small_gap_skips():
    ctx = {"regime": {"label": "Trend-Up"},
           "quotes": {"A": {"open": 100.5, "prevClose": 100, "ltp": 101}}}
    with _fno():
        assert S.gen_gap(ctx) == []


# ---------------------------------------------------------------------------
# gen_pcr_extreme (PCR contrarian)
# ---------------------------------------------------------------------------
def test_gen_pcr_extreme_contrarian():
    ctx = {"chains": {
        "A": {"pcr": 1.5, "underlying": 100},    # put-heavy → contrarian LONG
        "B": {"pcr": 0.5, "underlying": 200},    # call-crowded → contrarian SHORT
        "C": {"pcr": 1.0, "underlying": 100},    # neutral → skip
    }}
    out = S.gen_pcr_extreme(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}
    assert all(i["fno"] for i in out)


# ---------------------------------------------------------------------------
# gen_max_pain (expiry pin)
# ---------------------------------------------------------------------------
def test_gen_max_pain_pins_toward_strike():
    ctx = {"chains": {
        "A": {"maxPain": 100, "underlying": 103, "dte": 2},   # above pin → SHORT
        "B": {"maxPain": 100, "underlying": 97, "dte": 2},    # below pin → LONG
    }}
    out = S.gen_max_pain(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "SHORT", "B": "LONG"}


def test_gen_max_pain_only_near_expiry():
    ctx = {"chains": {"A": {"maxPain": 100, "underlying": 105, "dte": 20}}}  # far from expiry
    assert S.gen_max_pain(ctx) == []


# ---------------------------------------------------------------------------
# gen_pdhl (prior-day high/low breakout)
# ---------------------------------------------------------------------------
def test_gen_pdhl_breaks_prior_levels():
    ctx = {
        "daily": {"A": [_bar(105, 95)], "B": [_bar(105, 95)]},
        "quotes": {"A": {"ltp": 106}, "B": {"ltp": 94}},   # A>PDH LONG, B<PDL SHORT
    }
    with _fno():
        out = S.gen_pdhl(ctx)
    dirs = {i["symbol"]: i["direction"] for i in out}
    assert dirs == {"A": "LONG", "B": "SHORT"}


def test_gen_pdhl_inside_range_skips():
    ctx = {"daily": {"A": [_bar(105, 95)]}, "quotes": {"A": {"ltp": 100}}}
    with _fno():
        assert S.gen_pdhl(ctx) == []


# ---------------------------------------------------------------------------
# new-generator guard/skip branches (missing fields, no breakout, etc.)
# ---------------------------------------------------------------------------
def test_gen_rel_strength_guards():
    ctx = {
        "index": {"NIFTY": {"pChange": 0.0}},
        "scanner": [
            {"symbol": None, "pChange": 5, "ltp": 100},     # no symbol
            {"symbol": "Z", "pChange": None, "ltp": 100},   # no pChange
            {"symbol": "ILLIQ", "pChange": 5, "ltp": 100},  # not in the liquid set
        ],
        "scannerSyms": {"OTHER"},
    }
    with _fno():
        assert S.gen_rel_strength(ctx) == []


def test_gen_squeeze_guards():
    wide = [_bar(110, 100) for _ in range(6)]
    nr7 = _bar(105.5, 104.5)
    ctx = {
        "daily": {"A": wide + [nr7],        # no quote → no ltp
                  "B": wide + [nr7]},        # ltp inside the NR7 range → no break
        "quotes": {"B": {"ltp": 105.0}},
    }
    with _fno():
        assert S.gen_squeeze(ctx) == []


def test_gen_gap_missing_fields_skips():
    ctx = {"regime": {"label": "Trend-Up"},
           "quotes": {"A": {"open": None, "prevClose": 100, "ltp": 105},
                      "B": {"open": 103, "prevClose": None, "ltp": 105}}}
    with _fno():
        assert S.gen_gap(ctx) == []


def test_gen_pcr_extreme_guards():
    ctx = {"chains": {"A": {"pcr": None, "underlying": 100},
                      "B": {"pcr": 1.5, "underlying": None}}}
    assert S.gen_pcr_extreme(ctx) == []


def test_gen_max_pain_near_pin_skips():
    ctx = {"chains": {"A": {"maxPain": 100, "underlying": 100.5, "dte": 1}}}  # 0.5% < 1.5%
    assert S.gen_max_pain(ctx) == []


def test_gen_pdhl_guards():
    ctx = {"daily": {"A": [], "B": [_bar(105, 95)]}, "quotes": {}}   # empty bars / no ltp
    with _fno():
        assert S.gen_pdhl(ctx) == []


def test_gen_fut_basis_go_and_deduped():
    # premium+OI↑ once, then a duplicate symbol row must be ignored
    ctx = {"futures": [
        {"symbol": "A", "spot": 100, "basisPct": 1.0, "changeInOI": 500, "pChange": 1.0},
        {"symbol": "A", "spot": 100, "basisPct": 1.0, "changeInOI": 500, "pChange": 1.0},
    ]}
    out = S.gen_fut_basis(ctx)
    assert [i["symbol"] for i in out] == ["A"]


def test_dte_helper():
    from datetime import timedelta
    future = (datetime.now(S._IST) + timedelta(days=10)).strftime("%d-%b-%Y")
    assert S._dte(future) in (9, 10)               # boundary by time-of-day
    past = (datetime.now(S._IST) - timedelta(days=5)).strftime("%d-%b-%Y")
    assert S._dte(past) == 0                        # clamped to >= 0
    assert S._dte("garbage") is None


# ---------------------------------------------------------------------------
# playbook pick + sizing
# ---------------------------------------------------------------------------
def test_playbook_pick_fit_when_no_history():
    import backtest_daily as btd
    with _patch(btd, "peek_regime_leaderboard", lambda: None):
        sid, basis, cell = S._regime_playbook_pick("Recovery")
    # first non-adaptive strategy that lists Recovery in its regimeFit = meanrev
    assert sid == "meanrev" and basis == "fit" and cell is None


def test_playbook_pick_history_when_warm():
    import backtest_daily as btd
    data = {"regimeLeaderboard": {"rows": [
        {"regime": "Trend-Up", "best": "momentum",
         "cells": {"momentum": {"expectancyR": 0.4, "closed": 20}}}]}}
    with _patch(btd, "peek_regime_leaderboard", lambda: data):
        sid, basis, cell = S._regime_playbook_pick("Trend-Up")
    assert sid == "momentum" and basis == "history" and cell["expectancyR"] == 0.4


def test_playbook_pick_none_for_unknown_regime():
    import backtest_daily as btd
    with _patch(btd, "peek_regime_leaderboard", lambda: None):
        assert S._regime_playbook_pick(None) == (None, None, None)


def test_playbook_pick_vol_conditioned():
    import backtest_daily as btd
    data = {"regimeLeaderboard": {"rows": [
        {"regime": "Trend-Up", "best": "momentum", "cells": {
            "momentum": {"expectancyR": 0.30, "closed": 20},
            "meanrev": {"expectancyR": 0.20, "closed": 20}}}]},
        "volLeaderboard": {"rows": [
            {"volState": "Elevated", "cells": {
                "momentum": {"expectancyR": -0.20, "closed": 20},
                "meanrev": {"expectancyR": 0.60, "closed": 20}}}]}}
    with _patch(btd, "peek_regime_leaderboard", lambda: data), \
         _patch(btd, "peek_walkforward", lambda: None):
        # Elevated vol flips the pick: blended meanrev 0.36 > momentum 0.10
        assert S._regime_playbook_pick("Trend-Up", "Elevated")[0] == "meanrev"
        # without a vol_state it's regime-only → the higher regime edge wins
        assert S._regime_playbook_pick("Trend-Up")[0] == "momentum"


def test_conviction_mult_edge_bands():
    assert S._conviction_mult("history", {"expectancyR": 0.4, "closed": 20}) == 1.5
    assert S._conviction_mult("history", {"expectancyR": 0.2, "closed": 6}) == 1.25
    assert S._conviction_mult("history", {"expectancyR": 0.08, "closed": 3}) == 1.0
    assert S._conviction_mult("history", {"expectancyR": 0.02, "closed": 3}) == 0.75
    assert S._conviction_mult("history", {"expectancyR": -0.1, "closed": 3}) == 0.5
    assert S._conviction_mult("history", {"expectancyR": None, "closed": 3}) == 0.75
    assert S._conviction_mult("fit", None) == 0.75


def test_regime_strength_branches():
    assert S.regime_strength(None) == 0.5
    assert S.regime_strength({"label": "Trend-Up", "niftyPct": None}) == 0.5
    assert S.regime_strength({"label": "Trend-Up", "niftyPct": 1.5,
                              "breadthAdv": 100, "breadthDec": 0}) == 1.0
    assert S.regime_strength({"label": "Range", "niftyPct": 0.0,
                              "breadthAdv": 50, "breadthDec": 50}) == 1.0
    assert S.regime_strength({"label": "Recovery", "niftyPct": 1.2,
                              "priorDayMove": -2.5}) == 1.0
    assert S.regime_strength({"label": "Mixed", "niftyPct": 0.5}) == 0.3


def test_conviction_mult_regime_tilt():
    strong = {"label": "Trend-Up", "niftyPct": 1.5, "breadthAdv": 100, "breadthDec": 0}
    cell = {"expectancyR": 0.4, "closed": 20}
    # edge 1.5 × factor 1.2 = 1.8 → clamped to 1.5
    assert S.conviction_mult("history", cell, strong) == 1.5
    # no regime → plain edge band
    assert S.conviction_mult("history", cell, None) == 1.5


def test_clamp():
    assert S._clamp(5, 0, 1) == 1
    assert S._clamp(-5, 0, 1) == 0
    assert S._clamp(0.5, 0, 1) == 0.5


# ---------------------------------------------------------------------------
# gen_adaptive
# ---------------------------------------------------------------------------
def test_gen_adaptive_delegates_and_annotates():
    ctx = {
        "regime": {"label": "Mixed", "niftyPct": 0.1, "breadthAdv": 10, "breadthDec": 10},
        "volgainers": [{"symbol": "A", "week1volChange": 10, "pChange": 2.0, "ltp": 100}],
    }
    with _patch(S, "_regime_playbook_pick", lambda label, vol=None: ("vol_breakout", "fit", None)):
        out = S.gen_adaptive(ctx)
    assert out and out[0]["via"] == "vol_breakout"
    assert "sizeMult" in out[0]
    assert out[0]["reasons"][0].startswith("Regime playbook")


def test_gen_adaptive_empty_when_no_pick():
    with _patch(S, "_regime_playbook_pick", lambda label, vol=None: (None, None, None)):
        assert S.gen_adaptive({"regime": {"label": "Nope"}}) == []


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_strategy_meta_shape():
    meta = S.strategy_meta()
    assert len(meta) == len(S.STRATEGIES) == 17
    for m in meta:
        assert set(m) == {"id", "name", "description", "regimeFit"}
        assert "generate" not in m


def test_generate_dispatch():
    with _patch(S.nse, "_build_idea", lambda e: {"symbol": e["symbol"], "direction": "LONG"}):
        assert S.generate("momentum", {"scanner": [{"symbol": "A"}]})[0]["symbol"] == "A"
    assert S.generate("does-not-exist", {}) == []


# ---------------------------------------------------------------------------
# build_context per-symbol fan-out budget (NSE source-trim)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _build_context_stubs():
    """Stub every NSE dependency of build_context offline; the scanner returns far
    more names than the cap so we can prove the per-symbol quote/candle fan-out is
    bounded. Yields the list of symbols get_quote was actually called for."""
    import nse_quote
    big = [{"symbol": f"S{i}", "ltp": 100, "pChange": 1} for i in range(200)]
    calls = []

    def _rec(s):
        calls.append(s)
        return {"symbol": s, "ltp": 100}

    stubs = [
        (S.nse, "get_scanner", lambda **k: list(big)),
        (S.nse, "get_variations", lambda kind, n=40: list(big)),
        (S.nse, "get_volume_gainers", lambda n=40: []),
        (S.nse, "get_oi_spurts", lambda n=60: []),
        (S.nse, "get_futures", lambda n=60: []),
        (S.nse, "get_most_active", lambda kind, n=30: []),
        (S.nse, "get_index_snapshot", lambda: {}),
        (S, "_load_daily", lambda cand: {}),
        (S, "_load_chains", lambda ctx: {}),
        (nse_quote, "get_option_chain", lambda s: {"pcr": 1.0}),
        (nse_quote, "get_quote", _rec),
        (nse_quote, "get_ohlc", lambda s, **k: {"points": [], "error": None}),
    ]
    with contextlib.ExitStack() as es:
        for obj, name, val in stubs:
            es.enter_context(_patch(obj, name, val))
        yield calls


def test_build_context_caps_candidate_fanout():
    with _build_context_stubs() as calls:
        ctx = S.build_context()
    # Scanner alone offers 200 names, but the per-symbol fan-out is bounded.
    assert len(set(calls)) == S._CTX_CAND
    assert len(ctx["quotes"]) == S._CTX_CAND


def test_build_context_candidate_cap_is_tunable():
    with _patch(S, "_CTX_CAND", 12), _build_context_stubs() as calls:
        ctx = S.build_context()
    assert len(set(calls)) == 12
    assert len(ctx["quotes"]) == 12


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} strategy tests passed")


if __name__ == "__main__":
    _main()
