"""
Unit tests for sector_scan.py — the sector relative-strength (rotation) board.

The RS maths (`_ret` / `_blended` / `_median` / `_percentiles` / `_aggregate`) is
PURE and asserted exactly. `scan()` runs against a throwaway SQLite DB seeded via
db.eod_bars_put, so nothing touches NSE or the network.

Run: python test_sector_scan.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile
from datetime import datetime, timedelta

import pytest

from nse_pulse.eod import sector_scan as ss


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    from nse_pulse.core import db
    d = tempfile.mkdtemp(prefix="nse_sector_test_")
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


def _bar(d, close, prev, val=5e8):
    return {"d": d, "date": d, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close, "prevClose": prev,
            "volume": int(val / close), "value": val, "delivPct": None}


def _ramp(start, end, n=60, val=5e8):
    """n daily bars whose close moves linearly start→end (a clean trend)."""
    closes = [start + (end - start) * i / (n - 1) for i in range(n)]
    bars, prev = [], closes[0]
    for d, c in zip(_dates(n), closes):
        bars.append(_bar(d, round(c, 2), round(prev, 2), val))
        prev = c
    return bars


# ---------------------------------------------------------------------------
# _ret
# ---------------------------------------------------------------------------
def test_ret_basic_and_clamp():
    assert ss._ret([100, 110], 1) == pytest.approx(10.0)
    assert ss._ret([100, 110], 20) == pytest.approx(10.0)     # clamped to n-1
    assert ss._ret([100, 105, 110], 2) == pytest.approx(10.0)


def test_ret_guards():
    assert ss._ret([100], 5) is None                          # <2 closes
    assert ss._ret([0, 10], 1) is None                        # zero base


# ---------------------------------------------------------------------------
# _blended
# ---------------------------------------------------------------------------
def test_blended_weighted_mean_of_legs():
    closes = [100 + i for i in range(41)]                     # 100..140, monotonic
    rs, rl = ss._ret(closes, 20), ss._ret(closes, 60)
    assert rs is not None and rl is not None and rs != rl
    assert ss._blended(closes) == pytest.approx((rs + rl) / 2)  # default 0.5/0.5


def test_blended_none_when_too_thin():
    assert ss._blended([100]) is None


def test_blended_custom_weights():
    closes = [100 + i for i in range(41)]
    rs, rl = ss._ret(closes, 20), ss._ret(closes, 60)
    got = ss._blended(closes, weights=(1.0, 3.0))
    assert got == pytest.approx((1.0 * rs + 3.0 * rl) / 4.0)


# ---------------------------------------------------------------------------
# _median / _percentiles
# ---------------------------------------------------------------------------
def test_median():
    assert ss._median([3, 1, 2]) == 2
    assert ss._median([4, 1, 2, 3]) == 2.5
    assert ss._median([]) is None
    assert ss._median([None, 5, None]) == 5


def test_percentiles_ties_and_order():
    assert ss._percentiles([1, 2, 2, 3]) == [25.0, 75.0, 75.0, 100.0]
    assert ss._percentiles([]) == []
    assert ss._percentiles([9]) == [100.0]


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------
def test_aggregate_ranks_and_breadth():
    recs = [
        {"symbol": "A", "sector": "IT", "rs": 10, "blended": 15, "pChange": 2, "aboveMa20": True},
        {"symbol": "B", "sector": "IT", "rs": 6, "blended": 11, "pChange": 1, "aboveMa20": True},
        {"symbol": "C", "sector": "Banks", "rs": -8, "blended": -3, "pChange": -1, "aboveMa20": False},
    ]
    rows = ss._aggregate(recs, market_median=5)
    assert [r["sector"] for r in rows] == ["IT", "Banks"]      # strongest first
    it, bk = rows[0], rows[1]
    assert it["rank"] == 1 and bk["rank"] == 2
    assert it["rs"] == 8 and bk["rs"] == -8                    # median RS
    assert it["breadthUpPct"] == 100.0 and bk["breadthUpPct"] == 0.0
    assert it["aboveMa20Pct"] == 100.0 and bk["aboveMa20Pct"] == 0.0
    assert it["topNames"] == ["A", "B"]
    assert it["strength"] == 100.0 and bk["strength"] == 50.0
    assert it["count"] == 2 and bk["count"] == 1


def test_aggregate_handles_missing_ma():
    recs = [{"symbol": "A", "sector": "IT", "rs": 1, "blended": 1,
             "pChange": 0, "aboveMa20": None}]
    rows = ss._aggregate(recs, 0)
    assert rows[0]["aboveMa20Pct"] is None                     # no MA data → None, no crash


# ---------------------------------------------------------------------------
# scan (impure)
# ---------------------------------------------------------------------------
def _seed_universe(db):
    # IT: three names trending UP (leaders).
    db.eod_bars_put("TCS", _ramp(100, 140))
    db.eod_bars_put("INFY", _ramp(100, 135))
    db.eod_bars_put("WIPRO", _ramp(100, 130))
    # Banks: three names trending DOWN (laggards).
    db.eod_bars_put("HDFCBANK", _ramp(100, 94))
    db.eod_bars_put("ICICIBANK", _ramp(100, 92))
    db.eod_bars_put("SBIN", _ramp(100, 96))
    # Unclassified name — must be ignored by the sector scan.
    db.eod_bars_put("ZZUNKNOWN", _ramp(100, 200))
    # Classified but ILLIQUID (tiny turnover) — counted as classified, not scored.
    db.eod_bars_put("PIDILITIND", _ramp(100, 150, val=1e5))


def test_scan_ranks_sectors_and_picks_leaders():
    with _temp_db() as db:
        _seed_universe(db)
        r = ss.scan(min_price=20, min_value_cr=1.0)
    assert r["date"] == "2026-07-15"
    assert [s["sector"] for s in r["sectors"]] == ["IT", "Banks"]
    it, bk = r["sectors"][0], r["sectors"][1]
    assert it["rs"] > bk["rs"] and it["strength"] == 100.0
    # leaders come from the strongest sector, ranked by RS (TCS ramped most)
    assert r["leaders"] and all(x["sector"] == "IT" for x in r["leaders"])
    assert r["leaders"][0]["symbol"] == "TCS"
    # laggards from the weakest sector
    assert r["laggards"] and all(x["sector"] == "Banks" for x in r["laggards"])
    # counts: 7 classified (6 liquid + illiquid PIDILITIND), 6 scored; ZZUNKNOWN excluded
    assert r["classified"] == 7 and r["scanned"] == 6
    assert r["universe"] == 8
    assert r["market"]["names"] == 6


def test_scan_min_price_filter_excludes_penny_sectors():
    with _temp_db() as db:
        db.eod_bars_put("IDEA", _ramp(8, 9))          # Telecom, sub-₹20
        db.eod_bars_put("TCS", _ramp(100, 140))       # IT, liquid
        r = ss.scan(min_price=20, min_value_cr=1.0)
    secs = [s["sector"] for s in r["sectors"]]
    assert "IT" in secs and "Telecom" not in secs     # IDEA filtered by price


def test_scan_empty_db_has_note():
    with _temp_db():
        r = ss.scan()
    assert r["sectors"] == [] and r["leaders"] == []
    assert r["market"] is None and r["note"] and "Backfill" in r["note"]


def test_scan_names_per_sector_clamped():
    with _temp_db() as db:
        _seed_universe(db)
        r = ss.scan(min_price=20, min_value_cr=1.0, names_per_sector=1, lead_sectors=1)
    # only the single strongest sector contributes leaders, capped at 1 name
    assert len(r["leaders"]) == 1 and r["leaders"][0]["sector"] == "IT"


def test_status_reports_coverage():
    with _temp_db() as db:
        _seed_universe(db)
        st = ss.status()
    assert "sectorMap" in st and st["sectorMap"]["sectors"] >= 12
    assert "symbols" in st and "rows" in st


# ---------------------------------------------------------------------------
# strength_map / context — the reusable pillar the scanner + conviction board use
# ---------------------------------------------------------------------------
def _seed_four_sectors(db):
    """Four sectors with a clean strength gradient → percentiles 100/75/50/25."""
    grades = {
        "TCS": (100, 150), "INFY": (100, 145), "WIPRO": (100, 140),          # IT strongest
        "HINDUNILVR": (100, 120), "ITC": (100, 118), "NESTLEIND": (100, 116),  # FMCG mild up
        "HDFCBANK": (100, 101), "ICICIBANK": (100, 100), "SBIN": (100, 102),   # Banks flat
        "SUNPHARMA": (100, 88), "CIPLA": (100, 90), "DRREDDY": (100, 86),       # Pharma weakest
    }
    for sym, (a, b) in grades.items():
        db.eod_bars_put(sym, _ramp(a, b))


def test_strength_map_ranks_and_flags():
    with _temp_db() as db:
        _seed_four_sectors(db)
        smap = ss.strength_map(db.eod_bars_all(), min_price=20, min_value_cr=1.0)
    assert set(smap) >= {"IT", "FMCG", "Banks", "Pharma"}
    assert smap["IT"]["rank"] == 1 and smap["IT"]["strength"] == 100.0
    assert smap["Pharma"]["strength"] == 25.0
    assert smap["IT"]["total"] == len(smap) and smap["IT"]["count"] == 3
    # leading = top third (≥67), lagging = bottom third (≤33)
    it = ss.context(smap, "TCS")
    assert it["sector"] == "IT" and it["leading"] and not it["lagging"]
    ph = ss.context(smap, "SUNPHARMA")
    assert ph["lagging"] and not ph["leading"]
    mid = ss.context(smap, "HDFCBANK")     # 50th pct → neither
    assert not mid["leading"] and not mid["lagging"]


def test_context_none_for_unclassified_or_empty_map():
    with _temp_db() as db:
        _seed_four_sectors(db)
        smap = ss.strength_map(db.eod_bars_all(), min_price=20, min_value_cr=1.0)
    assert ss.context(smap, "ZZ_NOT_A_SYMBOL") is None   # unclassified
    assert ss.context({}, "TCS") is None                  # empty map
    assert ss.context(None, "TCS") is None


def test_strength_map_empty_without_history():
    with _temp_db():
        assert ss.strength_map({}, 20, 1.0) == {}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
