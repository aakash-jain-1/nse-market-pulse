"""
Intrabar trade resolution from real minute OHLCV
================================================
The sims used to decide target/stop against a single LTP point per cycle (60s
live, 5-min in the backtest). That misses wicks between samples and detects
exits late (at the next sample, not at the level), which flatters or distorts
win-rate and expectancy.

This module resolves a trade against actual minute candles: a LONG stop is hit
when a bar's LOW <= stop, a target when a bar's HIGH >= target (mirrored for
SHORT). When one bar straddles both levels we can't know the intrabar order, so
we assume the STOP filled first (conservative). Maximum favorable/adverse
excursion (MFE/MAE) is measured from true intrabar extremes.

Pure functions, no I/O — the caller supplies the candles (from
`nse_quote.get_ohlc`). `resolve()` returns None when there are no usable bars so
the caller can fall back to its LTP path (e.g. renamed tickers / indices with no
charting token).
"""

from datetime import datetime


def candle_dt(ms):
    """NSE bakes IST wall-clock into the epoch as if it were UTC; reading the
    UTC components back gives the correct IST time regardless of local tz."""
    return datetime.utcfromtimestamp(ms / 1000.0)


def _parse_ts(s):
    """Parse a naive IST timestamp ('YYYY-MM-DD HH:MM:SS' or ISO 'T' form)."""
    if isinstance(s, (int, float)):
        return candle_dt(s * 1000.0)
    txt = str(s).replace(" ", "T")
    # drop any timezone suffix; our stored timestamps are naive IST
    for sep in ("+", "Z"):
        if sep in txt[10:]:
            txt = txt[:10] + txt[10:].split(sep)[0]
    return datetime.fromisoformat(txt)


def _move_pct(direction, entry, px):
    return (px - entry) / entry * 100 if direction == "LONG" else (entry - px) / entry * 100


def resolve(trade, bars, risk_per_trade, max_sessions=None, tie="stop"):
    """
    Resolve one trade against `bars` (ascending list of {t,o,h,l,c,v}), mutating
    it in place. Returns the resulting status string
    ("TARGET"/"STOP"/"EXPIRED"/"OPEN"), or None when there are no usable bars
    (caller should fall back to its own LTP resolution).

    `trade` needs: direction, entry, stop, target, qty, openedTs. The horizon is
    `max_sessions` distinct trading sessions from entry (defaults to the trade's
    own maxSessions, else 3); if fully elapsed with no hit the trade is
    time-expired at the last available close, otherwise it stays OPEN (bars ran
    out mid-horizon, e.g. market still open today).
    """
    entry = trade["entry"]
    direction = trade["direction"]
    stop, target = trade.get("stop"), trade.get("target")
    opened = _parse_ts(trade["openedTs"])
    if max_sessions is None:
        max_sessions = trade.get("maxSessions", 3)

    rel = [b for b in bars
           if b.get("t") is not None and b.get("h") is not None
           and b.get("l") is not None and candle_dt(b["t"]) >= opened]
    if not rel:
        return None

    # Distinct sessions (dates) in view; the horizon is the first max_sessions.
    dates = []
    for b in rel:
        d = candle_dt(b["t"]).date()
        if d not in dates:
            dates.append(d)
    allowed = set(dates[:max_sessions])

    mfe, mae = 0.0, 0.0
    last_bar, last_close = None, entry
    for b in rel:
        if candle_dt(b["t"]).date() not in allowed:
            break
        last_bar, last_close = b, b["c"]
        hi, lo = b["h"], b["l"]
        mfe = max(mfe, _move_pct(direction, entry, hi if direction == "LONG" else lo))
        mae = min(mae, _move_pct(direction, entry, lo if direction == "LONG" else hi))

        if direction == "LONG":
            tgt_hit = target is not None and hi >= target
            stop_hit = stop is not None and lo <= stop
        else:
            tgt_hit = target is not None and lo <= target
            stop_hit = stop is not None and hi >= stop

        hit = exit_px = None
        if tgt_hit and stop_hit:
            hit, exit_px = ("STOP", stop) if tie == "stop" else ("TARGET", target)
        elif tgt_hit:
            hit, exit_px = "TARGET", target
        elif stop_hit:
            hit, exit_px = "STOP", stop
        if hit:
            return _apply(trade, hit, exit_px, b, opened, risk_per_trade, mfe, mae)

    # No target/stop within the horizon.
    if len(dates) >= max_sessions:
        return _apply(trade, "EXPIRED", last_close, last_bar, opened,
                      risk_per_trade, mfe, mae)
    return _apply(trade, "OPEN", last_close, last_bar, opened,
                  risk_per_trade, mfe, mae)


def _apply(trade, status, px, bar, opened, risk_per_trade, mfe, mae):
    direction, entry, qty = trade["direction"], trade["entry"], trade["qty"]
    trade["ltp"] = round(px, 2)
    trade["mfePct"] = round(mfe, 2)
    trade["maePct"] = round(mae, 2)
    trade["pnl"] = round(qty * (px - entry) * (1 if direction == "LONG" else -1), 2)
    trade["pnlPct"] = round(_move_pct(direction, entry, px), 2)
    trade["rMultiple"] = round(trade["pnl"] / risk_per_trade, 2) if risk_per_trade else 0.0
    trade["status"] = status
    if status == "OPEN":
        return status
    closed = candle_dt(bar["t"])
    trade["exitPrice"] = round(px, 2)
    trade["closedTs"] = closed.isoformat(timespec="seconds")
    trade["closedDay"] = closed.date().isoformat()
    trade["minsToExit"] = int((closed - opened).total_seconds() // 60)
    return status
