"""
Snapshot logger + backtester
============================
Periodically records the "demand" board and volume-gainers to a CSV so we can
later analyze whether stocks flagged as in-demand actually moved. Runs on a
background thread during market hours; snapshots can also be captured on demand.

The log is a flat CSV (one row per symbol per snapshot) using only the stdlib,
so there's no pandas/DB dependency. File lives in data/snapshots.csv (gitignored
via *.csv).
"""

import csv
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import nse_client as nse

IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LOG_FILE = os.path.join(DATA_DIR, "snapshots.csv")
IV_LOG_FILE = os.path.join(DATA_DIR, "iv_log.csv")

FIELDS = [
    "ts", "view", "rank", "symbol", "ltp", "pChange",
    "score", "signalCount", "volMult", "week1volChange", "volume", "value",
]

IV_FIELDS = [
    "ts", "symbol", "expiry", "atmStrike", "atmIV", "ceIV", "peIV",
    "pcr", "underlying",
]

INTERVAL = 60      # seconds between automatic snapshots
IV_INTERVAL = 300  # seconds between ATM-IV captures (heavier, so slower)

# Always-tracked indices + the most-active F&O stocks (liquid, consistent IV).
IV_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

_lock = threading.Lock()
_iv_lock = threading.Lock()
_thread = None
_running = False
_last_capture = None
_last_error = None
_last_iv_capture = None


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
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        new_file = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(rows)
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
    import nse_quote

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
    os.makedirs(DATA_DIR, exist_ok=True)
    with _iv_lock:
        new_file = not os.path.exists(IV_LOG_FILE)
        with open(IV_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=IV_FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(rows)
    _last_iv_capture = ts
    return len(rows)


def _loop():
    global _last_error, _last_iv_capture
    last_iv = 0.0
    while _running:
        try:
            if is_market_hours():
                capture_snapshot()
                if (time.time() - last_iv) >= IV_INTERVAL:
                    capture_iv()
                    last_iv = time.time()
                # Multi-strategy simulator: build the shared context once, mark
                # all strategies' trades to market, auto-take fresh ideas (mode-
                # aware) when enabled, and roll up today's regime + per-strategy
                # stats + NIFTY close.
                try:
                    import sim
                    ctx = sim.build_ctx()
                    sim.update(ctx)
                    if sim.get_auto():
                        sim.take(ctx=ctx, auto=True)
                    sim.daily_rollup(ctx)
                except Exception:
                    pass
        except Exception as e:
            _last_error = str(e)
        # Sleep in small steps so stop() is responsive.
        for _ in range(INTERVAL):
            if not _running:
                break
            time.sleep(1)


def start():
    global _thread, _running
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="snapshot-logger")
    _thread.start()


def stop():
    global _running
    _running = False


def _read_rows():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def status():
    rows = _read_rows()
    times = sorted({r["ts"] for r in rows})
    iv_rows = _read_iv_rows()
    iv_times = sorted({r["ts"] for r in iv_rows})
    return {
        "running": _running,
        "marketHours": is_market_hours(),
        "intervalSec": INTERVAL,
        "totalRows": len(rows),
        "snapshots": len(times),
        "firstSnapshot": times[0] if times else None,
        "lastSnapshot": times[-1] if times else None,
        "lastCapture": _last_capture,
        "lastError": _last_error,
        "logFile": LOG_FILE,
        "ivSnapshots": len(iv_times),
        "ivSymbols": len({r["symbol"] for r in iv_rows}),
        "lastIvCapture": _last_iv_capture,
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
    rows = [r for r in _read_rows() if r["view"] == view]
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


def _read_iv_rows():
    if not os.path.exists(IV_LOG_FILE):
        return []
    with open(IV_LOG_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def iv_rank(symbol):
    """
    IV rank/percentile for a symbol from logged ATM IV history.
      ivRank      = where current IV sits between its min & max (0-100%)
      ivPercentile= share of past observations at or below current IV
    Needs a couple of samples; the more history logged, the more meaningful.
    """
    symbol = (symbol or "").upper().strip()
    series = [
        (r["ts"], _to_float(r.get("atmIV")))
        for r in _read_iv_rows() if r.get("symbol") == symbol
    ]
    series = [(t, v) for t, v in series if v is not None]
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
