"""
conviction_calibration.py — does the conviction board's confirmation-stacking pay?

WHY THIS EXISTS
---------------
The EOD conviction board (`eod_conviction.py`) ranks names by how many INDEPENDENT
signals agree — breakout, delivery%, volume, trend, F&O OI, bulk/block deals, a
leading/lagging sector, and the option chain. The whole thesis is that *agreement
across independent evidence* raises the odds. That's a claim we can TEST: every
saved board is written into the `ideas` table, and those ideas get honest,
candle-accurate `TARGET`/`STOP` outcomes resolved over the following sessions.

This module reads back those resolved ideas and measures whether the thesis holds:
  • Do 4-pillar picks actually beat 2-pillar picks (win rate + realized move)?
  • Which individual pillar adds (or subtracts) edge — the "lift" of each?
  • Do the option-chain ⚠️ warnings (the soft-veto) really flag worse trades?

DESIGN
------
All the maths (`_bucket_stats`, `_pillars_in`, `_confirmations_of`, `_lift`) is PURE
and unit-tested on hand-built idea dicts. `report()` is the only impure part: one
`db.ideas_all()` read. It considers ONLY EOD-conviction ideas (tagged by the
`🏆 EOD conviction (N signals)` reason the board writes), and scores realized
outcomes: a `TARGET` is a win, a `STOP` a loss, `None` still open. `outcomePct` /
`maxMovePct` / `minMovePct` are already direction-adjusted (positive = in the
trade's favour), so expectancy is just their mean.

Educational / research — NOT investment advice.
"""

import re

# The reason the board stamps as reasons[0]; also carries the pillar count.
_TAG = "🏆 EOD conviction"
_SIGNALS_RE = re.compile(r"\((\d+)\s+signals?\)")

# pillar key → predicate over a lower-cased reason label. One reason maps to at most
# one pillar; warnings (the "⚠️ …" lines) are excluded before matching.
_PILLARS = [
    ("breakout", lambda r: any(t in r for t in ("breakout", "breakdown", "coiling", "hovering"))),
    ("trend",    lambda r: "uptrend" in r or "downtrend" in r),
    ("delivery", lambda r: "delivery" in r),
    ("volume",   lambda r: "average volume" in r),
    ("oi",       lambda r: r.startswith("f&o") or any(t in r for t in ("buildup", "covering", "unwinding"))),
    ("deal",     lambda r: "bulk/block" in r),
    ("sector",   lambda r: "sector" in r),
    ("option",   lambda r: "option chain" in r),
]
PILLAR_KEYS = [k for k, _ in _PILLARS]

_CONF_BUCKETS = [("2", 2, 2), ("3", 3, 3), ("4", 4, 4), ("5+", 5, 99)]


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def is_conviction(idea):
    """True if this idea came from the EOD conviction board (vs a live intraday one)."""
    rs = idea.get("reasons") or []
    return bool(rs) and isinstance(rs[0], str) and rs[0].startswith(_TAG)


def _confirmations_of(idea):
    """Pillar count the board recorded, from 'EOD conviction (N signals)'.
    Falls back to counting non-warning reasons when the header is missing."""
    rs = idea.get("reasons") or []
    if rs:
        m = _SIGNALS_RE.search(rs[0])
        if m:
            return int(m.group(1))
    return sum(1 for r in rs[1:] if not str(r).startswith("⚠️"))


def _pillars_in(idea):
    """Set of pillar keys that fired for an idea (parsed from its reason labels)."""
    out = set()
    for r in (idea.get("reasons") or [])[1:]:
        r = str(r)
        if r.startswith("⚠️"):
            continue
        low = r.lower()
        for key, pred in _PILLARS:
            if pred(low):
                out.add(key)
                break
    return out


def has_warning(idea):
    """True if the option-chain fuse flagged a conflict (a '⚠️ …' reason)."""
    return any(str(r).startswith("⚠️") for r in (idea.get("reasons") or []))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _bucket_stats(ideas):
    """Realized-outcome stats for a list of ideas (pure). Win rate is over RESOLVED
    ideas (TARGET/STOP); MFE/MAE averages span all ideas so a bucket is informative
    even before every idea resolves."""
    n = len(ideas)
    wins = sum(1 for i in ideas if i.get("outcome") == "TARGET")
    losses = sum(1 for i in ideas if i.get("outcome") == "STOP")
    resolved = wins + losses
    outs = [i.get("outcomePct") for i in ideas if i.get("outcome") in ("TARGET", "STOP")]
    return {
        "n": n,
        "resolved": resolved,
        "open": n - resolved,
        "wins": wins,
        "losses": losses,
        "winRate": round(wins / resolved * 100, 1) if resolved else None,
        "avgOutcomePct": _mean(outs),                     # realized expectancy (resolved)
        "avgBest": _mean([i.get("maxMovePct") for i in ideas]),   # mean MFE (all)
        "avgWorst": _mean([i.get("minMovePct") for i in ideas]),  # mean MAE (all)
        "avgConviction": _mean([i.get("conviction") for i in ideas]),
    }


def _lift(withs, withouts):
    """Win-rate & expectancy lift of a pillar: with-bucket minus without-bucket."""
    a, b = _bucket_stats(withs), _bucket_stats(withouts)
    def d(x, y):
        return round(x - y, 1) if (x is not None and y is not None) else None
    return {
        "with": a, "without": b,
        "winRateLift": d(a["winRate"], b["winRate"]),
        "expLift": d(a["avgOutcomePct"], b["avgOutcomePct"]),
    }


def _verdict(by_conf, totals):
    """One honest sentence: is win rate monotonically rising with pillar count?"""
    pts = [(int(b["bucket"].rstrip("+")), b["winRate"])
           for b in by_conf if b["winRate"] is not None and b["resolved"] >= 3]
    if totals["resolved"] < 8 or len(pts) < 2:
        return ("Not enough resolved history yet to judge — keep saving boards and let "
                "the outcomes resolve over the next few sessions.")
    pts.sort()
    rising = all(pts[i][1] <= pts[i + 1][1] + 1e-9 for i in range(len(pts) - 1))
    lo, hi = pts[0], pts[-1]
    if rising and hi[1] - lo[1] >= 5:
        return (f"Confirmation stacking is paying off: win rate climbs from "
                f"{lo[1]:.0f}% ({lo[0]} signals) to {hi[1]:.0f}% ({hi[0]} signals).")
    if hi[1] - lo[1] <= -5:
        return (f"Stacking is NOT helping on this sample: {hi[0]}-signal picks "
                f"({hi[1]:.0f}%) trail {lo[0]}-signal ones ({lo[1]:.0f}%).")
    return ("Mixed so far — more pillars aren't clearly beating fewer on this sample; "
            "gather more resolved history before trusting it.")


# ---------------------------------------------------------------------------
# report (impure: reads db.ideas)
# ---------------------------------------------------------------------------
def report(days=None, limit=5000):
    """Calibration of the saved EOD-conviction ideas. `days` optionally restricts to
    the last N calendar days. Returns {totals, byConfirmations, byRating, byDirection,
    byPillar, warningImpact, verdict, note}."""
    import db
    since = None
    if days:
        try:
            from datetime import date, timedelta
            since = (date.today() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            since = None

    ideas = [i for i in db.ideas_all(limit=limit, since=since) if is_conviction(i)]
    totals = _bucket_stats(ideas)
    if ideas:
        days_seen = sorted({i.get("day") for i in ideas if i.get("day")})
        totals.update(days=len(days_seen),
                      first=(days_seen[0] if days_seen else None),
                      last=(days_seen[-1] if days_seen else None))

    by_conf = []
    for label, lo, hi in _CONF_BUCKETS:
        b = _bucket_stats([i for i in ideas if lo <= _confirmations_of(i) <= hi])
        b["bucket"] = label
        by_conf.append(b)

    by_rating = []
    for r in ("High", "Medium", "Low"):
        s = _bucket_stats([i for i in ideas if i.get("rating") == r])
        s["rating"] = r
        by_rating.append(s)

    by_dir = []
    for d in ("LONG", "SHORT"):
        s = _bucket_stats([i for i in ideas if i.get("direction") == d])
        s["direction"] = d
        by_dir.append(s)

    by_pillar = []
    for key in PILLAR_KEYS:
        withs = [i for i in ideas if key in _pillars_in(i)]
        withouts = [i for i in ideas if key not in _pillars_in(i)]
        row = _lift(withs, withouts)
        row["pillar"] = key
        by_pillar.append(row)
    by_pillar.sort(key=lambda x: (x["winRateLift"] is None, -(x["winRateLift"] or 0)))

    warn = {"withWarn": _bucket_stats([i for i in ideas if has_warning(i)]),
            "noWarn": _bucket_stats([i for i in ideas if not has_warning(i)])}

    return {
        "totals": totals,
        "byConfirmations": by_conf,
        "byRating": by_rating,
        "byDirection": by_dir,
        "byPillar": by_pillar,
        "warningImpact": warn,
        "verdict": _verdict(by_conf, totals),
        "filters": {"days": int(days) if days else None, "limit": limit},
        "note": None if ideas else (
            "No saved conviction ideas yet — open the 🏆 Conviction tab and click "
            "💾 Save to Ideas on a board (or let the auto EOD refresh run). Outcomes "
            "resolve over the following sessions, then this report fills in."),
    }
