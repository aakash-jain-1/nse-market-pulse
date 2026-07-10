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

Books
-----
Trades carry a `book` tag so two parallel portfolios run off the SAME live
context and strategies:
- `cash` : the all-market book (every idea) — the original sim.
- `fno`  : only F&O-eligible ideas (idea['fno']) — a dedicated F&O book.
Both use identical risk-based sizing, so their scorecards are directly
comparable. All read views take a `book=` argument (default 'cash').

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
import time
from datetime import datetime, timezone, timedelta

import db
import nse_client as nse
import strategies as strat

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = os.path.join(os.path.dirname(__file__), "sim_state.json")
NOTIONAL = 100_000.0   # fallback notional when a trade has no usable stop
RISK_PER_TRADE = 2_000.0   # fixed rupees risked per trade (position sizing unit)
MAX_NOTIONAL = 500_000.0   # cap so a very tight stop can't blow up position size
DEFAULT_MAX_SESSIONS = 3
STATE_VERSION = 2


def size_position(entry, stop, risk=RISK_PER_TRADE):
    """
    Risk-based sizing: pick a quantity so that hitting the stop loses exactly
    `risk` rupees (per-share risk = |entry - stop|). Capped by MAX_NOTIONAL.
    Falls back to flat NOTIONAL when there's no usable stop.
    Returns (qty, notional). With the default risk every trade risks the same,
    so equity curves / expectancy are comparable across strategies; passing a
    scaled `risk` lets a strategy size up/down by conviction (see gen_adaptive).
    """
    if not entry or entry <= 0:
        return 0.0, 0.0
    risk_per_share = abs(entry - stop) if stop else 0
    if risk_per_share > 0:
        qty = risk / risk_per_share
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
    # Trades now live in SQLite (db.sim_trades). The JSON holds only the small,
    # document-shaped settings + the bounded per-day rollup.
    return {
        "version": STATE_VERSION,
        "auto": True,   # sims run automatically on startup (no user input needed)
        "entryMode": "continuous",
        "maxSessions": DEFAULT_MAX_SESSIONS,
        "daily": {},
        "lastAutoDate": {},
        "createdAt": _now(),
    }


def _load():
    _ensure_migrated()
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("version") != STATE_VERSION:
            return _default_state()
        st.pop("strategies", None)   # trades moved to SQLite; drop stale blob
        st.setdefault("daily", {})
        st.setdefault("lastAutoDate", {})
        st.setdefault("entryMode", "continuous")
        st.setdefault("maxSessions", DEFAULT_MAX_SESSIONS)
        st.setdefault("auto", True)
        return st
    except Exception:
        return _default_state()


def _save(state):
    state.pop("strategies", None)    # never persist trades back into JSON
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


_migrated = False


def _ensure_migrated():
    """One-time move of any trades embedded in sim_state.json into SQLite."""
    global _migrated
    if _migrated:
        return
    with _lock:
        if _migrated:
            return
        db.init()
        try:
            if db.sim_trade_count() == 0 and os.path.exists(STATE_FILE):
                with open(STATE_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                trades = []
                for sid, book in (raw.get("strategies") or {}).items():
                    for t in (book or {}).get("trades", []):
                        t.setdefault("strategy", sid)
                        trades.append(t)
                if trades:
                    db.sim_insert_trades(trades)
        except Exception:
            pass
        _migrated = True


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
def _open_trade(idea, strategy_id, regime_label, book="cash"):
    entry = idea.get("entry") or idea.get("ltp")
    if not entry:
        return None
    direction = idea["direction"]
    # Regime-conditioned sizing: strategies may carry a conviction multiplier
    # (only the adaptive playbook does today). 1.0 = the standard fixed risk, so
    # every fixed strategy stays perfectly comparable.
    size_mult = idea.get("sizeMult") or 1.0
    risk = round(RISK_PER_TRADE * size_mult, 2)
    qty, notional = size_position(entry, idea.get("stop"), risk=risk)
    return {
        "id": f"{book}|{strategy_id}|{idea['symbol']}|{direction}|"
              f"{datetime.now(IST).strftime('%Y%m%d%H%M%S')}",
        "book": book,
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
        "risk": risk,
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
        "closedDay": None,
        "minsToExit": None,
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


_last_intrabar = 0.0
INTRABAR_INTERVAL = 180   # seconds between minute-candle catch-up sweeps


def _baked_epoch(ts_str):
    dt = datetime.fromisoformat(str(ts_str).replace(" ", "T"))
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _intrabar_catchup(state, open_trades):
    """
    Between the coarse 60s LTP samples a stop/target can be pierced by a wick and
    missed. Every few minutes we re-check still-OPEN trades against real 1-min
    candles and close any that actually hit their level intrabar. Only CLOSES are
    applied here (open trades keep their live LTP mark); LTP remains the fallback
    for symbols with no charting token.
    """
    global _last_intrabar
    now = time.time()
    if now - _last_intrabar < INTRABAR_INTERVAL:
        return
    _last_intrabar = now

    open_trades = [t for t in open_trades if t["status"] == "OPEN"]
    if not open_trades:
        return
    try:
        import concurrent.futures as cf
        import nse_quote
        import intrabar
    except Exception:
        return

    first = {}
    for t in open_trades:
        s = t["symbol"]
        if s not in first or t["openedAt"] < first[s]:
            first[s] = t["openedAt"]
    to = nse_quote._baked_now()
    max_sessions = state.get("maxSessions", DEFAULT_MAX_SESSIONS)

    def _one(s):
        try:
            d = nse_quote.get_ohlc(s, interval=1,
                                   from_ts=_baked_epoch(first[s]) - 120, to_ts=to)
            return s, (d.get("points") or []) if not d.get("error") else []
        except Exception:
            return s, []

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        candles = dict(ex.map(_one, sorted(first)))

    for t in open_trades:
        bars = candles.get(t["symbol"])
        if not bars:
            continue
        probe = dict(t)
        probe["openedTs"] = t["openedAt"]
        probe["maxSessions"] = max_sessions
        res = intrabar.resolve(probe, bars, t.get("risk", RISK_PER_TRADE),
                               max_sessions=max_sessions)
        if res in ("TARGET", "STOP", "EXPIRED"):
            t["status"] = res
            t["exitPrice"] = probe["exitPrice"]
            t["ltp"] = probe["ltp"]
            t["pnl"] = probe["pnl"]
            t["pnlPct"] = probe["pnlPct"]
            t["rMultiple"] = probe["rMultiple"]
            t["mfePct"] = probe["mfePct"]
            t["maePct"] = probe["maePct"]
            t["minsToExit"] = probe.get("minsToExit")
            t["closedAt"] = probe["closedTs"].replace("T", " ")
            t["closedDay"] = probe.get("closedDay")


def update(ctx=None):
    """Re-price and resolve every OPEN trade; persist changes to the DB ledger."""
    with _lock:
        state = _load()
        open_trades = db.sim_open_trades()
        for t in open_trades:
            _refresh_trade(state, t)
        _intrabar_catchup(state, open_trades)
        db.sim_insert_trades(open_trades)   # REPLACE-updates the changed rows
        return state


def take(strategy_ids=None, ctx=None, auto=False, limit=10, book="cash"):
    """
    Snapshot ideas into each strategy's ledger, for one BOOK. Dedups by
    (symbol, direction) among that strategy's trades opened today IN THAT BOOK.
    Honors entry mode for AUTO calls (in 'open' mode, only the first take per
    strategy per day per book). Returns per-strategy counts of new trades.

    The 'fno' book takes only F&O-eligible ideas (idea['fno']) from the SAME
    shared context — a filtered, parallel portfolio of the exact same setups,
    so cash-vs-F&O performance is directly comparable (same risk-based sizing).
    """
    ctx = ctx or build_ctx()
    regime_label = (ctx.get("regime") or {}).get("label", "?")
    with _lock:
        state = _load()
        ids = strategy_ids or [s["id"] for s in strat.STRATEGIES]
        mode = state.get("entryMode", "continuous")
        today = _today()
        added = {}
        new_trades = []
        for sid in ids:
            akey = f"{book}:{sid}"
            if auto and mode == "open" and state["lastAutoDate"].get(akey) == today:
                added[sid] = 0
                continue
            # One entry per (symbol, direction) per strategy per day per book: dedup
            # against everything opened TODAY in this book (any status), not just
            # still-open trades. This stops continuous mode from instantly
            # re-entering the same setup the moment a trade closes. The name is free
            # to reappear next session.
            taken_keys = {(t["symbol"], t["direction"])
                          for t in db.sim_trades_where(strategy=sid, opened_date=today, book=book)}
            ideas = strat.generate(sid, ctx)
            if book == "fno":
                ideas = [i for i in ideas if i.get("fno")]
            longs = sorted([i for i in ideas if i["direction"] == "LONG"],
                           key=lambda x: x.get("conviction", 0), reverse=True)[:limit]
            shorts = sorted([i for i in ideas if i["direction"] == "SHORT"],
                            key=lambda x: x.get("conviction", 0), reverse=True)[:limit]
            n = 0
            for idea in longs + shorts:
                key = (idea["symbol"], idea["direction"])
                if key in taken_keys:
                    continue
                tr = _open_trade(idea, sid, regime_label, book)
                if tr:
                    new_trades.append(tr)
                    taken_keys.add(key)
                    n += 1
            added[sid] = n
            if auto and mode == "open":
                state["lastAutoDate"][akey] = today
        if new_trades:
            db.sim_insert_trades(new_trades)
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
        # Per-book × per-strategy stats for the heatmap. by[book][sid] = [trades].
        by = {"cash": {}, "fno": {}}
        for t in db.sim_all_trades():
            bk = t.get("book") or "cash"
            by.setdefault(bk, {}).setdefault(t["strategy"], []).append(t)

        def _stats_for(book_map):
            out = {}
            for s in strat.STRATEGIES:
                sid = s["id"]
                opened = closed = wins = 0
                realized = unreal = 0.0
                for t in book_map.get(sid, []):
                    if t.get("openedDate") == today:
                        opened += 1
                    if t["status"] == "OPEN":
                        unreal += t["pnl"]
                    elif (t.get("closedAt") or "").startswith(today):
                        closed += 1
                        realized += t["pnl"]
                        if t["status"] == "TARGET":
                            wins += 1
                out[sid] = {
                    "opened": opened, "closed": closed, "wins": wins,
                    "winRate": round(wins / closed * 100, 1) if closed else None,
                    "realized": round(realized, 2),
                    "unrealized": round(unreal, 2),
                }
            return out

        day["books"] = {bk: _stats_for(by.get(bk, {})) for bk in ("cash", "fno")}
        day["strategies"] = day["books"]["cash"]   # back-compat (cash = all-market)
        _save(state)
        return day


def daily_performance(days=30, book="cash"):
    """
    Date-wise P&L across ALL strategies, straight from the durable ledger — the
    'what happened today / this week' view. For each calendar day we attribute:
      • opened  — trades entered that day (by openedDate)
      • closed  — trades that RESOLVED that day (by closedDay/closedAt), with the
                  realized P&L, R-multiple sum and target/stop/expiry split
    Plus a `today` card that also carries the whole live open book + its unrealised
    mark-to-market (open MTM belongs to 'now', not to any past day). Regime / NIFTY
    context per day is merged from the rollup log when available.

    Note: does NOT call update() — open-trade MTM is kept fresh by summary()'s
    per-poll update (fetched together in the UI) and by the Auto loop; repeating
    the heavy reprice here would double it on a large open book.
    """
    _ensure_migrated()
    trades = db.sim_all_trades(book=book)
    today = _today()
    ctxd = _load().get("daily", {})

    agg = {}

    def bucket(d):
        return agg.setdefault(d, {
            "date": d, "opened": 0, "closed": 0, "wins": 0, "stops": 0,
            "expired": 0, "realized": 0.0, "realizedR": 0.0,
        })

    open_now, unreal = 0, 0.0
    for t in trades:
        od = t.get("openedDate")
        if od:
            bucket(od)["opened"] += 1
        if t["status"] == "OPEN":
            open_now += 1
            unreal += t.get("pnl") or 0.0
            continue
        cd = ((t.get("closedDay") or "")[:10]
              or (t.get("closedAt") or "")[:10] or od)
        if not cd:
            continue
        b = bucket(cd)
        b["closed"] += 1
        b["realized"] += t.get("pnl") or 0.0
        b["realizedR"] += t.get("rMultiple") or 0.0
        st = t["status"]
        if st == "TARGET":
            b["wins"] += 1
        elif st == "STOP":
            b["stops"] += 1
        else:
            b["expired"] += 1

    def finish(b):
        c = ctxd.get(b["date"], {}) or {}
        return {
            **b,
            "realized": round(b["realized"], 2),
            "realizedR": round(b["realizedR"], 2),
            "winRate": round(b["wins"] / b["closed"] * 100, 1) if b["closed"] else None,
            "regime": c.get("regime"),
            "niftyPct": c.get("niftyPct"),
        }

    rows = [finish(agg[d]) for d in sorted(agg, reverse=True)[:days]]

    tb = agg.get(today) or bucket(today)
    today_card = {
        **finish(tb),
        "openNow": open_now,
        "unrealized": round(unreal, 2),
    }
    return {"today": today_card, "rows": rows, "riskPerTrade": RISK_PER_TRADE}


def day_trades(date, limit=400, book="cash"):
    """Individual trades for one calendar date — the drill-down behind a daily row:
    trades that CLOSED that day (the realized P&L) + trades OPENED that day that are
    still running. Trimmed to display fields, newest first, each tagged with its
    strategy's display name."""
    _ensure_migrated()
    date = (date or "")[:10]
    names = {s["id"]: s["name"] for s in strat.STRATEGIES}
    closed, opened_open = [], []
    for t in db.sim_all_trades(book=book):
        if t["status"] == "OPEN":
            if t.get("openedDate") == date:
                opened_open.append(t)
            continue
        cd = (t.get("closedDay") or "")[:10] or (t.get("closedAt") or "")[:10]
        if cd == date:
            closed.append(t)
    closed.sort(key=lambda t: t.get("closedAt") or "", reverse=True)
    opened_open.sort(key=lambda t: t.get("openedAt") or "", reverse=True)

    def trim(t):
        return {
            "strategy": t["strategy"],
            "strategyName": names.get(t["strategy"], t["strategy"]),
            "symbol": t["symbol"], "direction": t["direction"],
            "fno": t.get("fno", False),
            "entry": t.get("entry"), "exitPrice": t.get("exitPrice"),
            "ltp": t.get("ltp"), "stop": t.get("stop"), "target": t.get("target"),
            "pnl": t.get("pnl"), "pnlPct": t.get("pnlPct"),
            "rMultiple": t.get("rMultiple"), "status": t["status"],
            "openedAt": t.get("openedAt"), "closedAt": t.get("closedAt"),
        }

    return {
        "date": date,
        "closed": [trim(t) for t in closed[:limit]],
        "open": [trim(t) for t in opened_open[:limit]],
        "closedTotal": len(closed), "openTotal": len(opened_open),
    }


def daily_matrix(book="cash"):
    """Day × strategy comparison grid for the heatmap (per book)."""
    state = _load()
    ids = [s["id"] for s in strat.STRATEGIES]
    dates = sorted(state.get("daily", {}).keys(), reverse=True)
    rows = []
    for d in dates:
        day = state["daily"][d]
        # Prefer the per-book stats; fall back to the legacy 'strategies' blob
        # (all-market = cash) for days logged before the book split.
        cells_src = (day.get("books", {}) or {}).get(book)
        if cells_src is None and book == "cash":
            cells_src = day.get("strategies", {})
        cells_src = cells_src or {}
        rows.append({
            "date": d,
            "regime": day.get("regime"),
            "niftyPct": day.get("niftyPct"),
            "breadth": day.get("breadth"),
            "cells": {sid: cells_src.get(sid) for sid in ids},
        })
    return {"strategies": strat.strategy_meta(), "rows": rows}


# ----------------------------------------------------------------------------
# Regime leaderboard / strategy-of-the-day / equity curves
# ----------------------------------------------------------------------------
# Nice display order for regimes (unknown/extra ones appended after).
_REGIME_ORDER = ["Trend-Up", "Recovery", "Range", "Pullback", "Mixed", "Trend-Down"]


def regime_leaderboard(min_closed=1, trades=None, book="cash"):
    """
    Aggregate every trade by (regime-at-entry × strategy): closed count, win%,
    average per-trade %, total P&L. Flags the best strategy per regime by avg %.
    This is the accumulating forward-test — 'what works on a recovery day?'.
    """
    if trades is None:
        _ensure_migrated()
        trades = db.sim_all_trades(book=book)
    by = {}
    for t in trades:
        by.setdefault(t["strategy"], []).append(t)
    ids = [s["id"] for s in strat.STRATEGIES]
    agg = {}
    regimes = set()
    for s in strat.STRATEGIES:
        sid = s["id"]
        for t in by.get(sid, []):
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


def strategy_of_the_day(regime_label=None, min_closed=3, lb=None):
    """
    Pick the strategy to lean on today: the one with the best historical avg %
    in the current regime (needs >= min_closed samples), else fall back to the
    strategy whose design fits this regime.
    """
    if regime_label is None:
        regime_label = current_regime().get("label")
    if lb is None:
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


def equity_curves(trades=None):
    """Cumulative realized P&L per strategy, ordered by close time (equity curve)."""
    if trades is None:
        _ensure_migrated()
        trades = db.sim_all_trades()
    by = {}
    for t in trades:
        by.setdefault(t["strategy"], []).append(t)
    out = {}
    for s in strat.STRATEGIES:
        sid = s["id"]
        closed = [t for t in by.get(sid, [])
                  if t["status"] != "OPEN" and t.get("closedAt")]
        closed.sort(key=lambda t: t["closedAt"])
        cum, pts = 0.0, []
        for t in closed:
            cum += t.get("pnl") or 0.0
            pts.append(round(cum, 0))
        out[sid] = {"points": pts, "final": round(cum, 0), "n": len(pts)}
    return out


def leaderboard_bundle(book="cash"):
    """One call for the whole leaderboard section: table + today's pick + curves."""
    regime = current_regime()
    trades = db.sim_all_trades(book=book)
    lb = regime_leaderboard(trades=trades)
    return {
        "regime": regime,
        "leaderboard": lb,
        "pick": strategy_of_the_day(regime.get("label"), lb=lb)["pick"],
        "equity": equity_curves(trades=trades),
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
    risk_sum = sum(t.get("risk") or RISK_PER_TRADE for t in closed)
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
        "riskSum": round(risk_sum, 2),
        "expectancyR": round(total_r / n, 2) if n else None,
        "weightedR": round(realized / risk_sum, 3) if risk_sum else None,
        "todayClosed": len(today_closed),
        "todayWinRate": round(today_wins / len(today_closed) * 100, 1) if today_closed else None,
    }


def summary(strategy_id=None, book="cash"):
    """Overview scorecards + regime; plus one strategy's trade detail if asked."""
    update()
    state = _load()
    regime = current_regime()

    all_trades = db.sim_all_trades(book=book)
    by_strat = {}
    for t in all_trades:
        by_strat.setdefault(t["strategy"], []).append(t)

    cards = []
    for s in strat.STRATEGIES:
        sc = _scorecard(by_strat.get(s["id"], []))
        cards.append({
            "id": s["id"], "name": s["name"], "description": s["description"],
            "regimeFit": s["regimeFit"], **sc,
            "fitsNow": regime.get("label") in s["regimeFit"],
        })

    lb = regime_leaderboard(trades=all_trades)
    out = {
        "mode": "overview",
        "book": book,
        "auto": state.get("auto", False),
        "entryMode": state.get("entryMode", "continuous"),
        "maxSessions": state.get("maxSessions", DEFAULT_MAX_SESSIONS),
        "notional": NOTIONAL,
        "riskPerTrade": RISK_PER_TRADE,
        "regime": regime,
        "pick": strategy_of_the_day(regime.get("label"), lb=lb)["pick"],
        "strategies": cards,
        "generatedAt": _now(),
    }

    # What the Regime-Adaptive track is delegating to right now + today's
    # conviction multiplier. Drives the "playbook flip" / high-conviction alerts
    # and the live delegation line on the scoreboard.
    a_via, a_basis, a_cell = strat._regime_playbook_pick(regime.get("label"))
    out["adaptive"] = {
        "regime": regime.get("label"),
        "via": a_via,
        "viaName": strat.STRATEGY_MAP.get(a_via, {}).get("name") if a_via else None,
        "basis": a_basis,
        "sizeMult": strat.conviction_mult(a_basis, a_cell, regime) if a_via else None,
        "strength": strat.regime_strength(regime),
    }

    if strategy_id and strategy_id in strat.STRATEGY_MAP:
        trades = by_strat.get(strategy_id, [])
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


def reset(book=None):
    """Clear the ledger. book=None wipes everything + resets settings; a specific
    book clears only that book's trades (settings and the other book untouched)."""
    with _lock:
        if book is None:
            db.sim_clear()
            _save(_default_state())
        else:
            db.sim_clear(book=book)


# ----------------------------------------------------------------------------
# All-time performance (durable, cross-session)
# ----------------------------------------------------------------------------
def performance(book="cash"):
    """
    Cross-session leaderboard ranked by expectancy (R). One row per strategy plus
    a portfolio total, computed over the entire durable ledger — this is the
    'how has each strategy actually done, all-time' view.
    """
    _ensure_migrated()
    trades = db.sim_all_trades(book=book)
    by = {}
    for t in trades:
        by.setdefault(t["strategy"], []).append(t)

    def _extra(ts):
        closed = [t for t in ts if t["status"] in ("TARGET", "STOP", "EXPIRED")]
        gains = sum(t["pnl"] for t in closed if (t["pnl"] or 0) > 0)
        losses = -sum(t["pnl"] for t in closed if (t["pnl"] or 0) < 0)
        holds = [t["minsToExit"] for t in closed if t.get("minsToExit") is not None]
        days = {t["openedDate"] for t in ts if t.get("openedDate")}
        return {
            "profitFactor": round(gains / losses, 2) if losses else (None if not gains else 99.9),
            "avgHoldMins": int(sum(holds) / len(holds)) if holds else None,
            "tradingDays": len(days),
        }

    rows = []
    for s in strat.STRATEGIES:
        ts = by.get(s["id"], [])
        rows.append({"id": s["id"], "name": s["name"],
                     **_scorecard(ts), **_extra(ts)})
    # Rank by expectancy (R), strategies with no closed trades sink to the bottom.
    rows.sort(key=lambda r: (r["expectancyR"] if r["expectancyR"] is not None else -1e9),
              reverse=True)

    totals = {**_scorecard(trades), **_extra(trades)}
    first = min((t["openedAt"] for t in trades), default=None)
    return {
        "rows": rows,
        "totals": totals,
        "tradeCount": len(trades),
        "since": first,
        "generatedAt": _now(),
    }
