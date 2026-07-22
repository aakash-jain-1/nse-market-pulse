"""
portfolio_backtest.py — replay strategy trades through a REAL book.

WHY THIS EXISTS
---------------
`backtest_daily.py` reports per-trade expectancy in **R** (win rate, avg R, etc.).
That answers "does this signal have an edge?" but NOT "could I actually have traded
it?". Real trading has constraints the per-trade view ignores:

  - **Finite capital** — you can't take every signal; money gets tied up in open
    positions and is unavailable until they close.
  - **A cap on concurrent positions** — you realistically watch/hold only N names.
  - **Position sizing** — a fixed-% risk (or equal-weight) rule, sized off the
    CURRENT equity, compounds wins and shrinks after losses.

A strategy with a great average R can still be untradeable if it fires 50 signals a
day and you can hold 5. This module takes the exact trades `backtest_daily` already
produces and REPLAYS them through a portfolio with those constraints, producing an
**equity curve** plus the metrics traders actually care about: CAGR, max drawdown,
Sharpe, profit factor, exposure.

DESIGN
------
- `simulate()` is PURE: given a list of trade dicts (the shape emitted by
  `backtest_daily.run(_collect=True)`) + book params, it returns the equity curve +
  metrics. No DB, no network — so it's fully unit-tested with hand-built trades.
- Capital model: opening a position reserves `qty * entry` cash (a simple, unified
  "capital committed" model — for shorts this treats margin as full notional, which
  is conservative). Closing returns `reserve + pnl`. Equity is marked at COST while a
  position is open (it steps on closes), so the curve is exact at every close; intraday
  marking-to-market would need per-day prices and is a future refinement.
- Same-day signal contention: when more signals fire than free slots/capital, they're
  taken in a deterministic order (by `rank_key` desc if the trades carry one, else a
  stable strategy/symbol order) — never by outcome, so there's no look-ahead bias.
- `run()` is the only impure part: it pulls trades from `backtest_daily.run` (live or
  the full EOD universe) and simulates the whole book + each strategy on its own.

Educational / research — NOT investment advice.
"""

import math
from datetime import datetime

from nse_pulse.backtest import backtest_daily as bd

_CLOSED = ("STOP", "TARGET", "EXPIRED")   # a trade with a known exit we can replay


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _d(s):
    """Parse an ISO 'YYYY-MM-DD' date; None on anything unparseable."""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _usable(t):
    """A trade we can replay: closed, with both dates + entry/stop/exit prices and a
    non-degenerate stop distance."""
    if t.get("status") not in _CLOSED:
        return False
    o, c = _d(t.get("openedDate")), _d(t.get("closedDate"))
    entry, stop, exit_px = t.get("entry"), t.get("stop"), t.get("exitPrice")
    if None in (o, c, entry, stop, exit_px):
        return False
    return entry > 0 and stop > 0 and exit_px > 0 and entry != stop and c >= o


def _move_pct(direction, entry, exit_px):
    """Per-share move %, direction-aware (independent of size)."""
    if direction == "SHORT":
        return (entry / exit_px - 1) * 100.0
    return (exit_px / entry - 1) * 100.0


def _pnl(direction, entry, exit_px, qty):
    return (entry - exit_px) * qty if direction == "SHORT" else (exit_px - entry) * qty


def _mark(pos, day_iso, closes):
    """A position's current equity contribution. With `closes` (mark-to-market) it's
    reserve + unrealized P&L at today's close; without it, just the reserve (cost basis).
    LONG: reserve+upnl = qty*close. SHORT: reserve + qty*(entry-close) (margin model).
    Carries the last seen close forward across gap/holiday days with no bar."""
    if not closes:
        return pos["reserve"]
    px = (closes.get(pos["symbol"]) or {}).get(day_iso)
    if px is None:
        px = pos.get("lastMark") or pos["entry"]
    pos["lastMark"] = px
    return pos["reserve"] + _pnl(pos["direction"], pos["entry"], px, pos["qty"])


def _max_drawdown(equities):
    """Largest peak-to-trough drop in the equity series, as a positive %."""
    peak, worst = None, 0.0
    for e in equities:
        peak = e if peak is None else max(peak, e)
        if peak > 0:
            worst = min(worst, (e - peak) / peak)
    return round(-worst * 100, 2)


def _sharpe(rets, periods_per_year):
    """Annualized Sharpe from a series of per-period returns (rf=0). None if <2 pts
    or zero variance."""
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return round(mean / sd * math.sqrt(periods_per_year), 2)


def _size(sizing, equity, cash, entry, stop, max_positions, max_alloc_pct, risk_pct):
    """Shares to buy/short for one position, honouring the sizing rule and the
    per-position notional cap + available cash. 0 → can't afford / stop too wide."""
    if sizing == "equal":
        budget = equity / max_positions
    else:  # "risk": lose ~risk_pct% of equity if the stop is hit
        per_share_risk = abs(entry - stop)
        budget = (risk_pct / 100.0 * equity) / per_share_risk * entry if per_share_risk else 0
    cap = min(cash, max_alloc_pct / 100.0 * equity)   # never over-commit one name
    budget = min(budget, cap)
    if budget <= 0 or entry <= 0:
        return 0
    return int(budget // entry)


# ---------------------------------------------------------------------------
# the simulator (pure)
# ---------------------------------------------------------------------------
def simulate(trades, start_capital=1_000_000.0, max_positions=5, risk_pct=1.0,
             sizing="risk", max_alloc_pct=25.0, rank_key=None, periods_per_year=252,
             closes=None):
    """Replay `trades` through a book with finite capital + a concurrent-position cap.

    `closes` (optional `{symbol: {date_iso: close}}`) marks open positions **to market**
    each day for a true intra-trade equity curve / drawdown; without it, positions are
    held at cost (equity steps on exits).

    Returns {startCapital, endCapital, totalReturnPct, cagrPct, maxDrawdownPct,
    sharpe, winRate, profitFactor, avgWinPct, avgLossPct, tradesTaken,
    tradesSkippedSlot, tradesSkippedCapital, tradesUsable, tradesTotal, avgHoldDays,
    maxConcurrent, avgExposurePct, start, end, days, equityCurve[], closedTrades,
    params, note}.
    """
    start_capital = float(start_capital) if start_capital and start_capital > 0 else 1_000_000.0
    max_positions = int(_clip(int(max_positions or 1), 1, 50))
    risk_pct = float(_clip(risk_pct or 1.0, 0.05, 20.0))
    max_alloc_pct = float(_clip(max_alloc_pct or 25.0, 1.0, 100.0))
    sizing = "equal" if str(sizing).lower() == "equal" else "risk"

    total = len(trades)
    usable = [t for t in trades if _usable(t)]

    params = {"startCapital": start_capital, "maxPositions": max_positions,
              "riskPct": risk_pct, "sizing": sizing, "maxAllocPct": max_alloc_pct}
    if not usable:
        return {"startCapital": start_capital, "endCapital": start_capital,
                "totalReturnPct": 0.0, "cagrPct": 0.0, "maxDrawdownPct": 0.0,
                "sharpe": None, "winRate": 0.0, "profitFactor": None,
                "avgWinPct": 0.0, "avgLossPct": 0.0, "tradesTaken": 0,
                "tradesSkippedSlot": 0, "tradesSkippedCapital": 0,
                "tradesUsable": 0, "tradesTotal": total, "avgHoldDays": None,
                "maxConcurrent": 0, "avgExposurePct": 0.0, "start": None,
                "end": None, "days": 0, "equityCurve": [], "closedTrades": 0,
                "params": params,
                "note": "No closeable trades to simulate — run a daily backtest first "
                        "(or widen the window / lower the liquidity filters)."}

    # Bucket opens by their open date; closes are looked up per open position.
    opens = {}
    for t in usable:
        opens.setdefault(_d(t["openedDate"]), []).append(t)

    def _rank(t):
        rk = t.get(rank_key) if rank_key else None
        # desc by rank when present; stable, look-ahead-free tiebreak otherwise
        return (-(rk if isinstance(rk, (int, float)) else 0),
                str(t.get("strategy")), str(t.get("symbol")))

    # Walk every event day; when marking to market, also include the intervening
    # trading days (from the closes calendar) so the equity curve/drawdown are daily.
    ev_dates = set(opens.keys()) | {_d(t["closedDate"]) for t in usable}
    lo, hi = min(ev_dates), max(ev_dates)
    if closes:
        cal = {_d(k) for m in closes.values() for k in m}
        dates = sorted(d for d in (ev_dates | cal) if d and lo <= d <= hi)
    else:
        dates = sorted(ev_dates)

    cash = start_capital
    open_pos = []                 # list of dicts: qty, entry, exit_px, close_d, direction, reserve
    taken = skip_slot = skip_cap = 0
    closed = []                   # realized trade records
    curve = []                    # [{date, equity, drawdownPct}]
    equities = [start_capital]    # for drawdown/sharpe (day 0 = start)
    exposures = []                # deployed / equity each day
    max_concurrent = 0

    for day in dates:
        # 1) close everything exiting today (frees capital before we re-deploy)
        still = []
        for p in open_pos:
            if p["close_d"] == day:
                pnl = _pnl(p["direction"], p["entry"], p["exit_px"], p["qty"])
                cash += p["reserve"] + pnl
                closed.append({**p, "pnl": pnl,
                               "movePct": _move_pct(p["direction"], p["entry"], p["exit_px"])})
            else:
                still.append(p)
        open_pos = still

        # 2) open today's signals in priority order, while slots + cash allow.
        # Size off marked-to-market equity (risk % of what the book is worth NOW).
        day_iso = day.strftime("%Y-%m-%d")
        equity_now = cash + sum(_mark(p, day_iso, closes) for p in open_pos)
        for t in sorted(opens.get(day, []), key=_rank):
            if len(open_pos) >= max_positions:
                skip_slot += 1
                continue
            qty = _size(sizing, equity_now, cash, t["entry"], t["stop"],
                        max_positions, max_alloc_pct, risk_pct)
            if qty <= 0:
                skip_cap += 1
                continue
            reserve = qty * t["entry"]
            cash -= reserve
            open_pos.append({"qty": qty, "entry": t["entry"], "exit_px": t["exitPrice"],
                             "close_d": _d(t["closedDate"]), "direction": t["direction"],
                             "reserve": reserve, "symbol": t.get("symbol"),
                             "strategy": t.get("strategy"), "open_d": day,
                             "holdDays": t.get("holdDays")})
            taken += 1

        # 3) mark the book to market (or cost) and record the day. Exposure stays the
        # fraction of CAPITAL committed (reserves), independent of the mark.
        reserved = sum(p["reserve"] for p in open_pos)
        equity = cash + sum(_mark(p, day_iso, closes) for p in open_pos)
        max_concurrent = max(max_concurrent, len(open_pos))
        exposures.append((reserved / equity) if equity > 0 else 0.0)
        dd_peak = max(equities + [equity])
        curve.append({"date": day_iso, "equity": round(equity, 2),
                      "drawdownPct": round((equity - dd_peak) / dd_peak * 100, 2)
                      if dd_peak > 0 else 0.0})
        equities.append(equity)

    end_capital = equities[-1]
    span_days = (dates[-1] - dates[0]).days or 1
    cagr = ((end_capital / start_capital) ** (365.0 / span_days) - 1) * 100 \
        if end_capital > 0 else -100.0

    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    gross_win = sum(c["pnl"] for c in wins)
    gross_loss = -sum(c["pnl"] for c in losses)
    rets = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))
            if equities[i - 1] > 0]
    holds = [c.get("holdDays") for c in closed if c.get("holdDays") is not None]

    return {
        "startCapital": round(start_capital, 2),
        "endCapital": round(end_capital, 2),
        "totalReturnPct": round((end_capital / start_capital - 1) * 100, 2),
        "cagrPct": round(cagr, 2),
        "maxDrawdownPct": _max_drawdown(equities),
        "sharpe": _sharpe(rets, periods_per_year),
        "winRate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        # None when there are no losses (undefined / "infinite") — keeps the JSON
        # valid; the UI shows ∞ when there were wins but no losing trades.
        "profitFactor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avgWinPct": round(sum(c["movePct"] for c in wins) / len(wins), 2) if wins else 0.0,
        "avgLossPct": round(sum(c["movePct"] for c in losses) / len(losses), 2) if losses else 0.0,
        "tradesTaken": taken,
        "tradesSkippedSlot": skip_slot,
        "tradesSkippedCapital": skip_cap,
        "tradesUsable": len(usable),
        "tradesTotal": total,
        "avgHoldDays": round(sum(holds) / len(holds), 1) if holds else None,
        "maxConcurrent": max_concurrent,
        "avgExposurePct": round(sum(exposures) / len(exposures) * 100, 1) if exposures else 0.0,
        "start": dates[0].strftime("%Y-%m-%d"),
        "end": dates[-1].strftime("%Y-%m-%d"),
        "days": span_days,
        "equityCurve": curve,
        "closedTrades": len(closed),
        "params": params,
        "note": None,
    }


# ---------------------------------------------------------------------------
# run (impure: pulls trades from backtest_daily)
# ---------------------------------------------------------------------------
def run(days=60, universe_size=40, source="live", start_capital=1_000_000.0,
        max_positions=5, risk_pct=1.0, sizing="risk", max_alloc_pct=25.0,
        max_hold=5, per_strategy=True, min_price=None, min_value_cr=None):
    """Run a daily backtest, then replay its trades through the book. Returns the
    overall portfolio result + (optionally) a per-strategy comparison so you can see
    which strategy actually compounds capital, not just which has the best per-trade R.
    """
    kw = dict(days=days, universe_size=universe_size, source=source,
              max_hold=max_hold, resolve="daily", _collect=True)
    if min_price is not None:
        kw["min_price"] = min_price
    if min_value_cr is not None:
        kw["min_value_cr"] = min_value_cr
    bt = bd.run(**kw)

    trades = bt.get("trades") or []
    # Rank same-day signal contention by each trade's entry-time conviction (bd attaches
    # `score`) so a finite book picks the STRONGEST signals, not an arbitrary first-N.
    # `closes` (traded symbols' daily closes) marks open positions to market each day.
    sim_kw = dict(start_capital=start_capital, max_positions=max_positions,
                  risk_pct=risk_pct, sizing=sizing, max_alloc_pct=max_alloc_pct,
                  rank_key="score", closes=bt.get("closes"))
    overall = simulate(trades, **sim_kw)

    out = {
        "source": "eod" if str(source).lower() == "eod" else "live",
        "window": {"days": bt.get("days") or days,
                   "universeWithData": bt.get("universeWithData"),
                   "universeAvailable": bt.get("universeAvailable"),
                   "range": bt.get("range"), "trades": len(trades)},
        "message": bt.get("message"),          # e.g. "no EOD history ingested yet"
        "overall": overall,
        "perStrategy": [],
        "generatedAt": bd._now(),
    }

    if per_strategy and trades:
        by_strat = {}
        for t in trades:
            by_strat.setdefault(t.get("strategy"), []).append(t)
        rows = []
        for sid, ts in by_strat.items():
            r = simulate(ts, **sim_kw)
            rows.append({"id": sid, "name": (bd.STRAT_MAP.get(sid) or {}).get("name", sid),
                         "totalReturnPct": r["totalReturnPct"], "cagrPct": r["cagrPct"],
                         "maxDrawdownPct": r["maxDrawdownPct"], "sharpe": r["sharpe"],
                         "winRate": r["winRate"], "profitFactor": r["profitFactor"],
                         "tradesTaken": r["tradesTaken"], "endCapital": r["endCapital"]})
        rows.sort(key=lambda x: (x["totalReturnPct"] if x["totalReturnPct"] is not None
                                 else -1e9), reverse=True)
        out["perStrategy"] = rows

    return out
