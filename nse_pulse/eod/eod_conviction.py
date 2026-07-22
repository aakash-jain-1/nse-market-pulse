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

from nse_pulse.eod import eod_scanner as _es

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
_SECTOR_W = 14            # weight of the "leading/lagging sector" confirmation pillar
_ROLL_W = 12              # weight of the "futures rollover confirms the view" pillar

# Option-chain fuse (max-pain / PCR / OI walls from the EOD FO bhavcopy).
_OPT_W = 12               # weight of the "option chain confirms the direction" pillar
_OPT_WARN = 8             # conviction shaved per option-chain warning (a soft veto)
_PIN_TOL = 3.0            # % from max-pain beyond which price has a tail/head-wind
_PCR_BULL = 1.20          # PCR at/above this = put-heavy (supportive for longs)
_PCR_BEAR = 0.60          # PCR at/below this = call-heavy (supportive for shorts)

_CR = 1e7                 # ₹1 crore (turnover filter; value is in rupees)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _rp(x):
    """Round a price/strike for a label — int when whole, else 2dp."""
    if x is None:
        return x
    return int(x) if float(x).is_integer() else round(x, 2)


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


def _sector_tag(sector, kind):
    """Label for the sector pillar, e.g. 'IT is a leading sector (#1/12, RS +4)'."""
    rk, tot, rs = sector.get("rank"), sector.get("total"), sector.get("rs")
    tag = f"{sector['sector']} is a {kind} sector"
    if rk and tot:
        extra = f"#{rk}/{tot}"
        if rs is not None:
            extra += f", RS {rs:+.0f}"
        tag += f" ({extra})"
    return tag


def _roll_pillar(roll, want_bullish):
    """The futures-rollover confirmation, or None. `roll` is a symbol's entry from
    `rollover.rank_map()`. It confirms a side only when the name is CARRYING (rollover%
    in the top fifth of the F&O universe today — positions being carried into next month,
    not closed) AND the net near+next OI direction matches the trade side. Rollover is
    cross-sectional, so this is meaningful even away from expiry (sharper in expiry week).
    (label, weight) or None. Pure."""
    if not roll or not roll.get("carrying") or roll.get("bullish") is None:
        return None
    if bool(roll["bullish"]) is not want_bullish:
        return None
    rp, rr = roll.get("rolloverPct"), roll.get("rolloverRank")
    side = "longs" if want_bullish else "shorts"
    rank_txt = f", rank {rr:.0f}" if rr is not None else ""
    return (f"🔄 high rollover {rp:.0f}% — {side} carrying into next month{rank_txt}",
            _ROLL_W)


def _pillars_long(f, oi, deal_side, sector=None, roll=None):
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
    if sector and sector.get("leading"):
        out.append((f"🧭 {_sector_tag(sector, 'leading')}", _SECTOR_W))
    rp = _roll_pillar(roll, want_bullish=True)
    if rp:
        out.append(rp)
    return out


def _pillars_short(f, oi, deal_side, sector=None, roll=None):
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
    if sector and sector.get("lagging"):
        out.append((f"🧭 {_sector_tag(sector, 'lagging')}", _SECTOR_W))
    rp = _roll_pillar(roll, want_bullish=False)
    if rp:
        out.append(rp)
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


def _nearest_wall(walls, entry, above):
    """The closest OI wall on one side of `entry` — the first resistance ABOVE (for a
    long) or support BELOW (for a short) that price must clear on the way to target."""
    cands = [w for w in (walls or [])
             if w.get("strike") is not None and w.get("oi")
             and (w["strike"] >= entry if above else w["strike"] <= entry)]
    if not cands:
        return None
    return min(cands, key=lambda w: w["strike"]) if above else max(cands, key=lambda w: w["strike"])


def _option_overlay(direction, entry, target, opt):
    """Fuse the EOD option chain (max-pain / PCR / OI walls) with a directional pick.

    Returns {maxPain, pcr, wall, confirms:[…], warns:[…]} or None when there's no
    usable chain. `confirms` back the trade (→ one extra pillar); `warns` fight it
    (→ conviction shaved, a transparent soft veto rather than a silent drop):
      • max-pain: near expiry price gravitates to it, so a long UNDER max-pain (short
        OVER it) has a tail-wind; the opposite side is a head-wind.
      • OI wall: a target BEYOND the nearest call (long) / put (short) OI wall must
        punch through heavy option interest — flagged; a wall past the target = room.
      • PCR: a put-heavy chain supports longs, a call-heavy one supports shorts.
    """
    if not opt or not entry:
        return None
    mp, pcr = opt.get("maxPain"), opt.get("pcr")
    long = direction == "LONG"
    confirms, warns = [], []

    if mp:
        gap = (entry - mp) / mp * 100.0            # +ve = above max-pain
        if long and gap <= -_PIN_TOL:
            confirms.append(f"below max-pain ₹{_rp(mp)} (expiry pull ↑)")
        elif long and gap >= _PIN_TOL:
            warns.append(f"{gap:.0f}% above max-pain ₹{_rp(mp)} (expiry pull ↓)")
        elif (not long) and gap >= _PIN_TOL:
            confirms.append(f"above max-pain ₹{_rp(mp)} (expiry pull ↓)")
        elif (not long) and gap <= -_PIN_TOL:
            warns.append(f"{abs(gap):.0f}% below max-pain ₹{_rp(mp)} (expiry pull ↑)")

    wall = _nearest_wall(opt.get("resistance") if long else opt.get("support"),
                         entry, above=long)
    if wall and target:
        ws = wall["strike"]
        if (long and target > ws) or ((not long) and target < ws):
            warns.append(f"target runs into {'call' if long else 'put'} OI wall ₹{_rp(ws)}")
        else:
            confirms.append(f"room to {'call' if long else 'put'} OI wall ₹{_rp(ws)}")

    if pcr is not None:
        if long and pcr >= _PCR_BULL:
            confirms.append(f"PCR {pcr} (put-heavy → support)")
        elif (not long) and pcr <= _PCR_BEAR:
            confirms.append(f"PCR {pcr} (call-heavy → resistance)")

    return {"maxPain": mp, "pcr": pcr, "wall": (wall["strike"] if wall else None),
            "confirms": confirms, "warns": warns}


def _apply_weights(pillars, weights):
    """Scale each pillar's SCORING weight by its calibration-derived multiplier
    (adaptive weighting). The confirmation COUNT is deliberately left untouched — only
    the blended score shifts, so a proven pillar re-orders names WITHIN a confirmation
    tier without ever letting one weighted signal jump the stacking discipline."""
    if not weights:
        return pillars
    from nse_pulse.eod import conviction_calibration as _cc
    out = []
    for lbl, w in pillars:
        key = _cc.pillar_of(lbl)
        out.append((lbl, w * (weights.get(key, 1.0) if key else 1.0)))
    return out


def _pick(f, bars, oi, deal_side, deal_list, sector=None, opt=None, roll=None,
          weights=None):
    """Build the best (LONG or SHORT) conviction pick for one name, or None.
    Chooses the side with more confirming pillars (higher score breaks ties).
    `roll` (optional, a symbol's `rollover.rank_map()` entry) adds a futures-rollover
    pillar; `weights` (optional {pillar_key: mult}) applies adaptive scoring — see board()."""
    longs = _apply_weights(_pillars_long(f, oi, deal_side, sector, roll), weights)
    shorts = _apply_weights(_pillars_short(f, oi, deal_side, sector, roll), weights)
    if not longs and not shorts:
        return None
    lscore, sscore = sum(w for _, w in longs), sum(w for _, w in shorts)
    if (len(longs), lscore) >= (len(shorts), sscore):
        direction, pillars, raw = "LONG", longs, lscore
    else:
        direction, pillars, raw = "SHORT", shorts, sscore
    plan = _plan(f.get("close"), direction, bars)

    # Fuse the EOD option chain: a confirming chain adds a pillar; a conflicting one
    # (target through an OI wall / pinned against max-pain) shaves conviction.
    reasons = [lbl for lbl, _ in pillars]
    confirmations = len(pillars)
    ov = _option_overlay(direction, f.get("close"), plan.get("target"), opt)
    if ov and ov["confirms"]:
        reasons.append("🎯 option chain: " + "; ".join(ov["confirms"][:2]))
        confirmations += 1
        raw += _OPT_W * (weights.get("option", 1.0) if weights else 1.0)
    warnings = list(ov["warns"]) if ov else []
    raw -= _OPT_WARN * len(warnings)
    conviction = round(_clip(raw, 0, 100), 1)
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
        "sector": ({"name": sector["sector"], "rank": sector.get("rank"),
                    "total": sector.get("total"), "rs": sector.get("rs"),
                    "strength": sector.get("strength"),
                    "leading": sector.get("leading"),
                    "lagging": sector.get("lagging")}
                   if sector else None),
        "options": ({"maxPain": ov["maxPain"], "pcr": ov["pcr"], "wall": ov["wall"],
                     "confirms": ov["confirms"], "warns": ov["warns"]}
                    if ov else None),
        "conviction": conviction,
        "confirmations": confirmations,
        "reasons": reasons,
        "warnings": warnings,
        "rating": _rating(conviction, confirmations),
        **plan,
    }


# ---------------------------------------------------------------------------
# board (impure: reads db.eod_bars / db.eod_oi / deals)
# ---------------------------------------------------------------------------
def board(limit=25, min_price=20.0, min_value_cr=2.0, min_pillars=2,
          with_deals=True, fno_only=False, lookback=_es.LOOKBACK,
          with_options=True, with_rollover=True, adaptive=False):
    """Rank the whole ingested EOD universe by STACKED conviction.

    A name makes the board only when at least `min_pillars` INDEPENDENT signals agree
    (breakout / delivery / volume / trend / OI buildup / bulk-deal / leading sector /
    option chain / futures rollover). Rows are sorted by confirmations first, then the
    blended conviction score. Needs no network beyond one tiny cached deals CSV (when
    `with_deals`); the option + rollover fuses reuse one cached FO-bhavcopy parse.

    `adaptive` feeds the confirmation-calibration back into scoring: each pillar's
    weight is nudged by its measured realized edge (win-rate lift), so pillars that
    have actually worked count for more. The confirmation COUNT (the primary sort key)
    is untouched — weighting only re-orders within a tier — and multipliers are
    neutral until a pillar has enough resolved history. Returns
    {date, longs, shorts, count, universe, scanned, filters, coverage,
    adaptive, adaptiveWeights, note}.
    """
    from nse_pulse.core import db
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

    # Sector relative-strength map (one pass over the same bars): a leading sector
    # is an extra LONG pillar, a lagging sector an extra SHORT pillar.
    smap, _ss = {}, None
    try:
        from nse_pulse.eod import sector_scan as _ss
        smap = _ss.strength_map(grouped, min_price, min_value_cr)
    except Exception:
        log.warning("board: sector-strength cross-reference failed", exc_info=True)
        _ss = None

    # Option-chain map (one parse of the FO bhavcopy): max-pain / PCR / OI walls per
    # F&O name → confirm or (soft-)veto a directional pick. Best-effort / off-hours.
    omap = {}
    if with_options:
        try:
            from nse_pulse.eod import eod_options
            _, omap = eod_options.oi_map()
        except Exception:
            log.warning("board: option-chain fuse failed", exc_info=True)
            omap = {}

    # Rollover map (near→next futures OI shift from the same FO bhavcopy): a name
    # CARRYING its positions into next month, on the trade's side, is an extra pillar.
    rmap = {}
    if with_rollover:
        try:
            from nse_pulse.eod import rollover
            _, rmap = rollover.rank_map()
        except Exception:
            log.warning("board: rollover fuse failed", exc_info=True)
            rmap = {}

    # Adaptive scoring weights from the confirmation calibration (best-effort): each
    # pillar's realized win-rate lift → a clamped, sample-shrunk scoring multiplier.
    weights = None
    if adaptive:
        try:
            from nse_pulse.eod import conviction_calibration as _cc
            weights = _cc.pillar_weights()
        except Exception:
            log.warning("board: adaptive-weight lookup failed", exc_info=True)
            weights = None

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
        sctx = _ss.context(smap, sym) if (_ss and smap) else None
        pick = _pick(f, bars, oi, _deal_side(dlist), dlist, sctx, omap.get(sym),
                     rmap.get(sym), weights)
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
        "withOptions": bool(omap),
        "withRollover": bool(rmap),
        "adaptive": bool(adaptive),
        "adaptiveWeights": (weights if adaptive else None),
        "filters": {"minPrice": min_price, "minValueCr": min_value_cr,
                    "minPillars": min_pillars, "fnoOnly": bool(fno_only),
                    "withDeals": bool(with_deals), "withOptions": bool(with_options),
                    "withRollover": bool(with_rollover),
                    "adaptive": bool(adaptive), "limit": limit},
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
        "reasons": ["🏆 EOD conviction (%d signals)" % p["confirmations"], *p["reasons"],
                    *[f"⚠️ {w}" for w in (p.get("warnings") or [])]],
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
    from nse_pulse.core import db
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
