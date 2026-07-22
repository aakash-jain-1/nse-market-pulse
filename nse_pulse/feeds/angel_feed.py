"""
Angel One SmartAPI live market feed (WebSocket) -> in-memory tick store
=======================================================================
Drop-in sibling of `dhan_feed.py` with the SAME public interface
(is_configured / sdk_available / start / stop / set_watch / snapshot /
public_status), so `app.py` can swap data sources without touching the routes,
SSE, chart or depth ladder.

Why Angel One: SmartAPI's Live Market Feed (WebSocket) + historical data are
genuinely FREE (Angel monetises brokerage on real orders, not API/data access),
unlike Dhan whose data feed is a paid ₹499/mo subscription. Angel's SNAP_QUOTE
packet carries LTP + day OHLC/volume + OI + top-5 market depth — everything the
Live tab needs.

Auth (never committed): a login with client code + MPIN + a TOTP 6-digit code.
We store the TOTP *secret* and compute the code with `pyotp`, so the daily
session refresh is fully automatic (no manual step). Credentials come from:
  env  ANGEL_API_KEY + ANGEL_CLIENT_CODE + ANGEL_MPIN + ANGEL_TOTP_SECRET
  or   angel_config.json {"api_key","client_code","mpin","totp_secret"}
Create the API key at smartapi.angelone.in (Create an App). Enable TOTP on the
Angel account to get the base32 secret.

Everything degrades gracefully: with no creds (or the SDK missing) it reports
configured=False and the app runs exactly as before.

Note on order APIs: since 01-Apr-2026 NSE requires a registered static IP for
API *order execution*. This module only streams market DATA (no orders), which
has no such requirement — it works fine on a normal home/dynamic IP.
"""

import collections
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

from nse_pulse.core import paths

IST = timezone(timedelta(hours=5, minutes=30))

PROVIDER = "angel"

# Angel One publishes a full instrument master (symbol -> numeric token) as JSON.
SCRIP_URL = ("https://margincalculator.angelbroking.com/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")
CONFIG_JSON = paths.root("angel_config.json")
SCRIP_TTL = 86400          # refresh the instrument master at most once a day

# SmartWebSocketV2 constants (mirror the SDK): NSE cash = exchangeType 1, and the
# SNAP_QUOTE mode (3) carries LTP + day OHLC/volume + OI + best-5 depth.
NSE_CM = 1
SNAP_QUOTE = 3
CORR_ID = "nsepulse01"      # 10-char tracking id echoed back on errors

# Angel streams prices as integers in paise (₹ × 100).
PAISE = 100.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_config_cache = {"mtime": None, "data": None}


def _load_config():
    """dict(api_key, client_code, mpin, totp_secret) from env first, then json."""
    env = {
        "api_key": os.environ.get("ANGEL_API_KEY"),
        "client_code": os.environ.get("ANGEL_CLIENT_CODE"),
        "mpin": os.environ.get("ANGEL_MPIN"),
        "totp_secret": os.environ.get("ANGEL_TOTP_SECRET"),
    }
    if all(env.values()):
        return {k: (v or "").strip() for k, v in env.items()}
    # Cache the parsed JSON keyed on file mtime, so the per-second SSE status
    # poll doesn't re-read + re-parse the file every tick (AUDIT.md L5).
    try:
        mtime = os.path.getmtime(CONFIG_JSON)
    except OSError:
        mtime = None
    c = _config_cache
    if c["data"] is not None and c["mtime"] == mtime:
        return c["data"]
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            d = json.load(f)
        data = {
            "api_key": (d.get("api_key") or "").strip(),
            "client_code": (d.get("client_code") or "").strip(),
            "mpin": (d.get("mpin") or d.get("pin") or "").strip(),
            "totp_secret": (d.get("totp_secret") or d.get("totp") or "").strip(),
        }
    except Exception:
        data = {"api_key": "", "client_code": "", "mpin": "", "totp_secret": ""}
    _config_cache.update(mtime=mtime, data=data)
    return data


def is_configured():
    c = _load_config()
    return bool(c["api_key"] and c["client_code"] and c["mpin"] and c["totp_secret"])


def sdk_available():
    try:
        import SmartApi  # noqa: F401
        import pyotp     # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Instrument master (symbol <-> token, NSE cash EQ)
# ---------------------------------------------------------------------------
_scrip_lock = threading.Lock()
_sym2sec = {}      # "RELIANCE" -> "2885"
_sec2sym = {}      # "2885"     -> "RELIANCE"
_scrip_at = 0.0


def _load_scrip(force=False):
    """Download + cache the NSE-equity slice of Angel's scrip master."""
    global _scrip_at
    with _scrip_lock:
        if _sym2sec and not force and (time.time() - _scrip_at) < SCRIP_TTL:
            return
        try:
            r = requests.get(SCRIP_URL, timeout=60)
            r.raise_for_status()
            rows = r.json()
        except Exception:
            return
        m, rev = {}, {}
        for row in rows:
            if row.get("exch_seg") != "NSE":
                continue
            sym = (row.get("symbol") or "").upper().strip()
            if not sym.endswith("-EQ"):        # cash equities carry the -EQ series
                continue
            tok = str(row.get("token") or "").strip()
            trad = sym[:-3]                     # "RELIANCE-EQ" -> "RELIANCE"
            name = (row.get("name") or "").upper().strip()
            if not tok:
                continue
            for key in (trad, name):
                if key:
                    m.setdefault(key, tok)
            rev[tok] = trad or name
        if m:
            _sym2sec.clear(); _sym2sec.update(m)
            _sec2sym.clear(); _sec2sym.update(rev)
            _scrip_at = time.time()


def resolve(symbol):
    """Symbol -> NSE-equity token (str), or None."""
    if not _sym2sec:
        _load_scrip()
    return _sym2sec.get((symbol or "").upper().strip())


def scrip_count():
    return len(_sym2sec)


# ---------------------------------------------------------------------------
# In-memory live store  (identical shape to dhan_feed so the UI is unchanged)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_latest = {}       # token -> {symbol, ltp, open, high, low, prevClose, volume, atp, oi, depth, ts}
_bars = {}         # token -> forming 1-min candle {t, o, h, l, c, v, _sv}
_watch = set()     # tokens currently subscribed (SNAP_QUOTE)
_focus = None      # token the chart is centered on

_sws = None
_smart = None
_running = False
_supervisor = None
_status = {
    "connected": False, "connectedAt": None, "lastMsgAt": None,
    "msgs": 0, "restarts": 0, "error": None, "errorAt": None,
}


def _now_iso():
    return datetime.now(IST).isoformat(timespec="seconds")


def is_market_open(dt=None):
    """NSE cash: Mon-Fri 09:15-15:30 IST."""
    dt = dt or datetime.now(IST)
    if dt.weekday() >= 5:
        return False
    m = dt.hour * 60 + dt.minute
    return (9 * 60 + 15) <= m <= (15 * 60 + 30)


def _market_window(dt=None):
    """When to actually HOLD the socket. Slightly wider than is_market_open (to
    catch the pre-open/closing auction). We do NOT connect outside it: no ticks
    flow then, and idle reconnect loops just waste login/connection attempts."""
    dt = dt or datetime.now(IST)
    if dt.weekday() >= 5:
        return False
    m = dt.hour * 60 + dt.minute
    return (9 * 60 + 8) <= m <= (15 * 60 + 40)


def _sleep_interruptible(secs):
    """Sleep in 1s steps so stop() stays responsive."""
    for _ in range(int(secs)):
        if not _running:
            return
        time.sleep(1)


def _baked_ms():
    """Epoch ms with IST wall-clock baked in as if UTC (matches get_ohlc `t`)."""
    now = datetime.now(IST).replace(tzinfo=None)
    return int(now.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _to_f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _px(x):
    """Paise int -> rupees float (or None)."""
    v = _to_f(x)
    return (v / PAISE) if v is not None else None


def _note_err(e):
    _status["error"] = str(e)
    _status["errorAt"] = _now_iso()


def _set_conn(on):
    _status["connected"] = bool(on)
    if on:
        _status["connectedAt"] = _now_iso()


def _norm_depth(buy, sell):
    def side(lst):
        out = []
        for lvl in (lst or [])[:5]:
            out.append({"price": _px(lvl.get("price")),
                        "qty": lvl.get("quantity") or 0})
        return out
    return {"bids": side(buy), "asks": side(sell)}


def _update_bar(tok, now_ms, ltp, day_vol):
    """Fold an LTP tick into the forming 1-min candle; persist finished minutes."""
    tmin = now_ms - (now_ms % 60000)
    bar = _bars.get(tok)
    if bar is None or bar["t"] != tmin:
        if bar is not None:
            _finalize_bar(tok, bar)
        _bars[tok] = {"t": tmin, "o": ltp, "h": ltp, "l": ltp, "c": ltp,
                      "v": 0, "_sv": day_vol if day_vol is not None else 0}
    else:
        bar["h"] = max(bar["h"], ltp)
        bar["l"] = min(bar["l"], ltp)
        bar["c"] = ltp
        if day_vol is not None and bar["_sv"]:
            bar["v"] = max(0, day_vol - bar["_sv"])
        elif day_vol is not None:
            bar["_sv"] = day_vol


def _finalize_bar(tok, bar):
    """Store a completed 1-min bar into db.min_bars (warms the backtest cache)."""
    sym = _sec2sym.get(tok)
    if not sym or bar.get("o") is None:
        return
    try:
        from nse_pulse.core import db
        db.min_bars_put(sym, [{"t": bar["t"], "o": bar["o"], "h": bar["h"],
                               "l": bar["l"], "c": bar["c"], "v": bar["v"]}])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WebSocket callbacks (bound onto the SmartWebSocketV2 instance)
# ---------------------------------------------------------------------------
def _on_data(wsapp, msg):
    """SmartWebSocketV2 SNAP_QUOTE packet (dict). Prices are in paise."""
    if not isinstance(msg, dict):
        return
    tok = msg.get("token")
    if not tok:
        return
    tok = str(tok)
    now_ms = _baked_ms()
    ltp = _px(msg.get("last_traded_price"))
    with _lock:
        rec = _latest.setdefault(tok, {"symbol": _sec2sym.get(tok)})
        rec["ts"] = now_ms
        if not rec.get("symbol"):
            rec["symbol"] = _sec2sym.get(tok)
        if ltp is not None:
            rec["ltp"] = ltp
        for src, dst in (("open_price_of_the_day", "open"),
                         ("high_price_of_the_day", "high"),
                         ("low_price_of_the_day", "low"),
                         ("average_traded_price", "atp"),
                         ("closed_price", "prevClose")):
            if src in msg:
                rec[dst] = _px(msg.get(src))
        if "volume_trade_for_the_day" in msg:
            rec["volume"] = msg.get("volume_trade_for_the_day")
        if msg.get("open_interest"):
            rec["oi"] = msg.get("open_interest")
        if "best_5_buy_data" in msg or "best_5_sell_data" in msg:
            rec["depth"] = _norm_depth(msg.get("best_5_buy_data"),
                                       msg.get("best_5_sell_data"))
        if ltp is not None:
            _update_bar(tok, now_ms, ltp, rec.get("volume"))
    _status["lastMsgAt"] = now_ms
    _status["msgs"] += 1


def _on_open(wsapp):
    _set_conn(True)
    _status["error"] = None
    _subscribe_current()


def _on_ws_error(*args):
    _note_err(" ".join(str(a) for a in args) or "ws error")


def _on_close(wsapp):
    _set_conn(False)


def _subscribe_current():
    """Subscribe the whole current watch set (SNAP_QUOTE) on the open socket."""
    sws = _sws
    if sws is None:
        return
    with _lock:
        toks = list(_watch)
    if not toks:
        return
    try:
        sws.subscribe(CORR_ID, SNAP_QUOTE, [{"exchangeType": NSE_CM, "tokens": toks}])
    except Exception as e:
        _note_err(e)


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------
def set_watch(symbols, focus=None):
    """Replace the watched set; subscribe/unsubscribe the delta on the live feed."""
    _load_scrip()
    want, unresolved = {}, []
    for s in (symbols or []):
        tok = resolve(s)
        if tok:
            want[tok] = (s or "").upper().strip()
        else:
            unresolved.append((s or "").upper().strip())

    global _focus
    with _lock:
        cur = set(_watch)
        newset = set(want)
        add, rem = newset - cur, cur - newset
        _watch.clear(); _watch.update(newset)
        for tok, sym in want.items():
            _latest.setdefault(tok, {"symbol": sym})
        if focus:
            _focus = resolve(focus) or _focus

    sws = _sws
    if sws is not None:
        try:
            if add:
                sws.subscribe(CORR_ID, SNAP_QUOTE,
                              [{"exchangeType": NSE_CM, "tokens": list(add)}])
            if rem:
                sws.unsubscribe(CORR_ID, SNAP_QUOTE,
                                [{"exchangeType": NSE_CM, "tokens": list(rem)}])
        except Exception as e:
            _note_err(e)

    return {"resolved": {want[t]: t for t in want}, "unresolved": unresolved,
            "watching": sorted(want.values())}


def snapshot(symbols=None):
    """Latest record per symbol (watched set by default), incl. the forming bar."""
    with _lock:
        if symbols:
            toks = [resolve(s) for s in symbols]
        else:
            toks = list(_watch)
        out = {}
        for tok in toks:
            if not tok:
                continue
            rec = _latest.get(tok)
            sym = (rec or {}).get("symbol") or _sec2sym.get(tok)
            if not sym:
                continue
            r = {k: v for k, v in (rec or {}).items()}
            bar = _bars.get(tok)
            if bar:
                r["bar"] = {"t": bar["t"], "o": bar["o"], "h": bar["h"],
                            "l": bar["l"], "c": bar["c"], "v": bar["v"]}
            out[sym] = r
        return out


# ---------------------------------------------------------------------------
# On-demand REST (stock-detail modal) — serve quote/chart from Angel instead of
# NSE for ARBITRARY symbols (not just the streamed watch set), so drilling into a
# stock stops hitting NSE per-symbol. Works off-hours (historical candles / last
# close). Every helper returns None on any miss so the caller falls back to NSE.
# ---------------------------------------------------------------------------
def _rest_depth(depth):
    """Angel getMarketData FULL depth {buy:[{price,quantity}],sell:[…]} → the
    {bids:[{price,qty}×5], asks:[…]} shape nse_quote.get_quote emits."""
    def side(rows):
        out = [{"price": _to_f(r.get("price")), "qty": r.get("quantity")}
               for r in (rows or [])[:5]]
        out += [{"price": None, "qty": None}] * (5 - len(out))
        return out
    return {"bids": side(depth.get("buy")), "asks": side(depth.get("sell"))}


def _map_market_data(symbol, row):
    """Angel getMarketData FULL row → nse_quote.get_quote() shape (incl. depth)."""
    ltp, close = _to_f(row.get("ltp")), _to_f(row.get("close"))
    ch = row.get("netChange")
    pch = row.get("percentChange")
    return {
        "symbol": symbol, "companyName": None,
        "ltp": ltp,
        "change": _to_f(ch) if ch is not None else (
            round(ltp - close, 2) if (ltp is not None and close is not None) else None),
        "pChange": _to_f(pch) if pch is not None else (
            round((ltp / close - 1) * 100, 2) if (ltp and close) else None),
        "open": _to_f(row.get("open")), "dayHigh": _to_f(row.get("high")),
        "dayLow": _to_f(row.get("low")), "prevClose": close,
        "vwap": _to_f(row.get("avgPrice")),
        "volume": row.get("tradeVolume"), "value": None, "deliveryPct": None,
        "yearHigh": _to_f(row.get("52WeekHigh")), "yearLow": _to_f(row.get("52WeekLow")),
        "priceBand": None,
        "lastUpdateTime": row.get("exchFeedTime") or row.get("exchTradeTime"),
        "depth": _rest_depth(row.get("depth") or {}),
        "source": "angel",
    }


def _map_ltp(symbol, d):
    """Angel ltpData row → get_quote() shape (LTP+OHLC only; no depth/volume)."""
    ltp, close = _to_f(d.get("ltp")), _to_f(d.get("close"))
    empty = [{"price": None, "qty": None}] * 5
    return {
        "symbol": symbol, "companyName": None, "ltp": ltp,
        "change": round(ltp - close, 2) if (ltp is not None and close is not None) else None,
        "pChange": round((ltp / close - 1) * 100, 2) if (ltp and close) else None,
        "open": _to_f(d.get("open")), "dayHigh": _to_f(d.get("high")),
        "dayLow": _to_f(d.get("low")), "prevClose": close,
        "vwap": None, "volume": None, "value": None, "deliveryPct": None,
        "yearHigh": None, "yearLow": None, "priceBand": None, "lastUpdateTime": None,
        "depth": {"bids": list(empty), "asks": list(empty)}, "source": "angel",
    }


def rest_quote(symbol):
    """Per-stock quote (shaped like nse_quote.get_quote) from Angel's REST — FULL
    market data (with 5-level depth) when the SDK supports it, else ltpData. Returns
    None unless logged in and the symbol resolves; None on any error so app.py falls
    back to NSE. Broker REST isn't behind NSE's Akamai, so this dodges the block."""
    smart = _smart
    if smart is None:
        return None
    sym = (symbol or "").upper().strip()
    tok = resolve(sym)
    if not tok:
        return None
    trad = _sec2sym.get(tok) or sym
    try:
        if hasattr(smart, "getMarketData"):
            resp = smart.getMarketData("FULL", {"NSE": [tok]})
            fetched = (((resp or {}).get("data") or {}).get("fetched") or [])
            if fetched:
                return _map_market_data(sym, fetched[0])
        resp = smart.ltpData("NSE", trad + "-EQ", tok)
        d = (resp or {}).get("data") or {}
        if d.get("ltp") is not None:
            return _map_ltp(sym, d)
    except Exception:
        return None
    return None


# Angel's historical (getCandleData) API is rate-limited on THREE sliding windows —
# per Angel's own docs: 3/sec, 180/min, 5000/hour. When bursted it returns a plain-text
# "Access denied because of exceeding access rate" (surfaced by the SDK as a
# DataException) — e.g. a user clicking through 1m/5m/15m/D or flicking between stocks.
# The nasty one is the *sliding* per-minute window: 180 calls in the first 10s blocks
# you for the rest of the minute even if your per-second rate is then zero. So we
# proactively honor BOTH the per-second gap and the per-minute cap (with headroom), and
# on an actual trip we back off exponentially (1s→2s→4s, Angel's recommendation). This
# keeps broker-first candles instead of needlessly falling back to NSE. Verified live:
# the calls are correct; only bursts trip it.
_candle_lock = threading.Lock()
_candle_calls = collections.deque()      # epoch secs of recent calls (sliding 60s window)
_CANDLE_MIN_GAP = 0.4                     # per-second cap 3/s → ~0.4s apart (headroom)
_CANDLE_PER_MIN = 170                     # per-minute cap 180 (sliding) → keep headroom
_CANDLE_BACKOFF = (1.0, 2.0, 4.0)        # exponential backoff on a rate-limit response

# Short TTL cache of candle rows. Re-opening the same stock/interval (or the modal's
# rest_ohlc + rest_chart fallback + the Live seed all wanting the same series) then serves
# from memory — fewer Angel calls, snappier UI, more headroom under the 180/min cap. Keyed
# by (token, interval, from-DATE) so different intervals/lookbacks don't collide; todate is
# excluded (the TTL handles the forming last candle, which the WebSocket refines live anyway).
_candle_cache = {}                       # key -> (epoch_ts, rows)
_candle_cache_lock = threading.Lock()
_CANDLE_TTL = 30.0
_CANDLE_CACHE_MAX = 256


def _candle_key(params):
    return "%s|%s|%s" % (params.get("symboltoken"), params.get("interval"),
                         (params.get("fromdate") or "")[:10])


def _candle_cache_get(key):
    with _candle_cache_lock:
        hit = _candle_cache.get(key)
        if hit and (time.time() - hit[0]) < _CANDLE_TTL:
            return hit[1]
    return None


def _candle_cache_put(key, rows):
    with _candle_cache_lock:
        if len(_candle_cache) >= _CANDLE_CACHE_MAX:     # bound memory: drop oldest half
            for k in sorted(_candle_cache, key=lambda k: _candle_cache[k][0])[:_CANDLE_CACHE_MAX // 2]:
                _candle_cache.pop(k, None)
        _candle_cache[key] = (time.time(), rows)


def _candle_throttle():
    """Block until a getCandleData call fits Angel's 3/s + 180/min sliding limits.
    Assumes _candle_lock is held (serializes all candle traffic)."""
    now = time.time()
    if _candle_calls:                                    # per-second: min gap
        gap = now - _candle_calls[-1]
        if gap < _CANDLE_MIN_GAP:
            time.sleep(_CANDLE_MIN_GAP - gap)
            now = time.time()
    while _candle_calls and now - _candle_calls[0] > 60:  # drop calls out of the window
        _candle_calls.popleft()
    if len(_candle_calls) >= _CANDLE_PER_MIN:            # per-minute: wait for room
        wait = 60 - (now - _candle_calls[0]) + 0.05
        if wait > 0:
            time.sleep(wait)
        now = time.time()
        while _candle_calls and now - _candle_calls[0] > 60:
            _candle_calls.popleft()


def _get_candles(smart, params):
    """Serialized, rate-limit-aware, TTL-cached getCandleData → list of candle rows
    (possibly empty), or None on failure. Serves a fresh cache hit without any Angel
    call; otherwise honors Angel's documented historical limits (3/s + 180/min sliding)
    and, on an actual rate-limit response, backs off exponentially (1s→2s→4s) so bursts
    degrade to a small delay instead of an NSE fallback."""
    key = _candle_key(params)
    cached = _candle_cache_get(key)                      # fast path: no lock, fully concurrent
    if cached is not None:
        return cached
    with _candle_lock:
        cached = _candle_cache_get(key)                  # re-check: a peer may have just filled it
        if cached is not None:
            return cached
        for attempt in range(len(_CANDLE_BACKOFF) + 1):
            _candle_throttle()
            _candle_calls.append(time.time())            # count the attempt (Angel does)
            try:
                rows = (smart.getCandleData(params) or {}).get("data") or []
                _candle_cache_put(key, rows)             # cache success only (incl. empty)
                return rows
            except Exception as e:
                if attempt >= len(_CANDLE_BACKOFF) or "access rate" not in str(e).lower():
                    return None
                time.sleep(_CANDLE_BACKOFF[attempt])     # exponential backoff, then retry
    return None


def rest_chart(symbol, days=5):
    """Intraday 5-min candles from Angel's getCandleData, shaped like
    nse_quote.get_chart() (points:[{t: epoch_ms, price}]). Works off-hours
    (historical). None on any miss → app.py falls back to NSE."""
    smart = _smart
    if smart is None:
        return None
    sym = (symbol or "").upper().strip()
    tok = resolve(sym)
    if not tok:
        return None
    now = datetime.now(IST)
    params = {
        "exchange": "NSE", "symboltoken": tok, "interval": "FIVE_MINUTE",
        "fromdate": (now - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    rows = _get_candles(smart, params)
    if rows is None:
        return None
    points = []
    for r in rows:
        if len(r) >= 5:
            t, c = _baked_iso_to_ms(r[0]), _to_f(r[4])
            if t is not None and c is not None:
                points.append({"t": t, "price": c})
    if not points:
        return None
    return {"symbol": sym, "name": None, "prevClose": None,
            "points": points, "source": "angel"}


# Angel getCandleData interval keywords, keyed by minutes-per-candle.
_ANGEL_IV = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE",
             15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE", 60: "ONE_HOUR"}


def rest_ohlc(symbol, interval=1, chart_type="I", days=None):
    """OHLCV candles from Angel's getCandleData, shaped like nse_quote.get_ohlc
    (points:[{t: baked-ms, o,h,l,c,v}]) — so the Live-tab chart seed and the detail
    modal's candles come from the broker instead of NSE. Daily when chart_type='D'.
    Returns None on any miss so app.py falls back to NSE. Works off-hours (historical)."""
    smart = _smart
    if smart is None:
        return None
    sym = (symbol or "").upper().strip()
    tok = resolve(sym)
    if not tok:
        return None
    daily = (chart_type == "D")
    try:
        iv = int(interval or 1)
    except (TypeError, ValueError):
        iv = 1
    ang_iv = "ONE_DAY" if daily else _ANGEL_IV.get(iv, "ONE_MINUTE")
    lookback = days or (120 if daily else 5)
    now = datetime.now(IST)
    params = {
        "exchange": "NSE", "symboltoken": tok, "interval": ang_iv,
        "fromdate": (now - timedelta(days=max(1, lookback))).strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    rows = _get_candles(smart, params)
    if rows is None:
        return None
    points = []
    for r in rows:
        if len(r) >= 6:
            t = _baked_iso_to_ms(r[0])
            if t is not None:
                points.append({"t": t, "o": _to_f(r[1]), "h": _to_f(r[2]),
                               "l": _to_f(r[3]), "c": _to_f(r[4]), "v": _to_f(r[5])})
    if len(points) < 2:
        return None
    return {"symbol": sym, "token": tok, "interval": interval,
            "chartType": chart_type, "points": points, "source": "angel"}


def _baked_iso_to_ms(s):
    """Angel candle ISO ('2026-07-20T09:15:00+05:30') → epoch ms with the IST
    wall-clock baked in as if UTC — matching get_ohlc's `t` and the live forming bar
    (_baked_ms), so seeded history and live bars line up on the chart. None on error."""
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)             # normalize to IST wall-clock
    dt = dt.replace(tzinfo=None)            # then treat that wall-clock as UTC
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------
def _quiet_logs():
    """SmartWebSocketV2 logs every tick at INFO into ./logs/<date>/app.log — mute
    it so we don't balloon a log file (and spam the console) during market hours."""
    try:
        import logging
        import logzero
        logzero.logger.setLevel(logging.WARNING)
        for h in logzero.logger.handlers:
            h.setLevel(logging.WARNING)
    except Exception:
        pass


def _supervise():
    """Own one SmartWebSocketV2 connection.

    Like the hardened dhan_feed: only hold the socket during `_market_window()`
    and reconnect with exponential backoff. Each (re)connect does a fresh TOTP
    login (tokens are day-scoped), and on_open re-subscribes the current watch.
    """
    global _sws, _smart
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    import pyotp

    backoff = 5
    while _running:
        if not _market_window():
            _set_conn(False)
            _status["error"] = None          # drop stale outside-hours errors
            _sleep_interruptible(30)
            continue

        cfg = _load_config()
        if not (cfg["api_key"] and cfg["client_code"] and cfg["mpin"]
                and cfg["totp_secret"]):
            _sleep_interruptible(10)
            continue

        sws = None
        try:
            smart = SmartConnect(api_key=cfg["api_key"])
            totp = pyotp.TOTP(cfg["totp_secret"]).now()
            data = smart.generateSession(cfg["client_code"], cfg["mpin"], totp)
            if not data or not data.get("status"):
                raise RuntimeError((data or {}).get("message") or "login failed")
            auth_token = data["data"]["jwtToken"]        # "Bearer <jwt>"
            feed_token = smart.getfeedToken()
            _smart = smart

            sws = SmartWebSocketV2(auth_token, cfg["api_key"], cfg["client_code"],
                                   feed_token)
            _quiet_logs()
            sws.on_open = _on_open
            sws.on_data = _on_data
            sws.on_error = _on_ws_error
            sws.on_close = _on_close
            _sws = sws
            backoff = 5                                  # healthy setup -> reset
            sws.connect()                                # blocks until socket closes
        except Exception as e:
            _note_err(e)
        finally:
            _set_conn(False)
            try:
                if sws is not None:
                    sws.close_connection()
            except Exception:
                pass
            _sws = None

        if _running and _market_window():
            _status["restarts"] += 1
            _sleep_interruptible(backoff)
            backoff = min(backoff * 2, 60)               # exponential backoff


def start():
    """Start the feed if credentials + SDK are present (no-op otherwise)."""
    global _running, _supervisor
    if _running:
        return
    if not (is_configured() and sdk_available()):
        return
    _running = True
    _load_scrip()
    _supervisor = threading.Thread(target=_supervise, daemon=True, name="angel-feed")
    _supervisor.start()


def stop():
    global _running
    _running = False
    s = _sws
    if s is not None:
        try:
            s.close_connection()
        except Exception:
            pass


def _coarse_error(err):
    """Map a raw exception string to a coarse, secret-free category for the UI.

    The status is served on an unauthenticated endpoint, and broker/HTTP errors
    can embed tokens, JWTs or request URLs — so we never surface the raw text
    (AUDIT.md M7). Full detail is in the server log.
    """
    if not err:
        return None
    s = str(err).lower()
    if any(k in s for k in ("401", "403", "unauthor", "forbidden", "denied",
                            "token", "jwt", "session", "login", "totp", "otp",
                            "credential", "api_key", "apikey")):
        return "auth_failed"
    if any(k in s for k in ("429", "rate", "too many")):
        return "rate_limited"
    if any(k in s for k in ("timeout", "timed out", "connection", "refused",
                            "reset", "unreachable", "getaddrinfo", "socket",
                            "ssl", "network", "dns")):
        return "network"
    if any(k in s for k in ("subscri", "not active", "data api", "plan", "806")):
        return "data_plan"
    return "error"


def public_status():
    """Config + connection health for the UI (safe to expose; no secrets)."""
    with _lock:
        watching = sorted(
            (_latest.get(tok) or {}).get("symbol") or _sec2sym.get(tok) or tok
            for tok in _watch
        )
    return {
        "provider": PROVIDER,
        "configured": is_configured(),
        "sdk": sdk_available(),
        "running": _running,
        "connected": _status["connected"],
        "marketOpen": is_market_open(),
        "connectedAt": _status["connectedAt"],
        "lastMsgAt": _status["lastMsgAt"],
        "msgs": _status["msgs"],
        "restarts": _status["restarts"],
        "error": _coarse_error(_status["error"]),
        "errorAt": _status["errorAt"],
        "instruments": scrip_count(),
        "watching": watching,
    }
