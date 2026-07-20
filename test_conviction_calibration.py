"""
Unit tests for conviction_calibration.py — the "does stacking pay?" report.

The maths is PURE and driven with hand-built idea dicts (no DB):
  - is_conviction     : only EOD-board ideas are scored (live ideas ignored)
  - _confirmations_of : parses "(N signals)"; falls back to counting reasons
  - _pillars_in       : maps reason labels to pillar keys; warnings excluded
  - has_warning       : detects the option-chain ⚠️ soft-veto
  - _bucket_stats     : win rate over RESOLVED, MFE/MAE over ALL ideas
  - _lift / _verdict  : per-pillar edge and the one-line stacking verdict
report() is the only impure part: one db.ideas_all() read, exercised against a
throwaway SQLite DB seeded via db.ideas_upsert (nothing touches NSE / network).

Run: python test_conviction_calibration.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile

import conviction_calibration as cc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    import db
    d = tempfile.mkdtemp(prefix="nse_calib_test_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = d
    db.DB_FILE = os.path.join(d, "market.db")
    db._initialized = False
    db.init()
    try:
        yield db
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)


def _idea(day="2026-07-10", sym="AAA", direction="LONG", conf=2, pillars=(),
          outcome=None, outcomePct=None, best=0.0, worst=0.0,
          rating="Medium", warns=(), conviction=50.0, live=False):
    """Build one idea dict shaped like the ones the board saves."""
    if live:
        reasons = ["momentum breakout", "vol 3x"]     # NOT an EOD-conviction idea
    else:
        reasons = ["🏆 EOD conviction (%d signals)" % conf,
                   *pillars, *["⚠️ " + w for w in warns]]
    return {
        "day": day, "symbol": sym, "direction": direction,
        "entry": 100.0, "stop": 94.0, "target": 112.0,
        "stopPct": 6.0, "targetPct": 12.0, "rr": 2.0,
        "conviction": conviction, "rating": rating, "reasons": reasons,
        "fno": True, "pChange": 1.0,
        "firstSeenAt": day + " 16:00", "lastSeenAt": day + " 16:00", "ltp": 100.0,
        "movePct": outcomePct or 0.0, "maxMovePct": best, "minMovePct": worst,
        "outcome": outcome, "outcomeAt": (day + " 12:00" if outcome else None),
        "outcomePct": outcomePct,
    }


# reusable pillar label bundles
_BREAK = "breakout — at/above 20d high"
_TREND = "uptrend (close > 20 > 50-DMA)"
_DELIV = "delivery 72% — real accumulation"
_SECTOR = "🧭 IT is a leading sector (#1/12, RS +5)"
_OPTION = "🎯 option chain: room to call OI wall ₹130"


# ---------------------------------------------------------------------------
# is_conviction / parsing
# ---------------------------------------------------------------------------
def test_is_conviction_only_board_ideas():
    assert cc.is_conviction(_idea(conf=3)) is True
    assert cc.is_conviction(_idea(live=True)) is False
    assert cc.is_conviction({"reasons": []}) is False
    assert cc.is_conviction({}) is False


def test_confirmations_parses_header():
    assert cc._confirmations_of(_idea(conf=4)) == 4
    assert cc._confirmations_of(_idea(conf=2)) == 2


def test_confirmations_fallback_counts_non_warning_reasons():
    # header without a "(N signals)" count → count the non-warning reasons
    idea = {"reasons": ["🏆 EOD conviction", _BREAK, _TREND, "⚠️ into a wall"]}
    assert cc._confirmations_of(idea) == 2


def test_pillars_in_maps_labels_and_excludes_warnings():
    idea = _idea(conf=5, pillars=[_BREAK, _TREND, _DELIV, _SECTOR, _OPTION],
                 warns=["target runs into call OI wall ₹110"])
    got = cc._pillars_in(idea)
    assert got == {"breakout", "trend", "delivery", "sector", "option"}
    # the "⚠️ …" warning line must not create a pillar
    assert "wall" not in " ".join(got)


def test_pillars_in_oi_and_deal():
    idea = _idea(conf=2, pillars=["F&O: long buildup (OI +12%)",
                                  "bulk/block: net BUY 2.1L sh"])
    assert cc._pillars_in(idea) == {"oi", "deal"}


def test_has_warning():
    assert cc.has_warning(_idea(warns=["into a wall"])) is True
    assert cc.has_warning(_idea()) is False


def test_pillar_of_maps_live_and_saved_labels():
    # exactly the labels the board emits (before the 🏆 prefix / ⚠️ warnings)
    assert cc.pillar_of("breakout — at/above 20d high") == "breakout"
    assert cc.pillar_of("coiling just under the 20d high") == "breakout"
    assert cc.pillar_of("breakdown — at/below 20d low") == "breakout"
    assert cc.pillar_of("uptrend (close > 20 > 50-DMA)") == "trend"
    assert cc.pillar_of("delivery 72% — real accumulation") == "delivery"
    assert cc.pillar_of("1.8x average volume") == "volume"
    assert cc.pillar_of("F&O long buildup (OI +12%)") == "oi"
    assert cc.pillar_of("F&O short covering (OI -9%)") == "oi"
    assert cc.pillar_of("🐋 bulk/block BUY (institutional print)") == "deal"
    assert cc.pillar_of("🧭 IT is a leading sector (#1/12, RS +5)") == "sector"
    assert cc.pillar_of("🎯 option chain: room to call OI wall ₹130") == "option"
    assert cc.pillar_of("🔄 high rollover 82% — longs carrying into next month, rank 95") == "rollover"
    assert cc.pillar_of("🔄 high rollover 78% — shorts carrying into next month, rank 90") == "rollover"
    assert cc.pillar_of("⚠️ target runs into call OI wall ₹110") is None
    assert cc.pillar_of("random noise") is None


# ---------------------------------------------------------------------------
# bucket stats / lift / verdict (pure)
# ---------------------------------------------------------------------------
def test_mean_ignores_none():
    assert cc._mean([1, None, 3]) == 2.0
    assert cc._mean([None]) is None
    assert cc._mean([]) is None


def test_bucket_stats_winrate_over_resolved_mfe_over_all():
    ideas = [
        _idea(outcome="TARGET", outcomePct=12.0, best=12.0, worst=-2.0),
        _idea(outcome="TARGET", outcomePct=12.0, best=12.0, worst=-1.0),
        _idea(outcome="STOP", outcomePct=-6.0, best=3.0, worst=-6.0),
        _idea(outcome=None, best=4.0, worst=-3.0),      # still open
    ]
    b = cc._bucket_stats(ideas)
    assert b["n"] == 4 and b["resolved"] == 3 and b["open"] == 1
    assert b["wins"] == 2 and b["losses"] == 1
    assert b["winRate"] == round(2 / 3 * 100, 1)         # 66.7
    assert b["avgOutcomePct"] == round((12 + 12 - 6) / 3, 2)  # resolved only
    assert b["avgBest"] == round((12 + 12 + 3 + 4) / 4, 2)    # MFE over ALL
    assert b["avgWorst"] == round((-2 - 1 - 6 - 3) / 4, 2)    # MAE over ALL


def test_bucket_stats_empty():
    b = cc._bucket_stats([])
    assert b["n"] == 0 and b["winRate"] is None and b["avgOutcomePct"] is None


def test_lift_positive_when_pillar_helps():
    withs = [_idea(outcome="TARGET", outcomePct=10.0) for _ in range(3)]
    withouts = [_idea(outcome="STOP", outcomePct=-5.0) for _ in range(3)]
    lift = cc._lift(withs, withouts)
    assert lift["with"]["winRate"] == 100.0
    assert lift["without"]["winRate"] == 0.0
    assert lift["winRateLift"] == 100.0
    assert lift["expLift"] == 15.0


def test_lift_none_when_side_unresolved():
    withs = [_idea(outcome=None)]        # nothing resolved → winRate None
    withouts = [_idea(outcome="TARGET", outcomePct=10.0)]
    lift = cc._lift(withs, withouts)
    assert lift["winRateLift"] is None   # can't diff against None


def test_verdict_insufficient_history():
    tot = {"resolved": 3}
    assert "Not enough" in cc._verdict([], tot)


def test_verdict_rising_pays_off():
    by_conf = [
        {"bucket": "2", "winRate": 30.0, "resolved": 5},
        {"bucket": "4", "winRate": 70.0, "resolved": 5},
    ]
    tot = {"resolved": 10}
    v = cc._verdict(by_conf, tot)
    assert "paying off" in v and "30%" in v and "70%" in v


def test_verdict_not_helping_when_falling():
    by_conf = [
        {"bucket": "2", "winRate": 70.0, "resolved": 5},
        {"bucket": "4", "winRate": 40.0, "resolved": 5},
    ]
    tot = {"resolved": 10}
    assert "NOT helping" in cc._verdict(by_conf, tot)


# ---------------------------------------------------------------------------
# adaptive weighting (pure)
# ---------------------------------------------------------------------------
def test_mult_gate_and_neutral():
    # too few resolved on the WITH side → neutral
    assert cc._mult_from_lift(20.0, 3, 50) == 1.0
    # too few on the WITHOUT side → neutral
    assert cc._mult_from_lift(20.0, 50, 3) == 1.0
    # no measurable lift → neutral
    assert cc._mult_from_lift(None, 50, 50) == 1.0


def test_mult_clamped():
    hi = cc._mult_from_lift(80.0, 500, 500)     # huge +lift, ample sample
    lo = cc._mult_from_lift(-90.0, 500, 500)    # huge -lift
    assert cc._W_LO <= lo < 1.0 < hi <= cc._W_HI


def test_mult_shrinks_toward_neutral_with_thin_sample():
    thin = cc._mult_from_lift(20.0, cc._W_MIN_SAMPLE + 1, cc._W_MIN_SAMPLE + 1)
    ample = cc._mult_from_lift(20.0, 500, 500)
    assert 1.0 < thin < ample                    # both up, thin closer to neutral


def test_mult_direction_matches_sign_of_lift():
    assert cc._mult_from_lift(15.0, 100, 100) > 1.0   # helped → up-weight
    assert cc._mult_from_lift(-15.0, 100, 100) < 1.0  # hurt → down-weight


def test_pillar_weights_from_report_shape():
    rep = {"byPillar": [
        {"pillar": "sector", "winRateLift": 30.0,
         "with": {"resolved": 100}, "without": {"resolved": 100}},
        {"pillar": "breakout", "winRateLift": -20.0,
         "with": {"resolved": 100}, "without": {"resolved": 100}},
        {"pillar": "option", "winRateLift": 40.0,
         "with": {"resolved": 2}, "without": {"resolved": 100}},   # thin → neutral
    ]}
    w = cc.pillar_weights(rep=rep)
    assert w["sector"] > 1.0 and w["breakout"] < 1.0 and w["option"] == 1.0
    assert cc._W_LO <= w["sector"] <= cc._W_HI


# ---------------------------------------------------------------------------
# report() — against a throwaway DB
# ---------------------------------------------------------------------------
def _seed(db):
    rows = []
    # 2-signal picks: mostly lose (1 win / 3 loss = 25%)
    rows += [_idea("2026-07-10", "L2_%d" % i, conf=2, pillars=[_BREAK, _TREND],
                   outcome=("TARGET" if i == 0 else "STOP"),
                   outcomePct=(12.0 if i == 0 else -6.0), best=3.0, worst=-6.0)
             for i in range(4)]
    # 4-signal picks (with sector + option): mostly win (3 win / 1 loss = 75%)
    rows += [_idea("2026-07-11", "L4_%d" % i, conf=4,
                   pillars=[_BREAK, _TREND, _DELIV, _SECTOR],
                   outcome=("TARGET" if i < 3 else "STOP"),
                   outcomePct=(12.0 if i < 3 else -6.0), best=12.0, worst=-6.0,
                   rating="High")
             for i in range(4)]
    # one warned pick that lost
    rows.append(_idea("2026-07-12", "W0", conf=3,
                      pillars=[_BREAK, _TREND, _OPTION],
                      outcome="STOP", outcomePct=-6.0, best=2.0, worst=-6.0,
                      warns=["target runs into call OI wall ₹110"]))
    # a live (non-conviction) idea → must be ignored entirely
    rows.append(_idea("2026-07-12", "LIVE", live=True,
                      outcome="TARGET", outcomePct=12.0))
    db.ideas_upsert(rows)


def test_report_note_when_empty():
    with _temp_db():
        r = cc.report()
        assert r["note"] and r["totals"]["n"] == 0


def test_report_excludes_live_ideas_and_counts_conviction_only():
    with _temp_db() as db:
        _seed(db)
        r = cc.report()
        # 4 + 4 + 1 = 9 conviction ideas; the LIVE one is excluded
        assert r["totals"]["n"] == 9
        assert r["totals"]["resolved"] == 9


def test_report_stacking_buckets_and_verdict():
    with _temp_db() as db:
        _seed(db)
        r = cc.report()
        by = {b["bucket"]: b for b in r["byConfirmations"]}
        assert by["2"]["n"] == 4 and by["2"]["winRate"] == 25.0
        assert by["4"]["n"] == 4 and by["4"]["winRate"] == 75.0
        assert "paying off" in r["verdict"]


def test_report_pillar_lift_and_warning_impact():
    with _temp_db() as db:
        _seed(db)
        r = cc.report()
        pillars = {p["pillar"]: p for p in r["byPillar"]}
        # sector fired only on the winning 4-signal names → strong positive lift
        assert pillars["sector"]["with"]["n"] == 4
        assert pillars["sector"]["winRateLift"] > 0
        # the single warned pick lost → warning group win rate < clean group
        w = r["warningImpact"]
        assert w["withWarn"]["n"] == 1 and w["noWarn"]["n"] == 8
        assert w["withWarn"]["winRate"] == 0.0


def test_report_by_rating_and_direction_shapes():
    with _temp_db() as db:
        _seed(db)
        r = cc.report()
        ratings = {b["rating"]: b for b in r["byRating"]}
        assert ratings["High"]["n"] == 4 and ratings["Medium"]["n"] == 5
        dirs = {b["direction"]: b for b in r["byDirection"]}
        assert dirs["LONG"]["n"] == 9 and dirs["SHORT"]["n"] == 0


def test_report_attaches_adaptive_weights():
    with _temp_db() as db:
        _seed(db)
        r = cc.report()
        # every pillar row carries its earned weight, and the top-level map agrees
        assert "adaptiveWeights" in r
        for row in r["byPillar"]:
            assert row["weight"] == r["adaptiveWeights"][row["pillar"]]
            assert cc._W_LO <= row["weight"] <= cc._W_HI
        # the seed is small (< min-sample), so weights are neutral — never a swing
        assert all(w == 1.0 for w in r["adaptiveWeights"].values())


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
