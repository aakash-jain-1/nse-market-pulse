"""
sector_scan.py — market-wide sector relative-strength (rotation) board.

WHY THIS EXISTS
---------------
Individual breakouts are stronger when the whole SECTOR is being bought — money
rotates between sectors (into IT, out of FMCG) over weeks, and riding the leading
sector is one of the most durable swing edges. This mines the ingested EOD
universe (`db.eod_bars`) to answer two questions off-hours, with no network:

    1. Which sectors are strongest RIGHT NOW (relative strength vs the market)?
    2. Within the leading sectors, which names lead (the actionable watchlist)?

HOW
---
Relative strength here is **cross-sectional**: we have no index history in the
bhavcopy, so the benchmark is the market itself — the MEDIAN blended return
across all liquid, classified names. A stock's RS = its blended return minus that
market median (in percentage points); a sector's strength = the median RS of its
present constituents. Sectors are then ranked, and the top names inside the
strongest sectors become the leader board.

All the maths (`_ret`, `_blended`, `_median`, `_percentiles`, `_aggregate`) is
PURE and unit-tested; `scan()` is the only impure part (one `db.eod_bars_all`
query, reusing `eod_scanner._features` for close/volume/trend/breakout fields).
"""

import bisect
import logging

from nse_pulse.eod import eod_scanner as _es
from nse_pulse.eod import sectors as _sec

log = logging.getLogger("sector_scan")

# Return windows (trading sessions) blended into the RS score, and their weights.
# Clamped to the history actually present, so it degrades gracefully when only a
# few days are backfilled (it just becomes a shorter-horizon RS).
SHORT_WIN, LONG_WIN = 20, 60
_WEIGHTS = (0.5, 0.5)          # (short, long)
LOOKBACK = 75                  # bars to load — LONG_WIN + margin for weekends
_MIN_BARS = 6                  # need at least this much history to score a name

# Sector-strength percentile thresholds for "leading" / "lagging" (top / bottom
# third). Used by `context()` so the Conviction board + EOD scanner can treat a
# leading sector as an extra confirmation pillar.
_LEAD_PCTILE = 67.0
_LAG_PCTILE = 33.0


# ---------------------------------------------------------------------------
# pure maths
# ---------------------------------------------------------------------------
def _ret(closes, w):
    """Percent return over the last `w` sessions (clamped to available history).
    None when there aren't at least two closes."""
    n = len(closes)
    if n < 2:
        return None
    w = min(w, n - 1)
    base = closes[-1 - w]
    if not base:
        return None
    return (closes[-1] - base) / base * 100.0


def _blended(closes, short=SHORT_WIN, long=LONG_WIN, weights=_WEIGHTS):
    """Weighted blend of the short- and long-window returns → one momentum number.
    None when history is too thin to compute either leg."""
    rs = _ret(closes, short)
    rl = _ret(closes, long)
    if rs is None and rl is None:
        return None
    if rs is None:
        return rl
    if rl is None:
        return rs
    ws, wl = weights
    return (ws * rs + wl * rl) / (ws + wl)


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0


def _percentiles(values):
    """Each value → its 0-100 percentile rank (fraction of values ≤ it, ties
    share a rank). Aligned to `values`. Empty in → empty out."""
    n = len(values)
    if not n:
        return []
    sv = sorted(values)
    return [round(bisect.bisect_right(sv, v) / n * 100, 1) for v in values]


def _aggregate(records, market_median):
    """Group scored per-name records into ranked sector rows. Pure.

    Each record needs: sector, rs, blended, pChange, aboveMa20 (bool|None).
    Returns sector rows sorted strongest→weakest, each with rank, a 0-100 strength
    percentile, median RS/return, breadth, and its top-3 names by RS.
    """
    by_sec = {}
    for r in records:
        by_sec.setdefault(r["sector"], []).append(r)

    rows = []
    for sec, members in by_sec.items():
        rs_vals = [m["rs"] for m in members if m["rs"] is not None]
        up = [m for m in members if (m.get("pChange") or 0) > 0]
        above = [m for m in members if m.get("aboveMa20")]
        have_ma = [m for m in members if m.get("aboveMa20") is not None]
        top = sorted(members, key=lambda m: (m["rs"] if m["rs"] is not None else -1e9),
                     reverse=True)[:3]
        rows.append({
            "sector": sec,
            "count": len(members),
            "rs": round(_median(rs_vals), 2) if rs_vals else None,
            "medianReturn": round(_median([m["blended"] for m in members]), 2)
            if members else None,
            "breadthUpPct": round(len(up) / len(members) * 100, 1) if members else None,
            "aboveMa20Pct": round(len(above) / len(have_ma) * 100, 1) if have_ma else None,
            "topNames": [m["symbol"] for m in top],
        })

    # rank strongest → weakest by sector RS (None sorts last)
    rows.sort(key=lambda x: (x["rs"] if x["rs"] is not None else -1e9), reverse=True)
    strengths = _percentiles([x["rs"] if x["rs"] is not None else -1e9 for x in rows])
    for i, x in enumerate(rows):
        x["rank"] = i + 1
        x["strength"] = strengths[i]
    return rows


# ---------------------------------------------------------------------------
# record building + sector-strength map (shared by scan / Conviction / EOD scan)
# ---------------------------------------------------------------------------
def _collect(grouped, min_price, min_value_cr, short, long):
    """Scored per-name records from grouped bars ({SYMBOL:[bars]}). Keeps only
    classified + liquid names with enough history to blend a return. Returns
    (records, classified) — `classified` counts every name with a known sector
    (pre-liquidity), matching scan()'s coverage stats. Pure given `grouped`."""
    min_val = (min_value_cr or 0) * _es._CR
    records, classified = [], 0
    for sym, bars in grouped.items():
        sec = _sec.sector_of(sym)
        if not sec:
            continue
        classified += 1
        f = _es._features(bars)
        if not f or f.get("bars", 0) < _MIN_BARS:
            continue
        if min_price and f["close"] < min_price:
            continue
        if min_val and (f.get("value") or 0) < min_val:
            continue
        closes = [b["close"] for b in bars if b.get("close") is not None]
        blended = _blended(closes, short, long)
        if blended is None:
            continue
        r20, r60 = _ret(closes, short), _ret(closes, long)
        pm20 = f.get("pctFromMa20")
        records.append({
            "symbol": sym, "sector": sec, "blended": blended, "rs": None,
            "close": f["close"], "value": f.get("value"),
            "pChange": f.get("pChange"),
            "ret20": round(r20, 2) if r20 is not None else None,
            "ret60": round(r60, 2) if r60 is not None else None,
            "trend": f.get("trend"),
            "pctFromHigh": f.get("pctFromHigh"),
            "aboveMa20": (pm20 > 0) if pm20 is not None else None,
        })
    return records, classified


def _rank_records(records):
    """Fill rs/rsRank in place (blended return minus the market median). Returns
    the market median, or None when there's nothing to rank."""
    if not records:
        return None
    market_median = _median([r["blended"] for r in records])
    ranks = _percentiles([r["blended"] for r in records])
    for r, rk in zip(records, ranks):
        r["rs"] = round(r["blended"] - market_median, 2)
        r["rsRank"] = rk
    return market_median


def strength_map(grouped, min_price=20.0, min_value_cr=2.0,
                 short=SHORT_WIN, long=LONG_WIN):
    """{sector: {rank, rs, strength, count, total}} from already-loaded grouped
    bars — so the Conviction board / EOD scanner can consult sector strength
    WITHOUT a second DB pass. `strength` is the 0-100 percentile used by
    `context()` to flag leading/lagging sectors. Empty when nothing scores."""
    records, _ = _collect(grouped, min_price, min_value_cr, short, long)
    market_median = _rank_records(records)
    if market_median is None:
        return {}
    rows = _aggregate(records, market_median)
    total = len(rows)
    return {r["sector"]: {"rank": r["rank"], "rs": r["rs"],
                          "strength": r["strength"], "count": r["count"],
                          "total": total} for r in rows}


def context(smap, symbol):
    """Sector context for a symbol against a `strength_map`, or None if the symbol
    is unclassified / its sector didn't score. `leading`/`lagging` flag the top /
    bottom third of sectors — the flag the Conviction board treats as a pillar."""
    sec = _sec.sector_of(symbol)
    if not sec or not smap:
        return None
    s = smap.get(sec)
    if not s:
        return None
    return {"sector": sec, "rank": s["rank"], "rs": s["rs"],
            "strength": s["strength"], "total": s["total"],
            "leading": s["strength"] >= _LEAD_PCTILE,
            "lagging": s["strength"] <= _LAG_PCTILE}


# ---------------------------------------------------------------------------
# scan (impure: reads db.eod_bars)
# ---------------------------------------------------------------------------
def scan(limit_sectors=None, names_per_sector=5, lead_sectors=4,
         min_price=20.0, min_value_cr=2.0, lookback=LOOKBACK,
         short=SHORT_WIN, long=LONG_WIN):
    """Rank sectors by relative strength and surface the leading names.

    Filters names by close ≥ `min_price` and turnover ≥ `min_value_cr` crore
    (liquid only, so the sector medians aren't noise). Benchmark is the median
    blended return across the kept universe. Returns {date, windows, market,
    sectors, leaders, laggards, universe, classified, scanned, coverage, note}.
    Needs no network — bars come from db.eod_bars.
    """
    from nse_pulse.core import db
    try:
        names_per_sector = _es._clip(int(names_per_sector), 1, 25)
    except (TypeError, ValueError):
        names_per_sector = 5
    try:
        lead_sectors = _es._clip(int(lead_sectors), 1, 30)
    except (TypeError, ValueError):
        lead_sectors = 4

    latest = db.eod_latest_date()
    grouped = db.eod_bars_all(since=_es._since(latest, lookback))

    records, classified = _collect(grouped, min_price, min_value_cr, short, long)
    scanned = len(records)

    if not records:
        return {
            "date": latest, "windows": {"short": short, "long": long},
            "market": None, "sectors": [], "leaders": [], "laggards": [],
            "universe": len(grouped), "classified": classified, "scanned": 0,
            "coverage": status(),
            "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                        "lookback": lookback, "namesPerSector": names_per_sector,
                        "leadSectors": lead_sectors},
            "note": ("No classified EOD history yet — click ‘Backfill history’ on "
                     "the EOD Scan tab to load recent bhavcopies (the more days, the "
                     "better relative strength works)."),
        }

    market_median = _rank_records(records)
    sector_rows = _aggregate(records, market_median)
    if limit_sectors:
        sector_rows = sector_rows[:int(limit_sectors)]

    # Leader board: top names (by RS) inside the strongest `lead_sectors` sectors,
    # skipping anything in a confirmed downtrend. This is the actionable watchlist.
    lead_names = {x["sector"] for x in sector_rows[:lead_sectors]}
    leaders = [r for r in records if r["sector"] in lead_names and r["trend"] != "down"]
    leaders.sort(key=lambda r: r["rsRank"], reverse=True)
    per_sec, picked = {}, []
    for r in leaders:
        if per_sec.get(r["sector"], 0) >= names_per_sector:
            continue
        per_sec[r["sector"]] = per_sec.get(r["sector"], 0) + 1
        picked.append(_leader_row(r))

    # Laggards: the weakest sector's names (avoid / short candidates).
    laggards = []
    if sector_rows:
        weak = sector_rows[-1]["sector"]
        wl = sorted((r for r in records if r["sector"] == weak),
                    key=lambda r: r["rsRank"])[:names_per_sector]
        laggards = [_leader_row(r) for r in wl]

    return {
        "date": latest,
        "windows": {"short": short, "long": long},
        "market": {"medianReturn": round(market_median, 2),
                   "breadthUpPct": round(
                       sum(1 for r in records if (r.get("pChange") or 0) > 0)
                       / len(records) * 100, 1),
                   "names": len(records)},
        "sectors": sector_rows,
        "leaders": picked,
        "laggards": laggards,
        "universe": len(grouped),
        "classified": classified,
        "scanned": scanned,
        "coverage": status(),
        "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                    "lookback": lookback, "namesPerSector": names_per_sector,
                    "leadSectors": lead_sectors},
        "note": None,
    }


def _leader_row(r):
    """Trim a scored record to the UI/board fields."""
    tags = []
    if r.get("trend") == "up":
        tags.append("uptrend")
    pfh = r.get("pctFromHigh")
    if pfh is not None and pfh >= -3:
        tags.append("near high" if pfh < 0 else "at high")
    return {
        "symbol": r["symbol"], "sector": r["sector"], "rs": r["rs"],
        "rsRank": r.get("rsRank"), "ret20": r.get("ret20"), "ret60": r.get("ret60"),
        "close": r["close"], "pChange": round(r["pChange"], 2) if r.get("pChange") is not None else None,
        "trend": r.get("trend"), "pctFromHigh": round(pfh, 2) if pfh is not None else None,
        "tags": tags,
    }


def status():
    """Coverage: EOD history depth + how many names the sector map classifies."""
    st = _es.status()
    st["sectorMap"] = _sec.coverage()
    return st
