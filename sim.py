"""
Recommendation simulator (forward-test)
========================================
"Buy your own recommendations." This takes the Ideas engine's LONG/SHORT
recommendations, enters a virtual position at the recommended entry, then tracks
each one against its own target / stop using live prices. It lets us DEMO —
after the fact — whether the recommendations actually played out (hit target =
right, hit stop = wrong), with a hit-rate / P&L scorecard broken down by
conviction.

This is deliberately SEPARATE from the manual paper-trading account (`paper.py`)
so auto-taken ideas never pollute the user's own virtual portfolio. State
persists to `sim_state.json` (gitignored). Educational only — NOT advice.

Model
-----
- Each idea is sized to a fixed notional (`NOTIONAL`), so every trade is weighted
  equally and P&L% == price move in the trade's direction.
- We only see periodic price snapshots (no intrabar data), so target/stop are
  evaluated on the latest observed LTP. Good enough for a directional demo.
- LONG wins when price rises to target; SHORT wins when price falls to target.
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta

import nse_client as nse

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = os.path.join(os.path.dirname(__file__), "sim_state.json")
NOTIONAL = 100_000.0  # virtual rupees deployed per idea (equal weighting)

_lock = threading.RLock()


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _default_state():
    return {"trades": [], "auto": False, "createdAt": _now()}


def _load():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("trades", [])
        st.setdefault("auto", False)
        st.setdefault("createdAt", _now())
        return st
    except Exception:
        return _default_state()


def _save(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _price(symbol):
    try:
        return nse.get_price(symbol)
    except Exception:
        return None


def _open_trade_from_idea(idea):
    entry = idea.get("entry") or idea.get("ltp")
    if not entry:
        return None
    direction = idea["direction"]
    qty = NOTIONAL / entry
    return {
        "id": f"{idea['symbol']}|{direction}|{datetime.now(IST).strftime('%Y%m%d%H%M%S')}",
        "symbol": idea["symbol"],
        "direction": direction,
        "conviction": idea.get("conviction"),
        "rating": idea.get("rating"),
        "reasons": idea.get("reasons", []),
        "fno": idea.get("fno", False),
        "entry": round(entry, 2),
        "stop": idea.get("stop"),
        "target": idea.get("target"),
        "stopPct": idea.get("stopPct"),
        "targetPct": idea.get("targetPct"),
        "rr": idea.get("rr"),
        "qty": qty,
        "notional": NOTIONAL,
        "status": "OPEN",
        "ltp": round(entry, 2),
        "mfePct": 0.0,  # max favorable excursion (best unrealised gain %)
        "maePct": 0.0,  # max adverse excursion (worst unrealised loss %)
        "pnl": 0.0,
        "pnlPct": 0.0,
        "openedAt": _now(),
        "exitPrice": None,
        "closedAt": None,
    }


def _move_pct(t, px):
    """Directional move % from entry (positive == in the trade's favour)."""
    if t["direction"] == "LONG":
        return (px - t["entry"]) / t["entry"] * 100
    return (t["entry"] - px) / t["entry"] * 100


def _refresh_trade(t):
    """Update one OPEN trade against the live price; close it if target/stop hit."""
    if t["status"] != "OPEN":
        return
    px = _price(t["symbol"])
    if px is None:
        return
    t["ltp"] = round(px, 2)
    fav = _move_pct(t, px)
    t["mfePct"] = round(max(t["mfePct"], fav), 2)
    t["maePct"] = round(min(t["maePct"], fav), 2)

    hit = None
    tgt, stop = t.get("target"), t.get("stop")
    if t["direction"] == "LONG":
        if tgt and px >= tgt:
            hit, exit_px = "TARGET", tgt
        elif stop and px <= stop:
            hit, exit_px = "STOP", stop
    else:  # SHORT
        if tgt and px <= tgt:
            hit, exit_px = "TARGET", tgt
        elif stop and px >= stop:
            hit, exit_px = "STOP", stop

    if hit:
        t["status"] = hit
        t["exitPrice"] = round(exit_px, 2)
        t["closedAt"] = _now()
        t["ltp"] = round(exit_px, 2)
        px = exit_px

    move = _move_pct(t, px)
    t["pnl"] = round(t["qty"] * (px - t["entry"]) * (1 if t["direction"] == "LONG" else -1), 2)
    t["pnlPct"] = round(move, 2)


def update():
    """Re-price every OPEN trade and close any that reached target/stop."""
    with _lock:
        state = _load()
        changed = False
        for t in state["trades"]:
            if t["status"] == "OPEN":
                before = (t["status"], t["ltp"])
                _refresh_trade(t)
                if before != (t["status"], t["ltp"]):
                    changed = True
        if changed:
            _save(state)
        return state


def take(fno_only=False, limit=10):
    """
    Snapshot the current recommendations into the simulator. Skips any idea that
    already has an OPEN trade for the same symbol+direction (so repeated/auto
    calls don't stack duplicates). Returns how many new trades were opened.
    """
    recos = nse.get_recommendations(fno_only=fno_only, limit=limit)
    ideas = (recos.get("longs") or []) + (recos.get("shorts") or [])
    with _lock:
        state = _load()
        open_keys = {(t["symbol"], t["direction"])
                     for t in state["trades"] if t["status"] == "OPEN"}
        added = 0
        for idea in ideas:
            key = (idea["symbol"], idea["direction"])
            if key in open_keys:
                continue
            tr = _open_trade_from_idea(idea)
            if tr:
                state["trades"].append(tr)
                open_keys.add(key)
                added += 1
        if added:
            _save(state)
    return added


def set_auto(on):
    with _lock:
        state = _load()
        state["auto"] = bool(on)
        _save(state)
        return state["auto"]


def get_auto():
    return _load().get("auto", False)


def reset():
    with _lock:
        _save(_default_state())


def _bucket_stats(trades):
    closed = [t for t in trades if t["status"] in ("TARGET", "STOP")]
    n = len(closed)
    wins = sum(1 for t in closed if t["status"] == "TARGET")
    pnl = sum(t["pnl"] for t in closed)
    return {
        "closed": n,
        "wins": wins,
        "losses": n - wins,
        "winRate": round(wins / n * 100, 1) if n else None,
        "pnl": round(pnl, 2),
    }


def summary():
    """Live scorecard: open positions, closed outcomes and aggregate hit-rate."""
    state = update()  # always mark-to-market before reporting
    trades = state["trades"]
    open_t = [t for t in trades if t["status"] == "OPEN"]
    closed_t = [t for t in trades if t["status"] in ("TARGET", "STOP")]

    open_t.sort(key=lambda t: t["pnlPct"], reverse=True)
    closed_t.sort(key=lambda t: t.get("closedAt") or "", reverse=True)

    realized = sum(t["pnl"] for t in closed_t)
    unrealized = sum(t["pnl"] for t in open_t)
    wins = sum(1 for t in closed_t if t["status"] == "TARGET")
    nclosed = len(closed_t)

    # Hit-rate by conviction rating, to show whether higher conviction == better.
    def band(t):
        return t.get("rating") or "?"

    by_conv = {}
    for t in closed_t:
        by_conv.setdefault(band(t), []).append(t)
    conv_rows = []
    for label in ("High", "Medium", "Low", "?"):
        if label in by_conv:
            s = _bucket_stats(by_conv[label])
            s["band"] = label
            conv_rows.append(s)

    return {
        "auto": state.get("auto", False),
        "notional": NOTIONAL,
        "generatedAt": _now(),
        "counts": {
            "total": len(trades),
            "open": len(open_t),
            "closed": nclosed,
            "target": wins,
            "stop": nclosed - wins,
        },
        "winRate": round(wins / nclosed * 100, 1) if nclosed else None,
        "realizedPnl": round(realized, 2),
        "unrealizedPnl": round(unrealized, 2),
        "totalPnl": round(realized + unrealized, 2),
        "byConviction": conv_rows,
        "open": open_t,
        "closed": closed_t[:50],
    }
