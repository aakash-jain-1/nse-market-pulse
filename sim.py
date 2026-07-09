"""
Multi-strategy recommendation simulator (forward-test)
======================================================
"Buy your own recommendations" — for SEVERAL strategies at once. Each strategy
in `strategies.py` gets its OWN parallel ledger: we snapshot its ideas, enter
at the recommended entry (flat notional), then track each trade against its
target / stop (with a multi-day horizon). Every day is tagged with a market
regime, so a day-by-day rollup shows WHICH strategy works in WHICH regime
(momentum on trend days, mean-reversion on recovery days, ...).

Kept SEPARATE from the manual paper account (`paper.py`). State persists to
`sim_state.json` (gitignored). Educational only — NOT investment advice.

Entry modes
-----------
- `continuous` : auto-take fresh qualifying ideas every cycle (deduped).
- `open`       : auto-take ONE snapshot per strategy per day (near the open).
(Manual "Take" always takes now, regardless of mode.)

Exit
----
Target or stop, else a multi-day horizon (`maxSessions`, default 3 trading
sessions) after which the trade is time-expired at the current price.
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta

import nse_client as nse
import strategies as strat

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = os.path.join(os.path.dirname(__file__), "sim_state.json")
NOTIONAL = 100_000.0   # fallback notional when a trade has no usable stop
RISK_PER_TRADE = 2_000.0   # fixed rupees risked per trade (position sizing unit)
MAX_NOTIONAL = 500_000.0   # cap so a very tight stop can't blow up position size
DEFAULT_MAX_SESSIONS = 3
STATE_VERSION = 2


def size_position(entry, stop):
    """
    Risk-based sizing: pick a quantity so that hitting the stop loses exactly
    RISK_PER_TRADE rupees (per-share risk = |entry - stop|). Capped by
    MAX_NOTIONAL. Falls back to flat NOTIONAL when there's no usable stop.
    Returns (qty, notional). This is what makes each trade risk the same, so
    equity curves / expectancy are comparable across strategies.
    """
    if not entry or entry <= 0:
        return 0.0, 0.0
    risk_per_share = abs(entry - stop) if stop else 0
    if risk_per_share > 0:
        qty = RISK_PER_TRADE / risk_per_share
        qty = min(qty, MAX_NOTIONAL / entry)
    else:
        qty = NOTIONAL / entry
    return qty, round(qty * entry, 2)

_lock = threading.RLock()


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _default_state():
    return {
        "version": STATE_VERSION,
        "auto": False,
        "entryMode": "continuous",
        "maxSessions": DEFAULT_MAX_SESSIONS,
        "strategies": {s["id"]: {"trades": []} for s in strat.STRATEGIES},
        "daily": {},
        "lastAutoDate": {},
        "createdAt": _now(),
    }


def _load():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        # Version-gate: v1 (single-ledger) state is superseded by per-strategy v2.
        if st.get("version") != STATE_VERSION:
            return _default_state()
        st.setdefault("strategies", {})
        for s in strat.STRATEGIES:
            st["strategies"].setdefault(s["id"], {"trades": []})
        st.setdefault("daily", {})
        st.setdefault("lastAutoDate", {})
        st.setdefault("entryMode", "continuous")
        st.setdefault("maxSessions", DEFAULT_MAX_SESSIONS)
        st.setdefault("auto", False)
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


# ----------------------------------------------------------------------------
# Context / regime
# ----------------------------------------------------------------------------
def _prior_day_move(state):
    """NIFTY %change of the most recent daily entry BEFORE today (for regime)."""
    today = _today()
    dates = sorted(d for d in state.get("daily", {}) if d < today)
    if not dates:
        return None
    return state["daily"][dates[-1]].get("niftyPct")


def build_ctx():
    """Full data bundle with today's regime attached (used for taking ideas)."""
    state = _load()
    ctx = strat.build_context()
    ctx["regime"] = strat.detect_regime(ctx, _prior_day_move(state))
    return ctx


def current_regime():
    """Cheap regime for the banner: index snapshot only, no full context."""
    state = _load()
    idx = {}
    try:
        idx = nse.get_index_snapshot()
    except Exception:
        pass
    return strat.detect_regime({"index": idx}, _prior_day_move(state))


# ----------------------------------------------------------------------------
# Trades
# ----------------------------------------------------------------------------
def _open_trade(idea, strategy_id, regime_label):
    entry = idea.get("entry") or idea.get("ltp")
    if not entry:
        return None
    direction = idea["direction"]
    qty, notional = size_position(entry, idea.get("stop"))
    return {
        "id": f"{strategy_id}|{idea['symbol']}|{direction}|"
              f"{datetime.now(IST).strftime('%Y%m%d%H%M%S')}",
        "strategy": strategy_id,
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
        "notional": notional,
        "risk": RISK_PER_TRADE,
        "status": "OPEN",
        "ltp": round(entry, 2),
        "mfePct": 0.0,
        "maePct": 0.0,
        "pnl": 0.0,
        "pnlPct": 0.0,
        "rMultiple": 0.0,
        "openedAt": _now(),
        "openedDate": _today(),
        "regimeAtEntry": regime_label,
        "exitPrice": None,
        "closedAt": None,
    }


def _move_pct(t, px):
    if t["direction"] == "LONG":
        return (px - t["entry"]) / t["entry"] * 100
    return (t["entry"] - px) / t["entry"] * 100


def _sessions_elapsed(state, opened_date):
    """Distinct trading sessions (daily-log dates) from entry through today."""
    today = _today()
    return len([d for d in state.get("daily", {}) if opened_date <= d <= today]) or 1


def _refresh_trade(state, t):
    """Reprice one OPEN trade; close on target/stop, or time-expire at horizon."""
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
    else:
        if tgt and px <= tgt:
            hit, exit_px = "TARGET", tgt
        elif stop and px >= stop:
            hit, exit_px = "STOP", stop

    if not hit and _sessions_elapsed(state, t["openedDate"]) > state.get("maxSessions", DEFAULT_MAX_SESSIONS):
        hit, exit_px = "EXPIRED", px

    if hit:
        t["status"] = hit
        t["exitPrice"] = round(exit_px, 2)
        t["closedAt"] = _now()
        t["ltp"] = round(exit_px, 2)
        px = exit_px

    t["pnl"] = round(t["qty"] * (px - t["entry"]) *
                     (1 if t["direction"] == "LONG" else -1), 2)
    t["pnlPct"] = round(_move_pct(t, px), 2)
    t["rMultiple"] = round(t["pnl"] / t.get("risk", RISK_PER_TRADE), 2)


def update(ctx=None):
    """Re-price and resolve every OPEN trade across all strategies."""
    with _lock:
        state = _load()
        for sid, book in state["strategies"].items():
            for t in book["trades"]:
                if t["status"] == "OPEN":
                    _refresh_trade(state, t)
        _save(state)
        return state


def take(strategy_ids=None, ctx=None, auto=False, limit=10):
    """
    Snapshot ideas into each strategy's ledger. Dedups by (symbol, direction)
    among that strategy's OPEN trades. Honors entry mode for AUTO calls (in
    'open' mode, only the first take per strategy per day). Returns per-strategy
    counts of new trades.
    """
    ctx = ctx or build_ctx()
    regime_label = (ctx.get("regime") or {}).get("label", "?")
    with _lock:
        state = _load()
        ids = strategy_ids or [s["id"] for s in strat.STRATEGIES]
        mode = state.get("entryMode", "continuous")
        today = _today()
        added = {}
        for sid in ids:
            if auto and mode == "open" and state["lastAutoDate"].get(sid) == today:
                added[sid] = 0
                continue
            book = state["strategies"].setdefault(sid, {"trades": []})
            # One entry per (symbol, direction) per strategy per day: dedup against
            # everything opened TODAY (any status), not just still-open trades.
            # This stops continuous mode from instantly re-entering the same setup
            # the moment a trade closes. The name is free to reappear next session.
            taken_keys = {(t["symbol"], t["direction"]) for t in book["trades"]
                          if t.get("openedDate") == today}
            ideas = strat.generate(sid, ctx)
            longs = sorted([i for i in ideas if i["direction"] == "LONG"],
                           key=lambda x: x.get("conviction", 0), reverse=True)[:limit]
            shorts = sorted([i for i in ideas if i["direction"] == "SHORT"],
                            key=lambda x: x.get("conviction", 0), reverse=True)[:limit]
            n = 0
            for idea in longs + shorts:
                key = (idea["symbol"], idea["direction"])
                if key in taken_keys:
                    continue
                tr = _open_trade(idea, sid, regime_label)
                if tr:
                    book["trades"].append(tr)
                    taken_keys.add(key)
                    n += 1
            added[sid] = n
            if auto and mode == "open":
                state["lastAutoDate"][sid] = today
        _save(state)
    return added


# ----------------------------------------------------------------------------
# Daily rollup
# ----------------------------------------------------------------------------
def daily_rollup(ctx=None):
    """Upsert today's per-strategy stats + regime + NIFTY close into the log."""
    ctx = ctx or build_ctx()
    regime = ctx.get("regime") or {}
    idx = (ctx.get("index") or {}).get("NIFTY") or {}
    today = _today()
    with _lock:
        state = _load()
        day = state["daily"].setdefault(today, {})
        day["regime"] = regime.get("label")
        day["niftyPct"] = regime.get("niftyPct")
        day["niftyLast"] = idx.get("last")
        day["priorDayMove"] = regime.get("priorDayMove")
        day["breadth"] = f"{int(regime.get('breadthAdv') or 0)}:{int(regime.get('breadthDec') or 0)}"
        strat_stats = {}
        for sid, book in state["strategies"].items():
            opened = closed = wins = 0
            realized = 0.0
            unreal = 0.0
            for t in book["trades"]:
                if t.get("openedDate") == today:
                    opened += 1
                if t["status"] == "OPEN":
                    unreal += t["pnl"]
                elif (t.get("closedAt") or "").startswith(today):
                    closed += 1
                    realized += t["pnl"]
                    if t["status"] == "TARGET":
                        wins += 1
            strat_stats[sid] = {
                "opened": opened, "closed": closed, "wins": wins,
                "winRate": round(wins / closed * 100, 1) if closed else None,
                "realized": round(realized, 2),
                "unrealized": round(unreal, 2),
            }
        day["strategies"] = strat_stats
        _save(state)
        return day


def daily_matrix():
    """Day × strategy comparison grid for the heatmap."""
    state = _load()
    ids = [s["id"] for s in strat.STRATEGIES]
    dates = sorted(state.get("daily", {}).keys(), reverse=True)
    rows = []
    for d in dates:
        day = state["daily"][d]
        rows.append({
            "date": d,
            "regime": day.get("regime"),
            "niftyPct": day.get("niftyPct"),
            "breadth": day.get("breadth"),
            "cells": {sid: (day.get("strategies", {}) or {}).get(sid) for sid in ids},
        })
    return {"strategies": strat.strategy_meta(), "rows": rows}


# ----------------------------------------------------------------------------
# Regime leaderboard / strategy-of-the-day / equity curves
# ----------------------------------------------------------------------------
# Nice display order for regimes (unknown/extra ones appended after).
_REGIME_ORDER = ["Trend-Up", "Recovery", "Range", "Pullback", "Mixed", "Trend-Down"]


def regime_leaderboard(min_closed=1):
    """
    Aggregate every trade by (regime-at-entry × strategy): closed count, win%,
    average per-trade %, total P&L. Flags the best strategy per regime by avg %.
    This is the accumulating forward-test — 'what works on a recovery day?'.
    """
    state = _load()
    ids = [s["id"] for s in strat.STRATEGIES]
    agg = {}
    regimes = set()
    for s in strat.STRATEGIES:
        sid = s["id"]
        for t in state["strategies"].get(sid, {"trades": []})["trades"]:
            rg = t.get("regimeAtEntry") or "?"
            regimes.add(rg)
            a = agg.setdefault((rg, sid),
                               {"closed": 0, "open": 0, "wins": 0,
                                "pnl": 0.0, "pnlPctSum": 0.0})
            if t["status"] == "OPEN":
                a["open"] += 1
            else:
                a["closed"] += 1
                if t["status"] == "TARGET":
                    a["wins"] += 1
                a["pnl"] += t.get("pnl") or 0.0
                a["pnlPctSum"] += t.get("pnlPct") or 0.0

    ordered = ([r for r in _REGIME_ORDER if r in regimes] +
               sorted(r for r in regimes if r not in _REGIME_ORDER))
    rows = []
    for rg in ordered:
        cells = {}
        best_sid, best_val = None, None
        for sid in ids:
            a = agg.get((rg, sid))
            if not a or (a["closed"] == 0 and a["open"] == 0):
                cells[sid] = None
                continue
            avg = a["pnlPctSum"] / a["closed"] if a["closed"] else None
            cells[sid] = {
                "closed": a["closed"], "open": a["open"],
                "winRate": round(a["wins"] / a["closed"] * 100, 1) if a["closed"] else None,
                "avgPnlPct": round(avg, 2) if avg is not None else None,
                "totalPnl": round(a["pnl"], 2),
            }
            if a["closed"] >= min_closed and avg is not None and (best_val is None or avg > best_val):
                best_val, best_sid = avg, sid
        rows.append({"regime": rg, "best": best_sid, "cells": cells})
    return {"strategies": strat.strategy_meta(), "rows": rows}


def strategy_of_the_day(regime_label=None, min_closed=3):
    """
    Pick the strategy to lean on today: the one with the best historical avg %
    in the current regime (needs >= min_closed samples), else fall back to the
    strategy whose design fits this regime.
    """
    if regime_label is None:
        regime_label = current_regime().get("label")
    lb = regime_leaderboard()
    row = next((r for r in lb["rows"] if r["regime"] == regime_label), None)
    ranked = []
    if row:
        for sid, cell in row["cells"].items():
            if cell and cell["closed"] >= min_closed and cell["avgPnlPct"] is not None:
                ranked.append({"id": sid, "name": strat.STRATEGY_MAP.get(sid, {}).get("name", sid), **cell})
        ranked.sort(key=lambda x: x["avgPnlPct"], reverse=True)

    if ranked:
        top = ranked[0]
        pick = {
            "id": top["id"], "name": top["name"], "basis": "history",
            "avgPnlPct": top["avgPnlPct"], "winRate": top["winRate"], "closed": top["closed"],
            "reason": (f"Best avg P&L ({top['avgPnlPct']:+.2f}%/trade, {top['winRate']}% win) "
                       f"in '{regime_label}' days across {top['closed']} closed trades."),
        }
    else:
        fit = [s for s in strat.STRATEGIES if regime_label in s.get("regimeFit", [])]
        if fit:
            s = fit[0]
            pick = {"id": s["id"], "name": s["name"], "basis": "fit",
                    "reason": f"No closed history in '{regime_label}' yet — {s['name']} is built for this regime."}
        else:
            pick = None
    return {"regime": regime_label, "pick": pick, "ranked": ranked}


def equity_curves():
    """Cumulative realized P&L per strategy, ordered by close time (equity curve)."""
    state = _load()
    out = {}
    for s in strat.STRATEGIES:
        sid = s["id"]
        closed = [t for t in state["strategies"].get(sid, {"trades": []})["trades"]
                  if t["status"] != "OPEN" and t.get("closedAt")]
        closed.sort(key=lambda t: t["closedAt"])
        cum, pts = 0.0, []
        for t in closed:
            cum += t.get("pnl") or 0.0
            pts.append(round(cum, 0))
        out[sid] = {"points": pts, "final": round(cum, 0), "n": len(pts)}
    return out


def leaderboard_bundle():
    """One call for the whole leaderboard section: table + today's pick + curves."""
    regime = current_regime()
    return {
        "regime": regime,
        "leaderboard": regime_leaderboard(),
        "pick": strategy_of_the_day(regime.get("label"))["pick"],
        "equity": equity_curves(),
        "generatedAt": _now(),
    }


# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
def _scorecard(trades):
    closed = [t for t in trades if t["status"] in ("TARGET", "STOP", "EXPIRED")]
    open_t = [t for t in trades if t["status"] == "OPEN"]
    wins = sum(1 for t in closed if t["status"] == "TARGET")
    n = len(closed)
    realized = sum(t["pnl"] for t in closed)
    unreal = sum(t["pnl"] for t in open_t)
    total_r = sum(t.get("rMultiple") or 0 for t in closed)
    today = _today()
    today_closed = [t for t in closed if (t.get("closedAt") or "").startswith(today)]
    today_wins = sum(1 for t in today_closed if t["status"] == "TARGET")
    return {
        "open": len(open_t),
        "closed": n,
        "target": wins,
        "stop": sum(1 for t in closed if t["status"] == "STOP"),
        "expired": sum(1 for t in closed if t["status"] == "EXPIRED"),
        "winRate": round(wins / n * 100, 1) if n else None,
        "realizedPnl": round(realized, 2),
        "unrealizedPnl": round(unreal, 2),
        "totalPnl": round(realized + unreal, 2),
        "totalR": round(total_r, 2),
        "expectancyR": round(total_r / n, 2) if n else None,
        "todayClosed": len(today_closed),
        "todayWinRate": round(today_wins / len(today_closed) * 100, 1) if today_closed else None,
    }


def summary(strategy_id=None):
    """Overview scorecards + regime; plus one strategy's trade detail if asked."""
    update()
    state = _load()
    regime = current_regime()

    cards = []
    for s in strat.STRATEGIES:
        sc = _scorecard(state["strategies"].get(s["id"], {"trades": []})["trades"])
        cards.append({
            "id": s["id"], "name": s["name"], "description": s["description"],
            "regimeFit": s["regimeFit"], **sc,
            "fitsNow": regime.get("label") in s["regimeFit"],
        })

    out = {
        "mode": "overview",
        "auto": state.get("auto", False),
        "entryMode": state.get("entryMode", "continuous"),
        "maxSessions": state.get("maxSessions", DEFAULT_MAX_SESSIONS),
        "notional": NOTIONAL,
        "regime": regime,
        "pick": strategy_of_the_day(regime.get("label"))["pick"],
        "strategies": cards,
        "generatedAt": _now(),
    }

    if strategy_id and strategy_id in state["strategies"]:
        trades = state["strategies"][strategy_id]["trades"]
        meta = strat.STRATEGY_MAP.get(strategy_id, {})
        open_t = sorted([t for t in trades if t["status"] == "OPEN"],
                        key=lambda t: t["pnlPct"], reverse=True)
        closed_t = sorted([t for t in trades if t["status"] != "OPEN"],
                          key=lambda t: t.get("closedAt") or "", reverse=True)
        out["detail"] = {
            "id": strategy_id,
            "name": meta.get("name", strategy_id),
            "description": meta.get("description"),
            "regimeFit": meta.get("regimeFit", []),
            "scorecard": _scorecard(trades),
            "open": open_t,
            "closed": closed_t[:200],
        }
    return out


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
def set_auto(on):
    with _lock:
        state = _load()
        state["auto"] = bool(on)
        _save(state)
        return state["auto"]


def get_auto():
    return _load().get("auto", False)


def set_entry_mode(mode):
    mode = "open" if mode == "open" else "continuous"
    with _lock:
        state = _load()
        state["entryMode"] = mode
        _save(state)
        return mode


def reset():
    with _lock:
        _save(_default_state())
