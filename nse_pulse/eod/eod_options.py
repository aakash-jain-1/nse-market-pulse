"""
eod_options.py — resilient End-of-Day option chain from the FO bhavcopy.

WHY THIS EXISTS
---------------
The live option chain (`nse_quote.get_option_chain`) rides NSE's anti-bot NextApi:
it 403s intermittently and reads empty/stale outside market hours. But NSE also
publishes every option contract's EOD OI / close / volume in the FO UDiFF bhavcopy
(a plain static-archive ZIP, no anti-bot gate). This module assembles that into an
option chain with the SAME analytics — PCR, max-pain, ATM, OI walls — so the
option view keeps working off-hours / weekends and when the live feed is blocked.

DESIGN
------
`bhavcopy.parse_fo_options()` is the PURE parser (option rows → per-expiry chain);
this module is the ANALYTICS layer. `chain()` / `summary()` return the **same
shape** as `nse_quote.get_option_chain` / `get_option_summary` (plus `eod: True`
and the bhavcopy `date`), so the existing frontend renderer draws them unchanged —
just with the fields the bhavcopy lacks (IV / bid-ask / Greeks) shown as blank.

Max-pain is delegated to `nse_quote._max_pain` (identical rows shape) to keep one
implementation. The raw FO text is cached module-side (30-min TTL, lock-guarded);
per-(symbol, expiry) chains are memoized briefly (EOD data changes once a day).
"""

import logging
import threading
import time
from datetime import datetime

log = logging.getLogger("eod_options")

_TEXT_TTL = 1800        # 30 min — the FO bhavcopy changes at most once a day
_CHAIN_TTL = 900        # 15 min — per-symbol assembled chain
_CHAIN_MAX = 128        # cap the memo so a long session can't grow unbounded

_MAP_TTL = 900         # 15 min — the market-wide OI map (all underlyings, one parse)

_text_cache = {"ts": 0.0, "text": None, "date": None}
_text_lock = threading.Lock()
_chain_cache = {}       # (symbol, expiry_arg) -> (ts, chain)
_map_cache = {"ts": 0.0, "date": None, "map": None}
_map_lock = threading.Lock()


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _fmt_expiry(iso):
    """'2026-07-28' → '28-Jul-2026' (match the live chain's display format)."""
    if not iso:
        return iso
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        return iso


def _atm(rows, underlying):
    """Strike nearest the underlying spot (the at-the-money row)."""
    ks = [r["strike"] for r in rows if r.get("strike") is not None]
    if not ks or not underlying:
        return None
    return min(ks, key=lambda k: abs(k - underlying))


def _walls(rows, leg, n=3):
    """Top-N strikes by OI on one leg — PUT walls = support, CALL walls = resistance."""
    vals = [{"strike": r["strike"], "oi": (r.get(leg) or {}).get("oi") or 0}
            for r in rows if r.get("strike") is not None and (r.get(leg) or {}).get("oi")]
    vals.sort(key=lambda x: -x["oi"])
    return vals[:n]


def _norm_leg(leg):
    """Fill the live-only fields the bhavcopy lacks so the leg matches nse_quote._leg."""
    if not leg:
        return None
    for k in ("iv", "bid", "ask", "pChgOi"):
        leg.setdefault(k, None)
    return leg


def _assemble(parsed, date, symbol, expiry=None):
    """Build a live-shaped chain dict from parse_fo_options() output (pure)."""
    from nse_pulse.core import nse_quote
    by = parsed.get("byExpiry") or {}
    iso_list = parsed.get("expiries") or []
    if not by:
        return {"symbol": symbol, "expiries": [], "rows": [], "eod": True,
                "date": date, "error": "no EOD options for symbol"}

    chosen = None
    if expiry:
        want = expiry.strip()
        for e in iso_list:
            if e == want or _fmt_expiry(e) == want:
                chosen = e
                break
    chosen = chosen or iso_list[0]

    slot = by[chosen]
    underlying = slot.get("underlying")
    rows, ce_tot, pe_tot = [], 0.0, 0.0
    for strike in sorted(slot["rows"]):
        legs = slot["rows"][strike]
        ce, pe = _norm_leg(legs.get("ce")), _norm_leg(legs.get("pe"))
        if ce and ce.get("oi"):
            ce_tot += ce["oi"]
        if pe and pe.get("oi"):
            pe_tot += pe["oi"]
        rows.append({"strike": strike, "ce": ce, "pe": pe})

    return {
        "symbol": symbol,
        "expiry": _fmt_expiry(chosen),
        "expiries": [_fmt_expiry(e) for e in iso_list],
        "underlying": underlying,
        "timestamp": date,
        "rows": rows,
        "ceTotOI": ce_tot,
        "peTotOI": pe_tot,
        "pcr": round(pe_tot / ce_tot, 2) if ce_tot else None,
        "maxPain": nse_quote._max_pain(rows),
        "atmStrike": _atm(rows, underlying),
        "support": _walls(rows, "pe"),
        "resistance": _walls(rows, "ce"),
        "lotSize": None,          # not in the bhavcopy; UI shows "—"
        "eod": True,
        "date": date,
    }


# ---------------------------------------------------------------------------
# impure — fetch (cached) + assemble
# ---------------------------------------------------------------------------
def _fo_text(force=False):
    """Cached decoded FO bhavcopy CSV text (30-min TTL, lock-guarded so cold
    callers don't each re-download). Returns (date_str, text)."""
    with _text_lock:
        if (not force and _text_cache["text"]
                and (time.time() - _text_cache["ts"]) < _TEXT_TTL):
            return _text_cache["date"], _text_cache["text"]
        from nse_pulse.eod import bhavcopy
        date, text = bhavcopy.fetch_fo_text()
        _text_cache.update(ts=time.time(), date=date, text=text)
        return date, text


def chain(symbol, expiry=None):
    """EOD option chain for `symbol` (+ optional expiry, ISO or '28-Jul-2026'),
    same shape as nse_quote.get_option_chain plus {eod: True, date}."""
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {"symbol": "", "expiries": [], "rows": [], "eod": True,
                "error": "no symbol"}

    ck = (symbol, expiry or "")
    hit = _chain_cache.get(ck)
    if hit and (time.time() - hit[0]) < _CHAIN_TTL:
        return hit[1]

    date, text = _fo_text()
    if not text:
        return {"symbol": symbol, "expiries": [], "rows": [], "eod": True,
                "date": date, "error": "EOD F&O bhavcopy unavailable"}

    from nse_pulse.eod import bhavcopy
    parsed = bhavcopy.parse_fo_options(text, underlying=symbol)
    out = _assemble(parsed, date, symbol, expiry)

    if len(_chain_cache) >= _CHAIN_MAX:
        _chain_cache.clear()
    _chain_cache[ck] = (time.time(), out)
    return out


def _analytics(slot):
    """max-pain / PCR / ATM / OI walls for one {underlying, rows} expiry slot (pure).
    Same numbers `_assemble` computes, minus the full per-strike rows."""
    from nse_pulse.core import nse_quote
    rows = [{"strike": k, "ce": _norm_leg(v.get("ce")), "pe": _norm_leg(v.get("pe"))}
            for k, v in sorted(slot["rows"].items())]
    ce_tot = sum((r["ce"] or {}).get("oi") or 0 for r in rows)
    pe_tot = sum((r["pe"] or {}).get("oi") or 0 for r in rows)
    u = slot.get("underlying")
    return {
        "underlying": u,
        "pcr": round(pe_tot / ce_tot, 2) if ce_tot else None,
        "maxPain": nse_quote._max_pain(rows),
        "atmStrike": _atm(rows, u),
        "resistance": _walls(rows, "ce"),      # top CALL-OI strikes = resistance
        "support": _walls(rows, "pe"),         # top PUT-OI strikes = support
        "ceTotOI": ce_tot,
        "peTotOI": pe_tot,
    }


def oi_map(force=False):
    """{SYMBOL: nearest-expiry {expiry, underlying, pcr, maxPain, atmStrike,
    resistance, support, …}} for ALL F&O underlyings from ONE parse of the FO
    bhavcopy — so the conviction board can fuse max-pain / PCR / OI walls into every
    pick without a parse per name. Cached (15-min TTL). Returns (date, map)."""
    with _map_lock:
        if (not force and _map_cache["map"] is not None
                and (time.time() - _map_cache["ts"]) < _MAP_TTL):
            return _map_cache["date"], _map_cache["map"]
    date, text = _fo_text(force=force)
    out = {}
    if text:
        from nse_pulse.eod import bhavcopy
        for sym, d in bhavcopy.parse_fo_options_all(text).items():
            exps = d.get("expiries") or []
            if not exps:
                continue
            out[sym] = {"expiry": _fmt_expiry(exps[0]), **_analytics(d["byExpiry"][exps[0]])}
    with _map_lock:
        _map_cache.update(ts=time.time(), date=date, map=out)
    return date, out


def summary(symbol):
    """PCR / max-pain / OI per expiry for `symbol` (same shape as
    nse_quote.get_option_summary plus {eod: True, date})."""
    from nse_pulse.core import nse_quote
    symbol = (symbol or "").upper().strip()
    date, text = _fo_text()
    if not symbol or not text:
        return {"symbol": symbol, "underlying": None, "expiries": [], "eod": True,
                "date": date}
    from nse_pulse.eod import bhavcopy
    parsed = bhavcopy.parse_fo_options(text, underlying=symbol)
    by = parsed.get("byExpiry") or {}
    out, underlying = [], None
    for iso in parsed.get("expiries") or []:
        slot = by[iso]
        rows = [{"strike": k, "ce": v.get("ce"), "pe": v.get("pe")}
                for k, v in slot["rows"].items()]
        ce_tot = sum((r["ce"] or {}).get("oi") or 0 for r in rows)
        pe_tot = sum((r["pe"] or {}).get("oi") or 0 for r in rows)
        underlying = underlying or slot.get("underlying")
        out.append({
            "expiry": _fmt_expiry(iso),
            "pcr": round(pe_tot / ce_tot, 2) if ce_tot else None,
            "maxPain": nse_quote._max_pain(rows),
            "ceTotOI": ce_tot,
            "peTotOI": pe_tot,
        })
    return {"symbol": symbol, "underlying": underlying, "expiries": out,
            "eod": True, "date": date}


def status():
    """Freshness of the cached FO text (no download unless already warm)."""
    c = _text_cache
    return {
        "date": c.get("date"),
        "cached": bool(c.get("text")),
        "ageSec": round(time.time() - c["ts"], 1) if c.get("ts") else None,
        "ttlSec": _TEXT_TTL,
        "source": "nsearchives FO UDiFF bhavcopy (EOD)",
    }
