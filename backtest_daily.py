"""
Daily-bar historical backtest
=============================
Answers "how would our strategies have worked over the last N days?" using REAL
NSE end-of-day history (`nse_client.get_stock_history` → daily OHLCV + delivery%),
which is the one historical source NSE doesn't block.

This is DIFFERENT from `backtest_strategies.py`:
  - `backtest_strategies` replays the LIVE intraday context we archived in
    `context_log` (high fidelity, but only covers days we captured).
  - `backtest_daily` (this file) reconstructs the strategies from DAILY bars going
    back months, so it can look back 30/60/90 days *today* — at the cost of
    fidelity: EOD entries (enter on the signal day's close), exits resolved on
    subsequent daily high/low (a bar that pierces both stop and target is counted
    as a STOP first — conservative), no intrabar wick precision.

Coverage: 6 strategies. Five come from ONE daily price/volume/delivery call per
symbol — Momentum, Mean-Reversion, Delivery%, High-Proximity, Volume-Breakout.
The sixth, OI Smart-Money, adds a second call per symbol for the near-month
futures' daily open-interest history (foCPV). VWAP / ORB / iVWAP remain
intraday-only (no daily equivalent) and are left to the live sim / context
backtest. Sizing matches the live sim: fixed RISK_PER_TRADE, outcomes in
R-multiples.
"""
import concurrent.futures as cf
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import intrabar
import nse_client as nse
import nse_quote
import sim
import strategies as strat
from sim import RISK_PER_TRADE, size_position

IST = timezone(timedelta(hours=5, minutes=30))

# Daily bars only finalise once per session, so a symbol pulled within this many
# hours is considered fresh and served straight from the SQLite cache. Past bars
# are immutable and kept forever; the TTL only governs how often we re-hit NSE to
# pick up the newest day. Full-universe repeat runs (same day) are then instant.
CACHE_TTL_HOURS = 12

# Strategies reconstructable from a single daily-history call, with their
# fixed stop/target (% of entry). RR is roughly comparable to the live ideas.
STRATS = [
    {"id": "momentum",     "name": "Multi-Signal Momentum",  "stop": 3.0, "target": 6.0,
     "desc": "Strong daily move (>=2%) confirmed by >=1.5x volume, closing near the day's extreme."},
    {"id": "meanrev",      "name": "Mean-Reversion Bounce",  "stop": 3.0, "target": 4.5,
     "desc": "Fade a sharp 1-day spike: buy a >=4% drop, sell a >=5% pop, expecting reversion."},
    {"id": "delivery",     "name": "Delivery% Accumulation", "stop": 3.0, "target": 6.0,
     "desc": "High delivery% (>=60%) = real (non-intraday) conviction; go with the day's direction."},
    {"id": "high52w",      "name": "High-Proximity Momentum","stop": 4.0, "target": 8.0,
     "desc": "Within 3% of the available-history high (52w proxy) on an up day (anchoring/breakout edge)."},
    {"id": "vol_breakout", "name": "Volume Breakout",        "stop": 3.0, "target": 6.0,
     "desc": "Close breaks the prior 20-day high/low on a >=2x volume expansion."},
    {"id": "oi_smart",     "name": "F&O OI Smart-Money",     "stop": 3.0, "target": 6.0,
     "desc": "Meaningful near-month OI build (>=8%) on >=1.2x volume with a real directional close: long buildup (price up + OI up) -> long, short buildup (price down + OI up) -> short. Entered/resolved on the equity bars."},
]
STRAT_MAP = {s["id"]: s for s in STRATS}

# OI Smart-Money gates. A rising-OI buildup must be a MEANINGFUL OI jump on
# ABOVE-AVERAGE volume with a real directional close, else it fires on noise —
# the old loose >=3% (any volume, any move) gate made it ~44% of all trades and
# the single biggest drag.
OI_MIN_PCT = 8.0
OI_MIN_VOL_MULT = 1.2
OI_MIN_RET = 0.5

# Strategy-of-the-day reads today's live regime and the historical regime
# leaderboard. The leaderboard comes from a (cheap, EOD-cached) backtest, so we
# memoise it in-process to keep page polls from recomputing it every time.
_SOD_TTL_S = 6 * 3600
_sod_cache = {"key": None, "ts": 0.0, "data": None}
_sod_lock = threading.Lock()

# Serialise heavy backtests: a full-universe run fans ~200+ symbols out over NSE,
# so two concurrent cold runs would stampede it (bot-block / DoS). One at a time;
# extra callers queue behind this lock (AUDIT.md M2).
_run_lock = threading.Lock()

NOT_COVERED = [
    {"id": "vwap", "name": "VWAP Trend",
     "reason": "Intraday cumulative VWAP has no daily-bar equivalent."},
    {"id": "orb", "name": "Opening-Range Breakout",
     "reason": "Needs the 09:15-09:30 opening range (minute candles)."},
    {"id": "ivwap", "name": "Intraday VWAP Reclaim",
     "reason": "Needs the intraday session VWAP path (minute candles)."},
]

# A curated liquid F&O default universe (Nifty-heavyweights + high-beta favourites).
# Kept modest so a first run is bounded (~universe_size history pulls). Users can
# raise universe_size to sweep the whole F&O list.
LIQUID = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK",
    "KOTAKBANK", "ITC", "LT", "BHARTIARTL", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "TATAMOTORS", "TATASTEEL", "WIPRO", "HCLTECH", "ADANIENT",
    "ADANIPORTS", "ONGC", "NTPC", "POWERGRID", "ULTRACEMCO", "TITAN", "ASIANPAINT",
    "NESTLEIND", "JSWSTEEL", "COALINDIA", "GRASIM", "BAJAJFINSV", "TECHM",
    "INDUSINDBK", "DRREDDY", "CIPLA", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO",
    "HINDALCO", "BRITANNIA", "APOLLOHOSP", "BPCL", "TATACONSUM", "M&M", "SBILIFE",
    "HDFCLIFE", "VEDL", "DLF", "PNB", "CANBK", "TRENT", "ZOMATO", "DMART",
]


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _clean_date(b):
    """Authoritative trade date as YYYY-MM-DD. NSE's CH_TIMESTAMP (`iso`) is baked
    a day early (00:00 IST expressed as UTC), so trust mTIMESTAMP (`date`)."""
    try:
        return datetime.strptime(b["date"], "%d-%b-%Y").strftime("%Y-%m-%d")
    except Exception:
        return (b.get("iso") or "")[:10]


def _dmy_to_iso(s):
    """foCPV date '08-Jul-2026' -> '2026-07-08'."""
    try:
        return datetime.strptime(s, "%d-%b-%Y").strftime("%Y-%m-%d")
    except Exception:
        return None


def _near_expiry():
    """The current near-month F&O expiry (e.g. '31-Jul-2026') for foCPV, or None.
    All stock futures share the monthly expiry, so one lookup covers everyone; the
    near contract's foCPV history spans ~3 months (enough for 30/60/90-day looks)."""
    try:
        rows = nse.get_futures(limit=15)
    except Exception:
        return None
    exps = sorted((r.get("daysToExpiry"), r.get("expiry")) for r in rows
                  if r.get("expiry") and (r.get("daysToExpiry") or -1) >= 0)
    return exps[0][1] if exps else None


def _oi_map_from_rows(rows):
    """{clean_date: oiPct} from near-month futures daily OI rows (oi + changeOi)."""
    out = {}
    for r in rows:
        d, oi, chg = r.get("d"), r.get("oi"), r.get("changeOi")
        if not d or oi is None or chg is None:
            continue
        prev = oi - chg
        out[d] = (chg / prev * 100) if prev else None
    return out


def _fresh(fetched_at):
    """Was this symbol pulled within CACHE_TTL_HOURS? (fetched_at is IST text.)"""
    if not fetched_at:
        return False
    try:
        dt = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except Exception:
        return False
    return (datetime.now(IST) - dt).total_seconds() < CACHE_TTL_HOURS * 3600


def _cached_bars(sym, chunks, chunk_days, force):
    """Daily bars for one symbol: SQLite if fresh, else fetch from NSE + upsert.
    Returns (ascending bars with 'd', from_cache: bool)."""
    kind = "bars:%d:%d" % (chunks, chunk_days)
    if not force:
        meta = db.eod_meta_get(sym, kind)
        if meta and _fresh(meta.get("fetched_at")):
            cached = db.eod_bars_get(sym)
            if cached:
                return cached, True
    try:
        bars = nse.get_stock_history(sym, chunks=chunks, chunk_days=chunk_days)
    except Exception:
        bars = []
    for b in bars:
        b["symbol"] = sym
        b["d"] = _clean_date(b)
    if bars:
        db.eod_bars_put(sym, bars)
        db.eod_meta_set(sym, kind, _now(), bars[-1]["d"], len(bars))
        return bars, False
    return db.eod_bars_get(sym), False   # NSE failed → fall back to any cache


def _cached_oi_rows(sym, expiry, force):
    """Near-month futures daily OI rows: SQLite if fresh, else fetch + upsert.
    Returns (rows with 'd', from_cache: bool)."""
    kind = "oi:%s" % (expiry or "")
    if not force:
        meta = db.eod_meta_get(sym, kind)
        if meta and _fresh(meta.get("fetched_at")):
            cached = db.eod_oi_get(sym, expiry)
            if cached:
                return cached, True
    try:
        rows = nse.get_futures_oi_history(sym, expiry)
    except Exception:
        rows = []
    for r in rows:
        r["d"] = _dmy_to_iso(r.get("date") or "")
    rows = [r for r in rows if r.get("d")]
    if rows:
        db.eod_oi_put(sym, expiry, rows)
        db.eod_meta_set(sym, kind, _now(), rows[-1]["d"], len(rows))
        return rows, False
    return db.eod_oi_get(sym, expiry), False


# ----------------------------------------------------------------------------
# Intrabar (minute-accurate) historical resolution
# ----------------------------------------------------------------------------
def _baked_s(date_iso, hour=0, minute=0):
    """Epoch-SECONDS for an IST wall-clock date/time, baked-as-UTC the way
    charting.nseindia.com expects (see nse_quote._baked_epoch)."""
    dt = datetime.strptime(date_iso, "%Y-%m-%d").replace(hour=hour, minute=minute)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _cached_minutes(sym, need_from_s, need_to_s, force):
    """1-min candles for [need_from_s, need_to_s]: SQLite if fresh & covering the
    lower bound, else fetch the window from NSE + upsert. (from_cache bool.)"""
    lo_ms, hi_ms = need_from_s * 1000, need_to_s * 1000
    if not force:
        meta = db.eod_meta_get(sym, "min")
        if meta and _fresh(meta.get("fetched_at")):
            a, _z, n = db.min_bars_span(sym)
            if n and a is not None and a <= lo_ms + 86400_000:   # lower bound covered
                return db.min_bars_get(sym, lo_ms, hi_ms), True
    try:
        d = nse_quote.get_ohlc(sym, interval=1, from_ts=need_from_s, to_ts=need_to_s)
        pts = (d.get("points") or []) if not d.get("error") else []
    except Exception:
        pts = []
    if pts:
        db.min_bars_put(sym, pts)
        last_d = intrabar.candle_dt(pts[-1]["t"]).strftime("%Y-%m-%d")
        db.eod_meta_set(sym, "min", _now(), last_d, len(pts))
        return db.min_bars_get(sym, lo_ms, hi_ms), False
    return db.min_bars_get(sym, lo_ms, hi_ms), False


def _prefetch_minutes(symbols, need_from_s, need_to_s, force):
    out, stats = {}, {"hit": 0, "fetched": 0}

    def _one(sym):
        return sym, _cached_minutes(sym, need_from_s, need_to_s, force)

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for sym, (pts, hit) in pool.map(_one, symbols):
            out[sym] = pts
            stats["hit" if hit else "fetched"] += 1
    return out, stats


def _reresolve_intrabar(t, bars_min, max_hold, date_index):
    """Re-resolve a daily trade on real minute candles, overwriting its outcome.
    Entry stays at the signal day's close; exits use the true intraday path.
    Returns 'intrabar' if resolved on candles, else 'daily' (kept as-is)."""
    if not bars_min:
        return "daily"
    view = {
        "direction": t["direction"], "entry": t["entry"], "stop": t["stop"],
        "target": t["target"], "qty": t["qty"],
        "openedTs": t["openedDate"] + " 15:30:00",   # entered at the signal-day close
    }
    status = intrabar.resolve(view, bars_min, RISK_PER_TRADE, max_sessions=max_hold)
    if status in (None, "OPEN"):
        return "daily"   # candles ran out mid-horizon → trust the daily resolution
    t["status"] = status
    t["exitPrice"] = view.get("exitPrice")
    t["pnl"] = view.get("pnl")
    t["pnlPct"] = view.get("pnlPct")
    t["rMultiple"] = view.get("rMultiple")
    t["mfePct"] = view.get("mfePct")
    t["maePct"] = view.get("maePct")
    t["minsToExit"] = view.get("minsToExit")
    cd = view.get("closedDay")
    t["closedDate"] = cd
    if cd and cd in date_index:
        t["holdDays"] = date_index[cd] - t["openIdx"]
    return "intrabar"


def _universe(size):
    """Curated liquid names first; extend with a spread sample of the rest."""
    try:
        fno = set(nse.get_fno_universe().get("stocks") or [])
    except Exception:
        fno = set()
    base = [s for s in LIQUID if not fno or s in fno]
    if size <= len(base):
        return base[:size]
    # Need more: spread-sample the remaining F&O names for A-Z coverage.
    rest = sorted(fno - set(base))
    if rest:
        stride = max(1, len(rest) // max(1, size - len(base)))
        base += rest[::stride][: size - len(base)]
    return base[:size]


def _features(bars):
    """Per-index EOD features from an ascending list of daily bars."""
    feats = []
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    run_hi = run_lo = None
    for i, b in enumerate(bars):
        c, hi, lo = b["close"], b["high"], b["low"]
        if None in (c, hi, lo):
            feats.append(None)
            continue
        run_hi = hi if run_hi is None else max(run_hi, hi)
        run_lo = lo if run_lo is None else min(run_lo, lo)
        pc = b.get("prevClose") or (bars[i - 1]["close"] if i else None)
        ret1 = (c / pc - 1) * 100 if pc else 0.0
        rng_pos = (c - lo) / (hi - lo) if hi > lo else 0.5
        prev_vol = [v for v in vols[max(0, i - 20):i] if v]
        vol20 = sum(prev_vol) / len(prev_vol) if prev_vol else None
        vol_mult = (b["volume"] / vol20) if (vol20 and b["volume"]) else None
        prev_hi = [h for h in highs[max(0, i - 20):i] if h is not None]
        prev_lo = [l for l in lows[max(0, i - 20):i] if l is not None]
        feats.append({
            "ret1": ret1, "rngPos": rng_pos, "volMult": vol_mult,
            "hi20": max(prev_hi) if prev_hi else None,
            "lo20": min(prev_lo) if prev_lo else None,
            "hh": run_hi, "ll": run_lo, "delivPct": b.get("delivPct"),
        })
    return feats


def _signals(f):
    """
    Close-independent signals (strategy_id, direction) for the as-of close of a
    day. High-proximity and volume-breakout also depend on the raw close vs level,
    so they're generated in the caller where the close is in hand.
    """
    out = []
    ret1, vm, rp = f["ret1"], f["volMult"], f["rngPos"]
    # Momentum
    if vm is not None:
        if ret1 >= 2 and vm >= 1.5 and rp >= 0.6:
            out.append(("momentum", "LONG"))
        elif ret1 <= -2 and vm >= 1.5 and rp <= 0.4:
            out.append(("momentum", "SHORT"))
    # Mean-reversion (fade the 1-day extreme)
    if ret1 <= -4:
        out.append(("meanrev", "LONG"))
    elif ret1 >= 5:
        out.append(("meanrev", "SHORT"))
    # Delivery% accumulation/distribution
    dp = f["delivPct"]
    if dp is not None and dp >= 60:
        if ret1 >= 0.5:
            out.append(("delivery", "LONG"))
        elif ret1 <= -0.5:
            out.append(("delivery", "SHORT"))
    return out


def _resolve(direction, entry, stop_px, tgt_px, bars, i, max_hold):
    """
    Walk subsequent daily bars; return (status, exitPrice, exitIdx, mfePct, maePct).
    A bar that straddles both stop and target is a STOP (conservative) — the same
    stop-first tie-break as intrabar.resolve() (AUDIT.md M9). Time-expire at close.
    MFE/MAE are the best/worst excursions from entry over the hold (daily-
    granularity here; minute mode recomputes them from real intraday wicks).
    """
    last = len(bars) - 1
    end = min(i + max_hold, last)
    mfe = mae = 0.0
    for j in range(i + 1, end + 1):
        hi, lo = bars[j]["high"], bars[j]["low"]
        if None in (hi, lo):
            continue
        if direction == "LONG":
            mfe = max(mfe, (hi / entry - 1) * 100)
            mae = min(mae, (lo / entry - 1) * 100)
            if lo <= stop_px:
                return "STOP", stop_px, j, mfe, mae
            if hi >= tgt_px:
                return "TARGET", tgt_px, j, mfe, mae
        else:
            mfe = max(mfe, (entry / lo - 1) * 100)
            mae = min(mae, (entry / hi - 1) * 100)
            if hi >= stop_px:
                return "STOP", stop_px, j, mfe, mae
            if lo <= tgt_px:
                return "TARGET", tgt_px, j, mfe, mae
    if end > i:
        return "EXPIRED", bars[end]["close"], end, mfe, mae
    return "OPEN", None, None, mfe, mae


def _trade(sid, direction, bars, feats, i, max_hold):
    meta = STRAT_MAP[sid]
    entry = bars[i]["close"]
    if not entry:
        return None
    sp, tp = meta["stop"], meta["target"]
    if direction == "LONG":
        stop_px, tgt_px = entry * (1 - sp / 100), entry * (1 + tp / 100)
    else:
        stop_px, tgt_px = entry * (1 + sp / 100), entry * (1 - tp / 100)
    qty, notional = size_position(entry, stop_px)
    status, exit_px, j, mfe, mae = _resolve(direction, entry, stop_px, tgt_px,
                                            bars, i, max_hold)
    t = {
        "symbol": bars[i].get("symbol"), "strategy": sid, "direction": direction,
        "entry": round(entry, 2), "stop": round(stop_px, 2), "target": round(tgt_px, 2),
        "qty": qty, "notional": notional, "status": status,
        "openedDate": bars[i]["d"], "openIdx": i,
        "mfePct": round(mfe, 2), "maePct": round(mae, 2), "minsToExit": None,
    }
    if status == "OPEN":
        t.update(exitPrice=None, closedDate=None, pnl=0.0, pnlPct=0.0,
                 rMultiple=0.0, holdDays=None)
        return t
    move = ((exit_px / entry - 1) * 100) if direction == "LONG" else ((entry / exit_px - 1) * 100)
    pnl = (exit_px - entry) * qty if direction == "LONG" else (entry - exit_px) * qty
    t.update(exitPrice=round(exit_px, 2), closedDate=bars[j]["d"],
             pnl=round(pnl, 2), pnlPct=round(move, 2),
             rMultiple=round(pnl / RISK_PER_TRADE, 2), holdDays=j - i)
    return t


def _backtest_symbol(bars, oi_map, cutoff_iso, max_hold):
    """All trades for one symbol across every strategy (no overlapping per s/strat)."""
    feats = _features(bars)
    trades = []
    busy = {}   # strategy_id -> index until which we're in a trade
    for i, b in enumerate(bars):
        if i + 1 >= len(bars):
            break  # need a future bar to resolve
        if b["d"] < cutoff_iso:
            continue
        f = feats[i]
        if not f:
            continue
        c = b["close"]
        sigs = list(_signals(f))
        # close-dependent signals (need the actual close vs levels)
        if i >= 60 and f["hh"] and f["ll"] and c:
            if c >= 0.97 * f["hh"] and f["ret1"] >= 0:
                sigs.append(("high52w", "LONG"))
            elif c <= 1.03 * f["ll"] and f["ret1"] <= 0:
                sigs.append(("high52w", "SHORT"))
        if f["volMult"] and f["volMult"] >= 2 and f["hi20"] and f["lo20"] and c:
            if c > f["hi20"]:
                sigs.append(("vol_breakout", "LONG"))
            elif c < f["lo20"]:
                sigs.append(("vol_breakout", "SHORT"))
        # OI Smart-Money: a meaningful OI build (>= OI_MIN_PCT) on above-average
        # volume with a real directional close = smart-money buildup (not noise).
        if oi_map:
            oi_pct = oi_map.get(b["d"])
            vm = f["volMult"]
            if (oi_pct is not None and oi_pct >= OI_MIN_PCT
                    and vm is not None and vm >= OI_MIN_VOL_MULT):
                if f["ret1"] >= OI_MIN_RET:
                    sigs.append(("oi_smart", "LONG"))
                elif f["ret1"] <= -OI_MIN_RET:
                    sigs.append(("oi_smart", "SHORT"))
        for sid, direction in sigs:
            if busy.get(sid, -1) >= i:
                continue   # already in a trade for this strategy on this name
            t = _trade(sid, direction, bars, feats, i, max_hold)
            if not t:
                continue
            trades.append(t)
            if t.get("openIdx") is not None and t["status"] != "OPEN":
                # block re-entry until this trade closes
                close_idx = i + (t["holdDays"] or 0)
                busy[sid] = close_idx
    return trades


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


# ----------------------------------------------------------------------------
# Market regime per trading day. We have no historical NIFTY feed here, so we
# build an equal-weight proxy from the SAME universe we already fetched: the
# median 1-day move + advance/decline breadth, classified with the identical
# thresholds as the live detector (strategies.detect_regime) so the labels line
# up. Every trade is then tagged with the regime of its ENTRY day.
# ----------------------------------------------------------------------------
_REGIME_ORDER = ["Trend-Up", "Recovery", "Range", "Pullback", "Mixed", "Trend-Down"]


def _classify_regime(today, adv, dec, prior):
    if today is None:
        return "Unknown"
    if prior is not None and prior <= -1.0 and today >= 0.3:
        return "Recovery"
    if prior is not None and prior >= 1.0 and today <= -0.3:
        return "Pullback"
    if today >= 0.6 and adv >= dec:
        return "Trend-Up"
    if today <= -0.6 and dec >= adv:
        return "Trend-Down"
    if abs(today) <= 0.4:
        return "Range"
    return "Mixed"


def _regime_map(hist):
    """date -> {label, mktPct, adv, dec, priorPct} via an equal-weight proxy."""
    rets = {}
    for bars in hist.values():
        for i in range(1, len(bars)):
            pc, c = bars[i - 1]["close"], bars[i]["close"]
            if pc and c:
                rets.setdefault(bars[i]["d"], []).append((c / pc - 1) * 100)
    out, prior = {}, None
    for d in sorted(rets):
        vals = rets[d]
        mkt = _median(vals)
        adv = sum(1 for r in vals if r > 0)
        dec = sum(1 for r in vals if r < 0)
        out[d] = {"label": _classify_regime(mkt, adv, dec, prior),
                  "mktPct": round(mkt, 2) if mkt is not None else None,
                  "adv": adv, "dec": dec,
                  "priorPct": round(prior, 2) if prior is not None else None}
        prior = mkt
    return out


def _regime_leaderboard(trades):
    """Matrix regime × strategy → expectancy/win%/count; best strategy per regime
    (by expectancy R, needs >=3 samples). Pure attribution — no look-ahead."""
    ids = [s["id"] for s in STRATS]
    agg, regimes = {}, set()
    for t in trades:
        if t["status"] == "OPEN":
            continue
        rg = t.get("regimeAtEntry") or "?"
        regimes.add(rg)
        a = agg.setdefault((rg, t["strategy"]),
                           {"closed": 0, "wins": 0, "r": 0.0, "pctSum": 0.0})
        a["closed"] += 1
        if t["status"] == "TARGET":
            a["wins"] += 1
        a["r"] += t.get("rMultiple") or 0.0
        a["pctSum"] += t.get("pnlPct") or 0.0
    ordered = ([r for r in _REGIME_ORDER if r in regimes] +
               sorted(r for r in regimes if r not in _REGIME_ORDER))
    rows = []
    for rg in ordered:
        cells, best_sid, best_val, n_rg = {}, None, None, 0
        for sid in ids:
            a = agg.get((rg, sid))
            if not a or not a["closed"]:
                cells[sid] = None
                continue
            n = a["closed"]
            n_rg += n
            exp = a["r"] / n
            cells[sid] = {"closed": n,
                          "winRate": round(a["wins"] / n * 100, 1),
                          "avgPnlPct": round(a["pctSum"] / n, 2),
                          "expectancyR": round(exp, 2),
                          "totalR": round(a["r"], 2)}
            if n >= 3 and (best_val is None or exp > best_val):
                best_val, best_sid = exp, sid
        rows.append({"regime": rg, "best": best_sid, "closed": n_rg, "cells": cells})
    return {"order": ids, "rows": rows}


def _regime_fit(sid):
    return set((strat.STRATEGY_MAP.get(sid) or {}).get("regimeFit") or [])


def _gated(by_strat):
    """A-priori affinity gate: keep only trades whose ENTRY regime is in the
    strategy's DESIGNED regimeFit (trading logic, NOT fit to this window). Reports
    per-strategy all-vs-in-fit and the combined gated portfolio."""
    per, kept = [], []
    for s in STRATS:
        sid = s["id"]
        fit = _regime_fit(sid)
        ts = by_strat.get(sid, [])
        in_fit = [t for t in ts if t.get("regimeAtEntry") in fit]
        kept += in_fit
        per.append({"id": sid, "name": s["name"], "fit": sorted(fit),
                    "all": _scorecard(ts), "gated": _scorecard(in_fit)})
    return {"perStrategy": per, "portfolio": _scorecard(kept)}


def _scorecard(trades):
    closed = [t for t in trades if t["status"] in ("TARGET", "STOP", "EXPIRED")]
    wins = [t for t in closed if t["status"] == "TARGET"]
    profitable = [t for t in closed if (t["pnl"] or 0) > 0]   # ended positive
    n = len(closed)
    total_r = sum(t["rMultiple"] for t in closed)
    realized = sum(t["pnl"] for t in closed)
    avg_pct = sum(t["pnlPct"] for t in closed) / n if n else None
    mfes = [t["mfePct"] for t in closed if t.get("mfePct") is not None]
    maes = [t["maePct"] for t in closed if t.get("maePct") is not None]
    mins = [t["minsToExit"] for t in closed if t.get("minsToExit") is not None]
    # equity curve by close date
    cl = sorted(closed, key=lambda t: t["closedDate"] or "")
    cum, pts = 0.0, []
    for t in cl:
        cum += t["pnl"]
        pts.append(round(cum, 0))
    return {
        "trades": len(trades), "open": len(trades) - n, "closed": n,
        "target": len(wins), "stop": sum(1 for t in closed if t["status"] == "STOP"),
        "expired": sum(1 for t in closed if t["status"] == "EXPIRED"),
        "winRate": round(len(wins) / n * 100, 1) if n else None,
        "profit": len(profitable),
        "profitRate": round(len(profitable) / n * 100, 1) if n else None,
        "avgPnlPct": round(avg_pct, 2) if avg_pct is not None else None,
        "totalR": round(total_r, 2), "expectancyR": round(total_r / n, 2) if n else None,
        "realizedPnl": round(realized, 2),
        "avgHoldDays": round(sum(t["holdDays"] for t in closed) / n, 1) if n else None,
        "avgMfePct": round(sum(mfes) / len(mfes), 2) if mfes else None,
        "avgMaePct": round(sum(maes) / len(maes), 2) if maes else None,
        "medMinsToExit": int(_median(mins)) if mins else None,
        "equity": {"points": pts, "final": round(cum, 0), "n": len(pts)},
    }


def run(days=30, universe_size=40, max_hold=5, chunks=3, chunk_days=80,
        include_oi=True, force=False, resolve="daily", _collect=False):
    """Public entry — serialised so concurrent callers can't stampede NSE
    (AUDIT.md M2). One backtest runs at a time; others queue on `_run_lock`.

    `_collect=True` also returns the flat `trades` list + `dayRegime` map (for the
    walk-forward validator) — omitted from the normal API payload to keep it lean."""
    with _run_lock:
        return _run_impl(days=days, universe_size=universe_size, max_hold=max_hold,
                         chunks=chunks, chunk_days=chunk_days, include_oi=include_oi,
                         force=force, resolve=resolve, _collect=_collect)


def _run_impl(days=30, universe_size=40, max_hold=5, chunks=3, chunk_days=80,
              include_oi=True, force=False, resolve="daily", _collect=False):
    db.init()
    days = max(5, min(int(days), 120))
    universe_size = max(5, min(int(universe_size), 260))
    max_hold = max(1, min(int(max_hold), 15))

    symbols = _universe(universe_size)
    expiry = _near_expiry() if include_oi else None
    hist = {}
    ois = {}
    cache = {"barsHit": 0, "barsFetched": 0}

    def _pull(sym):
        bars, bhit = _cached_bars(sym, chunks, chunk_days, force)
        if include_oi and expiry:
            oi_rows, _ = _cached_oi_rows(sym, expiry, force)
            oi = _oi_map_from_rows(oi_rows)
        else:
            oi = {}
        return sym, bars, oi, bhit

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for sym, bars, oi, bhit in pool.map(_pull, symbols):
            cache["barsHit" if bhit else "barsFetched"] += 1
            if bars:
                hist[sym] = bars
                ois[sym] = oi

    if not hist:
        return {"message": "No history returned from NSE (rate-limited or off-hours). Try again."}

    # cutoff = last available bar date minus `days` calendar days
    last_iso = max(b["d"] for bars in hist.values() for b in bars)
    last_dt = datetime.strptime(last_iso, "%Y-%m-%d")
    cutoff_iso = (last_dt - timedelta(days=days)).strftime("%Y-%m-%d")

    day_regime = _regime_map(hist)
    by_strat = {s["id"]: [] for s in STRATS}
    first_iso = last_iso
    for sym, bars in hist.items():
        for t in _backtest_symbol(bars, ois.get(sym, {}), cutoff_iso, max_hold):
            t["regimeAtEntry"] = (day_regime.get(t["openedDate"]) or {}).get("label", "?")
            by_strat[t["strategy"]].append(t)
            if t["openedDate"] < first_iso:
                first_iso = t["openedDate"]

    # Optional pass: re-resolve each daily trade on REAL 1-min candles so the
    # exit uses the true intraday path (which-came-first, wick timing, MFE/MAE)
    # instead of the daily high/low. Trades whose window predates NSE's ~30-40d
    # minute retention silently keep the daily resolution.
    resolved = {"intrabar": 0, "daily": 0}
    min_cache = None
    if resolve == "intrabar":
        need_from_s = _baked_s(first_iso)
        to_dt = last_dt + timedelta(days=max_hold + 2)
        need_to_s = min(_baked_s(to_dt.strftime("%Y-%m-%d"), 23, 59),
                        nse_quote._baked_now())
        syms = sorted({t["symbol"] for ts in by_strat.values() for t in ts})
        minutes, min_cache = _prefetch_minutes(syms, need_from_s, need_to_s, force)
        date_index = {sym: {b["d"]: i for i, b in enumerate(bars)}
                      for sym, bars in hist.items()}
        for ts in by_strat.values():
            for t in ts:
                tag = _reresolve_intrabar(t, minutes.get(t["symbol"]) or [],
                                          max_hold, date_index.get(t["symbol"], {}))
                resolved[tag] += 1

    rows = []
    all_trades = []
    for s in STRATS:
        ts = by_strat[s["id"]]
        all_trades += ts
        sc = _scorecard(ts)
        # a few sample trades (most recent) for drill-in
        sample = sorted([t for t in ts if t["status"] != "OPEN"],
                        key=lambda t: t["closedDate"] or "", reverse=True)[:12]
        rows.append({"id": s["id"], "name": s["name"], "description": s["desc"], **sc,
                     "sample": sample})
    rows.sort(key=lambda r: (r["expectancyR"] if r["expectancyR"] is not None else -1e9),
              reverse=True)

    regime_lb = _regime_leaderboard(all_trades)
    gated = _gated(by_strat)
    regime_dist = {}
    for d, r in day_regime.items():
        if d >= cutoff_iso:
            regime_dist[r["label"]] = regime_dist.get(r["label"], 0) + 1

    bars_counts = [len(b) for b in hist.values()]
    result = {
        "mode": "daily",
        "resolve": resolve,
        "resolved": resolved,
        "days": days,
        "range": {"from": cutoff_iso, "to": last_iso, "firstTrade": first_iso},
        "maxHold": max_hold,
        "riskPerTrade": RISK_PER_TRADE,
        "universeRequested": universe_size,
        "universeWithData": len(hist),
        "barsMedian": _median(bars_counts),
        "oiExpiry": expiry,
        "oiNames": sum(1 for v in ois.values() if v),
        "cache": {**cache, "ttlHours": CACHE_TTL_HOURS, "minCache": min_cache,
                  "store": db.eod_stats()},
        "strategies": rows,
        "totals": _scorecard(all_trades),
        "regimeLeaderboard": regime_lb,
        "regimeDist": regime_dist,
        "gated": gated,
        "notCovered": NOT_COVERED,
        "generatedAt": _now(),
    }
    if _collect:
        # Raw material for walk-forward validation (walkforward.py): every trade
        # tagged with openedDate + regimeAtEntry + rMultiple, plus the day→regime map.
        result["trades"] = all_trades
        result["dayRegime"] = day_regime
    return result


# ----------------------------------------------------------------------------
# Strategy of the day — read today's LIVE regime, then surface the strategy with
# the best HISTORICAL edge on that kind of day (from the daily-bar leaderboard).
# ----------------------------------------------------------------------------
def cached_regime_leaderboard(days=60, universe_size=60, resolve="daily"):
    """Memoised regime leaderboard for strategy-of-the-day (recomputed <=1/6h)."""
    key = (int(days), int(universe_size), resolve)
    c = _sod_cache
    if c["key"] == key and c["data"] and (time.time() - c["ts"]) < _SOD_TTL_S:
        return c["data"]
    with _sod_lock:
        if c["key"] == key and c["data"] and (time.time() - c["ts"]) < _SOD_TTL_S:
            return c["data"]
        r = run(days=days, universe_size=universe_size, resolve=resolve)
        data = {
            "regimeLeaderboard": r.get("regimeLeaderboard") or {"order": [], "rows": []},
            "regimeDist": r.get("regimeDist") or {},
            "days": r.get("days"),
            "range": r.get("range"),
            "universeWithData": r.get("universeWithData"),
        }
        _sod_cache.update(key=key, ts=time.time(), data=data)
        return data


def peek_regime_leaderboard():
    """Return the memoised leaderboard only if fresh — never computes. For the
    hot path (per-minute idea generation) so it can't block on a cold backtest."""
    c = _sod_cache
    if c["data"] and (time.time() - c["ts"]) < _SOD_TTL_S:
        return c["data"]
    return None


def strategy_of_day(days=60, universe_size=60, min_closed=5):
    """Today's live regime + the strategy with the best historical expectancy on
    that regime. Falls back to the a-priori regimeFit design when history is thin."""
    try:
        regime = sim.current_regime() or {}
    except Exception:
        regime = {}
    label = regime.get("label")

    try:
        lb_data = cached_regime_leaderboard(days=days, universe_size=universe_size)
    except Exception:
        lb_data = {"regimeLeaderboard": {"order": [], "rows": []}, "regimeDist": {},
                   "days": days, "range": None, "universeWithData": 0}
    lb = lb_data["regimeLeaderboard"]
    names = {s["id"]: s["name"] for s in STRATS}

    row = next((r for r in lb.get("rows", []) if r["regime"] == label), None)
    ranked = []
    if row:
        for sid in lb.get("order", []):
            c = (row["cells"] or {}).get(sid)
            if c and c["closed"] >= min_closed:
                ranked.append({
                    "id": sid, "name": names.get(sid, sid),
                    "expectancyR": c["expectancyR"], "winRate": c["winRate"],
                    "avgPnlPct": c["avgPnlPct"], "closed": c["closed"],
                    "fits": label in _regime_fit(sid),
                })
        ranked.sort(key=lambda x: x["expectancyR"], reverse=True)

    if ranked:
        top = ranked[0]
        pick = {**top,
                "reason": (f"Best historical edge on {label} days: "
                           f"{top['expectancyR']:+.2f}R/trade, {top['winRate']}% win "
                           f"over {top['closed']} trades.")}
        basis = "history"
    else:
        fit = [s for s in STRATS if label in _regime_fit(s["id"])]
        if fit:
            s = fit[0]
            pick = {"id": s["id"], "name": s["name"], "expectancyR": None,
                    "winRate": None, "closed": 0, "fits": True,
                    "reason": (f"No {label}-day history in the sample yet — "
                               f"{s['name']} is the strategy designed for this regime.")}
            basis = "fit"
        else:
            pick, basis = None, "none"

    return {
        "regime": regime,
        "basis": basis,
        "pick": pick,
        "ranked": ranked,
        "sample": {"days": lb_data.get("days"),
                   "universeWithData": lb_data.get("universeWithData"),
                   "range": lb_data.get("range"),
                   "regimeDays": (lb_data.get("regimeDist") or {}).get(label)},
        "generatedAt": _now(),
    }
