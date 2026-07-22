"""
Snapshot logger + backtester
============================
Periodically records the "demand" board and volume-gainers so we can later
analyze whether stocks flagged as in-demand actually moved, PLUS a trimmed
snapshot of the full strategy context (for offline strategy backtests). Runs on
a background thread during market hours; snapshots can also be captured on demand.

Storage is SQLite (`db.py`, data/market.db) — indexed, queryable, millions of
rows, no server. On first run any legacy snapshots.csv / iv_log.csv is imported.

Reliability
-----------
The capture loop is designed to run every market day unattended:
- Each cycle's sub-tasks (snapshot / IV / sim / context) are independently
  guarded, so one failing NSE call can't skip the others.
- A heartbeat (`_last_tick`), cycle counter and consecutive-error counter feed
  `health()`; after a few failed cycles the NSE session is force-rebuilt.
- A watchdog thread revives the worker if it ever dies (`_restarts`).
`health()` / `/api/log/health` expose whether capture is actually happening.
"""

import os
import threading
import time
from datetime import datetime, timezone, timedelta

from nse_pulse.core import db
from nse_pulse.core import nse_client as nse
from nse_pulse.core import paths

IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = paths.DATA_DIR

def _env_int(name, default, lo):
    """Read an int env override with a hard floor (so a fat-fingered value can't
    hammer NSE). Falls back to `default` on a missing/garbage value."""
    try:
        return max(lo, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


# Cadences are env-tunable so the market-hours NSE load can be dialed down without
# a code edit. Defaults trimmed (INTERVAL 60 -> 90) to cut the dominant per-minute
# fan-out that the pacer smooths but can't eliminate; floors stop a silly value from
# turning into a burst. Pair with NSE_CTX_CANDIDATES (strategies.build_context).
INTERVAL = _env_int("NSE_LOG_INTERVAL", 90, 30)          # seconds between snapshots
IV_INTERVAL = _env_int("NSE_LOG_IV_INTERVAL", 300, 60)   # ATM-IV captures (heavier)
CONTEXT_INTERVAL = _env_int("NSE_LOG_CONTEXT_INTERVAL", 300, 60)  # context archive
WATCHDOG_INTERVAL = 30  # seconds between watchdog liveness checks
# "Stalled" must sit above the tick cadence, else a raised INTERVAL looks unhealthy.
STALE_AFTER = max(180, INTERVAL * 2)  # no tick for this long (market hours) = stalled
REBUILD_AFTER = 3      # consecutive failed cycles before forcing a session rebuild

# Always-tracked indices + the most-active F&O stocks (liquid, consistent IV).
IV_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

_thread = None
_watchdog = None
_running = False
_last_capture = None
_last_error = None
_last_error_at = None
_last_iv_capture = None
_last_context_capture = None

# Health / heartbeat state (see health()).
_loop_started = None
_last_tick = 0.0          # epoch seconds of the last loop iteration
_cycles = 0               # loop iterations since start
_consecutive_errors = 0   # cycles in a row that failed to capture the snapshot
_restarts = 0             # times the watchdog revived a dead worker thread
_last_iv_run = 0.0        # throttle timers (epoch seconds)
_last_context_run = 0.0


def _now_ist():
    return datetime.now(IST)


def is_market_hours(dt=None):
    """NSE cash market: Mon-Fri, 09:15-15:30 IST."""
    dt = dt or _now_ist()
    if dt.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 30)


def _rows_for_snapshot():
    ts = _now_ist().isoformat(timespec="seconds")
    rows = []

    try:
        for i, r in enumerate(nse.get_demand_score(25)):
            rows.append({
                "ts": ts, "view": "demand", "rank": i + 1,
                "symbol": r.get("symbol"), "ltp": r.get("ltp"),
                "pChange": r.get("pChange"), "score": r.get("score"),
                "signalCount": r.get("signalCount"), "volMult": r.get("volMult"),
                "week1volChange": "", "volume": "", "value": "",
            })
    except Exception:
        pass

    try:
        for i, r in enumerate(nse.get_volume_gainers(25)):
            rows.append({
                "ts": ts, "view": "volgainers", "rank": i + 1,
                "symbol": r.get("symbol"), "ltp": r.get("ltp"),
                "pChange": r.get("pChange"), "score": "", "signalCount": "",
                "volMult": "", "week1volChange": r.get("week1volChange"),
                "volume": r.get("volume"), "value": "",
            })
    except Exception:
        pass

    return rows


def capture_snapshot():
    """Capture one snapshot now (ignores market hours). Returns rows written."""
    global _last_capture, _last_error
    rows = _rows_for_snapshot()
    if not rows:
        _last_error = "No data returned from NSE"
        return 0
    db.init()
    db.insert_snapshots(rows)
    _last_capture = _now_ist().isoformat(timespec="seconds")
    _last_error = None
    return len(rows)


def _iv_watchlist():
    """Indices (always) + most-active F&O stock underlyings for IV tracking."""
    syms = list(IV_INDICES)
    try:
        for r in nse.get_futures(limit=20):
            s = r.get("symbol")
            if s and s not in syms:
                syms.append(s)
    except Exception:
        pass
    return syms[:25]


def capture_iv():
    """Log the ATM implied volatility (CE/PE avg) for the IV watchlist."""
    global _last_iv_capture, _last_error
    from nse_pulse.core import nse_quote

    ts = _now_ist().isoformat(timespec="seconds")
    rows = []
    for sym in _iv_watchlist():
        try:
            oc = nse_quote.get_option_chain(sym)
            atm = oc.get("atmStrike")
            row = next((r for r in oc.get("rows", []) if r.get("strike") == atm), None)
            if not row:
                continue
            ce_iv = (row.get("ce") or {}).get("iv")
            pe_iv = (row.get("pe") or {}).get("iv")
            ivs = [v for v in (ce_iv, pe_iv) if v]
            if not ivs:
                continue
            rows.append({
                "ts": ts, "symbol": sym, "expiry": oc.get("expiry"),
                "atmStrike": atm, "atmIV": round(sum(ivs) / len(ivs), 2),
                "ceIV": ce_iv, "peIV": pe_iv, "pcr": oc.get("pcr"),
                "underlying": oc.get("underlying"),
            })
        except Exception:
            continue

    if not rows:
        return 0
    db.init()
    db.insert_iv(rows)
    _last_iv_capture = ts
    return len(rows)


# Only the fields each strategy generator actually reads, so the stored context
# stays small yet lets us replay strategies.generate() faithfully offline.
def _trim_context(ctx):
    def pick(rows, keys):
        return [{k: r.get(k) for k in keys if r.get(k) is not None}
                for r in (rows or [])]

    idx = ctx.get("index") or {}
    keep_idx = {}
    for name in ("NIFTY", "BANKNIFTY"):
        d = idx.get(name)
        if d:
            keep_idx[name] = {k: d.get(k) for k in
                              ("last", "pChange", "advances", "declines")}
    return {
        "scanner": pick(ctx.get("scanner"),
                        ["symbol", "ltp", "pChange", "oiSignal", "volMult",
                         "tags", "listCount", "lists", "value", "score", "signalCount"]),
        "gainers": pick(ctx.get("gainers"), ["symbol", "ltp", "pChange"]),
        "losers": pick(ctx.get("losers"), ["symbol", "ltp", "pChange"]),
        "volgainers": pick(ctx.get("volgainers"),
                           ["symbol", "ltp", "pChange", "week1volChange"]),
        "oispurts": pick(ctx.get("oispurts"),
                         ["symbol", "ltp", "pChange", "signalKind", "signal",
                          "oiPctChange", "latestOI", "changeInOI"]),
        "quotes": {s: {k: q.get(k) for k in
                       ("ltp", "vwap", "yearHigh", "yearLow", "deliveryPct", "pChange")}
                   for s, q in (ctx.get("quotes") or {}).items()},
        "index": keep_idx,
        "niftyPcr": ctx.get("niftyPcr"),
    }


def capture_context(ctx):
    """Store a trimmed+gzipped snapshot of the strategy context for backtesting."""
    global _last_context_capture
    if not ctx:
        return 0
    regime = ctx.get("regime") or {}
    now = _now_ist()
    n = db.insert_context(
        now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
        regime.get("label"), regime.get("niftyPct"), _trim_context(ctx))
    _last_context_capture = now.isoformat(timespec="seconds")
    return n


def _note_error(msg):
    global _last_error, _last_error_at
    _last_error = msg
    _last_error_at = _now_ist().isoformat(timespec="seconds")


def _run_cycle():
    """
    One logging cycle. Each sub-task is independently guarded, so a failure in
    (say) the option-chain IV pull can't stop the demand snapshot or the sim
    update. Returns True if the core demand/volgainers snapshot captured rows.
    """
    global _last_iv_run, _last_context_run
    ok = False
    ctx = None
    try:
        ok = capture_snapshot() > 0
    except Exception as e:
        _note_error("snapshot: " + str(e))

    try:
        if (time.time() - _last_iv_run) >= IV_INTERVAL:
            capture_iv()
            _last_iv_run = time.time()
    except Exception as e:
        _note_error("iv: " + str(e))

    # Multi-strategy simulator: build the shared context ONCE, mark all trades to
    # market, auto-take fresh ideas when enabled, roll up today's regime + stats,
    # and periodically archive the (trimmed) context so strategies can be
    # replayed offline (backtest).
    try:
        from nse_pulse.sim import sim
        ctx = sim.build_ctx()
        sim.update(ctx)
        if sim.get_auto():
            # Two parallel books off the SAME context: the all-market 'cash' book
            # and the dedicated 'fno' book (F&O-eligible ideas only).
            sim.take(ctx=ctx, auto=True, book="cash")
            sim.take(ctx=ctx, auto=True, book="fno")
        sim.daily_rollup(ctx)
        if (time.time() - _last_context_run) >= CONTEXT_INTERVAL:
            capture_context(ctx)
            _last_context_run = time.time()
    except Exception as e:
        _note_error("sim: " + str(e))

    # Off-screen alerts (Telegram/webhook). No-op unless configured, so this is
    # free for users who haven't opted in. Reuses the context we just built.
    try:
        from nse_pulse.web import notify
        notify.tick(ctx)
    except Exception as e:
        _note_error("notify: " + str(e))
    return ok


def _loop():
    global _loop_started, _last_tick, _cycles, _consecutive_errors
    _loop_started = _now_ist().isoformat(timespec="seconds")
    while _running:
        try:
            _last_tick = time.time()
            _cycles += 1
            if is_market_hours():
                if _run_cycle():
                    _consecutive_errors = 0
                else:
                    _consecutive_errors += 1
                    # NSE occasionally drops the session; rebuild it after a few
                    # failed cycles so we self-heal without a manual restart.
                    if _consecutive_errors % REBUILD_AFTER == 0:
                        try:
                            nse.get_session(force=True)
                            _note_error(f"forced session rebuild after "
                                        f"{_consecutive_errors} failed cycles")
                        except Exception:
                            pass
        except Exception as e:
            _note_error("loop: " + str(e))
        # Sleep in small steps so stop() stays responsive.
        for _ in range(INTERVAL):
            if not _running:
                break
            time.sleep(1)


def _watchdog_loop():
    """Revive the worker thread if it ever dies, so capture survives crashes."""
    global _thread, _restarts
    while _running:
        for _ in range(WATCHDOG_INTERVAL):
            if not _running:
                return
            time.sleep(1)
        if _running and (_thread is None or not _thread.is_alive()):
            _restarts += 1
            _note_error("watchdog: worker thread died - restarting")
            _thread = threading.Thread(target=_loop, daemon=True,
                                       name="snapshot-logger")
            _thread.start()


def start():
    global _thread, _watchdog, _running
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="snapshot-logger")
    _thread.start()
    _watchdog = threading.Thread(target=_watchdog_loop, daemon=True,
                                 name="snapshot-watchdog")
    _watchdog.start()


def stop():
    global _running
    _running = False


def health():
    """Derived health for the UI/monitoring: is capture actually happening?"""
    alive = bool(_thread and _thread.is_alive())
    since_tick = (time.time() - _last_tick) if _last_tick else None
    mkt = is_market_hours()
    # A live thread that hasn't ticked within STALE_AFTER during market hours is
    # stalled (stuck on a hung call); outside market hours idling is expected.
    stalled = bool(mkt and (since_tick is None or since_tick > STALE_AFTER))
    healthy = bool(_running and alive and not stalled)
    return {
        "healthy": healthy,
        "running": _running,
        "threadAlive": alive,
        "watchdogAlive": bool(_watchdog and _watchdog.is_alive()),
        "marketHours": mkt,
        "stalled": stalled,
        "loopStarted": _loop_started,
        "cycles": _cycles,
        "secondsSinceTick": round(since_tick, 1) if since_tick is not None else None,
        "consecutiveErrors": _consecutive_errors,
        "restarts": _restarts,
        "lastError": _last_error,
        "lastErrorAt": _last_error_at,
    }


def status():
    db.init()
    snap = db.snapshot_stats()
    iv = db.iv_stats()
    ctx = db.context_stats()
    return {
        "running": _running,
        "marketHours": is_market_hours(),
        "intervalSec": INTERVAL,
        "totalRows": snap["total"],
        "snapshots": snap["distinct"],
        "firstSnapshot": snap["first"],
        "lastSnapshot": snap["last"],
        "lastCapture": _last_capture,
        "lastError": _last_error,
        "db": db.DB_FILE,
        "ivSnapshots": iv["snapshots"],
        "ivSymbols": iv["symbols"],
        "lastIvCapture": _last_iv_capture,
        "contextCycles": ctx["cycles"],
        "contextDays": ctx["days"],
        "contextBytes": ctx["bytes"],
        "lastContextCapture": _last_context_capture,
        "health": health(),
    }


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def backtest(view="demand"):
    """
    Simple forward-return analysis: when a symbol first appeared in the given
    view, how did its price change from that first sighting to its most recent
    sighting in the log? This tests whether the signal was predictive.
    """
    db.init()
    rows = db.snapshot_rows(view)
    if not rows:
        return {"view": view, "symbols": [], "summary": None,
                "message": "No logged data yet for this view."}

    by_symbol = {}
    for r in rows:
        ltp = _to_float(r.get("ltp"))
        if ltp is None:
            continue
        rank = _to_float(r.get("rank"))
        by_symbol.setdefault(r["symbol"], []).append(
            (r["ts"], ltp, rank)
        )

    results = []
    for sym, pts in by_symbol.items():
        pts.sort(key=lambda x: x[0])
        first_ts, first_ltp, first_rank = pts[0]
        last_ts, last_ltp, _ = pts[-1]
        if not first_ltp:
            continue
        ret = (last_ltp - first_ltp) / first_ltp * 100
        results.append({
            "symbol": sym,
            "firstSeen": first_ts,
            "firstLtp": round(first_ltp, 2),
            "lastLtp": round(last_ltp, 2),
            "firstRank": int(first_rank) if first_rank else None,
            "forwardReturnPct": round(ret, 2),
            "sightings": len(pts),
        })

    results.sort(key=lambda x: x["forwardReturnPct"], reverse=True)

    n = len(results)
    summary = None
    if n:
        rets = [r["forwardReturnPct"] for r in results]
        winners = sum(1 for x in rets if x > 0)
        summary = {
            "symbols": n,
            "avgReturnPct": round(sum(rets) / n, 2),
            "hitRatePct": round(winners / n * 100, 1),
            "bestSymbol": results[0]["symbol"],
            "bestReturnPct": results[0]["forwardReturnPct"],
            "worstSymbol": results[-1]["symbol"],
            "worstReturnPct": results[-1]["forwardReturnPct"],
        }

    return {"view": view, "symbols": results, "summary": summary,
            "message": None}


def iv_rank(symbol):
    """
    IV rank/percentile for a symbol from logged ATM IV history.
      ivRank      = where current IV sits between its min & max (0-100%)
      ivPercentile= share of past observations at or below current IV
    Needs a couple of samples; the more history logged, the more meaningful.
    """
    symbol = (symbol or "").upper().strip()
    db.init()
    series = [(t, v) for t, v in db.iv_series(symbol) if v is not None]
    if len(series) < 2:
        return {"symbol": symbol, "enough": False, "samples": len(series)}
    series.sort(key=lambda x: x[0])
    ivs = [v for _, v in series]
    cur = ivs[-1]
    lo, hi = min(ivs), max(ivs)
    rank = 50.0 if hi == lo else (cur - lo) / (hi - lo) * 100
    pctile = sum(1 for v in ivs if v <= cur) / len(ivs) * 100
    return {
        "symbol": symbol, "enough": True, "samples": len(ivs),
        "currentIV": cur, "minIV": round(lo, 2), "maxIV": round(hi, 2),
        "ivRank": round(rank, 1), "ivPercentile": round(pctile, 1),
        "since": series[0][0], "lastTs": series[-1][0],
    }
