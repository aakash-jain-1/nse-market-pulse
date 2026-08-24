"""
Unit tests for corporate_actions — split/bonus detection + back-adjustment of daily bars.
Run: python -m pytest tests/test_corporate_actions.py   (or the whole suite)

The false-positive tests matter as much as the detection ones: adjusting a genuine crash
silently rewrites real price history, so each guard (adjacency, move size, clean-ratio
gate, two-sided persistence) has a test that would fail if it were removed.
"""

from datetime import date, timedelta

from nse_pulse.core import corporate_actions as ca


def _series(closes, start="2026-07-01", volume=10_000, step_weekdays=True):
    """Ascending daily bars from a list of closes, on consecutive weekdays.

    high/low straddle the close and `value` is kept consistent with price x volume so the
    turnover-invariance assertions are meaningful.
    """
    bars = []
    d = date.fromisoformat(start)
    for c in closes:
        if step_weekdays:
            while d.weekday() >= 5:
                d += timedelta(days=1)
        prev = bars[-1]["close"] if bars else c
        bars.append({
            "symbol": "X", "d": d.isoformat(), "date": d.isoformat(),
            "open": round(c * 0.99, 4), "high": round(c * 1.02, 4),
            "low": round(c * 0.98, 4), "close": c, "prevClose": prev,
            "vwap": round(c * 1.001, 4), "volume": volume, "value": c * volume,
            "trades": 500, "delivQty": volume // 2, "delivPct": 50.0,
        })
        d += timedelta(days=1)
    return bars


def _split_series(pre, post, factor, pre_vol=10_000):
    """A clean ex-date: `pre` closes on the old scale, `post` closes already on the NEW
    scale, with share volume scaling inversely so rupee turnover stays continuous."""
    bars = _series(pre + post)
    for b in bars[len(pre):]:
        b["volume"] = int(pre_vol * factor)
        b["value"] = b["close"] * b["volume"]
    # NSE reports the ex-date's prevClose on the STALE scale (verified live on
    # ANGELONE) — that unadjusted value is the direct cause of the phantom ret1.
    bars[len(pre)]["prevClose"] = pre[-1]
    return bars


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def test_detects_a_clean_split_and_snaps_to_the_round_factor():
    # A 5:1 split where the stock ALSO fell ~2% that day: the observed ratio is ~5.1,
    # but the real corporate action is 5.0 and the -2% is a genuine move that must
    # survive adjustment.
    bars = _series([500.0, 505.0, 500.0, 98.0, 99.0, 98.5, 99.5])
    bars[3]["prevClose"] = 500.0
    evs = ca.detect(bars)
    assert len(evs) == 1
    e = evs[0]
    assert e["i"] == 3 and e["kind"] == "split" and e["clean"] is True
    assert e["factor"] == 5.0                       # snapped, not the raw 5.10
    assert abs(e["ratio"] - 500.0 / 98.0) < 1e-6


def test_detects_a_bonus_at_the_lower_end_of_the_range():
    # A 1:2 bonus is only a -33% move — inside the candidate band, so it is trusted
    # ONLY because 1.5 is a recognisable factor.
    bars = _split_series([300.0, 302.0, 300.0], [200.0, 201.0, 199.0], 1.5)
    evs = ca.detect(bars)
    assert len(evs) == 1 and evs[0]["factor"] == 1.5 and evs[0]["clean"] is True


def test_detects_a_reverse_split():
    bars = _series([20.0, 20.5, 20.0, 200.0, 202.0, 198.0, 201.0])
    bars[3]["prevClose"] = 20.0
    evs = ca.detect(bars)
    assert len(evs) == 1 and evs[0]["kind"] == "reverse" and evs[0]["factor"] == 10.0


def test_a_violent_rally_is_not_mistaken_for_a_reverse_split():
    # +46% in a session is not reachable through trading either, but the only corporate
    # action that RAISES price is a consolidation, and those at least double it. Treating
    # a big rally as structural would rescale real history on the strength of a rally —
    # so upward events need REVERSE_MIN_MOVE_PCT, not HARD_MOVE_PCT.
    bars = _series([86.0, 87.0, 88.0, 89.0, 130.0])
    assert ca.detect(bars) == []
    # ...and a genuine consolidation still lands.
    real = _series([20.0, 20.5, 20.0, 60.0, 61.0, 59.0, 60.5])
    assert [e["factor"] for e in ca.detect(real)] == [3.0]


def test_a_demerger_is_caught_on_move_size_with_its_raw_ratio():
    # A demerger transfers value to the spun-off entity in no round proportion, so
    # there's no clean factor to snap to (VEDL, live: 2.849x). Past HARD_MOVE_PCT the
    # raw ratio is used, which is exactly what lets these through.
    bars = _series([773.6, 780.0, 773.6, 271.6, 275.0, 270.0, 274.0])
    bars[3]["prevClose"] = 773.6
    evs = ca.detect(bars)
    assert len(evs) == 1
    assert evs[0]["clean"] is False
    assert abs(evs[0]["factor"] - 773.6 / 271.6) < 1e-6


def test_detects_two_actions_in_one_history():
    bars = _series([1000.0, 1010.0, 1000.0, 200.0, 202.0, 200.0, 100.0, 101.0, 99.0])
    bars[3]["prevClose"] = 1000.0
    bars[6]["prevClose"] = 200.0
    evs = ca.detect(bars)
    assert [e["i"] for e in evs] == [3, 6]
    assert [e["factor"] for e in evs] == [5.0, 2.0]


# ---------------------------------------------------------------------------
# false positives — each of these MUST be left alone
# ---------------------------------------------------------------------------
def test_a_normal_move_is_never_touched():
    bars = _series([100.0, 104.0, 99.0, 101.0, 108.0, 103.0])
    assert ca.detect(bars) == []
    assert ca.adjust(bars) is bars          # same object: no copy on the common path


def test_an_odd_ratio_inside_the_candidate_band_is_left_alone():
    # -41% is violent but reachable, and 1.70 is not a split/bonus ratio, so this is
    # treated as a real move. Only >= HARD_MOVE_PCT overrides the clean-ratio rule.
    bars = _series([100.0, 101.0, 100.0, 58.8, 59.5, 58.0, 59.0])
    bars[3]["prevClose"] = 100.0
    assert ca.detect(bars) == []

    # Contrast: a ratio that IS within snapping distance of 1.5 at a similar move size
    # is taken, which is what makes the gate meaningful rather than a blanket cutoff.
    near = _series([100.0, 101.0, 100.0, 65.0, 66.0, 64.0, 65.5])
    near[3]["prevClose"] = 100.0
    assert [e["factor"] for e in ca.detect(near)] == [1.5]


def test_a_gap_in_history_is_not_guessed_at():
    # Same 5x drop, but the bars are 25 days apart: over that many idle sessions a
    # stock genuinely moves, so the ratio means nothing.
    bars = _series([500.0, 505.0, 500.0])
    later = _series([100.0, 101.0, 99.0], start="2026-08-05")
    for b in later:
        b["prevClose"] = 100.0
    assert ca.detect(bars + later) == []


def test_a_one_day_bad_print_is_rejected_in_both_directions():
    # The round trip is the trap: a single garbage close looks like a crash on the bad
    # day AND a "reverse split" on the day the price returns. Adjusting either would
    # rescale all history behind it, so the two-sided persistence check must kill both.
    bars = _series([2000.0, 2010.0, 1990.0, 1.0, 2000.0, 2005.0, 1995.0])
    bars[3]["prevClose"] = 1990.0
    bars[4]["prevClose"] = 1.0
    assert ca.detect(bars) == []


def test_a_sustained_crash_without_a_clean_ratio_is_kept_but_a_huge_one_is_adjusted():
    # Honest limitation, pinned so a future change is deliberate: without the corporate-
    # actions feed, size is the only evidence available. A -37% fall that stays down is
    # left alone (no clean ratio); a -80% fall is taken as structural, because NSE price
    # bands make it unreachable through trading.
    mild = _series([100.0, 101.0, 100.0, 63.0, 62.0, 61.0, 62.5])
    mild[3]["prevClose"] = 100.0
    assert ca.detect(mild) == []

    huge = _series([100.0, 101.0, 100.0, 20.0, 19.5, 20.5, 19.0])
    huge[3]["prevClose"] = 100.0
    assert len(ca.detect(huge)) == 1


# ---------------------------------------------------------------------------
# adjustment
# ---------------------------------------------------------------------------
def test_adjust_rescales_history_and_leaves_the_present_alone():
    bars = _split_series([500.0, 505.0, 500.0], [100.0, 101.0, 99.0], 5.0)
    out = ca.adjust(bars)
    # Back-adjusted: the newest bars keep their REAL traded prices, so rupee figures and
    # the liquidity floors downstream stay meaningful.
    assert [b["close"] for b in out[3:]] == [b["close"] for b in bars[3:]]
    assert out[0]["close"] == 100.0 and out[1]["close"] == 101.0
    assert out[0]["high"] == round(500.0 * 1.02 / 5, 4)
    assert out[0]["volume"] == bars[0]["volume"] * 5      # shares scale inversely
    assert bars[0]["close"] == 500.0                      # input not mutated


def test_adjust_keeps_rupee_turnover_and_scale_free_fields_intact():
    bars = _split_series([500.0, 505.0, 500.0], [100.0, 101.0, 99.0], 5.0)
    out = ca.adjust(bars)
    for a, b in zip(bars, out):
        assert a["value"] == b["value"]        # price x quantity is invariant
        assert a["delivPct"] == b["delivPct"]  # a ratio has no scale
        assert a["trades"] == b["trades"]      # so does a count of trades


def test_adjust_repairs_the_ex_dates_prev_close_which_is_the_actual_bug():
    bars = _split_series([500.0, 505.0, 500.0], [100.0, 101.0, 99.0], 5.0)
    ex = bars[3]
    assert ex["prevClose"] == 500.0                  # NSE's stale-scale value...
    assert (ex["close"] / ex["prevClose"] - 1) * 100 < -79   # ...a phantom -80% ret1

    out = ca.adjust(bars)
    fixed = out[3]
    assert fixed["prevClose"] == 100.0               # = the adjusted previous close
    assert abs((fixed["close"] / fixed["prevClose"] - 1) * 100) < 1e-9


def test_adjust_compounds_multiple_actions():
    bars = _series([1000.0, 1010.0, 1000.0, 200.0, 202.0, 200.0, 100.0, 101.0, 99.0])
    bars[3]["prevClose"] = 1000.0
    bars[6]["prevClose"] = 200.0
    out = ca.adjust(bars)
    # Oldest bars sit behind BOTH a 5x and a 2x, so they scale by 10x in total.
    assert out[0]["close"] == 100.0
    assert out[3]["close"] == 100.0        # behind the 2x only
    assert out[6]["close"] == 100.0        # current scale, untouched


def test_adjust_survives_missing_and_zero_fields():
    bars = _split_series([500.0, 505.0, 500.0], [100.0, 101.0, 99.0], 5.0)
    del bars[0]["vwap"]
    bars[1]["volume"] = 0
    bars[2]["delivQty"] = None
    out = ca.adjust(bars)
    assert "vwap" not in out[0]
    assert out[1]["volume"] == 0           # a 0 stays 0, not a division artefact
    assert out[2]["delivQty"] is None


# ---------------------------------------------------------------------------
# grouped helper
# ---------------------------------------------------------------------------
def test_adjust_grouped_reports_what_it_changed_and_passes_the_rest_through():
    quiet = _series([100.0, 101.0, 99.0, 102.0])
    split = _split_series([500.0, 505.0, 500.0], [100.0, 101.0, 99.0], 5.0)
    stats = {}
    out = ca.adjust_grouped({"QUIET": quiet, "SPLIT": split}, stats=stats)

    assert out["QUIET"] is quiet           # untouched symbols aren't copied
    assert out["SPLIT"][0]["close"] == 100.0
    assert stats["symbols"] == 2 and stats["adjusted"] == 1 and stats["events"] == 1
    assert stats["detail"][0]["symbol"] == "SPLIT"
    assert stats["detail"][0]["events"][0]["factor"] == 5.0


def test_adjust_grouped_isolates_a_broken_symbol():
    good = _split_series([500.0, 505.0, 500.0], [100.0, 101.0, 99.0], 5.0)
    bad = [{"d": "2026-07-01", "close": "oops"}, {"d": "2026-07-02", "close": None}]
    stats = {}
    out = ca.adjust_grouped({"GOOD": good, "BAD": bad}, stats=stats)
    # One malformed name must not sink a market-wide scan.
    assert out["BAD"] is bad
    assert out["GOOD"][0]["close"] == 100.0
    assert stats["adjusted"] == 1
