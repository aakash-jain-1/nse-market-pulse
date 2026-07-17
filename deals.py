"""
deals.py — NSE bulk & block deals ("smart-money" prints), market-wide.

WHY THIS EXISTS
---------------
Bulk & block deals are the trades big players (funds, HNIs, promoters) are legally
required to disclose — a genuine institutional-footprint signal. NSE publishes the
latest session's deals as small PLAIN CSVs on nsearchives (no anti-bot gate), so —
like the bhavcopy — we can fetch + parse them resiliently and OFF-HOURS:

    bulk  : /content/equities/bulk.csv
    block : /content/equities/block.csv

Each row: Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded,
Trade Price / Wght. Avg. Price[, Remarks]. When there are no deals for the session
the file contains a single "NO RECORDS" line (common for block deals).

DESIGN
------
- `parse_deals()` is PURE and unit-tested against hand-built CSV text (incl. the
  "NO RECORDS" sentinel).
- Downloads reuse `bhavcopy._download` (browser-like headers, one session rebuild
  on failure) and are cached module-side (30-min TTL, lock-guarded).
- `by_symbol()` powers a cheap cross-reference so the EOD scanner can flag a name a
  big player just bought/sold; `recent()` backs the deals API/UI.

Educational / research — NOT investment advice.
"""

import csv
import io
import logging
import threading
import time

log = logging.getLogger("deals")

ARCH = "https://nsearchives.nseindia.com"
_URLS = {
    "bulk": ARCH + "/content/equities/bulk.csv",
    "block": ARCH + "/content/equities/block.csv",
}
_TTL = 1800  # 30 min — deals are published once, end-of-day
_cache = {
    "bulk": {"ts": 0.0, "deals": [], "date": None},
    "block": {"ts": 0.0, "deals": [], "date": None},
}
_lock = threading.Lock()


def _kind(kind):
    return "block" if str(kind).lower() == "block" else "bulk"


def _num(x):
    """Coerce a deal cell to float, or None. Handles thousands commas / blanks."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if not s or s in ("-", "NA", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_deals(text):
    """Parse a bulk/block-deals CSV into a list of deal dicts (newest-first as the
    file ships them). Returns [] for the "NO RECORDS" sentinel or an empty file.

    deal = {date, symbol, name, client, side (BUY/SELL), qty, price, remarks}.
    """
    out = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v)
               for k, v in raw.items()}
        sym = (row.get("Symbol") or "").strip().upper()
        date = (row.get("Date") or "").strip()
        if not sym or sym == "NO RECORDS" or date.upper() == "NO RECORDS":
            continue
        out.append({
            "date": date,
            "symbol": sym,
            "name": (row.get("Security Name") or "").strip() or None,
            "client": (row.get("Client Name") or "").strip() or None,
            "side": (row.get("Buy/Sell") or "").strip().upper(),
            "qty": _num(row.get("Quantity Traded")),
            "price": _num(row.get("Trade Price / Wght. Avg. Price")),
            "remarks": (row.get("Remarks") or "").strip() or None,
        })
    return out


def latest(kind="bulk", force=False):
    """Cached latest bulk/block deals for `kind` (30-min TTL, lock-guarded). Returns
    the shared per-kind cache dict; treat it as READ-ONLY. An empty list is a valid
    cached result (e.g. no block deals that day), so validity is time-based."""
    import bhavcopy
    kind = _kind(kind)
    c = _cache[kind]
    with _lock:
        if not force and c["ts"] and (time.time() - c["ts"]) < _TTL:
            return c
        raw = bhavcopy._download(_URLS[kind])
        deals = parse_deals(raw.decode("utf-8", "replace")) if raw else []
        c.update(ts=time.time(), deals=deals, date=(deals[0]["date"] if deals else None))
        return c


def refresh(kind="bulk"):
    return latest(kind, force=True)


def recent(kind="bulk", limit=200):
    """Latest session's deals for `kind`: {kind, date, count, deals[:limit]}."""
    c = latest(kind)
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    return {"kind": _kind(kind), "date": c.get("date"),
            "count": len(c["deals"]), "deals": c["deals"][:limit]}


def by_symbol(kind="bulk"):
    """{SYMBOL: [deal, …]} for the latest session — a cheap cross-reference so the
    scanner can flag names with a big-player print. {} on any failure."""
    out = {}
    try:
        for d in latest(kind)["deals"]:
            out.setdefault(d["symbol"], []).append(d)
    except Exception:
        log.warning("deals by_symbol(%s) failed", kind, exc_info=True)
    return out


def status(refresh=False):
    """Freshness/coverage of the deals cache (no secrets)."""
    out = {}
    for k in ("bulk", "block"):
        c = latest(k, force=refresh) if refresh else _cache[k]
        out[k] = {"date": c.get("date"), "count": len(c.get("deals") or []),
                  "ageSec": round(time.time() - c["ts"], 1) if c.get("ts") else None,
                  "cached": bool(c.get("ts"))}
    out["ttlSec"] = _TTL
    out["source"] = "nsearchives bulk/block deals CSV"
    return out
