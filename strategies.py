"""
Strategy library + market-regime detector
==========================================
A named library of trade-idea generators, so the simulator can run several
strategies IN PARALLEL and we can learn — day by day — which one works in which
market regime (momentum shines on trend days, mean-reversion on recovery days,
etc.). Educational signal summaries — NOT investment advice.

Design
------
- `build_context()` fetches the shared live-data bundle ONCE (scanner, gainers/
  losers, OI spurts, futures, volume-gainers, most-active, index snapshot, PCR)
  so all strategies reuse it instead of each hammering NSE.
- `detect_regime(ctx, prior_day_move)` tags the day: Trend-Up / Trend-Down /
  Recovery / Range / Mixed, using NIFTY %change + advance-decline breadth (+ a
  prior-day move for "recovery").
- Each strategy is `{id, name, description, regimeFit, generate}` where
  `generate(ctx) -> list[idea]` and every idea shares the same shape as
  `nse_client._build_idea` (symbol / direction / entry / stop / target /
  conviction / rating / reasons ...), so the sim treats them uniformly.
"""

import time
from datetime import datetime, timedelta, timezone

import nse_client as nse

_IST = timezone(timedelta(hours=5, minutes=30))

# Session-scoped daily-bar cache for the squeeze / prior-day-level strategies.
# Prior sessions are immutable intraday, so a symbol's recent bars are fetched at
# most ONCE per trading day and reused across every build_context cycle (nearly
# free after the first). symbol -> (yyyy-mm-dd, ascending bars).
_daily_cache = {}
_DAILY_BARS = 12            # recent completed sessions we keep (NR7 needs 7)

# Short-TTL per-stock option-chain cache (PCR / max-pain strategies). Chains are
# heavy, so we bound the candidate set and refresh at most every few minutes.
# symbol -> (epoch, {pcr, maxPain, underlying, dte}).
_chain_cache = {}
_CHAIN_TTL_S = 300
_CHAIN_MAX = 10             # at most this many chains fetched per cold cycle


def _today_ist():
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _dte(expiry):
    """Calendar days to an NSE expiry string like '31-Jul-2026' (>=0), or None."""
    try:
        exp = datetime.strptime(expiry, "%d-%b-%Y").replace(tzinfo=_IST)
        return max(0, (exp - datetime.now(_IST)).days)
    except Exception:
        return None


def _load_daily(symbols):
    """Recent completed daily bars for a candidate set, session-cached (immutable
    intraday). Cold once/day per symbol; parallelised and fully best-effort."""
    today = _today_ist()
    stale = [s for s in symbols if _daily_cache.get(s, (None,))[0] != today]
    if stale:
        try:
            import concurrent.futures as cf

            def _h(s):
                try:
                    bars = nse.get_stock_history(s, chunks=1, chunk_days=25)
                    return s, (bars[-_DAILY_BARS:] if bars else [])
                except Exception:
                    return s, []

            with cf.ThreadPoolExecutor(max_workers=8) as ex:
                for s, bars in ex.map(_h, stale):
                    if bars:
                        _daily_cache[s] = (today, bars)
        except Exception:
            pass
    return {s: _daily_cache[s][1] for s in symbols
            if _daily_cache.get(s, (None,))[0] == today and _daily_cache[s][1]}


def _load_chains(ctx):
    """Per-stock option-chain sentiment (PCR / max-pain / spot / dte) for a small,
    liquid F&O subset, TTL-cached. Bounded + best-effort so it never stalls the
    per-minute snapshot loop."""
    # Prefer F&O scanner names (they have option chains) by descending score.
    cand, seen = [], set()
    for r in ctx.get("scanner", []):
        s = r.get("symbol")
        if s and s not in seen and (r.get("fno") or r.get("oiKind") or r.get("basisPct") is not None):
            seen.add(s)
            cand.append(s)
        if len(cand) >= _CHAIN_MAX:
            break
    now = time.time()
    stale = [s for s in cand if now - _chain_cache.get(s, (0,))[0] > _CHAIN_TTL_S]
    if stale:
        try:
            import concurrent.futures as cf
            import nse_quote

            def _oc(s):
                try:
                    oc = nse_quote.get_option_chain(s)
                    return s, {"pcr": oc.get("pcr"), "maxPain": oc.get("maxPain"),
                               "underlying": oc.get("underlying"),
                               "dte": _dte(oc.get("expiry"))}
                except Exception:
                    return s, None

            with cf.ThreadPoolExecutor(max_workers=6) as ex:
                for s, d in ex.map(_oc, stale):
                    if d and d.get("underlying"):
                        _chain_cache[s] = (now, d)
        except Exception:
            pass
    return {s: _chain_cache[s][1] for s in cand if s in _chain_cache}


# ----------------------------------------------------------------------------
# Shared context + regime
# ----------------------------------------------------------------------------
def build_context(fno_only=False):
    """Fetch every live list ONCE and bundle it for all strategy generators."""
    ctx = {"fnoOnly": fno_only}

    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    ctx["scanner"] = safe(lambda: nse.get_scanner(fno_only=fno_only, limit=250), [])
    ctx["gainers"] = safe(lambda: nse.get_variations("gainers", 40), [])
    ctx["losers"] = safe(lambda: nse.get_variations("losers", 40), [])
    ctx["volgainers"] = safe(lambda: nse.get_volume_gainers(40), [])
    ctx["oispurts"] = safe(lambda: nse.get_oi_spurts(60), [])
    ctx["futures"] = safe(lambda: nse.get_futures(60), [])
    ctx["value"] = safe(lambda: nse.get_most_active("value", 30), [])
    ctx["index"] = safe(nse.get_index_snapshot, {})

    # NIFTY PCR is a nice regime tint but optional (heavier option-chain call).
    ctx["niftyPcr"] = None
    try:
        import nse_quote
        oc = nse_quote.get_option_chain("NIFTY")
        ctx["niftyPcr"] = oc.get("pcr")
    except Exception:
        pass

    ctx["scannerSyms"] = {r["symbol"] for r in ctx["scanner"] if r.get("symbol")}

    # Per-symbol quotes for the quote-driven strategies (VWAP, 52-week-high,
    # delivery). Fetched ONCE here for a bounded, liquid candidate set and shared
    # via ctx["quotes"] so those three strategies don't each hit NSE per name.
    cand, seen = [], set()
    for src, n in ((ctx["scanner"], 30), (ctx["gainers"], 15), (ctx["losers"], 10)):
        for r in src[:n]:
            s = r.get("symbol")
            if s and s not in seen:
                seen.add(s)
                cand.append(s)
    cand = cand[:45]

    quotes = {}
    try:
        import concurrent.futures as cf
        import nse_quote

        def _q(s):
            try:
                return s, nse_quote.get_quote(s)
            except Exception:
                return s, None

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for s, q in ex.map(_q, cand):
                if q and q.get("ltp"):
                    quotes[s] = q
    except Exception:
        pass
    ctx["quotes"] = quotes

    # Intraday 5-min OHLCV for the same candidate set — powers the candle-based
    # strategies (Opening-Range Breakout, intraday VWAP). 5-min keeps payloads
    # light while still capturing the opening range (09:15-09:30) and VWAP path.
    # NOTE: candles are intentionally NOT stored in context_log (see
    # snapshot_logger._trim_context), so these strategies run in the live
    # forward-sim but are inert in the offline backtest.
    candles = {}
    try:
        import concurrent.futures as cf
        import nse_quote

        def _c(s):
            try:
                d = nse_quote.get_ohlc(s, interval=5)
                return s, (d.get("points") or []) if not d.get("error") else []
            except Exception:
                return s, []

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for s, pts in ex.map(_c, cand):
                if pts:
                    candles[s] = pts
    except Exception:
        pass
    ctx["candles"] = candles

    # Recent daily bars (session-cached) for the volatility-squeeze / prior-day
    # high-low strategies, and per-stock option-chain sentiment (PCR / max-pain)
    # for a small F&O subset. Both bounded + cached so they add ~no steady load.
    ctx["daily"] = _load_daily(cand)
    ctx["chains"] = _load_chains(ctx)
    return ctx


# India-VIX volatility bands. These are the conventional NSE readings: a sub-13
# print is a calm/complacent tape, 13-18 is the normal working range, and 18+ is
# elevated/fearful (spikes toward 20+ around events). Kept as a SEPARATE axis
# from the directional label so per-regime sample sizes (and the leaderboard /
# walk-forward keys) stay stable — volState is an extra tint, not a new label.
_VIX_CALM = 13.0
_VIX_ELEVATED = 18.0


def _vol_state(vix):
    """India VIX level -> coarse volatility regime (Calm / Normal / Elevated)."""
    if vix is None:
        return None
    if vix < _VIX_CALM:
        return "Calm"
    if vix < _VIX_ELEVATED:
        return "Normal"
    return "Elevated"


def _vix_pctile(vix, lo, hi):
    """Where today's VIX sits in its own 52-week range (0-100). Robust to the
    fact that 'high' VIX drifts over time — a percentile self-calibrates."""
    if vix is None or lo is None or hi is None or hi <= lo:
        return None
    return round(max(0.0, min(1.0, (vix - lo) / (hi - lo))) * 100, 1)


def detect_regime(ctx, prior_day_move=None):
    """
    Classify today's market regime from the index snapshot + breadth (+ the
    prior session's move for 'Recovery'), plus a volatility axis from India VIX.
    Cheap: only needs ctx['index']. The directional `label` is unchanged (6
    buckets); `volState`/`vix`/`vixPctile` add an orthogonal volatility read
    used by the board and, later, for vol-conditioned strategy selection.
    """
    idx = (ctx.get("index") or {}).get("NIFTY") or {}
    bank = (ctx.get("index") or {}).get("BANKNIFTY") or {}
    vixrow = (ctx.get("index") or {}).get("INDIAVIX") or {}
    today = idx.get("pChange")
    adv = idx.get("advances") or 0
    dec = idx.get("declines") or 0
    pcr = ctx.get("niftyPcr")
    vix = vixrow.get("last")
    vix_pctile = _vix_pctile(vix, vixrow.get("yearLow"), vixrow.get("yearHigh"))
    vol_state = _vol_state(vix)

    if today is None:
        label = "Unknown"
    elif prior_day_move is not None and prior_day_move <= -1.0 and today >= 0.3:
        label = "Recovery"
    elif prior_day_move is not None and prior_day_move >= 1.0 and today <= -0.3:
        label = "Pullback"
    elif today >= 0.6 and adv >= dec:
        label = "Trend-Up"
    elif today <= -0.6 and dec >= adv:
        label = "Trend-Down"
    elif abs(today) <= 0.4:
        label = "Range"
    else:
        label = "Mixed"

    bits = []
    if today is not None:
        bits.append(f"NIFTY {today:+.2f}%")
    if prior_day_move is not None:
        bits.append(f"prev {prior_day_move:+.2f}%")
    if adv or dec:
        bits.append(f"breadth {int(adv)}:{int(dec)}")
    if vix is not None:
        vp = f" p{vix_pctile:.0f}" if vix_pctile is not None else ""
        bits.append(f"VIX {vix:.2f}{vp} {vol_state}")
    if pcr is not None:
        bits.append(f"PCR {pcr}")

    return {
        "label": label,
        "niftyPct": today,
        "bankPct": bank.get("pChange"),
        "priorDayMove": prior_day_move,
        "breadthAdv": adv,
        "breadthDec": dec,
        "pcr": pcr,
        "vix": vix,
        "vixPctile": vix_pctile,
        "volState": vol_state,
        "note": " · ".join(bits),
    }


# ----------------------------------------------------------------------------
# Idea helper
# ----------------------------------------------------------------------------
def _rate(conviction):
    return "High" if conviction >= 66 else "Medium" if conviction >= 40 else "Low"


def _mk_idea(symbol, direction, ltp, conviction, reasons,
             stop_pct, tgt_pct, fno=False, extra=None):
    """Assemble one idea with a risk plan, matching _build_idea's shape."""
    if ltp is None or ltp <= 0:
        return None
    conviction = int(max(1, min(conviction, 99)))
    if direction == "LONG":
        stop = ltp * (1 - stop_pct / 100)
        target = ltp * (1 + tgt_pct / 100)
    else:
        stop = ltp * (1 + stop_pct / 100)
        target = ltp * (1 - tgt_pct / 100)
    idea = {
        "symbol": symbol,
        "direction": direction,
        "conviction": conviction,
        "rating": _rate(conviction),
        "ltp": round(ltp, 2),
        "entry": round(ltp, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "stopPct": round(stop_pct, 2),
        "targetPct": round(tgt_pct, 2),
        "rr": round(tgt_pct / stop_pct, 1) if stop_pct else None,
        "reasons": reasons,
        "fno": fno,
    }
    if extra:
        idea.update(extra)
    return idea


# ----------------------------------------------------------------------------
# Strategy generators
# ----------------------------------------------------------------------------
def gen_momentum(ctx):
    """A — Multi-Signal Momentum: the original engine over the scanner board."""
    ideas = []
    for e in ctx.get("scanner", []):
        idea = nse._build_idea(e)
        if idea:
            ideas.append(idea)
    return ideas


def gen_oi_smartmoney(ctx):
    """
    B — F&O OI Smart-Money: pure derivatives positioning. LONG on long-buildup /
    short-covering, SHORT on short-buildup / long-unwinding, weighted by the size
    of the OI change (not by price momentum). Surfaces institutional intent.
    """
    ideas = []
    seen = set()
    for r in ctx.get("oispurts", []):
        sym = r.get("symbol")
        kind = r.get("signalKind")
        ltp = r.get("ltp")
        if not sym or sym in seen or ltp is None or kind == "neutral":
            continue
        oi_pct = r.get("oiPctChange")
        if oi_pct is None:
            latest, chg = r.get("latestOI"), r.get("changeInOI")
            oi_pct = (chg / (latest - chg) * 100) if latest and chg else 0
        direction = "LONG" if kind == "buildup" else "SHORT"
        # Conviction: OI change magnitude, boosted when price confirms.
        conv = min(abs(oi_pct or 0) * 1.5, 80)
        pc = r.get("pChange")
        if pc is not None and ((direction == "LONG" and pc > 0) or
                               (direction == "SHORT" and pc < 0)):
            conv += min(abs(pc) * 3, 19)
        reasons = [r.get("signal") or "OI signal"]
        if oi_pct:
            reasons.append(f"OI {oi_pct:+.0f}% vs prev")
        if pc is not None:
            reasons.append(f"Underlying {pc:+.2f}% today")
        idea = _mk_idea(sym, direction, ltp, conv, reasons,
                        stop_pct=1.5, tgt_pct=3.0, fno=True,
                        extra={"oiSignal": r.get("signal")})
        if idea:
            ideas.append(idea)
            seen.add(sym)
    return ideas


def gen_meanrev(ctx):
    """
    C — Mean-Reversion Bounce (contrarian): LONG heavily-sold liquid names that
    show a bounce cue (short-covering / OI easing), SHORT over-extended gainers.
    Regime-tilted: leans long on Recovery/Range, trims fades on Trend-Up. Only
    considers liquid scanner names so fills are realistic.
    """
    ideas = []
    liquid = ctx.get("scannerSyms", set())
    regime = (ctx.get("regime") or {}).get("label", "Mixed")
    oi_kind = {}
    for r in ctx.get("oispurts", []):
        if r.get("symbol"):
            oi_kind[r["symbol"]] = r.get("signalKind")

    long_tilt = regime in ("Recovery", "Range", "Trend-Down")
    short_tilt = regime in ("Pullback", "Range", "Trend-Up")

    # Oversold → bounce LONG (contrarian to today's fall).
    for r in sorted(ctx.get("losers", []), key=lambda x: (x.get("pChange") or 0)):
        sym, pc, ltp = r.get("symbol"), r.get("pChange"), r.get("ltp")
        if not sym or pc is None or ltp is None or pc > -2.0:
            continue
        if liquid and sym not in liquid:
            continue
        conv = min(abs(pc) * 6, 70)
        reasons = [f"Oversold: down {pc:.2f}% today", "Mean-reversion bounce play"]
        if oi_kind.get(sym) == "buildup":
            conv += 15
            reasons.append("Short covering / longs building")
        if long_tilt:
            conv += 10
            reasons.append(f"{regime} regime favours bounces")
        idea = _mk_idea(sym, "LONG", ltp, conv, reasons,
                        stop_pct=2.0, tgt_pct=3.0, fno=sym in oi_kind)
        if idea:
            ideas.append(idea)

    # Over-extended → fade SHORT (contrarian to today's rip).
    for r in sorted(ctx.get("gainers", []), key=lambda x: -(x.get("pChange") or 0)):
        sym, pc, ltp = r.get("symbol"), r.get("pChange"), r.get("ltp")
        if not sym or pc is None or ltp is None or pc < 4.0:
            continue
        if liquid and sym not in liquid:
            continue
        conv = min(abs(pc) * 4, 65)
        reasons = [f"Over-extended: up {pc:.2f}% today", "Fade the spike"]
        if oi_kind.get(sym) == "short":
            conv += 15
            reasons.append("Long unwinding / shorts building")
        if short_tilt:
            conv += 10
            reasons.append(f"{regime} regime favours fades")
        idea = _mk_idea(sym, "SHORT", ltp, conv, reasons,
                        stop_pct=2.0, tgt_pct=3.0, fno=sym in oi_kind)
        if idea:
            ideas.append(idea)
    return ideas


def gen_vol_breakout(ctx):
    """
    D — Volume Breakout: only the volume explosions (>=5x the 1-week average) in
    the direction of the move — the classic 'something big just happened' trade.
    Aggressive: tight stop, quick target.
    """
    ideas = []
    seen = set()
    for r in ctx.get("volgainers", []):
        sym = r.get("symbol")
        vm = r.get("week1volChange") or 0
        pc = r.get("pChange")
        ltp = r.get("ltp")
        if not sym or sym in seen or ltp is None or pc is None:
            continue
        if vm < 5 or abs(pc) < 0.5:
            continue
        direction = "LONG" if pc >= 0 else "SHORT"
        conv = min(vm * 2, 60) + min(abs(pc) * 3, 39)
        reasons = [f"Volume ~{vm:.1f}x 1-week average",
                   f"Price {pc:+.2f}% breaking {'up' if pc >= 0 else 'down'}"]
        idea = _mk_idea(sym, direction, ltp, conv, reasons,
                        stop_pct=1.0, tgt_pct=2.5)
        if idea:
            ideas.append(idea)
            seen.add(sym)
    return ideas


def _is_fno(sym):
    try:
        return bool(nse.get_lot_size(sym))
    except Exception:
        return False


def gen_high52w(ctx):
    """
    E — 52-Week-High Momentum: nearness to the 52-week high predicts future
    returns (George & Hwang 2004) better than raw past returns, and doesn't
    reverse. LONG names trading within ~10% of their 52wH; SHORT names hugging
    the 52w low (weaker leg, kept lighter). Positional — suits the 3-session hold.
    """
    ideas = []
    for sym, q in ctx.get("quotes", {}).items():
        ltp, yh, yl = q.get("ltp"), q.get("yearHigh"), q.get("yearLow")
        pc = q.get("pChange")
        if not ltp:
            continue
        if yh and ltp / yh >= 0.90 and (pc is None or pc >= -0.5):
            prox = ltp / yh
            conv = (prox - 0.90) / 0.10 * 60 + 20
            if pc:
                conv += min(abs(pc) * 2, 19)
            reasons = [f"Within {(1 - prox) * 100:.1f}% of 52-week high",
                       "Nearness-to-52wH momentum (George-Hwang)"]
            if pc is not None:
                reasons.append(f"Price {pc:+.2f}% today")
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, 2.0, 4.0, fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
        elif yl and ltp / yl <= 1.05 and (pc is None or pc <= 0.5):
            prox = ltp / yl
            conv = (1.05 - prox) / 0.05 * 40 + 15
            reasons = [f"Within {(prox - 1) * 100:.1f}% of 52-week low",
                       "Breaking down from the lows"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, 2.0, 4.0, fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
    return ideas


def gen_vwap(ctx):
    """
    F — VWAP Trend: VWAP is the institutional intraday benchmark. LONG when price
    holds above VWAP with a green day, SHORT when it's below with a red day.
    Tight 1% stop, 2% target (1:2 intraday).
    """
    ideas = []
    for sym, q in ctx.get("quotes", {}).items():
        ltp, vwap, pc = q.get("ltp"), q.get("vwap"), q.get("pChange")
        if not ltp or not vwap:
            continue
        dist = (ltp - vwap) / vwap * 100
        if dist > 0.2 and (pc is None or pc >= 0):
            conv = min(dist * 8, 45) + min(abs(pc or 0) * 3, 40) + 10
            reasons = [f"Price {dist:+.2f}% above VWAP",
                       "Above the institutional VWAP benchmark"]
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, 1.0, 2.0, fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
        elif dist < -0.2 and (pc is None or pc <= 0):
            conv = min(abs(dist) * 8, 45) + min(abs(pc or 0) * 3, 40) + 10
            reasons = [f"Price {dist:+.2f}% below VWAP",
                       "Below the institutional VWAP benchmark"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, 1.0, 2.0, fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
    return ideas


def gen_delivery(ctx):
    """
    G — Delivery-% Accumulation (India-specific): a high delivery % means shares
    were taken to demat, not flipped intraday — genuine conviction. High delivery
    + up day = accumulation (LONG); high delivery + down day = distribution
    (SHORT). Single-day proxy (a rising multi-session trend is even stronger).
    """
    ideas = []
    for sym, q in ctx.get("quotes", {}).items():
        ltp, dp, pc = q.get("ltp"), q.get("deliveryPct"), q.get("pChange")
        if not ltp or dp is None or dp < 55:
            continue
        if (pc or 0) >= 0:
            conv = min((dp - 55) * 1.6, 45) + min((pc or 0) * 4, 45) + 10
            reasons = [f"Delivery {dp:.0f}% — shares held, not flipped",
                       "Accumulation footprint on an up day"]
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, 2.0, 3.0, fno=_is_fno(sym))
        else:
            conv = min((dp - 55) * 1.6, 45) + min(abs(pc or 0) * 4, 45) + 10
            reasons = [f"Delivery {dp:.0f}% on a down day",
                       "Distribution footprint — sellers delivering"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, 2.0, 3.0, fno=_is_fno(sym))
        if idea:
            ideas.append(idea)
    return ideas


def _min_of(p):
    """Minute-of-day (IST) for a candle, via the baked-epoch convention."""
    import intrabar
    dt = intrabar.candle_dt(p["t"])
    return dt.hour * 60 + dt.minute


def gen_orb(ctx):
    """
    H — Opening-Range Breakout: the first 15 minutes (09:15-09:30) set the day's
    opening range; a decisive break of that range, confirmed by volume, tends to
    run. LONG on a break above the OR high, SHORT below the OR low. Stop at the
    opposite end of the range, target = one range width projected (needs the
    minute candles in ctx['candles'], so it's a live/forward-sim strategy).
    """
    ideas = []
    OR_END = 9 * 60 + 30
    for sym, pts in ctx.get("candles", {}).items():
        if len(pts) < 4:
            continue
        orbars = [p for p in pts if p.get("h") is not None and _min_of(p) <= OR_END]
        rest = [p for p in pts if _min_of(p) > OR_END and p.get("c") is not None]
        if len(orbars) < 2 or not rest:
            continue
        orh = max(p["h"] for p in orbars)
        orl = min(p["l"] for p in orbars if p.get("l") is not None)
        if not orh or not orl or orh <= orl:
            continue
        rng = orh - orl
        ltp = rest[-1]["c"]
        if not ltp:
            continue
        # Volume confirmation: latest bar vs the opening-range average.
        or_vol = sum(p.get("v") or 0 for p in orbars) / len(orbars) or 1
        vmult = (rest[-1].get("v") or 0) / or_vol

        if ltp > orh:
            ext = (ltp - orh) / orh * 100
            conv = 32 + min(ext * 25, 35) + min(vmult * 8, 25)
            stop_pct = max(0.4, min((ltp - orl * 0.999) / ltp * 100, 3.0))
            tgt_pct = max(stop_pct, rng / ltp * 100)
            reasons = [f"Broke above opening range (OR {orl:.1f}-{orh:.1f})",
                       f"{ext:.2f}% past the OR high",
                       f"Vol {vmult:.1f}x the opening-range average"]
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, stop_pct, tgt_pct,
                            fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
        elif ltp < orl:
            ext = (orl - ltp) / orl * 100
            conv = 32 + min(ext * 25, 35) + min(vmult * 8, 25)
            stop_pct = max(0.4, min((orh * 1.001 - ltp) / ltp * 100, 3.0))
            tgt_pct = max(stop_pct, rng / ltp * 100)
            reasons = [f"Broke below opening range (OR {orl:.1f}-{orh:.1f})",
                       f"{ext:.2f}% past the OR low",
                       f"Vol {vmult:.1f}x the opening-range average"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, stop_pct, tgt_pct,
                            fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
    return ideas


def gen_ivwap(ctx):
    """
    I — Intraday VWAP Reclaim: a TRUE session VWAP computed from the minute
    candles (sum(typical*vol)/sum(vol)), unlike the quote's single cumulative
    number used by the 'vwap' strategy. LONG when price sits above VWAP and the
    last few candles are rising (holding the reclaim); SHORT below with falling
    candles. Tight intraday 1:2.
    """
    ideas = []
    for sym, pts in ctx.get("candles", {}).items():
        usable = [p for p in pts if p.get("v") and None not in
                  (p.get("h"), p.get("l"), p.get("c"))]
        if len(usable) < 6:
            continue
        num = sum((p["h"] + p["l"] + p["c"]) / 3 * p["v"] for p in usable)
        den = sum(p["v"] for p in usable)
        if den <= 0:
            continue
        vwap = num / den
        ltp = usable[-1]["c"]
        if not ltp:
            continue
        dist = (ltp - vwap) / vwap * 100
        closes = [p["c"] for p in usable[-4:]]
        rising = closes[-1] > closes[0]
        if dist > 0.15 and rising:
            conv = 20 + min(dist * 10, 45) + 15
            reasons = [f"Price {dist:+.2f}% above session VWAP {vwap:.1f}",
                       "Holding above VWAP, last candles rising"]
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, 1.0, 2.0, fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
        elif dist < -0.15 and not rising:
            conv = 20 + min(abs(dist) * 10, 45) + 15
            reasons = [f"Price {dist:+.2f}% below session VWAP {vwap:.1f}",
                       "Rejected at VWAP, last candles falling"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, 1.0, 2.0, fno=_is_fno(sym))
            if idea:
                ideas.append(idea)
    return ideas


def gen_fut_basis(ctx):
    """K — Futures Basis / Cost-of-Carry: read the spot↔future PRICE relationship,
    not just OI direction. A rich premium (future well above spot) funded by RISING
    OI = leveraged longs paying up → LONG; a discount/backwardation on rising OI =
    fresh shorts / carry stress → SHORT. Distinct from OI Smart-Money (which reads
    the OI *direction*); this reads the *basis*."""
    ideas = []
    seen = set()
    for r in ctx.get("futures", []):
        sym, spot, bp = r.get("symbol"), r.get("spot"), r.get("basisPct")
        if not sym or sym in seen or spot is None or bp is None:
            continue
        oi_rising = (r.get("changeInOI") or 0) > 0
        pc = r.get("pChange")
        if bp >= 0.5 and oi_rising:
            conv = 40 + min((bp - 0.5) * 20, 40)
            reasons = [f"Future at +{bp:.2f}% premium to spot (rich carry)",
                       "Open interest rising — leveraged longs funding the premium"]
            if pc is not None:
                reasons.append(f"Underlying {pc:+.2f}% today")
                conv += min(pc * 2, 15) if pc > 0 else 0
            idea = _mk_idea(sym, "LONG", spot, conv, reasons, 1.5, 3.0, fno=True,
                            extra={"basisPct": bp})
        elif bp <= -0.3 and oi_rising:
            conv = 40 + min((abs(bp) - 0.3) * 25, 40)
            reasons = [f"Future at {bp:.2f}% discount to spot (backwardation)",
                       "Open interest rising — fresh shorts / carry stress"]
            if pc is not None:
                reasons.append(f"Underlying {pc:+.2f}% today")
                conv += min(abs(pc) * 2, 15) if pc < 0 else 0
            idea = _mk_idea(sym, "SHORT", spot, conv, reasons, 1.5, 3.0, fno=True,
                            extra={"basisPct": bp})
        else:
            continue
        if idea:
            ideas.append(idea)
            seen.add(sym)
    return ideas


def gen_rel_strength(ctx):
    """L — Relative Strength vs NIFTY: buy the day's leaders (outperforming the
    index) and short the laggards. Relative momentum persists — a stock up 2% when
    NIFTY is flat is a leader; up 2% when NIFTY is up 3% is a laggard. Distinct from
    Momentum (an absolute volume+OI composite); this is purely relative-to-market."""
    ideas = []
    mkt = ((ctx.get("index") or {}).get("NIFTY") or {}).get("pChange")
    if mkt is None:
        return ideas
    liquid = ctx.get("scannerSyms", set())
    seen = set()
    for r in ctx.get("scanner", []):
        sym, pc, ltp = r.get("symbol"), r.get("pChange"), r.get("ltp")
        if not sym or sym in seen or pc is None or ltp is None:
            continue
        if liquid and sym not in liquid:
            continue
        rs = pc - mkt
        if rs >= 1.5 and pc > 0:
            conv = 35 + min(rs * 8, 55)
            reasons = [f"Outperforming NIFTY by {rs:+.2f}% ({pc:+.2f}% vs mkt {mkt:+.2f}%)",
                       "Relative-strength leader"]
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, 2.0, 4.0, fno=_is_fno(sym))
        elif rs <= -1.5 and pc < 0:
            conv = 35 + min(abs(rs) * 8, 55)
            reasons = [f"Lagging NIFTY by {rs:.2f}% ({pc:+.2f}% vs mkt {mkt:+.2f}%)",
                       "Relative-strength laggard"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, 2.0, 4.0, fno=_is_fno(sym))
        else:
            continue
        if idea:
            ideas.append(idea)
            seen.add(sym)
    return ideas


def _ranges(bars):
    return [b["high"] - b["low"] for b in bars
            if b.get("high") is not None and b.get("low") is not None]


def gen_squeeze(ctx):
    """M — Volatility Squeeze (NR7): the narrowest daily range in 7 sessions marks a
    volatility contraction; the expansion that follows tends to run (Crabel). When
    the latest completed session is the NR7 and today's price breaks its high/low,
    trade the break — LONG above, SHORT below."""
    ideas = []
    for sym, bars in ctx.get("daily", {}).items():
        rngs = _ranges(bars[-7:])
        if len(rngs) < 7:
            continue
        last = bars[-1]
        lh, ll = last.get("high"), last.get("low")
        lr = (lh - ll) if (lh is not None and ll is not None) else None
        if lr is None or lr <= 0 or lr > min(rngs):
            continue                       # last session must be the tightest (NR7)
        ltp = ((ctx.get("quotes") or {}).get(sym) or {}).get("ltp")
        if not ltp:
            continue
        tight = 1 - lr / (sum(rngs) / len(rngs))     # how much tighter than avg
        if ltp > lh:
            ext = (ltp - lh) / lh * 100
            conv = 30 + min(ext * 20, 30) + min(tight * 60, 30)
            reasons = [f"NR7 squeeze breakout above {lh:.1f}",
                       f"Tightest range in 7 sessions, +{ext:.2f}% past it"]
            idea = _mk_idea(sym, "LONG", ltp, conv, reasons, 2.0, 4.0, fno=_is_fno(sym))
        elif ltp < ll:
            ext = (ll - ltp) / ll * 100
            conv = 30 + min(ext * 20, 30) + min(tight * 60, 30)
            reasons = [f"NR7 squeeze breakdown below {ll:.1f}",
                       f"Tightest range in 7 sessions, {ext:.2f}% below it"]
            idea = _mk_idea(sym, "SHORT", ltp, conv, reasons, 2.0, 4.0, fno=_is_fno(sym))
        else:
            continue
        if idea:
            ideas.append(idea)
    return ideas


def gen_gap(ctx):
    """N — Gap-and-Go / Gap-Fade: a big opening gap either continues (go, on trend
    days) or fills (fade, on quiet/reversal days). Regime-tilted: hold the gap on
    Trend/Mixed, fade it on Range/Recovery/Pullback, judged by whether price is
    holding or rejecting the open."""
    ideas = []
    regime = (ctx.get("regime") or {}).get("label", "Mixed")
    fade = regime in ("Range", "Recovery", "Pullback")
    for sym, q in (ctx.get("quotes") or {}).items():
        op, pcl, ltp = q.get("open"), q.get("prevClose"), q.get("ltp")
        if not op or not pcl or not ltp:
            continue
        gap = (op - pcl) / pcl * 100
        if abs(gap) < 1.5:
            continue
        idea = None
        if not fade:                       # gap-and-go: trade the gap while it holds
            if gap > 0 and ltp >= op:
                conv = 35 + min(gap * 10, 50)
                idea = _mk_idea(sym, "LONG", ltp, conv,
                                [f"Gapped up {gap:+.2f}% and holding above the open",
                                 f"Gap-and-go ({regime})"], 1.5, 2.5, fno=_is_fno(sym))
            elif gap < 0 and ltp <= op:
                conv = 35 + min(abs(gap) * 10, 50)
                idea = _mk_idea(sym, "SHORT", ltp, conv,
                                [f"Gapped down {gap:.2f}% and holding below the open",
                                 f"Gap-and-go ({regime})"], 1.5, 2.5, fno=_is_fno(sym))
        else:                              # gap-fade: bet the gap closes back
            if gap > 0 and ltp < op:
                conv = 35 + min(gap * 10, 50)
                idea = _mk_idea(sym, "SHORT", ltp, conv,
                                [f"Gapped up {gap:+.2f}% but rejecting the open",
                                 f"Gap-fade toward prior close ({regime})"], 1.5, 2.5,
                                fno=_is_fno(sym))
            elif gap < 0 and ltp > op:
                conv = 35 + min(abs(gap) * 10, 50)
                idea = _mk_idea(sym, "LONG", ltp, conv,
                                [f"Gapped down {gap:.2f}% but recovering the open",
                                 f"Gap-fade toward prior close ({regime})"], 1.5, 2.5,
                                fno=_is_fno(sym))
        if idea:
            ideas.append(idea)
    return ideas


def gen_pcr_extreme(ctx):
    """O — PCR Contrarian: an extreme per-stock put/call ratio is a contrarian
    sentiment tell. Very high PCR (put-heavy — excessive fear) → LONG; very low PCR
    (call-crowded — excessive greed) → SHORT. Live-only (chains aren't archived)."""
    ideas = []
    for sym, d in (ctx.get("chains") or {}).items():
        pcr, ltp = d.get("pcr"), d.get("underlying")
        if pcr is None or not ltp:
            continue
        if pcr >= 1.3:
            conv = 30 + min((pcr - 1.3) * 60, 55)
            idea = _mk_idea(sym, "LONG", ltp, conv,
                            [f"PCR {pcr:.2f} — put-heavy (excessive bearishness)",
                             "Contrarian long on capitulation"], 2.0, 3.0, fno=True,
                            extra={"pcr": pcr})
        elif pcr <= 0.6:
            conv = 30 + min((0.6 - pcr) * 90, 55)
            idea = _mk_idea(sym, "SHORT", ltp, conv,
                            [f"PCR {pcr:.2f} — call-crowded (excessive bullishness)",
                             "Contrarian short on complacency"], 2.0, 3.0, fno=True,
                            extra={"pcr": pcr})
        else:
            continue
        if idea:
            ideas.append(idea)
    return ideas


def gen_max_pain(ctx):
    """P — Max-Pain Expiry Pin: into expiry week, option writers tend to pull price
    toward the max-pain strike. Spot meaningfully above max pain → SHORT toward it;
    below → LONG toward it. Only fires within ~5 days of expiry, scaled by nearness.
    Live-only, expiry-gated."""
    ideas = []
    for sym, d in (ctx.get("chains") or {}).items():
        mp, ltp, dte = d.get("maxPain"), d.get("underlying"), d.get("dte")
        if not mp or not ltp or dte is None or dte > 5:
            continue
        dist = (ltp - mp) / mp * 100
        if abs(dist) < 1.5:
            continue
        near = (6 - dte) / 6.0                       # closer to expiry ⇒ stronger
        tgt = max(2.0, min(abs(dist), 4.0))
        if dist > 0:
            conv = 20 + min(dist * 8, 45) + near * 25
            idea = _mk_idea(sym, "SHORT", ltp, conv,
                            [f"Spot {dist:+.2f}% above max pain {mp:g} (expiry in {dte}d)",
                             "Expiry pin — writers defend max pain"], 2.0, tgt, fno=True,
                            extra={"maxPain": mp})
        else:
            conv = 20 + min(abs(dist) * 8, 45) + near * 25
            idea = _mk_idea(sym, "LONG", ltp, conv,
                            [f"Spot {dist:.2f}% below max pain {mp:g} (expiry in {dte}d)",
                             "Expiry pin — writers defend max pain"], 2.0, tgt, fno=True,
                            extra={"maxPain": mp})
        if idea:
            ideas.append(idea)
    return ideas


def gen_pdhl(ctx):
    """Q — Prior-Day High/Low Breakout: yesterday's high and low are the most-
    watched intraday levels; a clean break tends to run. LONG above the prior-day
    high, SHORT below the prior-day low. Distinct from ORB (first-15-min range)."""
    ideas = []
    for sym, bars in (ctx.get("daily") or {}).items():
        if not bars:
            continue
        pdh, pdl = bars[-1].get("high"), bars[-1].get("low")
        ltp = ((ctx.get("quotes") or {}).get(sym) or {}).get("ltp")
        if not pdh or not pdl or not ltp:
            continue
        if ltp > pdh:
            ext = (ltp - pdh) / pdh * 100
            conv = 30 + min(ext * 25, 55)
            idea = _mk_idea(sym, "LONG", ltp, conv,
                            [f"Broke above the prior-day high {pdh:.1f}",
                             f"+{ext:.2f}% past yesterday's high"], 1.5, 3.0, fno=_is_fno(sym))
        elif ltp < pdl:
            ext = (pdl - ltp) / pdl * 100
            conv = 30 + min(ext * 25, 55)
            idea = _mk_idea(sym, "SHORT", ltp, conv,
                            [f"Broke below the prior-day low {pdl:.1f}",
                             f"{ext:.2f}% below yesterday's low"], 1.5, 3.0, fno=_is_fno(sym))
        else:
            continue
        if idea:
            ideas.append(idea)
    return ideas


def _regime_playbook_pick(regime_label, vol_state=None):
    """Which base strategy to follow in this regime: the historical best from the
    daily-backtest leaderboard when it's warm, else the first strategy DESIGNED
    for the regime (a-priori). Never triggers a (blocking) backtest compute.
    When today's `vol_state` (Calm/Normal/Elevated) is known, the ranking blends
    the volatility-bucket edge with the regime edge (vol-conditioned selection).
    Returns (strategy_id, basis, cell) where basis is 'history'|'fit'|None and
    cell is the winning leaderboard cell (expectancyR/closed/...) or None."""
    if regime_label:
        try:
            import backtest_daily as btd
            data = btd.peek_regime_leaderboard()
            if data:
                lb = data.get("regimeLeaderboard") or {}
                row = next((r for r in lb.get("rows", [])
                            if r.get("regime") == regime_label), None)
                if row:
                    cells = row.get("cells") or {}
                    vol_cells = btd._vol_cells(data.get("volLeaderboard"), vol_state)
                    ranked = sorted(
                        [{"id": sid, "expectancyR": c["expectancyR"],
                          "blendedR": btd._blend_r(
                              c["expectancyR"], (vol_cells.get(sid) or {}).get("expectancyR")),
                          "cell": c}
                         for sid, c in cells.items()
                         if c and c.get("expectancyR") is not None
                         and (c.get("closed") or 0) >= 3],
                        key=lambda x: (x["blendedR"] if x["blendedR"] is not None else -1e9),
                        reverse=True)
                    if ranked:
                        # Prefer a walk-forward-robust strategy over a higher-but-
                        # overfit in-sample edge (non-blocking peek; falls back to
                        # the raw in-sample best when no walk-forward is warm).
                        rob = btd.robustness_map(btd.peek_walkforward())
                        chosen, _skip = btd._prefer_robust(ranked, rob)
                        if chosen:
                            return chosen["id"], "history", chosen["cell"]
                    elif row.get("best"):
                        return row["best"], "history", cells.get(row["best"])
        except Exception:
            pass
        for s in STRATEGIES:
            if s["id"] != "adaptive" and regime_label in s.get("regimeFit", []):
                return s["id"], "fit", None
    return None, None, None


def _conviction_mult(basis, cell):
    """Regime-conditioned position-sizing multiplier (× the base RISK_PER_TRADE).
    Size UP when the delegated strategy has a strong, well-sampled historical
    edge in this regime; size DOWN when the best available edge is weak/negative
    or we're only going by a-priori fit. Bounded to [0.5, 1.5]."""
    if basis == "history" and cell:
        exp, n = cell.get("expectancyR"), cell.get("closed") or 0
        if exp is None:
            return 0.75
        if exp >= 0.30 and n >= 10:
            return 1.5
        if exp >= 0.15 and n >= 5:
            return 1.25
        if exp >= 0.05:
            return 1.0
        if exp >= 0.0:
            return 0.75
        return 0.5      # even the regime's best strategy is net-negative here
    return 0.75         # a-priori fit only — no historical conviction to size on


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def regime_strength(regime):
    """How *textbook-clear* today's regime is, in [0,1]. Days sitting right on the
    classification boundary (or in the residual 'Mixed' bucket) score low — the
    per-regime historical edge is less trustworthy there; decisive trend/reversal/
    quiet days score high. Used to tilt position size (see conviction_mult)."""
    if not regime:
        return 0.5
    label = regime.get("label")
    npct = regime.get("niftyPct")
    if npct is None:
        return 0.5
    adv = regime.get("breadthAdv") or 0
    dec = regime.get("breadthDec") or 0
    prior = regime.get("priorDayMove")
    tot = adv + dec
    skew = abs(adv - dec) / tot if tot else 0.0          # breadth lopsidedness [0,1]
    mag = abs(npct)
    if label in ("Trend-Up", "Trend-Down"):
        # decisive move (0.6% → 1.5%+) AND lopsided breadth
        move = _clamp((mag - 0.6) / (1.5 - 0.6), 0, 1)
        return round(0.5 * move + 0.5 * skew, 2)
    if label == "Range":
        # the tighter/more balanced, the stronger the range (0.4% → 0.0%)
        quiet = _clamp((0.4 - mag) / 0.4, 0, 1)
        return round(0.6 * quiet + 0.4 * (1 - skew), 2)
    if label in ("Recovery", "Pullback"):
        # decisive counter-move today + a sizeable prior-day extreme to revert from
        move = _clamp((mag - 0.3) / (1.2 - 0.3), 0, 1)
        pmag = _clamp((abs(prior) - 1.0) / (2.5 - 1.0), 0, 1) if prior is not None else 0.3
        return round(0.6 * move + 0.4 * pmag, 2)
    return 0.3          # Mixed / Unknown — residual bucket, inherently low conviction


def conviction_mult(basis, cell, regime=None):
    """Final regime-conditioned size multiplier in [0.5, 1.5]: the historical-edge
    band (_conviction_mult) tilted ±20% by how clear today's regime is. A strong
    edge on a decisive regime day sizes biggest; the same edge on a borderline day
    is trimmed. Without a regime it degrades to the plain edge band."""
    edge = _conviction_mult(basis, cell)
    if regime is None:
        return edge
    factor = 0.8 + 0.4 * regime_strength(regime)         # weak day 0.8× → strong 1.2×
    return round(_clamp(edge * factor, 0.5, 1.5), 2)


def gen_adaptive(ctx):
    """J — Regime-Adaptive: each session delegate to the strategy with the best
    historical edge in today's regime (the strategy-of-the-day). This is the
    sim's 'follow the playbook' track — it measures whether regime-switching
    beats any single fixed strategy over time."""
    regime = ctx.get("regime") or {}
    regime_label = regime.get("label")
    vol_state = regime.get("volState")
    sid, basis, cell = _regime_playbook_pick(regime_label, vol_state)
    if not sid or sid == "adaptive":
        return []
    meta = STRATEGY_MAP.get(sid)
    if not meta:
        return []
    mult = conviction_mult(basis, cell, regime)
    # Walk-forward verdict for the delegated strategy (non-blocking; may be absent).
    verdict = None
    if basis == "history":
        try:
            import backtest_daily as btd
            verdict = btd.robustness_map(btd.peek_walkforward()).get(sid)
        except Exception:
            verdict = None
    lead = (f"Regime playbook: {regime_label} day"
            + (f" · {vol_state} vol" if vol_state else "") + f" → {meta['name']}"
            + (" (best historical edge)" if basis == "history" else " (designed fit)")
            + (f", walk-forward {verdict}" if verdict else ""))
    edge_txt = ""
    if basis == "history" and cell and cell.get("expectancyR") is not None:
        edge_txt = (f" — regime edge {cell['expectancyR']:+.2f}R over "
                    f"{cell.get('closed', 0)} trades")
    size_note = (f"Conviction sizing \u00d7{mult:g}{edge_txt} · "
                 f"regime clarity {int(round(regime_strength(regime) * 100))}%")
    ideas = []
    for idea in (meta["generate"](ctx) or []):
        merged = dict(idea)
        merged["reasons"] = [lead, size_note] + list(idea.get("reasons", []))
        merged["via"] = sid
        merged["sizeMult"] = mult
        ideas.append(merged)
    return ideas


STRATEGIES = [
    {"id": "momentum", "name": "Multi-Signal Momentum",
     "description": "Go with today's move when confirmed by unusual volume + OI buildup + breadth (1:2 RR).",
     "regimeFit": ["Trend-Up", "Trend-Down"], "generate": gen_momentum},
    {"id": "oi_smart", "name": "F&O OI Smart-Money",
     "description": "Pure derivatives positioning: long buildup/short covering = LONG, short buildup/long unwinding = SHORT.",
     "regimeFit": ["Trend-Up", "Trend-Down", "Mixed"], "generate": gen_oi_smartmoney},
    {"id": "meanrev", "name": "Mean-Reversion Bounce",
     "description": "Contrarian: buy oversold liquid names for a bounce, fade over-extended spikes.",
     "regimeFit": ["Recovery", "Range", "Pullback"], "generate": gen_meanrev},
    {"id": "vol_breakout", "name": "Volume Breakout",
     "description": "Only >=5x volume explosions in the move's direction; tight stop, quick target.",
     "regimeFit": ["Trend-Up", "Trend-Down", "Mixed"], "generate": gen_vol_breakout},
    {"id": "high52w", "name": "52-Week-High Momentum",
     "description": "Buy names hugging their 52-week high (George-Hwang anchoring edge); fade names at 52w lows.",
     "regimeFit": ["Trend-Up", "Recovery"], "generate": gen_high52w},
    {"id": "vwap", "name": "VWAP Trend",
     "description": "Go with price vs the institutional VWAP benchmark: above+green = LONG, below+red = SHORT.",
     "regimeFit": ["Trend-Up", "Trend-Down"], "generate": gen_vwap},
    {"id": "delivery", "name": "Delivery% Accumulation",
     "description": "High delivery% = real conviction: up day = accumulation (LONG), down day = distribution (SHORT).",
     "regimeFit": ["Trend-Up", "Range"], "generate": gen_delivery},
    {"id": "orb", "name": "Opening-Range Breakout",
     "description": "Break of the first 15-min range (09:15-09:30) with volume: above OR high = LONG, below OR low = SHORT.",
     "regimeFit": ["Trend-Up", "Trend-Down", "Mixed"], "generate": gen_orb},
    {"id": "ivwap", "name": "Intraday VWAP Reclaim",
     "description": "True session VWAP from minute candles: holding above + rising = LONG, rejected below + falling = SHORT.",
     "regimeFit": ["Trend-Up", "Trend-Down"], "generate": gen_ivwap},
    {"id": "fut_basis", "name": "Futures Basis / Carry",
     "description": "Spot-vs-future basis + rising OI: rich premium = leveraged longs (LONG), discount/backwardation = fresh shorts (SHORT).",
     "regimeFit": ["Trend-Up", "Trend-Down", "Mixed"], "generate": gen_fut_basis},
    {"id": "rel_strength", "name": "Relative Strength vs NIFTY",
     "description": "Buy the day's leaders (outperforming the index), short the laggards — pure relative momentum vs the market.",
     "regimeFit": ["Trend-Up", "Trend-Down"], "generate": gen_rel_strength},
    {"id": "squeeze", "name": "Volatility Squeeze (NR7)",
     "description": "Narrowest daily range in 7 sessions (contraction) then a break of it: LONG above, SHORT below (Crabel expansion).",
     "regimeFit": ["Range", "Trend-Up", "Trend-Down"], "generate": gen_squeeze},
    {"id": "gap", "name": "Gap-and-Go / Fade",
     "description": "Big opening gap: hold the gap on trend days (go), bet it fills on quiet/reversal days (fade).",
     "regimeFit": ["Trend-Up", "Trend-Down", "Range"], "generate": gen_gap},
    {"id": "pcr_extreme", "name": "PCR Contrarian",
     "description": "Extreme per-stock put/call ratio as a contrarian tell: put-heavy = LONG, call-crowded = SHORT (live-only).",
     "regimeFit": ["Recovery", "Range", "Pullback"], "generate": gen_pcr_extreme},
    {"id": "max_pain", "name": "Max-Pain Expiry Pin",
     "description": "Into expiry week, price gravitates to max pain: above it = SHORT, below it = LONG (live-only, expiry-gated).",
     "regimeFit": ["Range", "Mixed"], "generate": gen_max_pain},
    {"id": "pdhl", "name": "Prior-Day High/Low Break",
     "description": "Break of yesterday's high/low — the most-watched intraday levels: LONG above PDH, SHORT below PDL.",
     "regimeFit": ["Trend-Up", "Trend-Down", "Mixed"], "generate": gen_pdhl},
    {"id": "adaptive", "name": "Regime-Adaptive",
     "description": "Follows the playbook: each session delegates to the strategy with the best historical edge in today's regime (the strategy-of-the-day). Measures whether regime-switching beats any single fixed strategy.",
     "regimeFit": ["Trend-Up", "Recovery", "Range", "Pullback", "Mixed", "Trend-Down"],
     "generate": gen_adaptive},
]

STRATEGY_MAP = {s["id"]: s for s in STRATEGIES}


def strategy_meta():
    """Registry metadata for the UI (no generator functions)."""
    return [{k: s[k] for k in ("id", "name", "description", "regimeFit")}
            for s in STRATEGIES]


def generate(strategy_id, ctx):
    s = STRATEGY_MAP.get(strategy_id)
    return s["generate"](ctx) if s else []
