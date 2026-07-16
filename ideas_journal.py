"""
Ideas journal — durable daily record of every idea + all-day move tracking
===========================================================================
The Ideas tab (`/api/recommendations`) is otherwise stateless: it regenerates
the top long/short setups every poll, so "entry" always equals the current
price and there's no memory of WHEN an idea first fired. That makes it
impossible to answer the obvious question — "an idea came at the open; how has
it done since?" — let alone "how did last week's ideas play out?".

This journal gives the Ideas view a real memory:
  • On an idea's FIRST appearance on a given day we freeze its plan
    (entry/stop/target) and stamp `firstSeenAt`. Those never change for the day.
  • Every poll re-prices the whole day's set and reports `movePct` — the move
    since entry IN THE IDEA'S DIRECTION (green = working, red = against) — plus
    the running best/worst (MFE/MAE).
  • The first time an idea's move touches its target or stop we record a sticky
    `outcome` (TARGET / STOP) + the time, so each idea gets an honest "was it
    right?" verdict without look-ahead.
  • Ideas persist for the day even after they drop out of the fresh top-N
    (`fresh=False`, shown as "tracking").

Persistence is now DURABLE and MULTI-DAY: records live in SQLite (`ideas`
table in data/market.db), so every session accumulates and a historical view
(`history()` / `day_ideas()`) can browse past days and how their ideas played
out. Educational — NOT advice.

Pricing note: re-pricing uses a caller-supplied `price_fn` that MUST be cheap
(a cached hot-list lookup). We never trigger a per-symbol network fetch here;
symbols that fall out of the hot lists keep their last-known price.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import db

IST = timezone(timedelta(hours=5, minutes=30))
MAX_PER_SIDE = 20          # cap how many tracked ideas we surface per direction
# Intrabar outcome pass: how often (seconds) we re-check unresolved ideas against
# real 1-min candles. Throttled + market-hours gated to stay light on NSE.
INTRABAR_INTERVAL = 180
_last_intrabar = 0.0
# Legacy per-day JSON (pre-SQLite). Imported once so a mid-day switch loses nothing.
LEGACY_FILE = os.path.join(os.path.dirname(__file__), "ideas_journal.json")

_lock = threading.RLock()
_migrated = False


def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _key(sym, direction):
    return f"{sym}|{direction}"


def _move_pct(direction, entry, px):
    """Signed move since entry in the idea's favour (LONG up = +, SHORT down = +)."""
    if not entry or px is None:
        return None
    raw = (px - entry) if direction == "LONG" else (entry - px)
    return round(raw / entry * 100, 2)


def _age_min(first_seen):
    try:
        dt = datetime.strptime(first_seen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        return max(0, int((datetime.now(IST) - dt).total_seconds() // 60))
    except Exception:
        return None


def _migrate_json_once():
    """Best-effort one-time import of the old ideas_journal.json (today only)."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if not os.path.exists(LEGACY_FILE):
            return
        with open(LEGACY_FILE, encoding="utf-8") as f:
            st = json.load(f)
        day = st.get("day")
        if not day or db.ideas_for_day(day):        # only if that day isn't in the DB yet
            return
        rows = []
        for rec in (st.get("ideas") or {}).values():
            rec = dict(rec)
            rec["day"] = day
            rec.setdefault("outcome", None)
            rec.setdefault("outcomeAt", None)
            rec.setdefault("outcomePct", None)
            rec.pop("lastLtp", None)
            rows.append(rec)
        db.ideas_upsert(rows)
    except Exception:
        pass


def _resolve_outcome(rec, mv, now):
    """Sticky first-touch verdict from the COARSE move (poll-time LTP).

    A cheap fallback: it can miss an intrabar wick between polls and records the
    poll-time move rather than the exact level. `resolve_outcomes_intrabar()`
    supersedes it with candle-accurate verdicts for tokened symbols (AUDIT.md L7).
    """
    if rec.get("outcome") or not rec.get("entry") or mv is None:
        return
    tp, sp = rec.get("targetPct"), rec.get("stopPct")
    if tp and mv >= tp:
        rec["outcome"], rec["outcomeAt"], rec["outcomePct"] = "TARGET", now, mv
    elif sp and mv <= -sp:
        rec["outcome"], rec["outcomeAt"], rec["outcomePct"] = "STOP", now, mv


def _market_ish():
    """Weekday 09:15–15:45 IST — the only window where new 1-min candles arrive,
    so we don't fetch (or throttle-burn) outside it."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm <= (15 * 60 + 45)


def _firstseen_baked_epoch(first_seen):
    """firstSeenAt ('YYYY-MM-DD HH:MM:SS', naive IST) -> charting baked epoch (s)."""
    import nse_quote
    dt = datetime.strptime(first_seen, "%Y-%m-%d %H:%M:%S")
    return nse_quote._baked_epoch(dt)


def resolve_outcomes_intrabar():
    """Candle-accurate first-touch verdicts for today's UNRESOLVED ideas (L7).

    Load-conscious by design: throttled to INTRABAR_INTERVAL, market-hours gated,
    only for ideas that still lack a verdict AND have a full plan, one batched
    1-min fetch per symbol (deduped, 6-worker pool, token-gated + 30s-cached in
    nse_quote), conservative STOP-first tie-break via intrabar.resolve. Symbols
    without a charting token keep enrich()'s coarse LTP verdict. The DB write only
    sets the outcome fields (re-read under the lock) so it can't clobber a
    concurrent poll's live ltp/movePct.
    """
    global _last_intrabar
    # Race-safe throttle: only one caller per interval wins the pass.
    with _lock:
        if (time.time() - _last_intrabar) < INTRABAR_INTERVAL or not _market_ish():
            return
        _last_intrabar = time.time()

    db.init()
    today = _today()
    pending = [r for r in db.ideas_for_day(today)
               if not r.get("outcome") and r.get("entry") and r.get("stop")
               and r.get("target") and r.get("firstSeenAt")]
    if not pending:
        return
    try:
        import concurrent.futures as cf
        import intrabar
        import nse_quote
    except Exception:
        return

    # One fetch per symbol, from its EARLIEST idea's first-seen time.
    first = {}
    for r in pending:
        s = r["symbol"]
        if s not in first or r["firstSeenAt"] < first[s]:
            first[s] = r["firstSeenAt"]
    to = nse_quote._baked_now()

    def _one(s):
        try:
            frm = _firstseen_baked_epoch(first[s]) - 120
            d = nse_quote.get_ohlc(s, interval=1, from_ts=frm, to_ts=to)
            return s, ((d.get("points") or []) if not d.get("error") else [])
        except Exception:
            return s, []

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        candles = dict(ex.map(_one, sorted(first)))

    verdicts = {}   # (symbol,direction) -> (outcome, outcomeAt, outcomePct)
    for r in pending:
        bars = candles.get(r["symbol"])
        if not bars:
            continue
        probe = {"direction": r["direction"], "entry": r["entry"],
                 "stop": r.get("stop"), "target": r.get("target"),
                 "qty": 1.0, "openedTs": r["firstSeenAt"], "maxSessions": 999}
        res = intrabar.resolve(probe, bars, 1.0, max_sessions=999)
        if res in ("TARGET", "STOP"):
            at = (probe.get("closedTs") or "").replace("T", " ") or _now()
            verdicts[_key(r["symbol"], r["direction"])] = (res, at, probe.get("pnlPct"))

    if not verdicts:
        return
    # Merge only the outcome fields onto the freshest rows (don't overwrite live
    # ltp/movePct a concurrent enrich() may have just written).
    with _lock:
        latest = {_key(r["symbol"], r["direction"]): r for r in db.ideas_for_day(today)}
        merged = []
        for k, (outcome, at, pct) in verdicts.items():
            cur = latest.get(k)
            if cur and not cur.get("outcome"):
                cur["outcome"], cur["outcomeAt"], cur["outcomePct"] = outcome, at, pct
                merged.append(cur)
        if merged:
            db.ideas_upsert(merged)


def enrich(fresh_longs, fresh_shorts, price_fn=None):
    """
    Record today's fresh ideas (freezing entry/plan/timestamp on first sight),
    re-price the entire day's set, resolve target/stop touches, persist to
    SQLite, and return (longs, shorts) each carrying a FIXED entry + firstSeenAt
    and a live ltp + movePct (+ best/worst + outcome + fresh flag + ageMin).
    Fresh ideas sort first (by conviction); ideas seen earlier today sort after
    them by biggest absolute move.
    """
    db.init()
    _migrate_json_once()
    # Kick the candle-accurate outcome pass in the background (throttled + market-
    # hours gated internally) so it never blocks this poll (AUDIT.md L7). Its
    # verdicts land in the DB and are picked up by this + subsequent reads.
    try:
        threading.Thread(target=resolve_outcomes_intrabar, daemon=True).start()
    except Exception:
        pass
    with _lock:
        today, now = _today(), _now()
        ideas = {_key(r["symbol"], r["direction"]): r for r in db.ideas_for_day(today)}
        fresh_keys = set()

        for idea in list(fresh_longs) + list(fresh_shorts):
            k = _key(idea["symbol"], idea["direction"])
            fresh_keys.add(k)
            rec = ideas.get(k)
            if rec is None:
                m0 = _move_pct(idea["direction"], idea.get("entry"), idea.get("ltp")) or 0.0
                ideas[k] = {
                    "day": today, "symbol": idea["symbol"], "direction": idea["direction"],
                    "entry": idea.get("entry"), "stop": idea.get("stop"),
                    "target": idea.get("target"), "stopPct": idea.get("stopPct"),
                    "targetPct": idea.get("targetPct"), "rr": idea.get("rr"),
                    "conviction": idea.get("conviction"), "rating": idea.get("rating"),
                    "reasons": idea.get("reasons", []), "fno": idea.get("fno", False),
                    "pChange": idea.get("pChange"),
                    "firstSeenAt": now, "lastSeenAt": now, "ltp": idea.get("ltp"),
                    "movePct": m0, "maxMovePct": m0, "minMovePct": m0,
                    "outcome": None, "outcomeAt": None, "outcomePct": None,
                }
            else:
                # Plan + firstSeenAt + outcome stay frozen; refresh the live-ish fields.
                rec["lastSeenAt"] = now
                if idea.get("ltp") is not None:
                    rec["ltp"] = idea["ltp"]
                if idea.get("conviction") is not None:
                    rec["conviction"] = idea["conviction"]
                    rec["rating"] = idea.get("rating", rec.get("rating"))
                if idea.get("reasons"):
                    rec["reasons"] = idea["reasons"]
                if idea.get("pChange") is not None:
                    rec["pChange"] = idea["pChange"]

        out = {"LONG": [], "SHORT": []}
        for k, rec in ideas.items():
            # Cheap re-price only: fresh idea's own ltp → cached map → last known.
            px = rec.get("ltp") if k in fresh_keys else None
            if px is None and price_fn:
                try:
                    px = price_fn(rec["symbol"])
                except Exception:
                    px = None
            if px is None:
                px = rec.get("ltp")            # last stored price
            rec["ltp"] = px

            mv = _move_pct(rec["direction"], rec.get("entry"), px)
            if mv is not None:
                rec["movePct"] = mv
                base_max = rec["maxMovePct"] if rec.get("maxMovePct") is not None else mv
                base_min = rec["minMovePct"] if rec.get("minMovePct") is not None else mv
                rec["maxMovePct"] = round(max(base_max, mv), 2)
                rec["minMovePct"] = round(min(base_min, mv), 2)
                _resolve_outcome(rec, mv, now)

            item = dict(rec)
            item["fresh"] = k in fresh_keys
            item["ageMin"] = _age_min(rec["firstSeenAt"])
            out[rec["direction"]].append(item)

        db.ideas_upsert(list(ideas.values()))

    def _rank(x):
        if x["fresh"]:
            return (0, -(x.get("conviction") or 0))
        return (1, -abs(x.get("movePct") or 0))

    longs = sorted(out["LONG"], key=_rank)[:MAX_PER_SIDE]
    shorts = sorted(out["SHORT"], key=_rank)[:MAX_PER_SIDE]
    return longs, shorts


def history(limit=60):
    """Per-day summary (newest first) for the Ideas history table."""
    db.init()
    _migrate_json_once()
    days = db.ideas_days(limit=limit)
    try:
        rmap = db.regime_by_day()
    except Exception:
        rmap = {}
    for r in days:
        for kk in ("avgBest", "avgWorst", "avgMove"):
            if r.get(kk) is not None:
                r[kk] = round(r[kk], 2)
        resolved = (r.get("targets") or 0) + (r.get("stops") or 0)
        r["resolved"] = resolved
        r["hitRate"] = round((r.get("targets") or 0) / resolved * 100, 1) if resolved else None
        info = rmap.get(r["day"]) or {}
        r["regime"] = info.get("label")
        r["niftyPct"] = info.get("niftyPct")
    return {"days": days, "stats": db.ideas_stats(), "generatedAt": _now()}


def day_ideas(day):
    """Every idea journaled on `day`, chronological, with its outcome + move."""
    db.init()
    _migrate_json_once()
    day = (day or "")[:10]
    rows = db.ideas_for_day(day)
    return {"date": day, "count": len(rows), "ideas": rows}


_RATING_RANK = {"High": 3, "Medium": 2, "Low": 1}


def recent(window_min=60, limit=30, min_rating=None):
    """
    Today's ideas, NEWEST first — a rolling "just flagged" feed. Optionally
    restrict to the last `window_min` minutes (0/None = whole day) and to a
    minimum conviction rating ("Medium"/"High"). Each row carries the frozen
    entry/plan + its live-ish move + outcome (as last persisted by enrich).
    """
    db.init()
    _migrate_json_once()
    today = _today()
    rows = db.ideas_for_day(today)
    rows.sort(key=lambda r: r.get("firstSeenAt") or "", reverse=True)

    if window_min:
        cutoff = (datetime.now(IST) - timedelta(minutes=int(window_min))
                  ).strftime("%Y-%m-%d %H:%M:%S")
        rows = [r for r in rows if (r.get("firstSeenAt") or "") >= cutoff]
    if min_rating:
        floor = _RATING_RANK.get(min_rating, 0)
        rows = [r for r in rows if _RATING_RANK.get(r.get("rating"), 0) >= floor]

    out = []
    for r in rows[:int(limit)]:
        r = dict(r)
        r["ageMin"] = _age_min(r.get("firstSeenAt"))
        out.append(r)
    return {"date": today, "windowMin": window_min, "minRating": min_rating,
            "count": len(out), "ideas": out}
