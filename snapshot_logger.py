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

FIELDS = [
    "ts", "view", "rank", "symbol", "ltp", "pChange",
    "score", "signalCount", "volMult", "week1volChange", "volume", "value",
]

INTERVAL = 60  # seconds between automatic snapshots

_lock = threading.Lock()
_thread = None
_running = False
_last_capture = None
_last_error = None


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


def _loop():
    global _last_error
    while _running:
        try:
            if is_market_hours():
                capture_snapshot()
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
