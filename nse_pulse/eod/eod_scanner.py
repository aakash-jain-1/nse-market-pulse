"""
eod_scanner.py — full-market End-of-Day / swing scanner.

WHY THIS EXISTS
---------------
The live scanner (`nse_client.get_scanner`) only sees the ~100-150 names in NSE's
intraday "hot lists" and reads all-zeros outside market hours. But we now persist
the WHOLE cash market's daily bars in `db.eod_bars` (loaded from the NSE bhavcopy
via `bhavcopy.ingest_db` / `bhavcopy.backfill` — ~2400 equities, not just the hot
lists). This module mines that history for end-of-day setups so the app has a
market-wide board that ALSO works off-hours and on weekends.

WHAT IT FINDS (per symbol, from its own daily bars)
    • proximity to / break of the recent N-day high or low  (breakouts/breakdowns)
    • gaps vs the prior close
    • unusual volume vs the trailing average
    • trend alignment vs the 20/50-day moving averages
    • a volatility squeeze (NR7 — today's range is the tightest in 7 sessions)
    • money flow (turnover) and, when present, delivery %

DESIGN
------
All the maths (`_features`, `_tags`, `_score`, the per-view sort keys) is PURE —
it takes a list of bars in hand and returns numbers, so it is fully unit-tested
against hand-built bars with no DB or network. `scan()` is the only impure part:
it pulls the grouped bars from `db.eod_bars_all()` (one query), runs the pure
pipeline over every name, filters, ranks by the chosen view and returns the top N.

Signals DEGRADE GRACEFULLY with history depth: 2 bars already give %-change and
gaps; ~20 unlock moving averages / average volume / a meaningful "20-day high".
Fields that need more bars than are present come back as None (never a crash), and
tags/scores just omit them. Backfill more days for richer swing signals.
"""

import logging

log = logging.getLogger("eod_scanner")

# How many trailing sessions define "recent" highs/lows, moving averages and the
# average-volume baseline. Also bounds how much history scan() loads per name.
LOOKBACK = 60
_MA_FAST, _MA_SLOW = 20, 50
_VOL_WIN = 20          # trailing sessions for the average-volume baseline
_NR_WIN = 7            # NR7 squeeze window
_CR = 1e7              # ₹1 crore, for the min-turnover filter (value is in rupees)

# Named scans. Each maps to a filter + a sort key over the computed features.
VIEWS = ("setups", "breakout", "breakdown", "gainers", "losers",
         "unusual", "squeeze", "value", "delivery")

# Delivery% at/above this = real (non-intraday) buying → accumulation conviction.
_DELIV_HOT = 60.0

# Sector relative-strength percentile thresholds (top / bottom third): a setup in
# a leading sector scores higher, one in a lagging sector lower. Kept in sync with
# sector_scan's `context()`; a breakout in a strong sector beats a lone breakout.
_SECTOR_LEAD = 67.0
_SECTOR_LAG = 33.0


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(cur, base):
    """Percent change of cur vs base, or None when base is missing/zero."""
    if cur is None or not base:
        return None
    return (cur - base) / base * 100.0


def _rng(hi, lo):
    if hi is None or lo is None:
        return None
    return hi - lo


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# feature extraction (pure)
# ---------------------------------------------------------------------------
def _features(bars):
    """Compute swing features from a symbol's daily bars (ascending by date).

    Returns None when there isn't enough to say anything (need today + a prior
    close). Everything that needs more depth than `bars` provides is None."""
    bars = [b for b in bars if b.get("close") is not None]
    n = len(bars)
    if n < 2:
        return None
    last, prev = bars[-1], bars[-2]
    close = last.get("close")
    if not close or close <= 0:
        return None
    prevc = last.get("prevClose")
    if prevc is None:
        prevc = prev.get("close")

    o, h, l = last.get("open"), last.get("high"), last.get("low")
    v, value = last.get("volume"), last.get("value")

    prior = bars[:-1]                      # everything before today
    hiN = max((b["high"] for b in prior if b.get("high") is not None), default=None)
    loN = min((b["low"] for b in prior if b.get("low") is not None), default=None)

    closes = [b["close"] for b in bars if b.get("close") is not None]
    ma_f = _mean(closes[-_MA_FAST:]) if len(closes) >= _MA_FAST else None
    ma_s = _mean(closes[-_MA_SLOW:]) if len(closes) >= _MA_SLOW else None

    prior_vols = [b.get("volume") for b in prior if b.get("volume")]
    avg_vol = _mean(prior_vols[-_VOL_WIN:]) if prior_vols else None
    vol_mult = (v / avg_vol) if (v and avg_vol) else None

    deliv = last.get("delivPct")
    prior_deliv = [b.get("delivPct") for b in prior if b.get("delivPct") is not None]
    avg_deliv = _mean(prior_deliv[-_VOL_WIN:]) if prior_deliv else None

    rng = _rng(h, l)
    range_pct = (rng / close * 100.0) if rng is not None else None
    # NR7 = today's range is a genuine contraction: strictly narrower than each of
    # the prior sessions in the window (a flat/identical series is NOT a squeeze).
    prior_ranges = [_rng(b.get("high"), b.get("low")) for b in bars[-_NR_WIN:-1]]
    prior_ranges = [r for r in prior_ranges if r is not None]
    nr7 = bool(rng is not None and len(prior_ranges) >= 3 and rng < min(prior_ranges))

    trend = None
    if ma_f is not None and ma_s is not None:
        if close > ma_f > ma_s:
            trend = "up"
        elif close < ma_f < ma_s:
            trend = "down"

    return {
        "symbol": last.get("symbol"),
        "date": last.get("d") or last.get("date"),
        "bars": n,
        "close": close,
        "ltp": close,
        "prevClose": prevc,
        "pChange": _pct(close, prevc),
        "gapPct": _pct(o, prevc),
        "volume": v,
        "avgVol": avg_vol,
        "volMult": vol_mult,
        "value": value,
        "hiN": hiN,
        "loN": loN,
        "windowDays": len(prior),
        "pctFromHigh": _pct(close, hiN),   # >= 0 → at/above the recent high
        "pctFromLow": _pct(close, loN),    # <= 0 → at/below the recent low
        "ma20": ma_f,
        "ma50": ma_s,
        "pctFromMa20": _pct(close, ma_f),
        "pctFromMa50": _pct(close, ma_s),
        "trend": trend,
        "rangePct": range_pct,
        "nr7": nr7,
        "delivPct": deliv,
        "avgDelivPct": round(avg_deliv, 1) if avg_deliv is not None else None,
        # delivery% jump vs its own trailing average → a spike in real buying
        "delivVsAvg": round(deliv - avg_deliv, 1) if (deliv is not None and avg_deliv) else None,
    }


def _tags(f):
    """Human-readable setup badges for a feature dict. ⭐ marks a confirmed break."""
    tags, wd = [], f.get("windowDays") or 0
    pfh, pfl = f.get("pctFromHigh"), f.get("pctFromLow")
    if pfh is not None and wd >= 10:
        if pfh >= 0:
            tags.append(f"⭐ {wd}d high")
        elif pfh >= -3:
            tags.append("near high")
    if pfl is not None and wd >= 10 and pfl <= 0:
        tags.append(f"⭐ {wd}d low")
    g = f.get("gapPct")
    if g is not None and g >= 2:
        tags.append(f"gap +{g:.1f}%")
    elif g is not None and g <= -2:
        tags.append(f"gap {g:.1f}%")
    vm = f.get("volMult")
    if vm is not None and vm >= 2:
        tags.append(f"{vm:.1f}x vol")
    if f.get("trend") == "up":
        tags.append("uptrend")
    elif f.get("trend") == "down":
        tags.append("downtrend")
    if f.get("nr7"):
        tags.append("squeeze")
    dp = f.get("delivPct")
    if dp is not None and dp >= 65:
        tags.append(f"🚚 deliv {dp:.0f}%")
    dv = f.get("delivVsAvg")
    if dv is not None and dv >= 12:
        tags.append(f"deliv +{dv:.0f}pp")     # delivery-% spike vs its average
    for d in (f.get("deals") or []):
        tags.append("🐋 bulk BUY" if d.get("side") == "BUY" else "🐋 bulk SELL")
        break                                  # one badge is enough for the board
    if f.get("sectorLeading") and f.get("sector"):
        rk = f.get("sectorRank")
        tags.append(f"🧭 {f['sector']} #{rk}" if rk else f"🧭 {f['sector']} lead")
    if f.get("carrying"):                        # F&O positions carried into next month
        rp = f.get("rolloverPct")
        tags.append(f"🔄 carrying {rp:.0f}%" if rp is not None else "🔄 carrying")
    return tags


def _score(f):
    """Bullish setup strength ~0-100 (breakout + volume + trend + gap + squeeze).
    A single comparable number for the default board; view sort keys can override."""
    s = 0.0
    pfh = f.get("pctFromHigh")
    if pfh is not None:
        if pfh >= 0:
            s += 30 + _clip(pfh, 0, 10)          # at/above the recent high
        elif pfh >= -3:
            s += 18 + (pfh + 3) * 4              # coiling just under it
    vm = f.get("volMult")
    if vm:
        s += _clip(vm, 0, 6) * 5                 # up to +30 for a volume surge
    if f.get("trend") == "up":
        s += 12
    g = f.get("gapPct")
    if g and g > 0:
        s += _clip(g, 0, 5) * 1.5
    if f.get("nr7"):
        s += 6
    dp = f.get("delivPct")
    if dp and dp >= 60:
        s += _clip((dp - 60) / 8, 0, 5)
    if any(d.get("side") == "BUY" for d in (f.get("deals") or [])):
        s += 8                                   # a big player bought today
    ss = f.get("sectorStrength")                 # sector relative-strength pillar
    if ss is not None:
        if ss >= _SECTOR_LEAD:
            s += 8                               # riding a leading sector
        elif ss <= _SECTOR_LAG:
            s -= 6                               # fighting a lagging sector
    if f.get("carrying") and f.get("rollBullish"):
        s += 6                                   # F&O positions carried into next month, bull side
    return round(s, 1)


# Each view: (predicate that keeps a row, sort key — higher ranks first).
def _neg(x):
    return -x if x is not None else float("-inf")


_VIEW_SPEC = {
    "setups":    (lambda f: True,
                  lambda f: f["score"]),
    "breakout":  (lambda f: f.get("pctFromHigh") is not None and f["pctFromHigh"] >= -3,
                  lambda f: (f["pctFromHigh"], f.get("volMult") or 0)),
    "breakdown": (lambda f: f.get("pctFromLow") is not None and f["pctFromLow"] <= 3,
                  lambda f: (_neg(f["pctFromLow"]), f.get("volMult") or 0)),
    "gainers":   (lambda f: (f.get("pChange") or 0) > 0,
                  lambda f: f.get("pChange") or 0),
    "losers":    (lambda f: (f.get("pChange") or 0) < 0,
                  lambda f: _neg(f.get("pChange"))),
    "unusual":   (lambda f: (f.get("volMult") or 0) >= 1.5,
                  lambda f: f.get("volMult") or 0),
    "squeeze":   (lambda f: bool(f.get("nr7")),
                  lambda f: _neg(f.get("rangePct"))),
    "value":     (lambda f: (f.get("value") or 0) > 0,
                  lambda f: f.get("value") or 0),
    # Accumulation: high delivery% on an up day (real buying, not intraday churn).
    # Rank by delivery% then the spike-vs-average so a genuine jump floats up.
    "delivery":  (lambda f: f.get("delivPct") is not None and f["delivPct"] >= _DELIV_HOT
                            and (f.get("pChange") or 0) >= 0,
                  lambda f: (f.get("delivPct") or 0, f.get("delivVsAvg") or 0)),
}


# ---------------------------------------------------------------------------
# scan (impure: reads db.eod_bars)
# ---------------------------------------------------------------------------
def _since(latest, lookback):
    """A YYYY-MM-DD floor ~`lookback` trading days before `latest`, so scan()
    loads only recent history. Calendar padding (×1.6 + 10) covers weekends and
    holidays without an exchange calendar."""
    if not latest:
        return None
    from datetime import datetime, timedelta
    try:
        d = datetime.strptime(latest[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - timedelta(days=int(lookback * 1.6) + 10)).strftime("%Y-%m-%d")


def _deal_map(enabled):
    """{SYMBOL: [deal,…]} across the latest bulk + block deals, or {} when disabled
    or unavailable (network/off). Cheap (tiny CSVs, cached 30 min in `deals`)."""
    if not enabled:
        return {}
    try:
        from nse_pulse.eod import deals
        out = deals.by_symbol("bulk")
        for sym, ds in deals.by_symbol("block").items():
            out.setdefault(sym, []).extend(ds)
        return out
    except Exception:
        log.warning("scan: deals cross-reference failed", exc_info=True)
        return {}


def _sector_strength(grouped, min_price, min_value_cr):
    """{sector: strength…} from the already-loaded bars, or {} on any failure.
    Lazy import breaks the sector_scan↔eod_scanner cycle (sector_scan imports us
    at module load; we only reach back into it at call time)."""
    try:
        from nse_pulse.eod import sector_scan
        return sector_scan.strength_map(grouped, min_price, min_value_cr)
    except Exception:
        log.warning("scan: sector-strength cross-reference failed", exc_info=True)
        return {}


def _attach_sector(f, smap, sym):
    """Tag a row with its sector's relative-strength context (leading/lagging),
    which `_score`/`_tags` then fold in as an extra confirmation pillar."""
    if not smap:
        return
    try:
        from nse_pulse.eod import sector_scan
        ctx = sector_scan.context(smap, sym)
    except Exception:
        ctx = None
    if not ctx:
        return
    f["sector"] = ctx["sector"]
    f["sectorStrength"] = ctx["strength"]
    f["sectorRank"] = ctx["rank"]
    f["sectorRs"] = ctx["rs"]
    f["sectorLeading"] = ctx["leading"]
    f["sectorLagging"] = ctx["lagging"]


def _rollover_map(enabled):
    """{SYMBOL: rollover metrics + carrying/shedding} from the EOD FO bhavcopy, or {}
    when disabled/unavailable. Reuses the cached FO text shared with the option chain /
    rollover tab / conviction board, so it's usually free. Only F&O names appear. Lazy
    import (rollover reaches back through eod_options)."""
    if not enabled:
        return {}
    try:
        from nse_pulse.eod import rollover
        _, rmap = rollover.rank_map()
        return rmap or {}
    except Exception:
        log.warning("scan: rollover cross-reference failed", exc_info=True)
        return {}


def _attach_rollover(f, rmap, sym):
    """Tag a row with its futures-rollover context (is it CARRYING positions into next
    month?), which `_score`/`_tags` fold in as an extra F&O confirmation. Only F&O names
    are in `rmap`; cash-only names are left untouched."""
    r = rmap.get(sym) if rmap else None
    if not r:
        return
    f["rolloverPct"] = r.get("rolloverPct")
    f["rolloverRank"] = r.get("rolloverRank")
    f["carrying"] = bool(r.get("carrying"))
    f["shedding"] = bool(r.get("shedding"))
    f["rollBullish"] = r.get("bullish")       # net near+next OI direction (True/False/None)
    f["rollOiState"] = r.get("oiState")
    f["daysToExpiry"] = r.get("daysToExpiry")


def scan(view="setups", limit=50, min_price=20.0, min_value_cr=1.0,
         fno_only=False, lookback=LOOKBACK, with_deals=False, with_rollover=False):
    """Rank the whole ingested EOD universe by `view`.

    Filters: close ≥ `min_price`, turnover ≥ `min_value_cr` crore, and (optional)
    F&O names only. `with_deals` cross-references the latest bulk/block deals so
    rows a big player traded get a 🐋 badge (+ score bonus). `with_rollover` folds in
    the EOD futures rollover so an F&O name CARRYING its positions into next month gets
    a 🔄 badge (+ score bonus on the bull side). Returns {view, date, rows, universe,
    scanned, matched, coverage, note}. Needs no network for the bars (from db.eod_bars);
    deals/rollover add one tiny cached fetch each when enabled."""
    from nse_pulse.core import db
    view = view if view in _VIEW_SPEC else "setups"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = _clip(limit, 1, 300)

    latest = db.eod_latest_date()
    grouped = db.eod_bars_all(since=_since(latest, lookback))
    deal_map = _deal_map(with_deals)
    smap = _sector_strength(grouped, min_price, min_value_cr)
    rmap = _rollover_map(with_rollover)

    fno = None
    if fno_only:
        try:
            fno = set(db.eod_oi_symbols())
        except Exception:
            fno = None
        if not fno:
            fno = None  # nothing ingested yet → don't silently hide everything

    keep_pred, sort_key = _VIEW_SPEC[view]
    min_val = (min_value_cr or 0) * _CR
    rows, scanned = [], 0
    for sym, bars in grouped.items():
        if fno is not None and sym not in fno:
            continue
        f = _features(bars)
        if not f:
            continue
        scanned += 1
        if min_price and f["close"] < min_price:
            continue
        if min_val and (f.get("value") or 0) < min_val:
            continue
        if not keep_pred(f):
            continue
        if deal_map.get(sym):
            f["deals"] = deal_map[sym]
        _attach_sector(f, smap, sym)
        _attach_rollover(f, rmap, sym)
        f["score"] = _score(f)
        f["tags"] = _tags(f)
        rows.append(f)

    rows.sort(key=sort_key, reverse=True)
    rows = rows[:limit]

    return {
        "view": view,
        "date": latest,
        "rows": rows,
        "universe": len(grouped),
        "scanned": scanned,
        "matched": len(rows),
        "coverage": status(),
        "withDeals": bool(deal_map),
        "withRollover": bool(rmap),
        "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                    "fnoOnly": bool(fno_only), "lookback": lookback,
                    "withDeals": bool(with_deals), "withRollover": bool(with_rollover)},
        "note": None if grouped else (
            "No EOD history yet — click ‘Backfill history’ to load recent "
            "bhavcopies (whole-market daily bars)."),
    }


def status():
    """Coverage of the ingested EOD history (rows/symbols/date span + F&O names)."""
    from nse_pulse.core import db
    st = db.eod_stats() or {}
    bars = st.get("bars") or {}
    try:
        fno = len(db.eod_oi_symbols())
    except Exception:
        fno = 0
    return {
        "symbols": bars.get("symbols") or 0,
        "rows": bars.get("rows") or 0,
        "from": bars.get("from"),
        "to": bars.get("to"),
        "fnoSymbols": fno,
        "lookback": LOOKBACK,
    }
