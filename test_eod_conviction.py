"""
Unit tests for eod_conviction.py — the EOD "conviction board".

The fusion logic is PURE and driven with hand-built feature dicts / bars:
  - _oi_state       : the 4 price×OI quadrants (long/short buildup, covering, unwind)
  - _deal_side      : nets a symbol's deals to BUY / SELL / None by quantity
  - _pillars_long/short : independent confirmations fire on the right features
  - _avg_range_pct / _plan : volatility-scaled 2R entry/stop/target (both directions)
  - _pick           : picks the stronger side, scores, rates
board() / save() are exercised against a throwaway SQLite DB seeded via
db.eod_bars_put / eod_oi_put, so nothing touches NSE or the network.

Run: python test_eod_conviction.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile
from datetime import datetime, timedelta

import eod_conviction as ec


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    import db
    d = tempfile.mkdtemp(prefix="nse_conv_test_")
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
    return {"d": d, "date": d, "open": close if o is None else o,
            "high": (close * 1.01) if h is None else h,
            "low": (close * 0.99) if l is None else l, "close": close,
            "prevClose": close if prev is None else prev, "volume": vol,
            "value": (close * vol) if val is None else val, "delivPct": deliv}


# ---------------------------------------------------------------------------
# _oi_state
# ---------------------------------------------------------------------------
def test_oi_state_quadrants():
    up = ec._oi_state([{"oi": 112000, "changeOi": 12000}], price_up=True)
    assert up["label"] == "long buildup" and up["bullish"] and up["oiPct"] == 12.0
    dn = ec._oi_state([{"oi": 112000, "changeOi": 12000}], price_up=False)
    assert dn["label"] == "short buildup" and not dn["bullish"]
    cov = ec._oi_state([{"oi": 88000, "changeOi": -12000}], price_up=True)
    assert cov["label"] == "short covering" and cov["bullish"]
    unw = ec._oi_state([{"oi": 88000, "changeOi": -12000}], price_up=False)
    assert unw["label"] == "long unwinding" and not unw["bullish"]


def test_oi_state_none_and_latest_usable_row():
    assert ec._oi_state([], price_up=True) is None
    assert ec._oi_state([{"oi": None, "changeOi": None}], price_up=True) is None
    # picks the freshest row that actually carries oi + changeOi
    rows = [{"oi": 100, "changeOi": 10}, {"oi": None, "changeOi": None}]
    st = ec._oi_state(rows, price_up=True)
    assert st is not None and st["oiPct"] == round(10 / 90 * 100, 1)


# ---------------------------------------------------------------------------
# _deal_side
# ---------------------------------------------------------------------------
def test_deal_side_nets_quantity():
    assert ec._deal_side([{"side": "BUY", "qty": 100}]) == "BUY"
    assert ec._deal_side([{"side": "SELL", "qty": 100}]) == "SELL"
    assert ec._deal_side([{"side": "BUY", "qty": 100},
                          {"side": "SELL", "qty": 40}]) == "BUY"
    assert ec._deal_side([{"side": "BUY", "qty": 50},
                          {"side": "SELL", "qty": 50}]) is None
    assert ec._deal_side([]) is None


# ---------------------------------------------------------------------------
# pillars
# ---------------------------------------------------------------------------
def _feat(**kw):
    base = {"symbol": "X", "close": 100.0, "windowDays": 40, "pctFromHigh": None,
            "pctFromLow": None, "trend": None, "delivPct": None, "delivVsAvg": None,
            "volMult": None, "pChange": 1.0, "value": 5e8}
    base.update(kw)
    return base


def test_pillars_long_fire_independently():
    f = _feat(pctFromHigh=0.5, trend="up", delivPct=72.0, delivVsAvg=15.0, volMult=3.0)
    oi = {"label": "long buildup", "bullish": True, "oiPct": 12.0}
    p = ec._pillars_long(f, oi, "BUY")
    labels = " | ".join(l for l, _ in p)
    assert len(p) == 6
    assert "breakout" in labels and "uptrend" in labels and "delivery 72%" in labels
    assert "long buildup" in labels and "bulk/block BUY" in labels


def test_pillars_long_none_when_flat():
    assert ec._pillars_long(_feat(), None, None) == []


def test_pillars_long_breakout_needs_window():
    # near-high but too little history → breakout pillar suppressed
    assert ec._pillars_long(_feat(pctFromHigh=0.2, windowDays=5), None, None) == []


def test_pillars_short_mirror():
    f = _feat(pctFromLow=-0.5, trend="down", delivPct=20.0, pChange=-2.0, volMult=2.0)
    oi = {"label": "short buildup", "bullish": False, "oiPct": 15.0}
    p = ec._pillars_short(f, oi, "SELL")
    labels = " | ".join(l for l, _ in p)
    assert "breakdown" in labels and "downtrend" in labels
    assert "short buildup" in labels and "bulk/block SELL" in labels
    assert "weak delivery" in labels


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def test_avg_range_pct():
    bars = [_bar(d, 100.0, h=102.0, l=98.0) for d in _dates(14)]   # 4% range
    assert round(ec._avg_range_pct(bars), 2) == 4.0
    assert ec._avg_range_pct([]) is None


def test_plan_long_and_short_2r():
    bars = [_bar(d, 100.0, h=103.0, l=97.0) for d in _dates(14)]   # 6% range → stop 7.8%
    lp = ec._plan(100.0, "LONG", bars)
    assert lp["stopPct"] == 7.8 and lp["targetPct"] == 15.6 and lp["rr"] == 2.0
    assert lp["stop"] < 100.0 < lp["target"]
    sp = ec._plan(100.0, "SHORT", bars)
    assert sp["stop"] > 100.0 > sp["target"]


def test_plan_stop_clamped():
    calm = [_bar(d, 100.0, h=100.2, l=99.8) for d in _dates(14)]   # ~0.4% range
    assert ec._plan(100.0, "LONG", calm)["stopPct"] == 3.0          # floored at 3%
    wild = [_bar(d, 100.0, h=120.0, l=80.0) for d in _dates(14)]    # 40% range
    assert ec._plan(100.0, "LONG", wild)["stopPct"] == 9.0          # capped at 9%
    assert ec._plan(0, "LONG", calm) == {}


def test_rating_thresholds():
    assert ec._rating(70, 3) == "High"
    assert ec._rating(50, 2) == "Medium"
    assert ec._rating(90, 1) == "Low"          # one pillar never rates High


# ---------------------------------------------------------------------------
# _pick
# ---------------------------------------------------------------------------
def test_pick_prefers_side_with_more_confirmations():
    f = _feat(pctFromHigh=0.5, trend="up", delivPct=70.0, volMult=2.0)
    bars = [_bar(d, 100.0) for d in _dates(14)]
    p = ec._pick(f, bars, {"label": "long buildup", "bullish": True, "oiPct": 12.0},
                 "BUY", [{"side": "BUY", "client": "FUND"}])
    assert p["direction"] == "LONG" and p["confirmations"] >= 4
    assert p["conviction"] > 0 and p["reasons"] and p["entry"] == 100.0
    assert p["deal"]["side"] == "BUY" and p["deal"]["client"] == "FUND"


def test_pick_none_when_no_pillars():
    assert ec._pick(_feat(), [_bar(d, 100.0) for d in _dates(5)], None, None, []) is None


# ---------------------------------------------------------------------------
# board() / save() against a seeded DB
# ---------------------------------------------------------------------------
def _seed(db):
    # STACKED: breakout + delivery accumulation + volume + uptrend (4 pillars, LONG).
    up = [_bar(d, 50.0 + i, deliv=45.0) for i, d in enumerate(_dates(40))]
    up.append(_bar("2026-07-15", 130.0, prev=100.0, o=101.0, h=132.0, l=124.0,
                   vol=4000, val=5e8, deliv=78.0))
    db.eod_bars_put("STACKED", up)
    db.eod_oi_put("STACKED", "2026-07-28",
                  [{"d": "2026-07-15", "close": 130.5, "spot": 130.0,
                    "oi": 112000, "changeOi": 12000}])
    # ONESIG: a single gainer signal (should fail a 2+ pillar bar).
    fl = [_bar(d, 100.0, prev=100.0, val=5e8) for d in _dates(40)]
    fl.append(_bar("2026-07-15", 100.4, prev=100.0, val=5e8))   # tiny up, no stack
    db.eod_bars_put("ONESIG", fl)
    # CHEAP: penny — filtered by min_price even if it would stack.
    ch = [_bar(d, 5.0 + i * 0.1, deliv=45.0) for i, d in enumerate(_dates(40))]
    ch.append(_bar("2026-07-15", 12.0, prev=8.0, h=12.5, l=9.0, vol=5000, val=5e8, deliv=80.0))
    db.eod_bars_put("CHEAP", ch)


def test_board_ranks_stacked_first_and_filters():
    with _temp_db() as db:
        _seed(db)
        b = ec.board(min_price=20, min_value_cr=1.0, min_pillars=2)
    assert b["date"] == "2026-07-15" and b["universe"] == 3
    syms = [p["symbol"] for p in b["longs"]]
    assert "STACKED" in syms and syms[0] == "STACKED"
    assert "ONESIG" not in syms                 # only one signal < 2 pillars
    assert "CHEAP" not in syms                  # below min_price
    top = b["longs"][0]
    assert top["confirmations"] >= 4 and top["rating"] in ("High", "Medium")
    assert any("delivery" in r for r in top["reasons"])
    assert top["entry"] and top["stop"] and top["target"]


def test_board_min_pillars_gate():
    with _temp_db() as db:
        _seed(db)
        strict = ec.board(min_price=20, min_value_cr=1.0, min_pillars=6)
    # 6 independent confirmations is a very high bar; STACKED has ~4 → empty.
    assert strict["count"] == 0


def test_board_empty_db_has_note():
    with _temp_db():
        b = ec.board()
    assert b["count"] == 0 and b["universe"] == 0 and b["note"]


def test_save_persists_and_skips_existing():
    with _temp_db() as db:
        _seed(db)
        b = ec.board(min_price=20, min_value_cr=1.0, min_pillars=2)
        res = ec.save(b)
        assert res["saved"] >= 1 and res["day"] == "2026-07-15"
        rows = db.ideas_for_day("2026-07-15")
        assert any(r["symbol"] == "STACKED" for r in rows)
        stacked = next(r for r in rows if r["symbol"] == "STACKED")
        assert stacked["reasons"][0].startswith("🏆 EOD conviction")
        assert stacked["entry"] and stacked["target"]
        # Saving again skips the already-present keys (no duplicates / clobber).
        res2 = ec.save(b)
        assert res2["saved"] == 0 and res2["skipped"] >= 1


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
