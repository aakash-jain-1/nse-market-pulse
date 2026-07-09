"""
Offline strategy backtester
===========================
Replays the SAME strategy generators (`strategies.py`) over the archived
per-cycle context stored in `db.context_log`, so we can measure how each of the
7 strategies WOULD have performed on real history — without waiting for live
forward-sim days to accumulate.

Method (mirrors the live sim's trade lifecycle, on a virtual clock):
  - Walk stored cycles in time order.
  - Each cycle: reprice open trades (target / stop / multi-day-horizon expiry),
    then take that cycle's fresh ideas (one entry per symbol+direction per day
    per strategy, matching the live dedup). `open` entry mode takes only in each
    day's first cycle; `continuous` takes every cycle.
  - Forward-price from later cycles' LTPs (quotes preferred, else the board).
  - Report per-strategy scorecards, equity curves, and a regime leaderboard.

Educational forward-/back-test of signal quality — NOT investment advice.
"""

import db
import sim
import strategies as strat

RISK_PER_TRADE = sim.RISK_PER_TRADE
DEFAULT_MAX_SESSIONS = 3
_REGIME_ORDER = ["Trend-Up", "Recovery", "Range", "Pullback", "Mixed", "Trend-Down"]


def _price_map(ctx):
    """symbol -> LTP for this cycle, quotes taking precedence over the boards."""
    m = {}
    for key in ("scanner", "gainers", "losers", "volgainers", "oispurts"):
        for r in ctx.get(key, []):
            s, p = r.get("symbol"), r.get("ltp")
            if s and p and s not in m:
                m[s] = p
    for s, q in (ctx.get("quotes") or {}).items():
        if s and q.get("ltp"):
            m[s] = q["ltp"]  # most accurate
    return m


def _move_pct(direction, entry, px):
    return (px - entry) / entry * 100 if direction == "LONG" else (entry - px) / entry * 100


def _resolve(t, px, sessions):
    """Close t if target/stop hit or horizon exceeded; return True if closed."""
    tgt, stop = t["target"], t["stop"]
    hit = exit_px = None
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
    if not hit and sessions > t["maxSessions"]:
        hit, exit_px = "EXPIRED", px
    if hit:
        t["status"] = hit
        t["exitPrice"] = round(exit_px, 2)
        t["pnlPct"] = round(_move_pct(t["direction"], t["entry"], exit_px), 2)
        t["pnl"] = round(t["qty"] * (exit_px - t["entry"]) *
                         (1 if t["direction"] == "LONG" else -1), 2)
        t["rMultiple"] = round(t["pnl"] / RISK_PER_TRADE, 2)
        return True
    return False


def _scorecard(trades):
    closed = [t for t in trades if t["status"] in ("TARGET", "STOP", "EXPIRED")]
    open_t = [t for t in trades if t["status"] == "OPEN"]
    wins = sum(1 for t in closed if t["status"] == "TARGET")
    n = len(closed)
    realized = sum(t["pnl"] for t in closed)
    total_r = sum(t.get("rMultiple") or 0 for t in closed)
    avg_pct = sum(t["pnlPct"] for t in closed) / n if n else None
    return {
        "trades": len(trades), "open": len(open_t), "closed": n,
        "target": wins, "stop": sum(1 for t in closed if t["status"] == "STOP"),
        "expired": sum(1 for t in closed if t["status"] == "EXPIRED"),
        "winRate": round(wins / n * 100, 1) if n else None,
        "avgPnlPct": round(avg_pct, 2) if avg_pct is not None else None,
        "realizedPnl": round(realized, 2),
        "totalR": round(total_r, 2),
        "expectancyR": round(total_r / n, 2) if n else None,
    }


def _equity(trades):
    closed = sorted([t for t in trades if t["status"] != "OPEN" and t.get("closedTs")],
                    key=lambda t: t["closedTs"])
    cum, pts = 0.0, []
    for t in closed:
        cum += t["pnl"]
        pts.append(round(cum, 0))
    return {"points": pts, "final": round(cum, 0), "n": len(pts)}


def _leaderboard(all_trades, ids, names):
    agg, regimes = {}, set()
    for sid in ids:
        for t in all_trades[sid]:
            rg = t.get("regimeAtEntry") or "?"
            regimes.add(rg)
            a = agg.setdefault((rg, sid), {"closed": 0, "wins": 0, "pnlPctSum": 0.0})
            if t["status"] != "OPEN":
                a["closed"] += 1
                if t["status"] == "TARGET":
                    a["wins"] += 1
                a["pnlPctSum"] += t["pnlPct"]
    ordered = ([r for r in _REGIME_ORDER if r in regimes] +
               sorted(r for r in regimes if r not in _REGIME_ORDER))
    rows = []
    for rg in ordered:
        cells, best_sid, best = {}, None, None
        for sid in ids:
            a = agg.get((rg, sid))
            if not a or not a["closed"]:
                cells[sid] = None
                continue
            avg = a["pnlPctSum"] / a["closed"]
            cells[sid] = {"closed": a["closed"],
                          "winRate": round(a["wins"] / a["closed"] * 100, 1),
                          "avgPnlPct": round(avg, 2)}
            if best is None or avg > best:
                best, best_sid = avg, sid
        rows.append({"regime": rg, "best": best_sid, "cells": cells})
    return {"strategies": [{"id": i, "name": names[i]} for i in ids], "rows": rows}


def run(strategy_ids=None, since_day=None, max_sessions=DEFAULT_MAX_SESSIONS,
        entry_mode="continuous", limit=10):
    db.init()
    cycles = db.context_cycles(since_day=since_day)
    if len(cycles) < 2:
        return {"message": "Not enough archived context yet — the logger captures "
                           "a strategy-context snapshot every few minutes during "
                           "market hours. Let it run, then backtest.",
                "cycles": len(cycles), "strategies": [], "leaderboard": None}

    ids = strategy_ids or [s["id"] for s in strat.STRATEGIES]
    names = {s["id"]: s["name"] for s in strat.STRATEGIES}
    days_sorted = sorted({c["day"] for c in cycles})
    day_index = {d: i for i, d in enumerate(days_sorted)}
    first_cycle_of_day = set()
    seen_days = set()
    for c in cycles:
        if c["day"] not in seen_days:
            seen_days.add(c["day"])
            first_cycle_of_day.add(c["ts"])

    books = {sid: [] for sid in ids}

    for c in cycles:
        ctx = c["ctx"]
        ctx["scannerSyms"] = {r["symbol"] for r in ctx.get("scanner", []) if r.get("symbol")}
        ctx["regime"] = {"label": c["regime"]}
        day, ts = c["day"], c["ts"]
        pm = _price_map(ctx)

        for sid in ids:
            # 1) reprice open trades
            for t in books[sid]:
                if t["status"] != "OPEN":
                    continue
                px = pm.get(t["symbol"])
                if px is None:
                    continue
                t["ltp"] = round(px, 2)
                sessions = day_index[day] - day_index[t["openedDay"]] + 1
                if _resolve(t, px, sessions):
                    t["closedTs"] = ts
                    t["closedDay"] = day
                else:
                    t["pnlPct"] = round(_move_pct(t["direction"], t["entry"], px), 2)
                    t["pnl"] = round(t["qty"] * (px - t["entry"]) *
                                     (1 if t["direction"] == "LONG" else -1), 2)
                    t["rMultiple"] = round(t["pnl"] / RISK_PER_TRADE, 2)

            # 2) take fresh ideas (respect entry mode)
            if entry_mode == "open" and ts not in first_cycle_of_day:
                continue
            taken = {(t["symbol"], t["direction"]) for t in books[sid]
                     if t["openedDay"] == day}
            ideas = strat.generate(sid, ctx)
            longs = sorted([i for i in ideas if i["direction"] == "LONG"],
                           key=lambda x: x.get("conviction", 0), reverse=True)[:limit]
            shorts = sorted([i for i in ideas if i["direction"] == "SHORT"],
                            key=lambda x: x.get("conviction", 0), reverse=True)[:limit]
            for idea in longs + shorts:
                key = (idea["symbol"], idea["direction"])
                entry = idea.get("entry") or idea.get("ltp")
                if key in taken or not entry:
                    continue
                qty, _ = sim.size_position(entry, idea.get("stop"))
                books[sid].append({
                    "symbol": idea["symbol"], "direction": idea["direction"],
                    "conviction": idea.get("conviction"), "entry": round(entry, 2),
                    "stop": idea.get("stop"), "target": idea.get("target"),
                    "qty": qty, "maxSessions": max_sessions,
                    "status": "OPEN", "ltp": round(entry, 2), "pnl": 0.0, "pnlPct": 0.0,
                    "rMultiple": 0.0, "openedTs": ts, "openedDay": day,
                    "regimeAtEntry": c["regime"],
                    "exitPrice": None, "closedTs": None, "closedDay": None,
                })
                taken.add(key)

    strat_out = []
    for sid in ids:
        sc = _scorecard(books[sid])
        strat_out.append({
            "id": sid, "name": names[sid],
            "regimeFit": strat.STRATEGY_MAP[sid]["regimeFit"],
            "equity": _equity(books[sid]), **sc,
        })

    return {
        "message": None,
        "cycles": len(cycles),
        "days": days_sorted,
        "range": {"from": cycles[0]["ts"], "to": cycles[-1]["ts"]},
        "maxSessions": max_sessions,
        "entryMode": entry_mode,
        "riskPerTrade": RISK_PER_TRADE,
        "strategies": strat_out,
        "leaderboard": _leaderboard(books, ids, names),
    }
