"""Split / bonus / demerger adjustment for daily bars.

NSE serves **raw traded prices** on every path we use — the UDiFF bhavcopy and
`generateSecurityWiseHistoricalData` alike — so a corporate-action ex-date is stored as
a genuine crash. Measured in our own `eod_bars` (2026-08-24): 36 close-to-close moves
>50%, the largest being real splits in the liquid F&O names the strategies actually
trade — ANGELONE −90.1% (10.1x), KOTAKBANK −80.3% (5.07x), CAMS/NUVAMA −80.4% (5.10x),
MCX −79.8% (4.96x), VEDL −64.9% (2.85x).

Left alone that does two kinds of damage. The ex-date itself hands `meanrev` / `gap` /
`vol_breakout` / `rel_strength` an enormous phantom signal (`backtest_daily._features`
takes `ret1` straight off the unadjusted `prevClose`), and — the longer-lived half —
every trailing window then straddles both price scales for 20+ sessions, so `hi20` /
`lo20` / `hh` / `ll` say the name sits ~90% below its 20-day high. That manufactures
breakdown tags while suppressing real breakouts, all the way through `eod_scanner`,
`sector_scan` RS and whatever `eod_conviction` fuses on top.

Two things that do NOT catch it, both verified rather than assumed:

* The **zero-price guard is blind** to it. `min(c, hi, lo) <= 0` passes, because every
  price is positive and internally consistent *within* the bar. Only the cross-day
  ratio is wrong.
* **`prevClose` is not a detector.** The tempting idea is that NSE re-bases previous
  close on an ex-date, so a `prevClose(T)` vs `close(T-1)` mismatch would flag it for
  free out of a column we already store. False on the live historical path: on
  ANGELONE's ex-date `prevClose` is **2,489.90** against a `close` of **246.50** — i.e.
  reported on the stale scale. Detection here therefore works off closes only.

Design
------
**Non-destructive.** `db.eod_bars` keeps NSE's truth; adjustment happens on read, so
`db_inspect` and the raw tables still show what NSE actually published, and fixing the
detector never requires re-ingesting anything.

**Back-adjusted, not forward-adjusted.** History is scaled down onto TODAY's scale
rather than scaling recent prices up. The newest bar must keep its real traded price:
entries, exits, the liquidity floors (`minPrice` / `minValueCr`) and every rupee figure
downstream are only meaningful in real money.

**Adjacent trading days only.** A ratio across a hole in our history is ambiguous — a
stock genuinely moves over 25 idle days — so a gap is skipped rather than guessed at.
Consequence worth knowing: an ex-date falling inside a backfill hole stays unadjusted
(`prevClose` on the far side is already re-based, so `ret1` is fine there, but the
trailing windows still straddle). Backfilling contiguously is what fixes those.
"""

import logging
import math
from datetime import date

log = logging.getLogger(__name__)

# A corporate action is inferred from the size of the move, because NSE price bands make
# a large one physically impossible through trading: most names carry a 2/5/10/20% daily
# circuit, and even F&O names (no fixed band) are held by a flexed dynamic band. So a
# >30% close-to-close move is already the interesting territory, and beyond ~45% nothing
# but a rescaling can produce it.
MIN_MOVE_PCT = 30.0    # candidate gate; normal circuit moves live below this
HARD_MOVE_PCT = 45.0   # a FALL this big is structural on size alone, whatever the ratio

# Deliberately asymmetric, because corporate actions are. Splits, bonuses and demergers
# all push price DOWN, and they're common — every one of the 13 events in our own data is
# a fall. The only thing that pushes price UP is a share consolidation (reverse split),
# which is rare here and never subtle: a 2:1 consolidation already doubles the price. So
# an upward rescaling must clear this much AND land on a clean ratio, which costs nothing
# real and stops a violent one-day RALLY from being mistaken for a corporate action.
REVERSE_MIN_MOVE_PCT = 90.0

# Ratios Indian splits and bonuses actually produce: face-value splits (10->1, 10->2,
# 10->5, 5->1, 2->1) and bonus issues (1:1 -> 2x, 1:2 -> 1.5x, 3:2 -> 2.5x, 2:1 -> 3x,
# 3:1 -> 4x, 4:1 -> 5x, 5:1 -> 6x). Anything under 1.5 is unreachable behind
# MIN_MOVE_PCT anyway (a 1:3 bonus is only -25%), so it isn't listed.
CLEAN_FACTORS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0, 25.0,
                 50.0, 100.0)
FACTOR_TOL = 0.03      # snap window, relative

# A corporate action is PERMANENT; a bad bhavcopy print is not. The check is deliberately
# TWO-sided — the old scale must hold before the ex-date and the new one after — because a
# single garbage close otherwise gets adjusted twice over: once as a crash on the bad day,
# and again as a "reverse split" on the day the price returns to normal, which would
# rescale all of history behind it by a nonsense factor.
PERSIST_BARS = 3

# Bars are dicts from db.eod_bars / nse.get_stock_history. Prices scale by the factor,
# share counts inversely — which leaves `value` (price x quantity) invariant, exactly as
# a real split does, so turnover filters keep working untouched. `trades` and `delivPct`
# are counts/ratios and are scale-free.
_PRICE_KEYS = ("open", "high", "low", "close", "prevClose", "vwap", "last")
_QTY_KEYS = ("volume", "delivQty")


def _snap(ratio):
    """The clean split/bonus factor this ratio is really reporting, or None.

    Snapping matters for accuracy, not tidiness: on a 5:1 split where the stock also
    fell 2% that day the observed ratio is 5.10, so adjusting by 5.0 correctly leaves
    the genuine -2% in the series, whereas adjusting by 5.10 would erase it.
    """
    for f in CLEAN_FACTORS:
        if abs(ratio / f - 1) <= FACTOR_TOL:
            return f
    return None


def _closes(bars):
    return [b.get("close") for b in bars if (b.get("close") or 0) > 0]


def _separated(bars, i, factor, dropped):
    """Do the two price scales sit cleanly on opposite sides of the ex-date?

    Splits the neighbourhood at the **geometric mean** of the old and new scales — the
    scale-symmetric midpoint, so the test behaves identically for a 1.5x bonus and a 10x
    split — and requires every close before the ex-date above it and every close from the
    ex-date on below it (mirrored for a reverse split).

    This is what separates a real corporate action from a bad print. A one-day garbage
    close fails going in (the old scale reappears immediately after) *and* fails coming
    out (the bad bar pollutes the "before" cluster on the recovery day), so neither half
    of the round trip gets adjusted.
    """
    ref = bars[i - 1].get("close")
    if not ref or factor <= 1:
        return False
    before = _closes(bars[max(0, i - 1 - PERSIST_BARS):i])
    after = _closes(bars[i:i + 1 + PERSIST_BARS])
    if not before or not after:
        return False
    mid = math.sqrt(factor)
    if dropped:
        thr = ref / mid
        return max(after) < thr < min(before)
    thr = ref * mid
    return min(after) > thr > max(before)


def _adjacent(prev, cur, max_gap_days=5):
    """Are these consecutive trading sessions? Spans a weekend plus a holiday."""
    a, b = prev.get("d"), cur.get("d")
    if not a or not b:
        return False
    try:
        # 'd' is the normalised YYYY-MM-DD key (the `date` column keeps the source
        # format, which differs per ingest path), so this parse is safe here.
        da = date(int(a[0:4]), int(a[5:7]), int(a[8:10]))
        db_ = date(int(b[0:4]), int(b[5:7]), int(b[8:10]))
    except (ValueError, TypeError, IndexError):
        return False
    return 0 < (db_ - da).days <= max_gap_days


def detect(bars):
    """Corporate actions in an ascending list of daily bars (pure).

    Returns `[{"i", "d", "factor", "ratio", "kind", "movePct", "clean"}, ...]` where `i`
    is the index of the ex-date bar — i.e. bars `[0, i)` are on the old scale — and
    `kind` is `"split"` (price fell) or `"reverse"` (price rose).
    """
    events = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        old, new = prev.get("close"), cur.get("close")
        if not old or not new or old <= 0 or new <= 0:
            continue
        if not _adjacent(prev, cur):
            continue
        move = (new / old - 1) * 100.0
        dropped = new < old
        if abs(move) < (MIN_MOVE_PCT if dropped else REVERSE_MIN_MOVE_PCT):
            continue
        ratio = (old / new) if dropped else (new / old)
        clean = _snap(ratio)
        if clean is None and not (dropped and abs(move) >= HARD_MOVE_PCT):
            # A recognisable split/bonus ratio is required, EXCEPT for a fall past
            # HARD_MOVE_PCT, where the raw ratio is used instead. That exception is
            # what lets a DEMERGER through: value transfers to the spun-off entity in
            # no round proportion, so there is no clean factor to snap to (VEDL, live:
            # 2.85x). Everywhere else an odd ratio is more likely a violent-but-real
            # move than a rescaling, so it's left alone.
            continue
        if not _separated(bars, i, ratio if clean is None else clean, dropped):
            continue
        events.append({
            "i": i, "d": cur.get("d"), "factor": clean or ratio, "ratio": ratio,
            "kind": "split" if dropped else "reverse", "movePct": move,
            "clean": clean is not None,
        })
    return events


def adjust(bars, events=None):
    """Back-adjusted copy of `bars` — history rescaled onto the newest bar's scale.

    Returns the input list **unchanged and uncopied** when there's nothing to do, which
    is the overwhelmingly common case (a market-wide read is ~3,500 symbols and only a
    handful ever have an event).
    """
    if not bars:
        return bars
    events = detect(bars) if events is None else events
    if not events:
        return bars

    # Walk backwards accumulating the multiplier: bars before an ex-date are `factor`
    # times too high (or too low, for a reverse split).
    at = {e["i"]: e for e in events}
    mult = [1.0] * len(bars)
    cum = 1.0
    for i in range(len(bars) - 1, -1, -1):
        mult[i] = cum
        e = at.get(i)
        if e:
            cum = cum / e["factor"] if e["kind"] == "split" else cum * e["factor"]

    out = []
    for i, b in enumerate(bars):
        m = mult[i]
        fix_prev = i in at
        if m == 1.0 and not fix_prev:
            out.append(b)
            continue
        nb = dict(b)
        if m != 1.0:
            # Rounded to keep the scaled values readable (4dp is well past the paise a
            # rupee price is quoted in, and a share count is an integer). Everything
            # downstream consumes ratios, so this is cosmetic, not lossy.
            for k in _PRICE_KEYS:
                v = nb.get(k)
                if isinstance(v, (int, float)):
                    nb[k] = round(v * m, 4)
            for k in _QTY_KEYS:
                v = nb.get(k)
                if isinstance(v, (int, float)) and v:
                    nb[k] = round(v / m)
        if fix_prev and i:
            # The ex-date's own `prevClose` is NSE's stale-scale number — the direct
            # cause of the phantom `ret1`. Re-point it at the adjusted previous close so
            # the day's return becomes the real one (~0%, not -90%).
            nb["prevClose"] = out[i - 1].get("close") if out else nb.get("prevClose")
        out.append(nb)
    return out


def adjust_grouped(grouped, stats=None):
    """`{SYMBOL: [bars]}` -> the same shape, back-adjusted per symbol.

    Pass a dict as `stats` to collect `{symbols, adjusted, events, detail}` for logging
    or an API payload; symbols with no corporate action are returned untouched.
    """
    out, n_ev, touched, detail = {}, 0, 0, []
    for sym, bars in (grouped or {}).items():
        try:
            evs = detect(bars)
        except Exception:            # one malformed symbol must not sink a market scan
            log.exception("corporate-action detect failed for %s", sym)
            out[sym] = bars
            continue
        if evs:
            touched += 1
            n_ev += len(evs)
            detail.append({"symbol": sym, "events": [
                {"d": e["d"], "factor": round(e["factor"], 4), "kind": e["kind"],
                 "movePct": round(e["movePct"], 1), "clean": e["clean"]} for e in evs]})
            out[sym] = adjust(bars, evs)
        else:
            out[sym] = bars
    if stats is not None:
        stats.update({"symbols": len(out), "adjusted": touched, "events": n_ev,
                      "detail": sorted(detail, key=lambda x: x["symbol"])})
    return out


def bars_all(since=None, stats=None):
    """`db.eod_bars_all(since)` with corporate actions adjusted — use this, not the raw
    read, anywhere bars feed features or returns."""
    from nse_pulse.core import db
    return adjust_grouped(db.eod_bars_all(since=since), stats=stats)
