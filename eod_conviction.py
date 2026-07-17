"""
eod_conviction.py — the EOD "conviction board" (tomorrow's watchlist).

WHY THIS EXISTS
---------------
We now compute a lot of INDEPENDENT end-of-day signals market-wide: breakouts of
the N-day high, delivery% accumulation, bulk/block deal footprints, and F&O OI
build-ups. Each on its own is noisy. But when SEVERAL line up on the same name —
a stock breaking out, on real delivery-based buying, with a bulk-deal print AND a
long OI build-up — that's a genuinely high-conviction setup worth a look tomorrow.

This module FUSES those signals into ONE ranked board. The key idea is
**confirmation stacking**: a pick is ranked first by how many independent pillars
fired, then by the blended score. A 4-pillar name beats a 1-pillar name even if
the single signal is strong — because agreement across independent evidence is
what actually raises the odds.

DESIGN
------
- The pillar logic (`_pillars_long` / `_pillars_short`), OI classification
  (`_oi_state`) and the trade plan (`_plan`) are PURE and unit-tested with
  hand-built feature dicts — no DB, no network.
- `board()` is the only impure part: it pulls the whole ingested universe's daily
  bars from `db.eod_bars_all` (one query — the same source the EOD scanner uses),
  the near-month OI series from `db.eod_oi_all`, and the latest bulk/block deals
  from `deals`, then runs the pure pipeline over every name. Works OFF-HOURS.
- Per-symbol price features are reused from `eod_scanner._features`, so breakout /
  delivery / volume / trend maths stay defined in exactly one place.
- `save()` writes the board into the `ideas` table (dated to the EOD session) so it
  shows up in the Ideas history as a durable watchlist — WITHOUT clobbering any
  live intraday idea already tracked for that (day, symbol, direction).

Educational / research — NOT investment advice.
"""

import logging
from datetime import datetime, timedelta, timezone

import eod_scanner as _es

log = logging.getLogger("eod_conviction")

IST = timezone(timedelta(hours=5, minutes=30))

# A pillar only counts as "confirming" past these floors.
_BREAKOUT_NEAR = -1.5     # within 1.5% of (or above) the N-day high = breakout pillar
_BREAKDOWN_NEAR = 1.5     # within 1.5% of (or below) the N-day low  = breakdown pillar
_DELIV_HOT = 60.0         # delivery% at/above this = real accumulation
_DELIV_SPIKE = 12.0       # delivery% jump vs its own average = a spike
_DELIV_WEAK = 35.0        # delivery% below this on a down day = churn/distribution
_VOL_HOT = 1.5            # today's volume vs its trailing average
_OI_BUILD = 8.0           # near-month OI change% that counts as a meaningful build
_MIN_WINDOW = 10          # need this much history before trusting a "breakout"

_CR = 1e7                 # ₹1 crore (turnover filter; value is in rupees)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _oi_state(oi_rows, price_up):
    """Classify the latest near-month OI move into a buildup label + magnitude.

    Uses the freshest row carrying both `oi` and `changeOi`. Returns
    {oiPct, label, bullish} or None when there's no usable OI. Pairing OI
    direction with the day's price direction gives the classic read:
      price ↑ + OI ↑ → long buildup (bullish)   price ↓ + OI ↑ → short buildup (bearish)
      price ↑ + OI ↓ → short covering (bullish)  price ↓ + OI ↓ → long unwinding (bearish)
    """
    row = None
    for r in reversed(oi_rows or []):
        if r.get("oi") is not None and r.get("changeOi") is not None:
            row = r
            break
    if not row:
        return None
    oi, chg = row["oi"], row["changeOi"]
    prev = oi - chg
    oi_pct = (chg / prev * 100.0) if prev else None
    if oi_pct is None:
        return None
    oi_up = chg > 0
    if price_up and oi_up:
        label, bullish = "long buildup", True
    elif (not price_up) and oi_up:
        label, bullish = "short buildup", False
    elif price_up and (not oi_up):
        label, bullish = "short covering", True
    else:
        label, bullish = "long unwinding", False
    return {"oiPct": round(oi_pct, 1), "label": label, "bullish": bullish}


def _deal_side(deals_for_sym):
    """Net a symbol's deals to a single BUY/SELL/None (by summed quantity)."""
    if not deals_for_sym:
        return None
    net = 0.0
    for d in deals_for_sym:
        q = d.get("qty") or 0
        net += q if d.get("side") == "BUY" else -q if d.get("side") == "SELL" else 0
    if net > 0:
        return "BUY"
    if net < 0:
        return "SELL"
    return None


def _pillars_long(f, oi, deal_side):
    """Independent bullish confirmations for a name. Returns [(label, weight), …].
    Each entry is one 'pillar'; the board ranks by how many fired, then by weight."""
    out = []
    wd = f.get("windowDays") or 0
    pfh = f.get("pctFromHigh")
    if pfh is not None and wd >= _MIN_WINDOW and pfh >= _BREAKOUT_NEAR:
        if pfh >= 0:
            out.append((f"breakout — at/above {wd}d high", 28 + _clip(pfh, 0, 8)))
        else:
            out.append((f"coiling just under the {wd}d high", 18))
    if f.get("trend") == "up":
        out.append(("uptrend (close > 20 > 50-DMA)", 16))
    dp, dv = f.get("delivPct"), f.get("delivVsAvg")
    if dp is not None and dp >= _DELIV_HOT:
        w = 18 + (6 if (dv is not None and dv >= _DELIV_SPIKE) else 0)
        spike = f" (+{dv:.0f}pp vs avg)" if (dv is not None and dv >= _DELIV_SPIKE) else ""
        out.append((f"delivery {dp:.0f}% — real accumulation{spike}", w))
    vm = f.get("volMult")
    if vm is not None and vm >= _VOL_HOT:
        out.append((f"{vm:.1f}x average volume", 8 + _clip((vm - 1.5) * 3, 0, 8)))
    if oi and oi["bullish"] and oi["oiPct"] >= _OI_BUILD and oi["label"] == "long buildup":
        out.append((f"F&O long buildup (OI +{oi['oiPct']:.0f}%)", 18))
    elif oi and oi["label"] == "short covering" and abs(oi["oiPct"]) >= _OI_BUILD:
        out.append((f"F&O short covering (OI {oi['oiPct']:.0f}%)", 12))
    if deal_side == "BUY":
        out.append(("🐋 bulk/block BUY (institutional print)", 16))
    return out


def _pillars_short(f, oi, deal_side):
    """Independent bearish confirmations for a name. Mirror of `_pillars_long`."""
    out = []
    wd = f.get("windowDays") or 0
    pfl = f.get("pctFromLow")
    if pfl is not None and wd >= _MIN_WINDOW and pfl <= _BREAKDOWN_NEAR:
        if pfl <= 0:
            out.append((f"breakdown — at/below {wd}d low", 28 + _clip(-pfl, 0, 8)))
        else:
            out.append((f"hovering just above the {wd}d low", 18))
    if f.get("trend") == "down":
        out.append(("downtrend (close < 20 < 50-DMA)", 16))
    dp = f.get("delivPct")
    if dp is not None and dp < _DELIV_WEAK and (f.get("pChange") or 0) < 0:
        out.append((f"weak delivery {dp:.0f}% on a down day (churn)", 8))
    vm = f.get("volMult")
    if vm is not None and vm >= _VOL_HOT:
        out.append((f"{vm:.1f}x average volume", 8 + _clip((vm - 1.5) * 3, 0, 8)))
    if oi and (not oi["bullish"]) and oi["oiPct"] >= _OI_BUILD and oi["label"] == "short buildup":
        out.append((f"F&O short buildup (OI +{oi['oiPct']:.0f}%)", 18))
    if deal_side == "SELL":
        out.append(("🐋 bulk/block SELL (institutional print)", 16))
    return out


def _avg_range_pct(bars, win=14):
    """Average daily (high−low)/close % over the last `win` bars — a cheap ATR%
    proxy for sizing the stop/target. Falls back to a sane default when thin."""
    vals = []
    for b in bars[-win:]:
        h, l, c = b.get("high"), b.get("low"), b.get("close")
        if h is not None and l is not None and c:
            vals.append((h - l) / c * 100.0)
    return (sum(vals) / len(vals)) if vals else None


def _plan(close, direction, bars):
    """A volatility-scaled entry/stop/target (2R) so a saved pick is a real idea.
    Stop ≈ 1.3× the stock's recent daily range (floored 3%, capped 9%)."""
    if not close or close <= 0:
        return {}
    atr = _avg_range_pct(bars) or 4.0
    stop_pct = round(_clip(atr * 1.3, 3.0, 9.0), 2)
    tgt_pct = round(stop_pct * 2.0, 2)              # fixed 2:1 reward:risk
    if direction == "LONG":
        stop = round(close * (1 - stop_pct / 100), 2)
        target = round(close * (1 + tgt_pct / 100), 2)
    else:
        stop = round(close * (1 + stop_pct / 100), 2)
        target = round(close * (1 - tgt_pct / 100), 2)
    return {"entry": round(close, 2), "stop": stop, "target": target,
            "stopPct": stop_pct, "targetPct": tgt_pct, "rr": 2.0}


def _rating(conviction, confirmations):
    if confirmations >= 3 and conviction >= 60:
        return "High"
    if confirmations >= 2 and conviction >= 40:
        return "Medium"
    return "Low"


def _pick(f, bars, oi, deal_side, deal_list):
    """Build the best (LONG or SHORT) conviction pick for one name, or None.
    Chooses the side with more confirming pillars (higher score breaks ties)."""
    longs = _pillars_long(f, oi, deal_side)
    shorts = _pillars_short(f, oi, deal_side)
    if not longs and not shorts:
        return None
    lscore, sscore = sum(w for _, w in longs), sum(w for _, w in shorts)
    if (len(longs), lscore) >= (len(shorts), sscore):
        direction, pillars, raw = "LONG", longs, lscore
    else:
        direction, pillars, raw = "SHORT", shorts, sscore
    conviction = round(_clip(raw, 0, 100), 1)
    plan = _plan(f.get("close"), direction, bars)
    return {
        "symbol": f.get("symbol"),
        "direction": direction,
        "date": f.get("date"),
        "close": f.get("close"),
        "ltp": f.get("close"),
        "pChange": f.get("pChange"),
        "delivPct": f.get("delivPct"),
        "delivVsAvg": f.get("delivVsAvg"),
        "volMult": round(f["volMult"], 1) if f.get("volMult") else None,
        "value": f.get("value"),
        "oi": oi,
        "deal": ({"side": deal_side,
                  "client": (deal_list[0].get("client") if deal_list else None)}
                 if deal_side else None),
        "conviction": conviction,
        "confirmations": len(pillars),
        "reasons": [lbl for lbl, _ in pillars],
        "rating": _rating(conviction, len(pillars)),
        **plan,
    }


# ---------------------------------------------------------------------------
# board (impure: reads db.eod_bars / db.eod_oi / deals)
# ---------------------------------------------------------------------------
def board(limit=25, min_price=20.0, min_value_cr=2.0, min_pillars=2,
          with_deals=True, fno_only=False, lookback=_es.LOOKBACK):
    """Rank the whole ingested EOD universe by STACKED conviction.

    A name makes the board only when at least `min_pillars` INDEPENDENT signals
    agree (breakout / delivery / volume / trend / OI buildup / bulk-deal). Rows are
    sorted by confirmations first, then the blended conviction score. Needs no
    network beyond one tiny cached deals CSV (when `with_deals`). Returns
    {date, longs, shorts, count, universe, scanned, filters, coverage, note}.
    """
    import db
    try:
        limit = _clip(int(limit), 1, 100)
    except (TypeError, ValueError):
        limit = 25
    try:
        min_pillars = _clip(int(min_pillars), 1, 6)
    except (TypeError, ValueError):
        min_pillars = 2

    latest = db.eod_latest_date()
    grouped = db.eod_bars_all(since=_es._since(latest, lookback))
    oi_all = db.eod_oi_all(since=_es._since(latest, lookback))
    deal_map = _es._deal_map(with_deals)

    fno = None
    if fno_only:
        try:
            fno = set(db.eod_oi_symbols()) or None
        except Exception:
            fno = None

    min_val = (min_value_cr or 0) * _CR
    picks, scanned = [], 0
    for sym, bars in grouped.items():
        if fno is not None and sym not in fno:
            continue
        f = _es._features(bars)
        if not f:
            continue
        scanned += 1
        if min_price and f["close"] < min_price:
            continue
        if min_val and (f.get("value") or 0) < min_val:
            continue
        price_up = (f.get("pChange") or 0) >= 0
        oi = _oi_state(oi_all.get(sym), price_up)
        dlist = deal_map.get(sym) or []
        pick = _pick(f, bars, oi, _deal_side(dlist), dlist)
        if not pick or pick["confirmations"] < min_pillars:
            continue
        picks.append(pick)

    picks.sort(key=lambda p: (p["confirmations"], p["conviction"]), reverse=True)
    longs = [p for p in picks if p["direction"] == "LONG"][:limit]
    shorts = [p for p in picks if p["direction"] == "SHORT"][:limit]

    return {
        "date": latest,
        "longs": longs,
        "shorts": shorts,
        "count": len(longs) + len(shorts),
        "universe": len(grouped),
        "scanned": scanned,
        "withDeals": bool(deal_map),
        "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                    "minPillars": min_pillars, "fnoOnly": bool(fno_only),
                    "withDeals": bool(with_deals), "limit": limit},
        "coverage": _es.status(),
        "note": None if grouped else (
            "No EOD history yet — load it first (⬇ Backfill history on the EOD "
            "Scan tab) so the conviction board has data."),
    }


# ---------------------------------------------------------------------------
# persistence → ideas table (durable watchlist in the Ideas history)
# ---------------------------------------------------------------------------
def _to_idea_row(p, day, now):
    """Map a conviction pick to an `ideas` record. Reasons are prefixed so it's
    unmistakably the EOD conviction board (not a live intraday idea)."""
    m0 = 0.0
    return {
        "day": day, "symbol": p["symbol"], "direction": p["direction"],
        "entry": p.get("entry"), "stop": p.get("stop"), "target": p.get("target"),
        "stopPct": p.get("stopPct"), "targetPct": p.get("targetPct"), "rr": p.get("rr"),
        "conviction": p.get("conviction"), "rating": p.get("rating"),
        "reasons": ["🏆 EOD conviction (%d signals)" % p["confirmations"], *p["reasons"]],
        "fno": bool(p.get("oi")), "pChange": p.get("pChange"),
        "firstSeenAt": now, "lastSeenAt": now, "ltp": p.get("close"),
        "movePct": m0, "maxMovePct": m0, "minMovePct": m0,
        "outcome": None, "outcomeAt": None, "outcomePct": None,
    }


def save(b=None, **kw):
    """Persist a board's picks into the `ideas` table, dated to the EOD session so
    they surface in the Ideas history as a durable watchlist. NEVER clobbers an
    existing (day, symbol, direction) record (e.g. a tracked live idea) — those are
    skipped. Returns {saved, skipped, day}."""
    import db
    if b is None:
        b = board(**kw)
    day = (b.get("date") or _today())[:10]
    now = _now()
    existing = {(r["symbol"], r["direction"]) for r in db.ideas_for_day(day)}
    rows, skipped = [], 0
    for p in (b.get("longs") or []) + (b.get("shorts") or []):
        if (p["symbol"], p["direction"]) in existing:
            skipped += 1
            continue
        rows.append(_to_idea_row(p, day, now))
    db.ideas_upsert(rows)
    return {"saved": len(rows), "skipped": skipped, "day": day}
