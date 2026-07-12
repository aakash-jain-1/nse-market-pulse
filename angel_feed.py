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

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

IST = timezone(timedelta(hours=5, minutes=30))

PROVIDER = "angel"

# Angel One publishes a full instrument master (symbol -> numeric token) as JSON.
SCRIP_URL = ("https://margincalculator.angelbroking.com/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")
CONFIG_JSON = os.path.join(os.path.dirname(__file__), "angel_config.json")
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
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return {
            "api_key": (d.get("api_key") or "").strip(),
            "client_code": (d.get("client_code") or "").strip(),
            "mpin": (d.get("mpin") or d.get("pin") or "").strip(),
            "totp_secret": (d.get("totp_secret") or d.get("totp") or "").strip(),
        }
    except Exception:
        return {"api_key": "", "client_code": "", "mpin": "", "totp_secret": ""}


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
        import db
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
        "error": _status["error"],
        "errorAt": _status["errorAt"],
        "instruments": scrip_count(),
        "watching": watching,
    }
