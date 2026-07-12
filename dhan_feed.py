"""
Dhan live market feed (WebSocket) -> in-memory tick store
=========================================================
Everything else in this project is HTTP polling of NSE's unofficial endpoints
(finest granularity ~1-min candles + ~10-12s quote snapshots). This module adds
a *genuine* realtime source: Dhan's free Live Market Feed over WebSocket, which
pushes tick-by-tick LTP, day OHLC/volume, OI and 5-level market depth.

Design
------
- A single background supervisor thread holds one `MarketFeed` connection
  (dhanhq SDK) and reconnects on drop / rebuilds on hard failure.
- Every packet updates an in-memory `_latest[securityId]` record and folds the
  LTP into a *forming 1-minute candle* (`_bars`) whose timestamp uses the same
  "IST wall-clock baked as UTC" epoch convention as `nse_quote.get_ohlc`, so the
  live bar lines up perfectly with the seeded historical candles on the chart.
- Flask reads this store (snapshot / SSE); it never blocks on the socket.

Credentials (never committed):
  env  DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN
  or   dhan_config.json  {"client_id": "...", "access_token": "..."}
Generate the access token at web.dhan.co -> Profile -> Access DhanHQ APIs.

The whole module degrades gracefully: with no creds (or the SDK missing) it just
reports `configured=False` and the app runs exactly as before.
"""

import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

IST = timezone(timedelta(hours=5, minutes=30))

# Dhan publishes a public instrument master (symbol -> numeric securityId).
SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CONFIG_JSON = os.path.join(os.path.dirname(__file__), "dhan_config.json")
SCRIP_TTL = 86400          # refresh the instrument master at most once a day

# MarketFeed constants (mirrors dhanhq.MarketFeed): NSE cash = segment 1, and the
# v2 "Full" packet (21) carries LTP + day OHLC/volume + OI + 5-level depth.
SEG_NSE = 1
MODE_FULL = 21

PROVIDER = "dhan"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_config():
    """(client_id, access_token) from env first, then dhan_config.json."""
    cid = os.environ.get("DHAN_CLIENT_ID")
    tok = os.environ.get("DHAN_ACCESS_TOKEN")
    if cid and tok:
        return cid.strip(), tok.strip()
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("client_id") or "").strip(), (d.get("access_token") or "").strip()
    except Exception:
        return None, None


def is_configured():
    cid, tok = _load_config()
    return bool(cid and tok)


def sdk_available():
    try:
        import dhanhq  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Instrument master (symbol <-> securityId, NSE cash EQ)
# ---------------------------------------------------------------------------
_scrip_lock = threading.Lock()
_sym2sec = {}      # "RELIANCE" -> "2885"
_sec2sym = {}      # "2885"     -> "RELIANCE"
_scrip_at = 0.0


def _load_scrip(force=False):
    """Download + cache the NSE-equity slice of Dhan's scrip master."""
    global _scrip_at
    with _scrip_lock:
        if _sym2sec and not force and (time.time() - _scrip_at) < SCRIP_TTL:
            return
        try:
            r = requests.get(SCRIP_URL, timeout=30)
            r.raise_for_status()
        except Exception:
            return
        m, rev = {}, {}
        for row in csv.DictReader(io.StringIO(r.text)):
            if (row.get("SEM_EXM_EXCH_ID") == "NSE"
                    and row.get("SEM_SEGMENT") == "E"
                    and row.get("SEM_SERIES") == "EQ"):
                sym = (row.get("SEM_TRADING_SYMBOL") or "").upper().strip()
                sid = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                if sym and sid:
                    m[sym] = sid
                    rev[sid] = sym
        if m:
            _sym2sec.clear(); _sym2sec.update(m)
            _sec2sym.clear(); _sec2sym.update(rev)
            _scrip_at = time.time()


def resolve(symbol):
    """Symbol -> NSE-equity securityId (str), or None."""
    if not _sym2sec:
        _load_scrip()
    return _sym2sec.get((symbol or "").upper().strip())


def scrip_count():
    return len(_sym2sec)


# ---------------------------------------------------------------------------
# In-memory live store
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_latest = {}       # sid -> {symbol, ltp, open, high, low, close, prevClose, volume, atp, oi, depth, ts}
_bars = {}         # sid -> forming 1-min candle {t, o, h, l, c, v, _sv}
_watch = set()     # sids currently subscribed (Full)
_focus = None      # sid the chart is centered on

_feed = None
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
    catch the pre-open/closing auction), but crucially we do NOT connect outside
    it: Dhan rate-limits connection attempts, and idle sockets outside hours just
    drop + reconnect in a storm (HTTP 429). No ticks flow then anyway."""
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


def _note_err(e):
    _status["error"] = str(e)
    _status["errorAt"] = _now_iso()


def _set_conn(on):
    _status["connected"] = bool(on)
    if on:
        _status["connectedAt"] = _now_iso()


def _norm_depth(depth):
    bids, asks = [], []
    for lvl in (depth or [])[:5]:
        bids.append({"price": _to_f(lvl.get("bid_price")),
                     "qty": lvl.get("bid_quantity") or 0})
        asks.append({"price": _to_f(lvl.get("ask_price")),
                     "qty": lvl.get("ask_quantity") or 0})
    return {"bids": bids, "asks": asks}


def _update_bar(sid, now_ms, ltp, day_vol):
    """Fold an LTP tick into the forming 1-min candle; persist finished minutes."""
    tmin = now_ms - (now_ms % 60000)
    bar = _bars.get(sid)
    if bar is None or bar["t"] != tmin:
        if bar is not None:
            _finalize_bar(sid, bar)
        _bars[sid] = {"t": tmin, "o": ltp, "h": ltp, "l": ltp, "c": ltp,
                      "v": 0, "_sv": day_vol if day_vol is not None else 0}
    else:
        bar["h"] = max(bar["h"], ltp)
        bar["l"] = min(bar["l"], ltp)
        bar["c"] = ltp
        if day_vol is not None and bar["_sv"]:
            bar["v"] = max(0, day_vol - bar["_sv"])
        elif day_vol is not None:
            bar["_sv"] = day_vol


def _finalize_bar(sid, bar):
    """Store a completed 1-min bar into db.min_bars (warms the backtest cache)."""
    sym = _sec2sym.get(sid)
    if not sym or bar.get("o") is None:
        return
    try:
        import db
        db.min_bars_put(sym, [{"t": bar["t"], "o": bar["o"], "h": bar["h"],
                               "l": bar["l"], "c": bar["c"], "v": bar["v"]}])
    except Exception:
        pass


def _on_message(feed, msg):
    """dhanhq callback: parsed packet dict (Ticker/Quote/Full/PrevClose/...)."""
    if not isinstance(msg, dict):
        return  # market-status string / disconnect / unknown
    sid = msg.get("security_id")
    if sid is None:
        return
    sid = str(sid)
    now_ms = _baked_ms()
    ltp = _to_f(msg.get("LTP"))
    with _lock:
        rec = _latest.setdefault(sid, {"symbol": _sec2sym.get(sid)})
        rec["ts"] = now_ms
        if not rec.get("symbol"):
            rec["symbol"] = _sec2sym.get(sid)
        if ltp is not None:
            rec["ltp"] = ltp
        for src, dst in (("open", "open"), ("high", "high"), ("low", "low"),
                         ("close", "close"), ("avg_price", "atp")):
            if src in msg:
                rec[dst] = _to_f(msg.get(src))
        if "volume" in msg:
            rec["volume"] = msg.get("volume")
        if "OI" in msg:
            rec["oi"] = msg.get("OI")
        if msg.get("type") == "Previous Close":
            rec["prevClose"] = _to_f(msg.get("prev_close"))
        if "depth" in msg:
            rec["depth"] = _norm_depth(msg.get("depth"))
        if ltp is not None:
            _update_bar(sid, now_ms, ltp, rec.get("volume"))
    _status["lastMsgAt"] = now_ms
    _status["msgs"] += 1


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------
def _instruments():
    """Current watch set as dhanhq (segment, securityId, mode) tuples."""
    return [(SEG_NSE, sid, MODE_FULL) for sid in _watch]


def set_watch(symbols, focus=None):
    """Replace the watched set; subscribe/unsubscribe the delta on the live feed."""
    _load_scrip()
    want, unresolved = {}, []
    for s in (symbols or []):
        sid = resolve(s)
        if sid:
            want[sid] = (s or "").upper().strip()
        else:
            unresolved.append((s or "").upper().strip())

    global _focus
    with _lock:
        cur = set(_watch)
        newset = set(want)
        add, rem = newset - cur, cur - newset
        _watch.clear(); _watch.update(newset)
        for sid, sym in want.items():
            _latest.setdefault(sid, {"symbol": sym})
        if focus:
            _focus = resolve(focus) or _focus

    f = _feed
    if f is not None:
        try:
            if add:
                f.subscribe_symbols([(SEG_NSE, sid, MODE_FULL) for sid in add])
            if rem:
                f.unsubscribe_symbols([(SEG_NSE, sid, MODE_FULL) for sid in rem])
        except Exception as e:
            _note_err(e)

    return {"resolved": {want[s]: s for s in want}, "unresolved": unresolved,
            "watching": sorted(want.values())}


def snapshot(symbols=None):
    """Latest record per symbol (watched set by default), incl. the forming bar."""
    with _lock:
        if symbols:
            sids = [resolve(s) for s in symbols]
        else:
            sids = list(_watch)
        out = {}
        for sid in sids:
            if not sid:
                continue
            rec = _latest.get(sid)
            sym = (rec or {}).get("symbol") or _sec2sym.get(sid)
            if not sym:
                continue
            r = {k: v for k, v in (rec or {}).items()}
            bar = _bars.get(sid)
            if bar:
                r["bar"] = {"t": bar["t"], "o": bar["o"], "h": bar["h"],
                            "l": bar["l"], "c": bar["c"], "v": bar["v"]}
            out[sym] = r
        return out


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------
def _supervise():
    """Own the MarketFeed connection.

    Two things keep us on the right side of Dhan's connection limits:
      1. We only hold the socket during `_market_window()` — no ticks flow outside
         it, and an idle socket just drops + reconnects in a storm (HTTP 429).
      2. We drive the SDK's *pull* API (run_forever + get_data) instead of its
         run() loop, so reconnects use OUR exponential backoff. The SDK's own
         loop reconnects every ~1s, which trips the rate limit on a flaky link.
    Dynamic (un)subscribe still works: set_watch() schedules the packet onto this
    feed's event loop, and each fresh connect re-subscribes the current watch set.
    """
    global _feed
    from dhanhq import DhanContext, MarketFeed
    backoff = 5
    while _running:
        if not _market_window():
            _set_conn(False)
            _status["error"] = None          # drop stale outside-hours errors
            _sleep_interruptible(30)
            continue

        cid, tok = _load_config()
        if not (cid and tok):
            _sleep_interruptible(10)
            continue

        feed = None
        try:
            ctx = DhanContext(cid, tok)
            feed = MarketFeed(ctx, _instruments(), version="v2")
            _feed = feed
            feed.run_forever()               # connect once (blocks until open)
            _set_conn(True)
            _status["error"] = None
            backoff = 5                      # healthy connect -> reset backoff
            while _running and _market_window():
                data = feed.get_data()       # blocks for the next packet
                _on_message(feed, data)
        except Exception as e:
            _note_err(e)
        finally:
            _set_conn(False)
            try:
                if feed is not None:
                    feed.close_connection()
            except Exception:
                pass
            _feed = None

        if _running and _market_window():
            _status["restarts"] += 1
            _sleep_interruptible(backoff)
            backoff = min(backoff * 2, 60)   # exponential backoff vs HTTP 429


def start():
    """Start the feed if credentials + SDK are present (no-op otherwise)."""
    global _running, _supervisor
    if _running:
        return
    if not (is_configured() and sdk_available()):
        return
    _running = True
    _load_scrip()
    _supervisor = threading.Thread(target=_supervise, daemon=True, name="dhan-feed")
    _supervisor.start()


def stop():
    global _running
    _running = False
    f = _feed
    if f is not None:
        try:
            f.close_connection()
        except Exception:
            pass


def public_status():
    """Config + connection health for the UI (safe to expose; no secrets)."""
    with _lock:
        watching = sorted(
            (_latest.get(sid) or {}).get("symbol") or _sec2sym.get(sid) or sid
            for sid in _watch
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
