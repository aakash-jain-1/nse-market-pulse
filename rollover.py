"""
rollover.py — futures rollover tracker from the EOD FO bhavcopy.

WHY THIS EXISTS
---------------
NSE stock/index futures are monthly (expiry = last Thursday). In the days around
expiry, traders carrying a view "roll" their position from the expiring near-month
contract into the next month. HOW MUCH rolls, and at WHAT price spread, is a genuine
read on conviction:

  • Rollover %  — how much of the near+next open interest already sits in the next
    month. Rising toward expiry = positions being CARRIED (conviction to hold the
    view), not just closed. A name rolling far above the market median is one where
    big players are staying in.
  • Roll cost (spread) — (next − near) price as a %. Positive (contango) = it costs
    to carry a long (demand to stay long / cost of carry); negative (backwardation)
    = often a dividend or bearish pressure.
  • OI state — pairing the day's NET OI change (near+next) with the price move gives
    the classic long-buildup / short-buildup / covering / unwinding read, so "high
    rollover WITH rising total OI" (fresh conviction) is told apart from "rollover on
    shrinking OI" (positions merely being closed).

DESIGN
------
The FO UDiFF bhavcopy carries every futures contract's EOD OI / close / settle / spot
for near, next AND far month — a plain static-archive ZIP with no anti-bot gate — so
this works OFF-HOURS / weekends and when the live feed is blocked, exactly like the
EOD option chain. `bhavcopy.parse_fo_futures_all()` is the PURE parser (all expiries
per symbol); this module is the ANALYTICS layer: per-name metrics + a CROSS-SECTIONAL
rank (each name's rollover vs the market median today — meaningful without needing a
rollover history). It reuses `eod_options._fo_text()` so the big FO file is fetched
and cached ONCE for both the option and rollover views.

Educational / research — NOT investment advice.
"""

import logging
import threading
import time
from datetime import datetime

log = logging.getLogger("rollover")

_CR = 1e7                 # ₹1 crore (turnover filter; value is in rupees)
_CACHE_TTL = 900          # 15 min — the board (one FO parse) changes once a day
_cache = {"ts": 0.0, "key": None, "board": None}

_MAP_TTL = 900            # 15 min — the market-wide {sym: metrics} map (conviction fuse)
_map_cache = {"ts": 0.0, "date": None, "map": None}
_map_lock = threading.Lock()

# Cross-sectional flags: a name is "carrying" when its rollover% ranks in the top
# fifth of the F&O universe today, "shedding" when in the bottom fifth.
_HI_PCTILE = 80.0
_LO_PCTILE = 20.0


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _days_between(iso_a, iso_b):
    """Whole days from ISO date a → b (b − a), or None if either is unparseable."""
    try:
        a = datetime.strptime(iso_a[:10], "%Y-%m-%d").date()
        b = datetime.strptime(iso_b[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (b - a).days


def _oi_state(net_chg_oi, price_up):
    """Classic price×OI quadrant on the NET (near+next) OI change → (label, bullish).
    net_chg_oi None → (None, None)."""
    if net_chg_oi is None:
        return None, None
    oi_up = net_chg_oi > 0
    if price_up and oi_up:
        return "long buildup", True
    if (not price_up) and oi_up:
        return "short buildup", False
    if price_up and (not oi_up):
        return "short covering", True
    return "long unwinding", False


def _metrics(fut, data_date):
    """Rollover metrics for one symbol's parsed futures dict (near vs next month), or
    None when it lacks two usable expiries. Pure."""
    exps = fut.get("expiries") or []
    if len(exps) < 2:
        return None
    by = fut["byExpiry"]
    near, nxt = by[exps[0]], by[exps[1]]
    noi, xoi = near.get("oi"), nxt.get("oi")
    npx, xpx = near.get("close"), nxt.get("close")
    if not noi or not xoi or not npx or not xpx:
        return None

    denom = noi + xoi
    rollover = round(xoi / denom * 100.0, 1) if denom else None
    roll_cost = round((xpx - npx) / npx * 100.0, 2) if npx else None

    days_gap = _days_between(near["expiry"], nxt["expiry"])
    annualized = (round(roll_cost * 365.0 / days_gap, 1)
                  if (roll_cost is not None and days_gap) else None)
    dte = _days_between(data_date, near["expiry"]) if data_date else None

    spot = near.get("underlying")
    basis = round((npx - spot) / spot * 100.0, 2) if spot else None

    net_chg = None
    if near.get("changeOi") is not None or nxt.get("changeOi") is not None:
        net_chg = (near.get("changeOi") or 0) + (nxt.get("changeOi") or 0)
    label, bullish = _oi_state(net_chg, (near.get("pChange") or 0) >= 0)

    return {
        "symbol": fut["symbol"],
        "kind": fut.get("kind"),
        "spot": spot,
        "nearExpiry": near["expiry"],
        "nextExpiry": nxt["expiry"],
        "daysToExpiry": dte,
        "nearPrice": npx,
        "nextPrice": xpx,
        "nearOI": noi,
        "nextOI": xoi,
        "pChange": near.get("pChange"),
        "rolloverPct": rollover,
        "rollCostPct": roll_cost,
        "annualizedRollPct": annualized,
        "basisPct": basis,
        "netChgOi": net_chg,
        "freshOi": (net_chg > 0) if net_chg is not None else None,
        "oiState": label,
        "bullish": bullish,
        "value": near.get("value"),
    }


def _percentile_ranks(vals):
    """Percentile rank (0–100) for each value vs the list (ties share the mean rank).
    Empty/one → all 50.0. Pure — same idea as sector_scan's cross-sectional rank."""
    n = len(vals)
    if n <= 1:
        return [50.0] * n
    order = sorted(range(n), key=lambda i: vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        # average position of the tie block, mapped to 0..100
        pos = (i + j) / 2.0
        pct = round(pos / (n - 1) * 100.0, 1)
        for k in range(i, j + 1):
            ranks[order[k]] = pct
        i = j + 1
    return ranks


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else round((xs[m - 1] + xs[m]) / 2.0, 1)


def _mode_str(vals):
    """Most common non-empty string (used to pick the universe's shared near expiry)."""
    counts = {}
    for v in vals:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _attach_ranks(rows):
    """Attach the cross-sectional rolloverRank (percentile vs the given rows) plus the
    carrying / shedding flags to each row, in place. Pure."""
    ranks = _percentile_ranks([r["rolloverPct"] for r in rows])
    for r, pr in zip(rows, ranks):
        r["rolloverRank"] = pr
        r["carrying"] = pr >= _HI_PCTILE
        r["shedding"] = pr <= _LO_PCTILE
    return rows


# ---------------------------------------------------------------------------
# board (impure: reads the cached FO bhavcopy text via eod_options._fo_text)
# ---------------------------------------------------------------------------
def board(min_price=20.0, min_value_cr=0.5, limit=50, sort="rollover", force=False):
    """Rank the F&O universe by futures ROLLOVER from the latest FO bhavcopy.

    A name qualifies when it has both a near AND next month future with OI + price and
    clears the liquidity floors (near price ≥ `min_price`, near turnover ≥
    `min_value_cr`). Each row gets a cross-sectional `rolloverRank` (percentile vs the
    market today). `sort` ∈ {rollover, rollcost, basis, dte}. Returns
    {date, rows, count, universe, medianRollover, nearExpiry, daysToExpiry, filters,
    note}. Off-hours; reuses the cached FO text (no extra download)."""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    sort = sort if sort in ("rollover", "rollcost", "basis", "dte") else "rollover"

    key = (round(float(min_price or 0), 2), round(float(min_value_cr or 0), 3),
           limit, sort)
    if not force and _cache["board"] is not None and _cache["key"] == key \
            and (time.time() - _cache["ts"]) < _CACHE_TTL:
        return _cache["board"]

    import eod_options
    date, text = eod_options._fo_text(force=force)
    if not text:
        out = {"date": date, "rows": [], "count": 0, "universe": 0,
               "medianRollover": None, "nearExpiry": None, "daysToExpiry": None,
               "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                           "limit": limit, "sort": sort},
               "note": "EOD F&O bhavcopy unavailable — try the ⬇ Backfill on the EOD "
                       "Scan tab, or NSE may be blocked (see the banner)."}
        _cache.update(ts=time.time(), key=key, board=out)
        return out

    import bhavcopy
    futs = bhavcopy.parse_fo_futures_all(text)
    universe = len(futs)
    min_val = (min_value_cr or 0) * _CR

    rows = []
    for fut in futs.values():
        m = _metrics(fut, date)
        if not m or m["rolloverPct"] is None:
            continue
        if min_price and (m["nearPrice"] or 0) < min_price:
            continue
        if min_val and (m.get("value") or 0) < min_val:
            continue
        rows.append(m)

    _attach_ranks(rows)

    keyfn = {
        "rollover": lambda r: (r["rolloverPct"] is not None, r["rolloverPct"] or 0),
        "rollcost": lambda r: (r["rollCostPct"] is not None, r["rollCostPct"] or 0),
        "basis": lambda r: (r["basisPct"] is not None, r["basisPct"] or 0),
        "dte": lambda r: (-(r["daysToExpiry"] if r["daysToExpiry"] is not None else 1e9)),
    }[sort]
    rows.sort(key=keyfn, reverse=(sort != "dte"))

    near_exp = _mode_str([r["nearExpiry"] for r in rows])
    dte = next((r["daysToExpiry"] for r in rows
                if r["nearExpiry"] == near_exp and r["daysToExpiry"] is not None), None)

    note = None
    if rows and dte is not None and dte > 12:
        note = (f"Near expiry is ~{dte} days out — rollover is naturally low and least "
                "informative early in the cycle; it's sharpest in the expiry week.")

    out = {
        "date": date,
        "rows": rows[:limit],
        "count": len(rows),
        "universe": universe,
        "medianRollover": _median([r["rolloverPct"] for r in rows]),
        "nearExpiry": near_exp,
        "daysToExpiry": dte,
        "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                    "limit": limit, "sort": sort},
        "note": note,
    }
    _cache.update(ts=time.time(), key=key, board=out)
    return out


def rank_map(force=False):
    """{SYMBOL: rollover metrics + cross-sectional rolloverRank / carrying / shedding}
    for EVERY F&O name with a usable near+next future — so the conviction board can fold
    rollover in as a confirmation pillar without its own parse. Unlike `board()` the rank
    is over the WHOLE futures universe (not a price/value-filtered slice) so any pick can
    look up its standing. Cached (15-min TTL). Returns (date, map). Off-hours."""
    with _map_lock:
        if (not force and _map_cache["map"] is not None
                and (time.time() - _map_cache["ts"]) < _MAP_TTL):
            return _map_cache["date"], _map_cache["map"]
    import eod_options
    date, text = eod_options._fo_text(force=force)
    out = {}
    if text:
        import bhavcopy
        rows = []
        for fut in bhavcopy.parse_fo_futures_all(text).values():
            m = _metrics(fut, date)
            if m and m["rolloverPct"] is not None:
                rows.append(m)
        for r in _attach_ranks(rows):
            out[r["symbol"]] = r
    with _map_lock:
        _map_cache.update(ts=time.time(), date=date, map=out)
    return date, out


def status():
    """Freshness of the rollover board cache (no download)."""
    c = _cache
    return {
        "cached": c.get("board") is not None,
        "date": (c["board"] or {}).get("date") if c.get("board") else None,
        "ageSec": round(time.time() - c["ts"], 1) if c.get("ts") else None,
        "ttlSec": _CACHE_TTL,
        "source": "nsearchives FO UDiFF bhavcopy (EOD futures, all expiries)",
    }
