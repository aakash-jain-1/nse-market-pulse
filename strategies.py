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

import nse_client as nse


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
    return ctx


def detect_regime(ctx, prior_day_move=None):
    """
    Classify today's market regime from the index snapshot + breadth (+ the
    prior session's move for 'Recovery'). Cheap: only needs ctx['index'].
    """
    idx = (ctx.get("index") or {}).get("NIFTY") or {}
    bank = (ctx.get("index") or {}).get("BANKNIFTY") or {}
    today = idx.get("pChange")
    adv = idx.get("advances") or 0
    dec = idx.get("declines") or 0
    pcr = ctx.get("niftyPcr")

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
]

STRATEGY_MAP = {s["id"]: s for s in STRATEGIES}


def strategy_meta():
    """Registry metadata for the UI (no generator functions)."""
    return [{k: s[k] for k in ("id", "name", "description", "regimeFit")}
            for s in STRATEGIES]


def generate(strategy_id, ctx):
    s = STRATEGY_MAP.get(strategy_id)
    return s["generate"](ctx) if s else []
