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

import concurrent.futures as cf
from datetime import datetime, timedelta, timezone

from nse_pulse.core import db
from nse_pulse.core import intrabar
from nse_pulse.core import nse_quote
from nse_pulse.sim import sim
from nse_pulse.sim import strategies as strat

RISK_PER_TRADE = sim.RISK_PER_TRADE
DEFAULT_MAX_SESSIONS = 3
IST = timezone(timedelta(hours=5, minutes=30))
_REGIME_ORDER = ["Trend-Up", "Recovery", "Range", "Pullback", "Mixed", "Trend-Down"]


def _epoch_s(ts_iso):
    """Cycle/trade timestamps are IST wall-clock. charting.nseindia.com expects
    that wall clock baked as UTC (see nse_quote._baked_epoch), NOT real unix."""
    dt = datetime.fromisoformat(str(ts_iso).replace(" ", "T"))
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)   # keep the IST wall clock, drop the offset
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


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
    """Close t if target/stop hit or horizon exceeded; return True if closed.

    Coarse single-price path shared with the live sim via intrabar.resolve_point
    (AUDIT.md M9)."""
    hit, exit_px = intrabar.resolve_point(
        t["direction"], t["entry"], t["stop"], t["target"], px)
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


def _median(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _scorecard(trades):
    closed = [t for t in trades if t["status"] in ("TARGET", "STOP", "EXPIRED")]
    open_t = [t for t in trades if t["status"] == "OPEN"]
    wins = sum(1 for t in closed if t["status"] == "TARGET")
    n = len(closed)
    realized = sum(t["pnl"] for t in closed)
    total_r = sum(t.get("rMultiple") or 0 for t in closed)
    avg_pct = sum(t["pnlPct"] for t in closed) / n if n else None
    mfes = [t.get("mfePct") for t in closed if t.get("mfePct") is not None]
    maes = [t.get("maePct") for t in closed if t.get("maePct") is not None]
    mins = [t.get("minsToExit") for t in closed if t.get("minsToExit") is not None]
    return {
        "trades": len(trades), "open": len(open_t), "closed": n,
        "target": wins, "stop": sum(1 for t in closed if t["status"] == "STOP"),
        "expired": sum(1 for t in closed if t["status"] == "EXPIRED"),
        "winRate": round(wins / n * 100, 1) if n else None,
        "avgPnlPct": round(avg_pct, 2) if avg_pct is not None else None,
        "realizedPnl": round(realized, 2),
        "totalR": round(total_r, 2),
        "expectancyR": round(total_r / n, 2) if n else None,
        "avgMfePct": round(sum(mfes) / len(mfes), 2) if mfes else None,
        "avgMaePct": round(sum(maes) / len(maes), 2) if maes else None,
        "medianMinsToExit": int(_median(mins)) if mins else None,
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


def _take_entries(cycles, ids, day_index, entry_mode, max_sessions, limit):
    """Pass 1: walk cycles chronologically and open trades (no resolution).
    Also returns a per-symbol LTP series (for the fallback resolver)."""
    first_cycle_of_day, seen_days = set(), set()
    for c in cycles:
        if c["day"] not in seen_days:
            seen_days.add(c["day"])
            first_cycle_of_day.add(c["ts"])

    books = {sid: [] for sid in ids}
    series = {}   # symbol -> [(ts, day, px), ...] chronological
    for c in cycles:
        ctx = c["ctx"]
        ctx["scannerSyms"] = {r["symbol"] for r in ctx.get("scanner", []) if r.get("symbol")}
        ctx["regime"] = {"label": c["regime"]}
        day, ts = c["day"], c["ts"]
        pm = _price_map(ctx)
        for s, px in pm.items():
            series.setdefault(s, []).append((ts, day, px))

        for sid in ids:
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
                    "rMultiple": 0.0, "mfePct": 0.0, "maePct": 0.0, "minsToExit": None,
                    "openedTs": ts, "openedDay": day, "regimeAtEntry": c["regime"],
                    "exitPrice": None, "closedTs": None, "closedDay": None,
                })
                taken.add(key)
    return books, series


def _fetch_candles(symbols, opened_lo, opened_hi, max_sessions):
    """Pass 2 prep: pull 1-min OHLCV once per unique symbol (bounded pool)."""
    frm = _epoch_s(opened_lo) - 120
    to = min(_epoch_s(opened_hi) + (max_sessions + 1) * 86400, nse_quote._baked_now())

    def _one(sym):
        try:
            d = nse_quote.get_ohlc(sym, interval=1, from_ts=frm, to_ts=to)
            return sym, (d.get("points") or []) if not d.get("error") else []
        except Exception:
            return sym, []

    out = {}
    if not symbols:
        return out
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for sym, pts in ex.map(_one, sorted(symbols)):
            out[sym] = pts
    return out


def _resolve_ltp(trade, series, day_index):
    """Fallback: resolve against the coarse per-cycle LTP series (no candles)."""
    opened_day = trade["openedDay"]
    for ts, day, px in series:
        if ts < trade["openedTs"]:
            continue
        trade["ltp"] = round(px, 2)
        sessions = day_index.get(day, 0) - day_index.get(opened_day, 0) + 1
        if _resolve(trade, px, sessions):
            trade["closedTs"], trade["closedDay"] = ts, day
            return
        trade["pnlPct"] = round(_move_pct(trade["direction"], trade["entry"], px), 2)
        trade["pnl"] = round(trade["qty"] * (px - trade["entry"]) *
                             (1 if trade["direction"] == "LONG" else -1), 2)
        trade["rMultiple"] = round(trade["pnl"] / RISK_PER_TRADE, 2)


def run(strategy_ids=None, since_day=None, max_sessions=DEFAULT_MAX_SESSIONS,
        entry_mode="continuous", limit=10, resolve="intrabar"):
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

    # Pass 1 — open all trades from archived context.
    books, series = _take_entries(cycles, ids, day_index, entry_mode,
                                  max_sessions, limit)

    # Pass 2 — resolve exits against real minute candles (LTP fallback).
    candles = {}
    intrabar_syms = fallback_syms = 0
    if resolve == "intrabar":
        all_trades = [t for sid in ids for t in books[sid]]
        if all_trades:
            symbols = {t["symbol"] for t in all_trades}
            lo = min(t["openedTs"] for t in all_trades)
            hi = max(t["openedTs"] for t in all_trades)
            candles = _fetch_candles(symbols, lo, hi, max_sessions)

    for sid in ids:
        for t in books[sid]:
            bars = candles.get(t["symbol"]) if resolve == "intrabar" else None
            res = None
            if bars:
                res = intrabar.resolve(t, bars, RISK_PER_TRADE,
                                       max_sessions=max_sessions)
            if res is None:
                fallback_syms += 1
                _resolve_ltp(t, series.get(t["symbol"], []), day_index)
            else:
                intrabar_syms += 1

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
        "resolve": resolve,
        "resolved": {"intrabar": intrabar_syms, "ltpFallback": fallback_syms},
        "riskPerTrade": RISK_PER_TRADE,
        "strategies": strat_out,
        "leaderboard": _leaderboard(books, ids, names),
    }
