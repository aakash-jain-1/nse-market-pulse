"""
Unit tests for eod_scanner.py — the full-market End-of-Day / swing scanner.

The feature maths (`_features` / `_tags` / `_score`) and the per-view predicates
+ sort keys are PURE, so they're driven with hand-built daily bars and asserted
exactly. `scan()` / `status()` are exercised against a throwaway SQLite DB seeded
via db.eod_bars_put / eod_oi_put, so nothing touches NSE or the network.

Run: python test_eod_scanner.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile
from datetime import datetime, timedelta

from nse_pulse.eod import eod_scanner as es


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    from nse_pulse.core import db
    d = tempfile.mkdtemp(prefix="nse_eodscan_test_")
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


def _dates(n, end="2026-07-15"):
    e = datetime.strptime(end, "%Y-%m-%d").date()
    return [(e - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]


def _bar(d, close, prev=None, o=None, h=None, l=None, vol=1000, val=None, deliv=None):
    return {
        "d": d, "date": d,
        "open": close if o is None else o,
        "high": (close * 1.005) if h is None else h,
        "low": (close * 0.995) if l is None else l,
        "close": close,
        "prevClose": close if prev is None else prev,
        "volume": vol,
        "value": (close * vol) if val is None else val,
        "delivPct": deliv,
    }


def _flat(n, price=100.0, **kw):
    """n identical flat sessions at `price` (a calm baseline to break out of)."""
    return [_bar(d, price, prev=price, vol=kw.get("vol", 1000)) for d in _dates(n)]


# ---------------------------------------------------------------------------
# pure micro-helpers
# ---------------------------------------------------------------------------
def test_mean_pct_rng_clip():
    assert es._mean([2, 4, 6]) == 4
    assert es._mean([2, None, 6]) == 4          # None filtered
    assert es._mean([]) is None
    assert es._mean([None]) is None
    assert es._pct(110, 100) == 10.0
    assert es._pct(90, 100) == -10.0
    assert es._pct(10, 0) is None               # zero base guarded
    assert es._pct(None, 100) is None
    assert es._rng(11, 9) == 2
    assert es._rng(None, 9) is None
    assert es._clip(5, 0, 10) == 5 and es._clip(-1, 0, 10) == 0 and es._clip(99, 0, 10) == 10


def test_neg_helper():
    assert es._neg(3) == -3
    assert es._neg(-2) == 2
    assert es._neg(None) == float("-inf")       # Nones sort last in a desc rank


# ---------------------------------------------------------------------------
# _features
# ---------------------------------------------------------------------------
def test_features_needs_two_bars():
    assert es._features([]) is None
    assert es._features([_bar("2026-07-15", 100)]) is None


def test_features_rejects_bad_close():
    bars = [_bar("2026-07-14", 100), _bar("2026-07-15", 0)]
    assert es._features(bars) is None           # close <= 0 → nothing to say


def test_features_basic_change_gap_highs():
    bars = _flat(20, 100.0)
    # today: gap up to 101, close 110, above the flat 100.5 recent high, 3x volume
    bars.append(_bar("2026-07-16", 110.0, prev=100.0, o=101.0, h=112.0, l=108.0,
                     vol=3000, val=5e8, deliv=72.0))
    f = es._features(bars)
    assert f["bars"] == 21 and f["close"] == 110.0 and f["prevClose"] == 100.0
    assert round(f["pChange"], 2) == 10.0
    assert round(f["gapPct"], 2) == 1.0
    assert f["pctFromHigh"] > 0                 # closed above the prior-window high
    assert f["pctFromLow"] > 0
    assert f["windowDays"] == 20
    assert round(f["volMult"], 2) == 3.0        # 3000 vs the 1000 baseline
    assert f["delivPct"] == 72.0


def test_features_prevclose_falls_back_to_prior_bar():
    bars = [_bar("2026-07-14", 100.0), _bar("2026-07-15", 105.0)]
    bars[1]["prevClose"] = None                 # missing → use prior bar's close
    f = es._features(bars)
    assert f["prevClose"] == 100.0 and round(f["pChange"], 2) == 5.0


def test_features_moving_averages_and_trend_up():
    # 50 rising closes → close > ma20 > ma50 ⇒ uptrend.
    bars = [_bar(d, 50.0 + i) for i, d in enumerate(_dates(50))]
    f = es._features(bars)
    assert f["ma20"] is not None and f["ma50"] is not None
    assert f["ma20"] > f["ma50"]
    assert f["trend"] == "up"
    assert f["pctFromMa20"] > 0


def test_features_trend_down_and_none_when_short():
    bars = [_bar(d, 120.0 - i) for i, d in enumerate(_dates(50))]  # falling
    assert es._features(bars)["trend"] == "down"
    # <50 bars → ma50 unknown → trend can't be asserted → None (graceful)
    short = [_bar(d, 100.0 + i) for i, d in enumerate(_dates(10))]
    fs = es._features(short)
    assert fs["ma50"] is None and fs["trend"] is None


def test_features_nr7_squeeze():
    # six wide sessions then a very tight one → today's range is the 7-day min.
    bars = []
    for i, d in enumerate(_dates(6)):
        bars.append(_bar(d, 100.0, h=105.0, l=95.0))
    bars.append(_bar("2026-07-16", 100.0, h=100.2, l=99.8))
    f = es._features(bars)
    assert f["nr7"] is True
    assert f["rangePct"] < 1.0


def test_features_no_nr7_when_today_widest():
    bars = [_bar(d, 100.0, h=100.5, l=99.5) for d in _dates(6)]
    bars.append(_bar("2026-07-16", 100.0, h=110.0, l=90.0))  # widest → not a squeeze
    assert es._features(bars)["nr7"] is False


def test_features_delivery_average_and_spike():
    # 20 sessions around 40% delivery, then a spike to 80% today.
    bars = [_bar(d, 100.0, deliv=40.0) for d in _dates(20)]
    bars.append(_bar("2026-07-16", 101.0, prev=100.0, deliv=80.0))
    f = es._features(bars)
    assert f["delivPct"] == 80.0
    assert f["avgDelivPct"] == 40.0
    assert f["delivVsAvg"] == 40.0             # +40 percentage points vs its average


def test_features_delivery_none_when_absent():
    f = es._features(_flat(10, 100.0))         # no delivPct on any bar
    assert f["delivPct"] is None and f["avgDelivPct"] is None and f["delivVsAvg"] is None


# ---------------------------------------------------------------------------
# _tags / _score
# ---------------------------------------------------------------------------
def test_tags_breakout_volume_trend_deliv():
    bars = [_bar(d, 50.0 + i) for i, d in enumerate(_dates(50))]
    bars.append(_bar("2026-07-16", 130.0, prev=99.0, o=125.0, h=131.0, l=124.0,
                     vol=4000, val=5e8, deliv=80.0))
    tags = es._tags(es._features(bars))
    assert any("high" in t for t in tags)
    assert any("x vol" in t for t in tags)
    assert "uptrend" in tags
    assert any(t.startswith("gap +") for t in tags)
    assert any("deliv 80%" in t for t in tags)


def test_tags_breakdown_and_gap_down():
    bars = _flat(20, 100.0)
    bars.append(_bar("2026-07-16", 88.0, prev=100.0, o=95.0, h=96.0, l=87.0, vol=1000))
    tags = es._tags(es._features(bars))
    assert any("low" in t for t in tags)
    assert any(t.startswith("gap -") for t in tags)


def test_tags_delivery_spike_and_deal_badge():
    f = {"delivPct": 78.0, "delivVsAvg": 20.0,
         "deals": [{"side": "BUY", "client": "BIG FUND"}]}
    tags = es._tags(f)
    assert any("deliv 78%" in t for t in tags)
    assert any("+20pp" in t for t in tags)
    assert any("bulk BUY" in t for t in tags)


def test_score_deal_buy_bonus():
    base = {"score": 0, "pChange": 1.0}
    plain = es._score(dict(base))
    with_buy = es._score({**base, "deals": [{"side": "BUY"}]})
    assert with_buy >= plain + 8               # a bulk BUY lifts the score


def test_tags_empty_on_calm_shorthistory():
    # 5 flat bars: no MA/trend, no breakout (<10-day window), no vol/gap.
    assert es._tags(es._features(_flat(5, 100.0))) == []


def test_score_breakout_beats_flat():
    hot = _flat(30, 100.0)
    hot.append(_bar("2026-07-16", 112.0, prev=100.0, o=102.0, h=113.0, l=109.0,
                    vol=3000, val=5e8))
    calm = _flat(31, 100.0)
    assert es._score(es._features(hot)) > es._score(es._features(calm))
    assert es._score(es._features(calm)) >= 0


# ---------------------------------------------------------------------------
# view predicates + sort keys
# ---------------------------------------------------------------------------
def test_view_spec_covers_all_named_views():
    assert set(es.VIEWS) == set(es._VIEW_SPEC)


def test_view_predicates():
    up = {"pctFromHigh": 5.0, "pctFromLow": 12.0, "pChange": 3.0, "volMult": 2.5,
          "nr7": False, "rangePct": 4.0, "value": 5e8, "score": 40, "delivPct": 75.0}
    dn = {"pctFromHigh": -9.0, "pctFromLow": -1.0, "pChange": -4.0, "volMult": 0.8,
          "nr7": True, "rangePct": 0.3, "value": 1e8, "score": 5, "delivPct": 75.0}
    keep = {k: v[0] for k, v in es._VIEW_SPEC.items()}
    assert keep["breakout"](up) and not keep["breakout"](dn)
    assert keep["breakdown"](dn) and not keep["breakdown"](up)
    assert keep["gainers"](up) and not keep["gainers"](dn)
    assert keep["losers"](dn) and not keep["losers"](up)
    assert keep["unusual"](up) and not keep["unusual"](dn)
    assert keep["squeeze"](dn) and not keep["squeeze"](up)
    assert keep["value"](up) and keep["setups"](dn)  # setups keeps everything
    # delivery: needs high delivery% AND a non-negative day
    assert keep["delivery"](up) and not keep["delivery"](dn)  # dn is a down day
    assert not keep["delivery"]({"delivPct": 30.0, "pChange": 2.0})  # too low deliv


def test_since_floor():
    s = es._since("2026-07-15", 60)
    assert s and s < "2026-07-15"
    assert es._since(None, 60) is None
    assert es._since("garbage", 60) is None


# ---------------------------------------------------------------------------
# scan() / status() against a seeded DB
# ---------------------------------------------------------------------------
def _seed_universe(db):
    # BREAK: calm then a volume-confirmed breakout above the recent high.
    br = _flat(30, 100.0)
    br.append(_bar("2026-07-15", 110.0, prev=100.0, o=101.0, h=112.0, l=108.0,
                   vol=3000, val=5e8, deliv=70.0))
    db.eod_bars_put("BREAK", br)
    # DOWN: calm then a breakdown below the recent low (a loser).
    dn = _flat(30, 100.0)
    dn.append(_bar("2026-07-15", 90.0, prev=100.0, o=96.0, h=97.0, l=88.0,
                   vol=1000, val=4e8))
    db.eod_bars_put("DOWN", dn)
    # CHEAP: penny name — filtered out by min_price.
    ch = _flat(5, 5.0)
    ch.append(_bar("2026-07-15", 6.0, prev=5.0, vol=1000, val=5e8))
    db.eod_bars_put("CHEAP", ch)
    # ILLIQUID: fine price but tiny turnover — filtered out by min_value.
    il = _flat(5, 100.0)
    il.append(_bar("2026-07-15", 101.0, prev=100.0, vol=100, val=1e5))
    db.eod_bars_put("ILLIQUID", il)
    # SHORTHIST: a single bar — counted in the universe but not scannable.
    db.eod_bars_put("SHORTHIST", [_bar("2026-07-15", 100.0, val=5e8)])
    # F&O membership: only BREAK has futures OI history.
    db.eod_oi_put("BREAK", "2026-07-28",
                  [{"d": "2026-07-15", "close": 110.5, "spot": 110.0, "oi": 9e5}])


def test_scan_setups_ranks_breakout_first():
    with _temp_db() as db:
        _seed_universe(db)
        r = es.scan(view="setups", min_price=20, min_value_cr=1.0)
    assert r["view"] == "setups"
    assert r["date"] == "2026-07-15"
    assert r["universe"] == 5                 # 5 symbols have bars in the window
    assert r["scanned"] == 4                  # SHORTHIST (1 bar) can't be scored
    syms = [row["symbol"] for row in r["rows"]]
    assert syms[0] == "BREAK"                 # breakout tops the board
    assert "CHEAP" not in syms                # < min_price
    assert "ILLIQUID" not in syms             # < min_value
    assert "SHORTHIST" not in syms
    assert r["rows"][0]["tags"] and any("high" in t for t in r["rows"][0]["tags"])


def test_scan_breakout_and_breakdown_views():
    with _temp_db() as db:
        _seed_universe(db)
        bo = es.scan(view="breakout", min_price=20)
        bd = es.scan(view="breakdown", min_price=20)
    assert [x["symbol"] for x in bo["rows"]] == ["BREAK"]
    assert [x["symbol"] for x in bd["rows"]] == ["DOWN"]


def test_scan_gainers_losers_unusual_value():
    with _temp_db() as db:
        _seed_universe(db)
        assert [x["symbol"] for x in es.scan(view="gainers", min_price=20)["rows"]] == ["BREAK"]
        assert [x["symbol"] for x in es.scan(view="losers", min_price=20)["rows"]] == ["DOWN"]
        assert [x["symbol"] for x in es.scan(view="unusual", min_price=20)["rows"]] == ["BREAK"]
        val = es.scan(view="value", min_price=20)["rows"]
        assert val[0]["symbol"] == "BREAK" and val[0]["value"] >= val[1]["value"]


def test_scan_min_price_and_limit_and_fno():
    with _temp_db() as db:
        _seed_universe(db)
        assert es.scan(view="setups", min_price=200)["matched"] == 0   # excludes all
        assert es.scan(view="setups", min_price=20, limit=1)["matched"] == 1
        fno = es.scan(view="setups", min_price=20, fno_only=True)
        assert [x["symbol"] for x in fno["rows"]] == ["BREAK"]          # only F&O name


def test_scan_delivery_view_keeps_high_delivery_up_days():
    with _temp_db() as db:
        # ACCUM: 20 quiet days ~40% deliv, then an up day at 78% delivery.
        acc = [_bar(d, 100.0, prev=100.0, val=5e8, deliv=40.0) for d in _dates(20)]
        acc.append(_bar("2026-07-15", 103.0, prev=100.0, val=5e8, deliv=78.0))
        db.eod_bars_put("ACCUM", acc)
        # CHURN: same move but low delivery% (intraday churn) → excluded.
        ch = [_bar(d, 100.0, prev=100.0, val=5e8, deliv=25.0) for d in _dates(20)]
        ch.append(_bar("2026-07-15", 103.0, prev=100.0, val=5e8, deliv=28.0))
        db.eod_bars_put("CHURN", ch)
        r = es.scan(view="delivery", min_price=20, min_value_cr=1.0)
    assert [x["symbol"] for x in r["rows"]] == ["ACCUM"]
    assert r["rows"][0]["delivPct"] == 78.0


def test_scan_with_deals_annotates_and_boosts(monkeypatch=None):
    from nse_pulse.eod import deals
    with _temp_db() as db:
        _seed_universe(db)
        # Stub the deals cross-reference: a big player BOUGHT the DOWN name.
        orig = deals.by_symbol
        deals.by_symbol = lambda kind="bulk": (
            {"DOWN": [{"side": "BUY", "client": "BIG FUND"}]} if kind == "bulk" else {})
        try:
            r = es.scan(view="setups", min_price=20, with_deals=True)
        finally:
            deals.by_symbol = orig
    assert r["withDeals"] is True and r["filters"]["withDeals"] is True
    down = next(x for x in r["rows"] if x["symbol"] == "DOWN")
    assert down.get("deals") and any("bulk BUY" in t for t in down["tags"])


def test_scan_with_deals_off_by_default():
    with _temp_db() as db:
        _seed_universe(db)
        r = es.scan(view="setups", min_price=20)
    assert r["withDeals"] is False and r["filters"]["withDeals"] is False


def test_scan_unknown_view_falls_back_to_setups():
    with _temp_db() as db:
        _seed_universe(db)
        assert es.scan(view="nonsense", min_price=20)["view"] == "setups"


def test_scan_empty_db_has_note():
    with _temp_db():
        r = es.scan()
    assert r["rows"] == [] and r["universe"] == 0
    assert r["note"] and "Backfill" in r["note"]


def test_scan_limit_is_clamped():
    with _temp_db() as db:
        _seed_universe(db)
        assert es.scan(view="setups", min_price=20, limit=99999)["view"] == "setups"
        assert es.scan(view="setups", min_price=20, limit="oops")["matched"] >= 0


def test_status_reports_coverage():
    with _temp_db() as db:
        _seed_universe(db)
        st = es.status()
    assert st["symbols"] == 5 and st["rows"] > 5
    assert st["to"] == "2026-07-15"
    assert st["fnoSymbols"] == 1
    assert st["lookback"] == es.LOOKBACK


# ---------------------------------------------------------------------------
# sector relative-strength pillar (score bonus + 🧭 tag)
# ---------------------------------------------------------------------------
def test_score_folds_in_sector_strength():
    base = es._score({"pctFromHigh": 0.0})                       # breakout, no sector
    lead = es._score({"pctFromHigh": 0.0, "sectorStrength": 90.0})
    lag = es._score({"pctFromHigh": 0.0, "sectorStrength": 10.0})
    assert lead == round(base + 8, 1)                            # leading sector bonus
    assert lag == round(base - 6, 1)                             # lagging sector penalty


def test_tags_leading_sector_badge():
    lead = es._tags({"pctFromHigh": 0.0, "windowDays": 20,
                     "sector": "IT", "sectorRank": 1, "sectorLeading": True})
    assert any(t.startswith("🧭 IT #1") for t in lead)
    # present but not leading → no compass badge
    plain = es._tags({"pctFromHigh": 0.0, "windowDays": 20,
                      "sector": "IT", "sectorLeading": False})
    assert not any(t.startswith("🧭") for t in plain)


def _ramp(a, b, n=40, val=5e8):
    cs = [a + (b - a) * i / (n - 1) for i in range(n)]
    out, prev = [], cs[0]
    for d, c in zip(_dates(n), cs):
        out.append(_bar(d, round(c, 2), prev=round(prev, 2), val=val))
        prev = c
    return out


def test_scan_annotates_leading_sector():
    with _temp_db() as db:
        # IT trending up (leading sector); Banks trending down.
        db.eod_bars_put("TCS", _ramp(80, 120))
        db.eod_bars_put("INFY", _ramp(80, 112))
        db.eod_bars_put("WIPRO", _ramp(80, 110))
        for s in ("HDFCBANK", "ICICIBANK", "SBIN"):
            db.eod_bars_put(s, _ramp(120, 96))
        r = es.scan(view="setups", min_price=20, min_value_cr=1.0)
    tcs = next((x for x in r["rows"] if x["symbol"] == "TCS"), None)
    assert tcs is not None
    assert tcs.get("sector") == "IT" and tcs.get("sectorLeading") is True
    assert tcs.get("sectorStrength") == 100.0
    assert any(t.startswith("🧭 IT") for t in tcs["tags"])


# ---------------------------------------------------------------------------
# futures-rollover pillar (score bonus + 🔄 carrying tag)
# ---------------------------------------------------------------------------
def test_score_folds_in_rollover():
    base = es._score({"pctFromHigh": 0.0})                       # breakout, no rollover
    carry = es._score({"pctFromHigh": 0.0, "carrying": True, "rollBullish": True})
    bear = es._score({"pctFromHigh": 0.0, "carrying": True, "rollBullish": False})
    assert carry == round(base + 6, 1)                          # carried, bull side → bonus
    assert bear == base                                         # bearish OI → no bull bonus


def test_tags_carrying_badge():
    hot = es._tags({"pctFromHigh": 0.0, "windowDays": 20,
                    "carrying": True, "rolloverPct": 82.0})
    assert any("🔄 carrying 82%" in t for t in hot)
    plain = es._tags({"pctFromHigh": 0.0, "windowDays": 20, "carrying": False})
    assert not any("carrying" in t for t in plain)


def test_attach_rollover_only_touches_fno_names():
    rmap = {"ACME": {"rolloverPct": 70.0, "rolloverRank": 90.0, "carrying": True,
                     "shedding": False, "bullish": False, "oiState": "short buildup",
                     "daysToExpiry": 5}}
    f = {"symbol": "ACME"}
    es._attach_rollover(f, rmap, "ACME")
    assert f["carrying"] is True and f["rollBullish"] is False and f["rolloverPct"] == 70.0
    g = {"symbol": "CASH"}
    es._attach_rollover(g, rmap, "CASH")            # cash-only name not in the F&O map
    assert "carrying" not in g
    es._attach_rollover(g, {}, "CASH")              # empty map → no-op
    assert "carrying" not in g


def test_scan_with_rollover_annotates_and_boosts():
    from nse_pulse.eod import rollover
    with _temp_db() as db:
        _seed_universe(db)
        orig = rollover.rank_map
        rollover.rank_map = lambda force=False: ("2026-07-15", {
            "BREAK": {"rolloverPct": 84.0, "rolloverRank": 96.0, "carrying": True,
                      "shedding": False, "bullish": True, "oiState": "long buildup",
                      "daysToExpiry": 3}})
        try:
            r = es.scan(view="setups", min_price=20, with_rollover=True)
        finally:
            rollover.rank_map = orig
    assert r["withRollover"] is True and r["filters"]["withRollover"] is True
    brk = next(x for x in r["rows"] if x["symbol"] == "BREAK")
    assert brk.get("carrying") is True and brk.get("rollBullish") is True
    assert any("carrying" in t for t in brk["tags"])
    down = next(x for x in r["rows"] if x["symbol"] == "DOWN")   # not in the map
    assert "carrying" not in down


def test_scan_with_rollover_off_by_default():
    from nse_pulse.eod import rollover
    called = []
    with _temp_db() as db:
        _seed_universe(db)
        orig = rollover.rank_map
        rollover.rank_map = lambda force=False: (called.append(1) or ("2026-07-15", {}))
        try:
            r = es.scan(view="setups", min_price=20)
        finally:
            rollover.rank_map = orig
    assert r["withRollover"] is False and r["filters"]["withRollover"] is False
    assert called == []                                          # not fetched when disabled


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
