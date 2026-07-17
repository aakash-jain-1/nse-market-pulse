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

import logging
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("nse_client")

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
    # Size the connection pool above our fan-out. Several features sweep NSE with
    # 6-worker thread pools (intrabar catch-up, daily backtest, futures sweep) and
    # they SHARE this one session across www./charting.nseindia.com, so the
    # default per-host pool of 10 overflows — urllib3 then discards connections
    # ("connection pool is full") and re-does the TLS handshake each time. A
    # bigger pool lets it reuse connections instead.
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.get(BASE, timeout=15)
    s.get(BASE + "/market-data/live-equity-market", timeout=15)
    return s


def get_session(force=False):
    """Return a cookie-warmed session, rebuilding lazily.

    The rebuild (2 blocking NSE GETs, up to ~30s) happens OUTSIDE `_lock` so a
    slow warm-up can't serialize every other request thread (AUDIT.md M3). The
    lock is held only for the quick pointer read/swap.
    """
    global _session, _session_ts
    with _lock:
        cur, ts = _session, _session_ts
    if not force and cur is not None and (time.time() - ts) <= _SESSION_TTL:
        return cur

    try:
        fresh = _build_session()
    except Exception:
        log.warning("NSE session rebuild failed", exc_info=True)
        with _lock:
            if _session is not None:
                return _session   # fall back to the stale session
        raise

    with _lock:
        # Another thread may have rebuilt while we warmed up. If theirs is very
        # recent, keep it and discard our duplicate warm-up (avoids a stampede
        # when many threads hit a dead session at once).
        just_rebuilt = _session is not None and (time.time() - _session_ts) < 5
        still_valid = _session is not None and (time.time() - _session_ts) <= _SESSION_TTL
        if just_rebuilt or (not force and still_valid):
            return _session
        _session = fresh
        _session_ts = time.time()
        return _session


# Short-lived path-keyed cache so the SAME read-only list endpoint isn't hit
# several times within one cycle. get_scanner(), strategies.build_context() and
# get_demand_score() pull heavily OVERLAPPING hot lists (gainers/losers/volume-
# gainers/most-active/oi-spurts/futures), and the 60s snapshot logger overlaps
# the frontend polls too — so the same GET fires many times a minute. A small
# TTL collapses those duplicates into one live request without meaningfully
# changing freshness (recommendations already cache 12s, price map 20s, index
# 30s). Only successful JSON is cached; failures always retry live. Callers can
# pass ttl=0 to force a live fetch.
_FETCH_TTL = 15          # seconds
_FETCH_CACHE_MAX = 128   # paths are a fixed handful; cap is just a safety net
_fetch_cache = {}        # path -> (ts, json)
_fetch_lock = threading.Lock()


def _fetch(path, ttl=_FETCH_TTL):
    """Fetch JSON, transparently rebuilding the session once on failure.

    Results are cached per `path` for `ttl` seconds (ttl=0 forces a live fetch).

    CONTRACT (AUDIT2 N7): the cached object is SHARED — concurrent callers get the
    same dict/list back. Callers MUST treat the result as READ-ONLY; mutating it
    would corrupt the cache and every other caller's view. Copy first if you need
    to mutate (all current getters only read).
    """
    if ttl:
        hit = _fetch_cache.get(path)      # atomic read; benign if a racer refetches
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
    try:
        r = get_session().get(BASE + path, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        r = get_session(force=True).get(BASE + path, timeout=15)
        r.raise_for_status()
        data = r.json()
    if ttl:
        with _fetch_lock:
            if len(_fetch_cache) >= _FETCH_CACHE_MAX and path not in _fetch_cache:
                oldest = min(_fetch_cache, key=lambda k: _fetch_cache[k][0])
                _fetch_cache.pop(oldest, None)
            _fetch_cache[path] = (time.time(), data)
    return data


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

_index_cache = {"ts": 0.0, "data": None}
_INDEX_TTL = 30  # seconds


def get_index_snapshot():
    """
    Live snapshot of the headline indices from NSE's /api/allIndices, normalized
    to {NIFTY|BANKNIFTY|FINNIFTY|INDIAVIX: {last, pChange, prevClose, advances,
    declines, unchanged, yearHigh, yearLow}}. Each equity index row carries its
    constituents' advance/decline breadth — a cheap, reliable market-regime
    signal — while INDIA VIX adds the volatility axis (its yearHigh/yearLow let
    the regime engine compute a 52-week vol percentile). Cached ~30s.
    """
    if _index_cache["data"] and (time.time() - _index_cache["ts"]) < _INDEX_TTL:
        return _index_cache["data"]
    out = {}
    want = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY",
            "NIFTY FIN SERVICE": "FINNIFTY", "INDIA VIX": "INDIAVIX"}
    try:
        data = _fetch("/api/allIndices")
        for r in data.get("data", []):
            name = r.get("index") or r.get("indexSymbol")
            key = want.get(name)
            if not key:
                continue
            out[key] = {
                "last": _num(r.get("last")),
                "pChange": _num(r.get("percentChange")),
                "prevClose": _num(r.get("previousClose")),
                "advances": _num(r.get("advances")),
                "declines": _num(r.get("declines")),
                "unchanged": _num(r.get("unchanged")),
                "yearHigh": _num(r.get("yearHigh")),
                "yearLow": _num(r.get("yearLow")),
            }
    except Exception:
        pass
    if out:
        _index_cache.update(ts=time.time(), data=out)
    return out or (_index_cache["data"] or {})


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


_lots_cache = {"ts": 0.0, "map": None}
_LOTS_TTL = 86400  # lot sizes change only on periodic NSE revisions


def get_lot_sizes():
    """
    F&O market lot sizes for every underlying, from NSE's published
    fo_mktlots.csv (UNDERLYING, SYMBOL, then a lot column per expiry month).
    Returns {SYMBOL: lot}. Cached a day. Lot size is ~constant across the near
    months, so we take the first numeric month column.
    """
    if _lots_cache["map"] and (time.time() - _lots_cache["ts"]) < _LOTS_TTL:
        return _lots_cache["map"]
    import csv
    import io
    out = {}
    try:
        s = get_session()
        r = s.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", timeout=20)
        r.raise_for_status()
        for row in list(csv.reader(io.StringIO(r.text)))[1:]:
            if len(row) < 3:
                continue
            sym = (row[1] or "").strip().upper()
            if not sym or sym == "SYMBOL":
                continue
            lot = next((int(c.strip()) for c in row[2:] if c.strip().isdigit()), None)
            if lot:
                out[sym] = lot
    except Exception:
        pass
    if out:
        _lots_cache.update(ts=time.time(), map=out)
        return out
    if _lots_cache["map"]:
        return _lots_cache["map"]
    # fo_mktlots.csv failed and we have no cache — fall back to the FO bhavcopy's
    # per-contract lot column (static archive, no anti-bot gate).
    try:
        import bhavcopy
        lots = bhavcopy.lot_sizes()
        if lots:
            _lots_cache.update(ts=time.time(), map=lots)
            return lots
    except Exception:
        pass
    return {}


def get_lot_size(symbol):
    """Lot size (shares per contract) for one F&O underlying, or None."""
    if not symbol:
        return None
    return get_lot_sizes().get(symbol.upper().strip())


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


def _build_idea(e):
    """
    Turn one aggregated scanner record into a directional LONG/SHORT trade idea
    with a conviction score, plain-English reasons and a simple risk plan
    (entry / stop / target). Returns None when there isn't a clean directional
    edge. This is a signal summary, NOT investment advice.
    """
    ltp = e.get("ltp")
    pc = e.get("pChange")
    if ltp is None or ltp <= 0:
        return None

    sig = e.get("oiSignal")
    vm = e.get("volMult") or 0
    tags = e.get("tags") or []
    reasons = []
    bull = bear = 0.0

    # Price momentum (capped so one huge move can't dominate everything).
    if pc is not None:
        if pc > 0:
            bull += min(abs(pc), 8)
            reasons.append(f"Price up {pc:+.2f}% today")
        elif pc < 0:
            bear += min(abs(pc), 8)
            reasons.append(f"Price down {pc:+.2f}% today")

    # OI buildup = derivatives conviction; fresh positions weigh most.
    if sig == "Long buildup":
        bull += 6
        reasons.append("Long buildup — rising price with rising OI (fresh longs)")
    elif sig == "Short covering":
        bull += 3
        reasons.append("Short covering — shorts exiting into strength")
    elif sig == "Short buildup":
        bear += 6
        reasons.append("Short buildup — falling price with rising OI (fresh shorts)")
    elif sig == "Long unwinding":
        bear += 3
        reasons.append("Long unwinding — longs exiting into weakness")

    # Unusual volume amplifies whichever way price is moving.
    if vm >= 2:
        boost = min(vm / 3.0, 5.0)
        if pc is not None and pc >= 0:
            bull += boost
        elif pc is not None and pc < 0:
            bear += boost
        reasons.append(f"Unusual volume ~{vm:.1f}x 1-week average")

    if "💰 Money flow" in tags:
        reasons.append("Heavy traded value (money flow)")

    lc = e.get("listCount") or len(e.get("lists") or [])
    if lc >= 3:
        breadth = (lc - 2) * 1.5
        if pc is not None and pc >= 0:
            bull += breadth
        elif pc is not None and pc < 0:
            bear += breadth
        reasons.append(f"Shows up across {lc} independent signals")

    net = bull - bear
    if abs(net) < 2 or not reasons:
        return None

    direction = "LONG" if net > 0 else "SHORT"
    conviction = int(min(round(abs(net) / 22.0 * 100), 99))
    rating = "High" if conviction >= 66 else "Medium" if conviction >= 40 else "Low"

    # Risk plan: stop scales with how much it's already moved (more volatile =>
    # wider stop), target is 2x the risk (1:2 reward-to-risk).
    move = abs(pc) if pc is not None else 1.0
    stop_pct = max(1.0, min(move * 0.6, 3.0))
    tgt_pct = stop_pct * 2
    if direction == "LONG":
        stop = ltp * (1 - stop_pct / 100)
        target = ltp * (1 + tgt_pct / 100)
    else:
        stop = ltp * (1 + stop_pct / 100)
        target = ltp * (1 - tgt_pct / 100)

    return {
        "symbol": e["symbol"],
        "direction": direction,
        "conviction": conviction,
        "rating": rating,
        "ltp": round(ltp, 2),
        "pChange": pc,
        "entry": round(ltp, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "stopPct": round(stop_pct, 2),
        "targetPct": round(tgt_pct, 2),
        "rr": round(tgt_pct / stop_pct, 1),
        "reasons": reasons,
        "fno": e.get("fno", False),
        "oiSignal": sig,
        "volMult": round(vm, 1) if vm else None,
    }


_reco_cache = {"ts": 0.0, "data": None}
_RECO_TTL = 12  # share one scanner sweep across the Ideas tab + the new-idea alert poll


def get_recommendations(fno_only=False, limit=None):
    """
    Ranked LONG / SHORT trade ideas derived from the live signal aggregate.
    Each idea carries a conviction score, plain-English reasons and a simple
    entry/stop/target plan. Educational signal summary — NOT investment advice.

    The (unfiltered) enriched set is cached briefly so the dashboard's Ideas tab
    and the always-on new-idea alert poll don't each trigger a full scanner
    sweep; the F&O toggle only filters the already-computed view.

    `limit` (per side) is applied only to the returned view; the journal always
    records the full qualifying set. Default None = return everything.
    """
    now = time.time()
    c = _reco_cache
    if not (c["data"] and (now - c["ts"]) < _RECO_TTL):
        rows = get_scanner(limit=250)
        # Journal ALL qualifying ideas (not just the fno subset) so entries stay
        # consistent regardless of the F&O toggle; the toggle only filters the view.
        ideas = [i for i in (_build_idea(e) for e in rows) if i]
        longs = sorted([i for i in ideas if i["direction"] == "LONG"],
                       key=lambda x: x["conviction"], reverse=True)
        shorts = sorted([i for i in ideas if i["direction"] == "SHORT"],
                        key=lambda x: x["conviction"], reverse=True)

        # Fix each idea's entry + timestamp on first sight today and re-price the
        # whole day's set, so the UI can show "given HH:MM" + move-since-entry even
        # after an idea drops out of the fresh top set. Re-pricing uses ONLY the
        # cached hot-list map (a dict lookup) — never a per-symbol network fetch.
        try:
            pmap = get_price_map()
        except Exception:
            pmap = {}
        try:
            import ideas_journal
            longs, shorts = ideas_journal.enrich(longs, shorts,
                                                 price_fn=lambda s: pmap.get(s))
        except Exception:
            log.warning("ideas_journal.enrich failed; serving unenriched ideas",
                        exc_info=True)

        c["data"] = {"longs": longs, "shorts": shorts,
                     "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        c["ts"] = now

    d = c["data"]
    longs, shorts = d["longs"], d["shorts"]
    if fno_only:
        longs = [i for i in longs if i.get("fno")]
        shorts = [i for i in shorts if i.get("fno")]
    if limit:
        longs, shorts = longs[:limit], shorts[:limit]
    return {
        "longs": longs,
        "shorts": shorts,
        "count": len(longs) + len(shorts),
        "generatedAt": d["generatedAt"],
    }


_hist_cache = {}
_HIST_TTL = 900  # daily history barely changes intraday; cache 15 min


def get_stock_history(symbol, chunks=3, chunk_days=80):
    """
    Daily OHLC + volume + delivery% history via NSE's securityArchives endpoint
    (the one historical source that isn't blocked). Returns an ascending list of
    {date, iso, open, high, low, close, prevClose, vwap, volume, value, trades,
    delivQty, delivPct}. Cached ~15 min.

    NSE caps each request to ~70 trading days from `to` (ignoring an older
    `from`), so we fetch several back-to-back windows and merge to reach ~130
    trading days — enough for 90-day returns and a 50-DMA.
    """
    from datetime import timedelta
    symbol = symbol.upper().strip()
    key = (symbol, chunks, chunk_days)
    hit = _hist_cache.get(key)
    if hit and (time.time() - hit[0]) < _HIST_TTL:
        return hit[1]

    s = get_session()
    s.get(BASE + "/get-quote/equity?symbol=" + symbol, timeout=15)
    ref = {"Referer": BASE + "/get-quote/equity?symbol=" + symbol}

    merged = {}
    end = datetime.now()
    for _ in range(chunks):
        start = end - timedelta(days=chunk_days)
        f, t = start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")
        url = (f"/api/historicalOR/generateSecurityWiseHistoricalData?from={f}&to={t}"
               f"&symbol={symbol}&type=priceVolumeDeliverable&series=EQ")
        try:
            r = s.get(BASE + url, headers=ref, timeout=20)
            r.raise_for_status()
            rows = r.json().get("data", []) or []
        except Exception:
            rows = []
        for d in rows:
            iso = d.get("CH_TIMESTAMP")
            if not iso or iso in merged:
                continue
            merged[iso] = {
                "date": d.get("mTIMESTAMP"),
                "iso": iso,
                "open": _num(d.get("CH_OPENING_PRICE")),
                "high": _num(d.get("CH_TRADE_HIGH_PRICE")),
                "low": _num(d.get("CH_TRADE_LOW_PRICE")),
                "close": _num(d.get("CH_CLOSING_PRICE")),
                "prevClose": _num(d.get("CH_PREVIOUS_CLS_PRICE")),
                "vwap": _num(d.get("VWAP")),
                "volume": _num(d.get("CH_TOT_TRADED_QTY")),
                "value": _num(d.get("CH_TOT_TRADED_VAL")),
                "trades": _num(d.get("CH_TOTAL_TRADES")),
                "delivQty": _num(d.get("COP_DELIV_QTY")),
                "delivPct": _num(d.get("COP_DELIV_PERC")),
            }
        end = start - timedelta(days=1)

    out = [x for x in merged.values() if x["close"] is not None]
    out.sort(key=lambda x: x.get("iso") or "")
    _hist_cache[key] = (time.time(), out)
    return out


_focpv_cache = {}


def get_futures_oi_history(symbol, expiry, calendar_days=55):
    """
    Daily futures OI history for one contract via NSE's historicalOR/foCPV
    endpoint (the working historical-derivatives feed; needs the
    /report-detail/fo_eq_security referer and an UPPERCASE expiry like
    '28-JUL-2026'). Returns ascending [{date, close, spot, oi, changeOi,
    volume, lot}]. Cached ~15 min.
    """
    from datetime import timedelta, datetime as _dt
    symbol = symbol.upper().strip()
    if not expiry:
        return []
    exp = expiry.upper()
    key = (symbol, exp, calendar_days)
    hit = _focpv_cache.get(key)
    if hit and (time.time() - hit[0]) < _HIST_TTL:
        return hit[1]

    to = datetime.now()
    frm = to - timedelta(days=calendar_days)
    f, t = frm.strftime("%d-%m-%Y"), to.strftime("%d-%m-%Y")
    url = (f"/api/historicalOR/foCPV?from={f}&to={t}&instrumentType=FUTSTK"
           f"&symbol={symbol}&year={to.year}&expiryDate={exp}")
    s = get_session()
    ref = {"Referer": BASE + "/report-detail/fo_eq_security"}
    try:
        r = s.get(BASE + url, headers=ref, timeout=25)
        r.raise_for_status()
        rows = r.json().get("data", []) or []
    except Exception:
        rows = []

    out = []
    for d in rows:
        out.append({
            "date": d.get("FH_TIMESTAMP"),
            "close": _num(d.get("FH_CLOSING_PRICE")),
            "spot": _num(d.get("FH_UNDERLYING_VALUE")),
            "oi": _num(d.get("FH_OPEN_INT")),
            "changeOi": _num(d.get("FH_CHANGE_IN_OI")),
            "volume": _num(d.get("FH_TOT_TRADED_QTY")),
            "lot": _num(d.get("FH_MARKET_LOT")),
        })

    def _order(x):
        try:
            return _dt.strptime(x["date"], "%d-%b-%Y")
        except Exception:
            return _dt.min

    out = [x for x in out if x["oi"] is not None]
    out.sort(key=_order)
    _focpv_cache[key] = (time.time(), out)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(a, b):
    return (a / b - 1) * 100 if (a is not None and b) else None


def get_stock_deepdive(symbol):
    """
    Everything about one stock in one place: 30/60/90-day price/volume/delivery
    history + derived stats, the live derivatives (futures basis + OI buildup)
    and options (PCR / max-pain / support-resistance) picture, and a synthesized
    "what to watch today" read (bias + key levels). Educational, not advice.

    NOTE: NSE's historical F&O (OI-over-time) endpoint is blocked, so the OI
    view is the live snapshot; intraday OI history is what snapshot_logger keeps.
    """
    import math
    symbol = symbol.upper().strip()
    hist = get_stock_history(symbol)
    if not hist or len(hist) < 5:
        return {"symbol": symbol, "error": "No historical data (check the symbol; only EQ series supported)."}

    closes = [d["close"] for d in hist]
    highs = [d["high"] for d in hist if d["high"] is not None]
    lows = [d["low"] for d in hist if d["low"] is not None]
    vols = [d["volume"] or 0 for d in hist]
    delivs = [d["delivPct"] for d in hist if d["delivPct"] is not None]
    last = closes[-1]

    def ret_over(nb):
        return _pct(last, closes[-1 - nb]) if len(closes) > nb else None

    stats = {
        "lastClose": round(last, 2),
        "ret30": round(ret_over(30), 2) if ret_over(30) is not None else None,
        "ret60": round(ret_over(60), 2) if ret_over(60) is not None else None,
        "ret90": round(ret_over(90), 2) if ret_over(90) is not None else None,
        "sma20": round(_mean(closes[-20:]), 2) if len(closes) >= 20 else None,
        "sma50": round(_mean(closes[-50:]), 2) if len(closes) >= 50 else None,
        "high90": round(max(highs[-90:]), 2) if highs else None,
        "low90": round(min(lows[-90:]), 2) if lows else None,
        "avgVol20": round(_mean(vols[-20:])) if len(vols) >= 5 else None,
        "lastVol": vols[-1],
        "avgDeliv20": round(_mean(delivs[-20:]), 1) if delivs else None,
        "lastDeliv": delivs[-1] if delivs else None,
    }
    stats["volRatio"] = round(stats["lastVol"] / stats["avgVol20"], 2) if stats.get("avgVol20") else None
    stats["pctFromHigh"] = round(_pct(last, stats["high90"]), 2) if stats.get("high90") else None
    stats["pctFromLow"] = round(_pct(last, stats["low90"]), 2) if stats.get("low90") else None

    # Delivery trend: recent 5 days vs the prior 15 (rising delivery into a rally
    # = genuine accumulation rather than pure intraday churn).
    if len(delivs) >= 20:
        stats["delivTrend"] = round(_mean(delivs[-5:]) - _mean(delivs[-20:-5]), 1)
    else:
        stats["delivTrend"] = None

    # Annualized volatility from daily log-ish returns.
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) >= 10:
        mu = _mean(rets)
        var = _mean([(x - mu) ** 2 for x in rets])
        stats["annVolPct"] = round((var ** 0.5) * math.sqrt(252) * 100, 1)
    else:
        stats["annVolPct"] = None

    # Live derivatives + options (best-effort; may be thin pre-market).
    import nse_quote
    deriv = None
    try:
        deriv = nse_quote.get_near_future(symbol)
    except Exception:
        pass
    options = None
    try:
        oc = nse_quote.get_option_chain(symbol)
        if oc and not oc.get("error"):
            sup = oc.get("support") or []
            res = oc.get("resistance") or []
            options = {
                "expiry": oc.get("expiry"), "pcr": oc.get("pcr"),
                "maxPain": oc.get("maxPain"), "atm": oc.get("atmStrike"),
                "supportStrike": sup[0]["strike"] if sup else None,
                "resistanceStrike": res[0]["strike"] if res else None,
                "supportList": sup[:3], "resistanceList": res[:3],
            }
    except Exception:
        pass

    # Historical futures OI for the near contract (real OI-over-time now that
    # historicalOR/foCPV works). Gives an OI trend + lot size.
    oi_history = []
    lot_size = None
    if deriv and deriv.get("expiry"):
        try:
            oi_history = get_futures_oi_history(symbol, deriv["expiry"])
            if oi_history:
                lot_size = oi_history[-1].get("lot")
        except Exception:
            pass

    # Recent OI trend (last ~5 sessions) vs price → buildup / unwinding read.
    # Kept short so the Jun→Jul rollover (which mechanically inflates near-month
    # OI as the contract becomes front-month) doesn't masquerade as conviction.
    if len(oi_history) >= 4:
        w = oi_history[-min(5, len(oi_history)):]
        oi0, oi1 = w[0]["oi"], w[-1]["oi"]
        px0, px1 = w[0]["close"], w[-1]["close"]
        stats["oiChangePctRecent"] = round(_pct(oi1, oi0), 1) if oi0 else None
        stats["oiTrendDays"] = len(w)
        if oi0 and px0:
            oi_up = oi1 >= oi0
            px_up = px1 >= px0
            stats["oiPriceRead"] = ("Long buildup" if oi_up and px_up else
                                    "Short buildup" if oi_up and not px_up else
                                    "Short covering" if not oi_up and px_up else
                                    "Long unwinding")

    analysis = _analyze_stock(symbol, last, stats, deriv, options)

    # Trim series to ~90 points for the client chart.
    series = [
        {"date": d["date"], "close": d["close"], "volume": d["volume"], "delivPct": d["delivPct"]}
        for d in hist[-90:]
    ]
    return {
        "symbol": symbol,
        "series": series,
        "oiHistory": oi_history,
        "lotSize": lot_size,
        "stats": stats,
        "derivatives": deriv,
        "options": options,
        "analysis": analysis,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _analyze_stock(symbol, last, stats, deriv, options):
    """Synthesize a bias (score -100..100), reasons and key levels for today."""
    score = 0.0
    notes = []

    sma20, sma50 = stats.get("sma20"), stats.get("sma50")
    if sma20 and sma50:
        if last > sma20 > sma50:
            score += 25; notes.append(f"Uptrend: price ₹{last:g} > 20-DMA ₹{sma20:g} > 50-DMA ₹{sma50:g}")
        elif last < sma20 < sma50:
            score -= 25; notes.append(f"Downtrend: price ₹{last:g} < 20-DMA ₹{sma20:g} < 50-DMA ₹{sma50:g}")
        elif last > sma20:
            score += 10; notes.append(f"Price above 20-DMA (₹{sma20:g}) — short-term strength")
        elif last < sma20:
            score -= 10; notes.append(f"Price below 20-DMA (₹{sma20:g}) — short-term weakness")

    r30 = stats.get("ret30")
    if r30 is not None:
        score += max(-15, min(15, r30 * 0.8))
        notes.append(f"1-month return {r30:+.1f}%")

    # Delivery: high & rising delivery supports a real move.
    dp, dt = stats.get("avgDeliv20"), stats.get("delivTrend")
    if dp is not None:
        if dp >= 55:
            notes.append(f"High delivery ({dp:.0f}% avg) — investment-style buying, less speculative")
            score += 6
        elif dp <= 35:
            notes.append(f"Low delivery ({dp:.0f}% avg) — largely intraday/speculative")
    if dt is not None and abs(dt) >= 5:
        notes.append(f"Delivery% {'rising' if dt > 0 else 'falling'} ({dt:+.0f} pts vs prior weeks)")
        score += 4 if dt > 0 else -4

    # Volume surge.
    vr = stats.get("volRatio")
    if vr and vr >= 1.5:
        notes.append(f"Volume {vr:.1f}x its 20-day average — heightened interest")

    # Position within range.
    pfh, pfl = stats.get("pctFromHigh"), stats.get("pctFromLow")
    if pfh is not None and pfh > -3:
        notes.append(f"Near 90-day high (only {pfh:+.1f}% away) — breakout watch")
        score += 5
    if pfl is not None and pfl < 3:
        notes.append(f"Near 90-day low ({pfl:+.1f}% above) — support test / oversold watch")
        score -= 5

    # Derivatives OI buildup.
    if deriv and deriv.get("signal") and deriv.get("signal") != "OI n/a":
        sig, kind = deriv.get("signal"), deriv.get("signalKind")
        notes.append(f"Futures: {sig} (basis {deriv.get('basisPct')}% )")
        score += 12 if kind == "buildup" else -8 if kind == "short" else 0

    # Historical OI trend read (last ~10 sessions of the near contract).
    read = stats.get("oiPriceRead")
    if read:
        chg = stats.get("oiChangePctRecent")
        chg_txt = f"{chg:+.0f}% OI" if chg is not None else "OI"
        notes.append(f"{read} over last {stats.get('oiTrendDays')} sessions ({chg_txt})")
        score += 8 if read in ("Long buildup", "Short covering") else -8

    # Options positioning.
    if options and options.get("pcr") is not None:
        pcr = options["pcr"]
        if pcr >= 1.2:
            notes.append(f"Option PCR {pcr} — put-heavy (often supportive / bullish)")
            score += 6
        elif pcr <= 0.7:
            notes.append(f"Option PCR {pcr} — call-heavy (often resistance / bearish)")
            score -= 6
        else:
            notes.append(f"Option PCR {pcr} — balanced")

    score = max(-100, min(100, round(score)))
    bias = "Bullish" if score >= 20 else "Bearish" if score <= -20 else "Neutral"

    # Key levels for today: blend recent structure with option walls.
    supports = [x for x in [stats.get("sma20"), stats.get("low90"),
                            options.get("supportStrike") if options else None] if x and x < last]
    resistances = [x for x in [stats.get("high90"),
                               options.get("resistanceStrike") if options else None] if x and x > last]
    if sma20 and sma20 > last:
        resistances.append(sma20)
    support = round(max(supports), 2) if supports else None
    resistance = round(min(resistances), 2) if resistances else None

    return {
        "bias": bias,
        "score": score,
        "notes": notes,
        "support": support,
        "resistance": resistance,
    }


_all_fut_cache = {"ts": 0.0, "rows": None}
_ALL_FUT_TTL = 90  # seconds; a full sweep is expensive so cache aggressively
_all_fut_lock = threading.Lock()  # single-flight: coalesce concurrent cold sweeps


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

    # Single-flight: only one thread runs the ~215-symbol sweep; concurrent
    # callers wait and then get the just-filled cache (AUDIT.md M2).
    with _all_fut_lock:
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
    Return the latest known price for a symbol, most-live first:
      1. the merged hot-list LTP map (fast, no extra request),
      2. the per-stock NextApi quote (any symbol, live, during market hours),
      3. the NSE EOD bhavcopy close (resilient: works off-hours & when the live
         JSON API is down; prices ANY listed name — broadens the tradable
         universe well beyond the ~100-150 hot-list symbols).
    Lazy imports avoid circular dependencies (nse_quote/bhavcopy import this).
    """
    if not symbol:
        return None
    sym = symbol.upper()
    price = get_price_map().get(sym)
    if price is not None:
        return price
    try:
        import nse_quote
        price = nse_quote.get_ltp(sym)
        if price is not None:
            return price
    except Exception:
        pass
    try:
        import bhavcopy
        return bhavcopy.eod_close(sym)
    except Exception:
        return None
