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
from datetime import datetime, timezone, timedelta

import db

IST = timezone(timedelta(hours=5, minutes=30))
MAX_PER_SIDE = 20          # cap how many tracked ideas we surface per direction
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
    """Sticky first-touch verdict: once target/stop is reached, freeze it."""
    if rec.get("outcome") or not rec.get("entry") or mv is None:
        return
    tp, sp = rec.get("targetPct"), rec.get("stopPct")
    if tp and mv >= tp:
        rec["outcome"], rec["outcomeAt"], rec["outcomePct"] = "TARGET", now, mv
    elif sp and mv <= -sp:
        rec["outcome"], rec["outcomeAt"], rec["outcomePct"] = "STOP", now, mv


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
