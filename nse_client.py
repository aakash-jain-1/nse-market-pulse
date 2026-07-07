"""
NSE data client
================
Handles the NSE India session (cookie warm-up) and returns clean, normalized
lists of dicts for each "in-demand" view. Shared by both the CLI scanner and
the Flask web dashboard.

NSE blocks plain requests, so we first hit the homepage to grab session
cookies, then reuse that session for the API calls. Sessions expire, so we
lazily rebuild them on failure.
"""

import threading
import time
from datetime import datetime

import requests

BASE = "https://www.nseindia.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

ENDPOINTS = {
    "gainers": "/api/live-analysis-variations?index=gainers",
    "losers": "/api/live-analysis-variations?index=loosers",
    "volume": "/api/live-analysis-most-active-securities?index=volume",
    "value": "/api/live-analysis-most-active-securities?index=value",
    "volgainers": "/api/live-analysis-volume-gainers",
    "oispurts": "/api/live-analysis-oi-spurts-underlyings",
    "stockfut": "/api/liveEquity-derivatives?index=stock_fut",
}

# Module-level session, rebuilt lazily and guarded so concurrent web requests
# don't stampede NSE with warm-up calls.
_session = None
_session_ts = 0.0
_lock = threading.Lock()
_SESSION_TTL = 300  # seconds before we proactively refresh cookies


def _build_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(BASE, timeout=15)
    s.get(BASE + "/market-data/live-equity-market", timeout=15)
    return s


def get_session(force=False):
    global _session, _session_ts
    with _lock:
        expired = (time.time() - _session_ts) > _SESSION_TTL
        if force or _session is None or expired:
            _session = _build_session()
            _session_ts = time.time()
        return _session


def _fetch(path):
    """Fetch JSON, transparently rebuilding the session once on failure."""
    try:
        r = get_session().get(BASE + path, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        r = get_session(force=True).get(BASE + path, timeout=15)
        r.raise_for_status()
        return r.json()


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def get_variations(kind, limit=20):
    """Top gainers / losers. kind is 'gainers' or 'losers'."""
    data = _fetch(ENDPOINTS[kind])
    bucket = data.get("allSec") or data.get("NIFTY") or {}
    rows = bucket.get("data", [])[:limit]
    return [
        {
            "symbol": r.get("symbol"),
            "ltp": _num(r.get("ltp")),
            "pChange": _num(r.get("perChange")),
            "prevClose": _num(r.get("prev_price")),
            "volume": _num(r.get("trade_quantity")),
        }
        for r in rows
    ]


def get_most_active(kind, limit=20):
    """Most active by 'volume' or 'value'."""
    data = _fetch(ENDPOINTS[kind])
    rows = data.get("data", [])[:limit]
    return [
        {
            "symbol": r.get("symbol"),
            "ltp": _num(r.get("lastPrice")),
            "pChange": _num(r.get("pChange")),
            "volume": _num(r.get("totalTradedVolume")),
            "value": _num(r.get("totalTradedValue")),
        }
        for r in rows
    ]


def get_volume_gainers(limit=20):
    """Stocks trading far above their average volume (unusual activity)."""
    data = _fetch(ENDPOINTS["volgainers"])
    rows = data.get("data", [])[:limit]
    return [
        {
            "symbol": r.get("symbol"),
            "ltp": _num(r.get("ltp")),
            "pChange": _num(r.get("pChange")),
            "volume": _num(r.get("volume")),
            "week1AvgVolume": _num(r.get("week1AvgVolume")),
            "week1volChange": _num(r.get("week1volChange")),
        }
        for r in rows
    ]


# Short-lived cache for the underlying price map so the OI tab doesn't refetch
# several lists on every single request.
_price_cache = {"ts": 0.0, "map": {}}
_PRICE_TTL = 20  # seconds


def _underlying_price_map():
    """
    Build symbol -> % price change map for F&O underlyings by combining the
    most reliable live sources. The OI-spurts endpoint gives us open interest
    but NOT the underlying's price direction, so we cross-reference here.
    """
    if (time.time() - _price_cache["ts"]) < _PRICE_TTL and _price_cache["map"]:
        return _price_cache["map"]

    pmap = {}

    # 1) Most active stock futures: underlying + pChange together (best match
    #    for F&O names). This is the highest-quality source for this purpose.
    try:
        d = _fetch("/api/liveEquity-derivatives?index=stock_fut")
        for r in d.get("data", []):
            sym = r.get("underlying")
            pc = _num(r.get("pChange"))
            if sym and pc is not None:
                pmap[sym] = pc
    except Exception:
        pass

    # 2) Broad gainers / losers buckets (cash market) fill in the rest.
    for kind in ("gainers", "losers"):
        try:
            for r in get_variations(kind, limit=500):
                if r["symbol"] and r["pChange"] is not None:
                    pmap.setdefault(r["symbol"], r["pChange"])
        except Exception:
            pass

    # 3) Most active by volume / value as a final backfill.
    for kind in ("volume", "value"):
        try:
            for r in get_most_active(kind, limit=100):
                if r["symbol"] and r["pChange"] is not None:
                    pmap.setdefault(r["symbol"], r["pChange"])
        except Exception:
            pass

    _price_cache["ts"] = time.time()
    _price_cache["map"] = pmap
    return pmap


def _oi_signal(oi_up, price_change):
    """
    Classify the OI + price combination. Returns (label, kind) where kind is
    one of 'buildup' (bullish-ish), 'short' (bearish-ish), or 'neutral' when
    we don't have the underlying's price direction.
    """
    if price_change is None:
        return ("OI Rising" if oi_up else "OI Falling", "neutral")
    price_up = price_change >= 0
    if oi_up and price_up:
        return ("Long buildup", "buildup")
    if oi_up and not price_up:
        return ("Short buildup", "short")
    if not oi_up and price_up:
        return ("Short covering", "buildup")
    return ("Long unwinding", "short")


def get_oi_spurts(limit=20):
    """
    F&O Open Interest spurts. Rising OI shows fresh positions being built in
    the derivatives market. Combined with the underlying's price direction:
      price up + OI up   -> long buildup (bullish demand)
      price down + OI up -> short buildup (bearish)
      price up + OI down -> short covering
      price down + OI down -> long unwinding
    """
    data = _fetch(ENDPOINTS["oispurts"])
    rows = data.get("data", [])[:limit]
    prices = _underlying_price_map()
    out = []
    for r in rows:
        latest = _num(r.get("latestOI"))
        prev = _num(r.get("prevOI"))
        change = _num(r.get("changeInOI"))
        pct = None
        if latest is not None and prev not in (None, 0):
            pct = (latest - prev) / prev * 100
        sym = r.get("symbol")
        pchange = prices.get(sym)
        label, kind = _oi_signal((change or 0) >= 0, pchange)
        out.append(
            {
                "symbol": sym,
                "ltp": _num(r.get("underlyingValue")),
                "pChange": pchange,
                "latestOI": latest,
                "prevOI": prev,
                "changeInOI": change,
                "oiPctChange": pct,
                "volume": _num(r.get("volume")),
                "signal": label,
                "signalKind": kind,
            }
        )
    return out


def _oi_change_map():
    """symbol -> changeInOI from the OI-spurts endpoint (best-effort)."""
    out = {}
    try:
        data = _fetch(ENDPOINTS["oispurts"])
        for r in data.get("data", []):
            sym = r.get("symbol")
            if sym:
                out[sym] = _num(r.get("changeInOI"))
    except Exception:
        pass
    return out


def _days_to_expiry(expiry):
    """expiry like '28-Jul-2026' -> integer days from today (or None)."""
    if not expiry:
        return None
    try:
        exp = datetime.strptime(expiry, "%d-%b-%Y").date()
        return (exp - datetime.now().date()).days
    except Exception:
        return None


def get_futures(limit=25):
    """
    Most-active stock futures enriched with:
      - basis / premium-discount vs spot (futures price - underlying spot)
      - basis % and annualized carry (by days to expiry)
      - OI + change in OI (cross-referenced) and long/short buildup signal
    """
    data = _fetch(ENDPOINTS["stockfut"])
    rows = data.get("data", [])[:limit]
    oi_changes = _oi_change_map()

    out = []
    for r in rows:
        sym = r.get("underlying")
        fut = _num(r.get("lastPrice"))
        spot = _num(r.get("underlyingValue"))
        pchg = _num(r.get("pChange"))
        basis = None
        basis_pct = None
        annualized = None
        dte = _days_to_expiry(r.get("expiryDate"))
        if fut is not None and spot not in (None, 0):
            basis = fut - spot
            basis_pct = basis / spot * 100
            if dte and dte > 0:
                annualized = basis_pct * (365.0 / dte)

        change_oi = oi_changes.get(sym)
        label, kind = _oi_signal(
            (change_oi or 0) >= 0 if change_oi is not None else (pchg or 0) >= 0,
            pchg,
        )
        if change_oi is None:
            # Without a real OI change we can't assert buildup direction.
            label, kind = ("OI n/a", "neutral")

        out.append(
            {
                "symbol": sym,
                "expiry": r.get("expiryDate"),
                "daysToExpiry": dte,
                "ltp": fut,
                "spot": spot,
                "pChange": pchg,
                "basis": round(basis, 2) if basis is not None else None,
                "basisPct": round(basis_pct, 2) if basis_pct is not None else None,
                "annualizedPct": round(annualized, 1) if annualized is not None else None,
                "oi": _num(r.get("openInterest")),
                "changeInOI": change_oi,
                "volume": _num(r.get("volume")),
                "signal": label,
                "signalKind": kind,
            }
        )
    return out


_fno_cache = {"ts": 0.0, "data": None}
_FNO_TTL = 3600  # the F&O list changes rarely; cache for an hour


def get_fno_universe():
    """
    Full F&O universe: all derivative-enabled index and stock underlyings.
    Cached for an hour since NSE only revises the list periodically.
    """
    if _fno_cache["data"] and (time.time() - _fno_cache["ts"]) < _FNO_TTL:
        return _fno_cache["data"]
    data = _fetch("/api/underlying-information")
    d = data.get("data", {})
    indices = [x.get("symbol") for x in d.get("IndexList", []) if x.get("symbol")]
    stocks = [x.get("symbol") for x in d.get("UnderlyingList", []) if x.get("symbol")]
    out = {"indices": indices, "stocks": sorted(stocks), "count": len(indices) + len(stocks)}
    _fno_cache.update(ts=time.time(), data=out)
    return out


def get_scanner(
    direction="any",
    min_abs_change=None,
    min_vol_mult=None,
    min_value_cr=None,
    oi="any",
    fno_only=False,
    limit=60,
):
    """
    Unified "in-demand right now" scanner. Aggregates every cheap hot list into
    one per-symbol record with a composite score and human-readable tags, then
    applies filters. This is the one-stop board for spotting intraday activity.

    Filters (all optional):
      direction     : 'up' | 'down' | 'any'   (by % price change)
      min_abs_change: minimum |% change|
      min_vol_mult  : minimum volume-vs-average multiple (unusual volume)
      min_value_cr  : minimum traded value in Rs crore (money flow)
      oi            : 'any' | 'long' | 'short' (OI buildup direction)
      fno_only      : only F&O underlyings (present in futures/OI lists)
    """
    agg = {}

    def rec(sym):
        return agg.setdefault(sym, {
            "symbol": sym, "ltp": None, "pChange": None, "volume": None,
            "value": None, "volMult": None, "oiSignal": None, "oiKind": None,
            "changeInOI": None, "basisPct": None, "tags": [], "lists": set(),
            "score": 0.0, "fno": False,
        })

    def setk(e, k, v):
        if v is not None and e.get(k) is None:
            e[k] = v

    # Unusual volume (the strongest short-term "something's happening" signal).
    try:
        for r in get_volume_gainers(limit=40):
            if not r["symbol"]:
                continue
            e = rec(r["symbol"]); e["lists"].add("vol")
            setk(e, "ltp", r.get("ltp")); setk(e, "pChange", r.get("pChange"))
            mult = r.get("week1volChange") or 0
            e["volMult"] = mult
            e["score"] += min(mult / 10.0, 15.0)
            if "🔥 Unusual volume" not in e["tags"]:
                e["tags"].append("🔥 Unusual volume")
    except Exception:
        pass

    # Money flow (heavy traded value).
    try:
        for i, r in enumerate(get_most_active("value", limit=25)):
            if not r["symbol"]:
                continue
            e = rec(r["symbol"]); e["lists"].add("value")
            setk(e, "ltp", r.get("ltp")); setk(e, "pChange", r.get("pChange"))
            e["value"] = r.get("value")
            e["score"] += (25 - i) * 0.6
            if "💰 Money flow" not in e["tags"]:
                e["tags"].append("💰 Money flow")
    except Exception:
        pass

    # High absolute volume.
    try:
        for i, r in enumerate(get_most_active("volume", limit=25)):
            if not r["symbol"]:
                continue
            e = rec(r["symbol"]); e["lists"].add("volume")
            setk(e, "ltp", r.get("ltp")); setk(e, "pChange", r.get("pChange"))
            setk(e, "volume", r.get("volume"))
            e["score"] += (25 - i) * 0.3
    except Exception:
        pass

    # Price momentum (both directions).
    for kind, tag in (("gainers", "📈 Momentum up"), ("losers", "📉 Momentum down")):
        try:
            for r in get_variations(kind, limit=25):
                if not r["symbol"]:
                    continue
                e = rec(r["symbol"]); e["lists"].add(kind)
                setk(e, "ltp", r.get("ltp")); setk(e, "pChange", r.get("pChange"))
                setk(e, "volume", r.get("volume"))
                e["score"] += abs(r.get("pChange") or 0) / 2.0
                if tag not in e["tags"]:
                    e["tags"].append(tag)
        except Exception:
            pass

    # OI buildup (derivatives conviction).
    try:
        for r in get_oi_spurts(limit=40):
            if not r["symbol"]:
                continue
            e = rec(r["symbol"]); e["lists"].add("oi"); e["fno"] = True
            setk(e, "ltp", r.get("ltp")); setk(e, "pChange", r.get("pChange"))
            e["oiSignal"] = r.get("signal"); e["oiKind"] = r.get("signalKind")
            e["changeInOI"] = r.get("changeInOI")
            if r.get("signalKind") == "buildup":
                e["score"] += 6
                if "🟢 " + (r.get("signal") or "") not in e["tags"]:
                    e["tags"].append("🟢 " + r.get("signal"))
            elif r.get("signalKind") == "short":
                e["score"] += 3
                if "🔴 " + (r.get("signal") or "") not in e["tags"]:
                    e["tags"].append("🔴 " + r.get("signal"))
    except Exception:
        pass

    # Futures basis (adds F&O flag + premium/discount context).
    try:
        for r in get_futures(limit=40):
            if not r["symbol"]:
                continue
            e = rec(r["symbol"]); e["fno"] = True
            setk(e, "ltp", r.get("spot")); setk(e, "pChange", r.get("pChange"))
            e["basisPct"] = r.get("basisPct")
    except Exception:
        pass

    # Multi-list presence bonus: breadth of interest across independent signals.
    for e in agg.values():
        n = len(e["lists"])
        if n >= 3:
            e["score"] += (n - 2) * 4
            if "⭐ Multi-signal" not in e["tags"]:
                e["tags"].insert(0, "⭐ Multi-signal")

    # ---- Filters ----
    rows = list(agg.values())

    def keep(e):
        pc = e.get("pChange")
        if direction == "up" and not (pc is not None and pc >= 0):
            return False
        if direction == "down" and not (pc is not None and pc < 0):
            return False
        if min_abs_change is not None and (pc is None or abs(pc) < min_abs_change):
            return False
        if min_vol_mult is not None and (e.get("volMult") is None or e["volMult"] < min_vol_mult):
            return False
        if min_value_cr is not None:
            v = e.get("value")
            if v is None or v < min_value_cr * 1e7:
                return False
        if oi == "long" and e.get("oiKind") != "buildup":
            return False
        if oi == "short" and e.get("oiKind") != "short":
            return False
        if fno_only and not e.get("fno"):
            return False
        return True

    rows = [e for e in rows if keep(e)]
    rows.sort(key=lambda x: x["score"], reverse=True)
    rows = rows[:limit]
    for e in rows:
        e["score"] = round(e["score"], 1)
        e["listCount"] = len(e.pop("lists"))
    return rows


_all_fut_cache = {"ts": 0.0, "rows": None}
_ALL_FUT_TTL = 90  # seconds; a full sweep is expensive so cache aggressively


def get_all_futures(force=False):
    """
    Near-month futures for the ENTIRE F&O universe (~215 names), not just the
    ~20 most-active. Fetched concurrently per symbol via getSymbolDerivativesData
    with a small worker pool (to stay polite to NSE) and cached for 90s.

    Returns rows sorted by traded volume (most active first).
    """
    if (not force and _all_fut_cache["rows"] is not None
            and (time.time() - _all_fut_cache["ts"]) < _ALL_FUT_TTL):
        return _all_fut_cache["rows"]

    from concurrent.futures import ThreadPoolExecutor
    import nse_quote

    uni = get_fno_universe()
    symbols = (uni.get("indices") or []) + (uni.get("stocks") or [])

    def one(sym):
        try:
            return nse_quote.get_near_future(sym)
        except Exception:
            return None

    rows = []
    # Modest concurrency: enough to finish in a few seconds, gentle on NSE.
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r in pool.map(one, symbols):
            if r and r.get("ltp") is not None:
                rows.append(r)

    rows.sort(key=lambda x: (x.get("volume") or 0), reverse=True)
    _all_fut_cache.update(ts=time.time(), rows=rows)
    return rows


def get_demand_score(limit=25):
    """
    A simple composite "demand" ranking. Stocks that show up across multiple
    hot lists (gainers, most-active-by-value, volume-gainers) score higher.
    This surfaces names with BOTH strong price momentum and heavy real money /
    volume behind them - the strongest short-term demand signals.
    """
    scores = {}

    def bump(sym, pts, info):
        if not sym:
            return
        entry = scores.setdefault(
            sym, {"symbol": sym, "score": 0.0, "signals": []}
        )
        entry["score"] += pts
        entry.update(info)
        entry["signals"].append(pts)

    try:
        for r in get_volume_gainers(limit=30):
            mult = r.get("week1volChange") or 0
            # Cap the volume-multiplier contribution so one crazy outlier
            # doesn't dominate the board.
            pts = min(mult / 10.0, 15.0)
            bump(
                r["symbol"],
                pts,
                {"ltp": r.get("ltp"), "pChange": r.get("pChange"),
                 "volMult": mult},
            )
    except Exception:
        pass

    try:
        for i, r in enumerate(get_most_active("value", limit=20)):
            bump(
                r["symbol"],
                20 - i,  # higher rank = more money flow
                {"ltp": r.get("ltp"), "pChange": r.get("pChange"),
                 "value": r.get("value")},
            )
    except Exception:
        pass

    try:
        for r in get_variations("gainers", limit=20):
            pc = r.get("pChange") or 0
            bump(
                r["symbol"],
                pc / 2.0,  # reward strong % gains
                {"ltp": r.get("ltp"), "pChange": r.get("pChange")},
            )
    except Exception:
        pass

    ranked = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    for r in ranked:
        r["score"] = round(r["score"], 1)
        r["signalCount"] = len(r.pop("signals"))
    return ranked[:limit]


# Short-lived cache for a broad symbol -> last price map, assembled from every
# live list we already fetch. Used by the paper-trading engine to price fills.
_ltp_cache = {"ts": 0.0, "map": {}}
_LTP_TTL = 10  # seconds


def get_price_map():
    """
    Merge the LTPs from every live list into one {symbol: price} dict. This is
    a best-effort price source limited to symbols currently appearing in the
    hot lists (~100-150 names). NSE's per-stock quote endpoint is blocked, so
    this is the most reliable price lookup we have without a broker feed.
    """
    if (time.time() - _ltp_cache["ts"]) < _LTP_TTL and _ltp_cache["map"]:
        return _ltp_cache["map"]

    pmap = {}

    def absorb(rows):
        for r in rows or []:
            sym, ltp = r.get("symbol"), r.get("ltp")
            if sym and ltp is not None:
                pmap[sym] = ltp

    for fn in (
        lambda: get_most_active("volume", 50),
        lambda: get_most_active("value", 50),
        lambda: get_volume_gainers(50),
        lambda: get_variations("gainers", 500),
        lambda: get_variations("losers", 500),
        lambda: get_oi_spurts(50),
    ):
        try:
            absorb(fn())
        except Exception:
            pass

    _ltp_cache["ts"] = time.time()
    _ltp_cache["map"] = pmap
    return pmap


def get_price(symbol):
    """
    Return the latest known price for a symbol. First checks the merged hot-list
    map (fast, no extra request); falls back to the per-stock NextApi quote so
    that ANY tradable symbol can be priced (enables paper-trading anything).
    """
    if not symbol:
        return None
    sym = symbol.upper()
    price = get_price_map().get(sym)
    if price is not None:
        return price
    # Lazy import avoids a circular dependency (nse_quote imports this module).
    try:
        import nse_quote
        return nse_quote.get_ltp(sym)
    except Exception:
        return None
