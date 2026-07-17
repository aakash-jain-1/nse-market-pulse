"""
bhavcopy.py — NSE End-of-Day (EOD) bhavcopy ingestion + resilient EOD source.

WHY THIS EXISTS
---------------
NSE's live JSON API (`nse_client` / `nse_quote`) is anti-bot and flaky: sessions
die, some endpoints 403, others return empty payloads, and everything reads
all-zeros outside market hours. It's also LIMITED — only the ~100-150 symbols in
the live "hot lists" get a price. That caps what we can price, paper-trade and
scan.

NSE ALSO publishes a plain daily archive — the "UDiFF" Common Bhavcopy — as
static ZIP/CSV files on nsearchives.nseindia.com. Those files have NO anti-bot
gate, so they are a reliable FALLBACK price/history source and let us broaden
coverage to the WHOLE market (~2400 cash equities + the full ~210-name F&O
universe) instead of only the hot lists. They also work fine off-hours/weekends.

Two files per trading day (both share one UDiFF schema; TradDt is YYYY-MM-DD):
    CM (cash):  /content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
    FO (deriv): /content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip

DESIGN
------
- Parsing (`parse_cm` / `parse_fo`) is PURE and fully unit-tested against
  hand-built CSV text using the real column names.
- Downloads are best-effort with weekend/holiday walk-back (`_recent_trading_
  days` + per-day 404 → try the previous session) and a module-level cache
  (`latest()`, 30-min TTL, lock-guarded so concurrent callers don't stampede).
- Wired in as the LAST-RESORT price in `nse_client.get_price()` and as a lot-size
  fallback in `nse_client.get_lot_sizes()`. `ingest_db()` loads the parsed bars
  into the `eod_bars`/`eod_oi` cache so the daily backtester's universe widens.

This deliberately reimplements the small slice of `jugaad-data` we need (bhavcopy
download+parse) with zero extra dependencies and full control over the format,
rather than taking on an unmaintained third-party dep.
"""

import csv
import io
import logging
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bhavcopy")

ARCH = "https://nsearchives.nseindia.com"
_CM_TMPL = ARCH + "/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
_FO_TMPL = ARCH + "/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
# Security-wise delivery position — a PLAIN (unzipped) CSV with per-symbol
# delivery quantity/percentage that the UDiFF CM bhavcopy omits. Dated DDMMYYYY.
_DELIV_TMPL = ARCH + "/products/content/sec_bhavdata_full_{dmy}.csv"

# Cash-market series we treat as tradable equities (skip govt bonds/T-bills/ETF
# oddities/SGBs etc.). EQ = rolling, BE/BZ = trade-for-trade/surveillance,
# SM/ST = SME board. EQ wins when a symbol appears under multiple series.
EQUITY_SERIES = frozenset({"EQ", "BE", "BZ", "SM", "ST"})

_IST = timezone(timedelta(hours=5, minutes=30))

_LATEST_TTL = 1800  # 30 min — bhavcopy is EOD, so it changes at most once a day
_cache = {"ts": 0.0, "cm": {}, "fo": {}, "cmDate": None, "foDate": None, "date": None}
_lock = threading.Lock()
_backfill_lock = threading.Lock()  # serialize backfills so they don't stampede the archive


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def _num(x):
    """Coerce a bhavcopy cell to float, or None. Blank/'-'/garbage → None."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if not s or s in ("-", "NA", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(cur, prev):
    """Percent change cur-vs-prev, or None when prev is missing/zero."""
    if cur is None or not prev:
        return None
    return (cur - prev) / prev * 100.0


def _ymd(d):
    """date | datetime | 'YYYY-MM-DD' → 'YYYYMMDD' for the archive URL."""
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    elif isinstance(d, datetime):
        d = d.date()
    return d.strftime("%Y%m%d")


def _dmy8(d):
    """date | datetime | 'YYYY-MM-DD' → 'DDMMYYYY' for the delivery archive URL."""
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    elif isinstance(d, datetime):
        d = d.date()
    return d.strftime("%d%m%Y")


def cm_url(d):
    return _CM_TMPL.format(ymd=_ymd(d))


def fo_url(d):
    return _FO_TMPL.format(ymd=_ymd(d))


def deliv_url(d):
    return _DELIV_TMPL.format(dmy=_dmy8(d))


def _today_ist():
    return datetime.now(_IST).date()


def _recent_trading_days(date=None, n=7):
    """The most recent `n` weekdays at/ before `date` (defaults to today IST),
    newest first. Holidays are handled by the caller (a 404 download → skip)."""
    d = date or _today_ist()
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    elif isinstance(d, datetime):
        d = d.date()
    out, cur = [], d
    while len(out) < max(1, n):
        if cur.weekday() < 5:  # Mon-Fri
            out.append(cur)
        cur -= timedelta(days=1)
    return out


def _unzip(raw):
    """Return the decoded CSV text from a single-file bhavcopy zip."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = zf.namelist()
    if not names:
        raise ValueError("empty bhavcopy zip")
    return zf.read(names[0]).decode("utf-8", "replace")


def parse_cm(text, series=EQUITY_SERIES):
    """Parse a CM (cash) UDiFF bhavcopy into {SYMBOL: bar}.

    bar = {symbol, d, series, open, high, low, close, prevClose, last, volume,
    value, trades, pChange}. Only equity `series` rows are kept; when a symbol
    appears under several series the EQ one wins.
    """
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("FinInstrmTp") or "").strip().upper() != "STK":
            continue
        srs = (row.get("SctySrs") or "").strip().upper()
        if series and srs not in series:
            continue
        sym = (row.get("TckrSymb") or "").strip().upper()
        if not sym:
            continue
        # EQ is the canonical listing; don't let a BE/SM duplicate overwrite it.
        if sym in out and out[sym]["series"] == "EQ" and srs != "EQ":
            continue
        close = _num(row.get("ClsPric"))
        prev = _num(row.get("PrvsClsgPric"))
        out[sym] = {
            "symbol": sym,
            "d": (row.get("TradDt") or "").strip(),
            "series": srs,
            "open": _num(row.get("OpnPric")),
            "high": _num(row.get("HghPric")),
            "low": _num(row.get("LwPric")),
            "close": close,
            "prevClose": prev,
            "last": _num(row.get("LastPric")),
            "volume": _num(row.get("TtlTradgVol")),
            "value": _num(row.get("TtlTrfVal")),
            "trades": _num(row.get("TtlNbOfTxsExctd")),
            "pChange": _pct(close, prev),
        }
    return out


def parse_fo(text):
    """Parse an FO (derivatives) UDiFF bhavcopy.

    Returns {date, futures, lots, underlying}:
      futures   : {SYMBOL: near-month future row} for stock (STF) + index (IDF)
                  futures — kind/expiry/close/prevClose/underlying/settle/oi/
                  changeOi/volume/lot/pChange (nearest expiry wins).
      lots      : {SYMBOL: lot} from NewBrdLotQty (any row; constant per name).
      underlying: {SYMBOL: spot} from the future's UndrlygPric.
    Options (STO/IDO) rows only contribute their lot size here.
    """
    futs, lots = {}, {}
    date = None
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("TckrSymb") or "").strip().upper()
        if not sym:
            continue
        if date is None:
            date = (row.get("TradDt") or "").strip() or None
        lot = _num(row.get("NewBrdLotQty"))
        if lot and sym not in lots:
            lots[sym] = int(lot)
        tp = (row.get("FinInstrmTp") or "").strip().upper()
        if tp not in ("STF", "IDF"):
            continue
        exp = (row.get("XpryDt") or "").strip()
        cur = futs.get(sym)
        # Keep the nearest expiry (ISO dates sort lexicographically).
        if cur is not None and cur["expiry"] and (not exp or exp >= cur["expiry"]):
            continue
        close = _num(row.get("ClsPric"))
        prev = _num(row.get("PrvsClsgPric"))
        futs[sym] = {
            "symbol": sym,
            "kind": "index" if tp == "IDF" else "stock",
            "expiry": exp,
            "close": close,
            "prevClose": prev,
            "underlying": _num(row.get("UndrlygPric")),
            "settle": _num(row.get("SttlmPric")),
            "oi": _num(row.get("OpnIntrst")),
            "changeOi": _num(row.get("ChngInOpnIntrst")),
            "volume": _num(row.get("TtlTradgVol")),
            "lot": int(lot) if lot else None,
            "pChange": _pct(close, prev),
        }
    underlying = {s: f["underlying"] for s, f in futs.items()
                  if f.get("underlying") is not None}
    return {"date": date, "futures": futs, "lots": lots, "underlying": underlying}


def parse_fo_options(text, underlying=None):
    """Parse OPTION rows (STO stock / IDO index) from an FO UDiFF bhavcopy into a
    per-expiry chain. `parse_fo` drops these to stay light; this pulls them for the
    resilient EOD option-chain view.

    `underlying` (a symbol) filters to ONE name — cheap, since the FO file has tens
    of thousands of option rows. Returns:
        {date, symbol, expiries:[ISO,...] (nearest first),
         byExpiry: {ISO_expiry: {underlying: spot, rows: {strike: {ce, pe}}}}}
    Each leg = {oi, chgOi, ltp, change, volume, prevClose}. The bhavcopy has no
    IV / bid-ask, so the analytics layer fills those as None to match the live
    `get_option_chain` leg shape.
    """
    want = underlying.upper().strip() if underlying else None
    date = None
    by = {}
    for row in csv.DictReader(io.StringIO(text)):
        tp = (row.get("FinInstrmTp") or "").strip().upper()
        if tp not in ("STO", "IDO"):
            continue
        sym = (row.get("TckrSymb") or "").strip().upper()
        if not sym or (want and sym != want):
            continue
        if date is None:
            date = (row.get("TradDt") or "").strip() or None
        ot = (row.get("OptnTp") or "").strip().upper()
        if ot not in ("CE", "PE"):
            continue
        strike = _num(row.get("StrkPric"))
        if strike is None:
            continue
        exp = (row.get("XpryDt") or "").strip()
        if not exp:
            continue
        close = _num(row.get("ClsPric"))
        prev = _num(row.get("PrvsClsgPric"))
        leg = {
            "oi": _num(row.get("OpnIntrst")),
            "chgOi": _num(row.get("ChngInOpnIntrst")),
            "ltp": close,
            "change": (close - prev) if (close is not None and prev is not None) else None,
            "volume": _num(row.get("TtlTradgVol")),
            "prevClose": prev,
        }
        slot = by.setdefault(exp, {"underlying": None, "rows": {}})
        if slot["underlying"] is None:
            slot["underlying"] = _num(row.get("UndrlygPric"))
        slot["rows"].setdefault(strike, {"ce": None, "pe": None})[ot.lower()] = leg
    # XpryDt is an ISO date (YYYY-MM-DD), so lexicographic sort == chronological;
    # options that have expired aren't in the file, so the earliest is the nearest.
    return {"date": date, "symbol": want, "expiries": sorted(by), "byExpiry": by}


def parse_sec_delivery(text, series=EQUITY_SERIES):
    """Parse an NSE `sec_bhavdata_full` (security-wise delivery position) CSV into
    {SYMBOL: {delivQty, delivPct}} — the delivery data the UDiFF CM bhavcopy omits.

    Two quirks handled: (1) the header cells carry a LEADING SPACE (` SERIES`,
    ` DELIV_PER`, …) so we match on stripped/upper-cased names, and (2) `DELIV_PER`
    is `-` for series NSE doesn't compute delivery on (bonds/ETFs) → None. Only the
    equity `series` are kept; EQ wins when a symbol appears under several series.
    High delivery% = real (non-intraday) buying — a conviction/accumulation signal.
    """
    out = {}
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip().upper() for h in next(reader)]
    except StopIteration:
        return out
    idx = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if (i is not None and i < len(row)) else None

    for row in reader:
        if not row:
            continue
        srs = (cell(row, "SERIES") or "").strip().upper()
        if series and srs not in series:
            continue
        sym = (cell(row, "SYMBOL") or "").strip().upper()
        if not sym:
            continue
        if sym in out and out[sym]["series"] == "EQ" and srs != "EQ":
            continue
        out[sym] = {
            "symbol": sym,
            "series": srs,
            "delivQty": _num(cell(row, "DELIV_QTY")),
            "delivPct": _num(cell(row, "DELIV_PER")),
            "date": (cell(row, "DATE1") or "").strip(),
        }
    return out


# ---------------------------------------------------------------------------
# network (best-effort; all failures degrade to None/empty)
# ---------------------------------------------------------------------------
def _download(url):
    """Fetch archive bytes, or None on any failure (missing day/holiday/network).

    A 404 (holiday / not published yet) returns None immediately. Any other error
    is retried once with a rebuilt NSE session (covers a dead cookie jar)."""
    import nse_client as nse
    hdr = {"Referer": nse.BASE + "/", "Accept": "*/*",
           "User-Agent": nse.HEADERS["User-Agent"]}
    for force in (False, True):
        try:
            r = nse.get_session(force=force).get(url, headers=hdr, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except Exception:
            if force:
                log.warning("bhavcopy download failed: %s", url, exc_info=True)
                return None
    return None


def _fetch(kind, date, walk):
    url_fn = cm_url if kind == "cm" else fo_url
    parse_fn = parse_cm if kind == "cm" else parse_fo
    for d in _recent_trading_days(date, walk):
        raw = _download(url_fn(d))
        if not raw:
            continue
        try:
            parsed = parse_fn(_unzip(raw))
        except Exception:
            log.warning("bhavcopy %s parse failed for %s", kind, d, exc_info=True)
            continue
        ok = parsed if kind == "cm" else parsed.get("futures")
        if ok:
            return d.strftime("%Y-%m-%d"), parsed
    empty = {} if kind == "cm" else {"date": None, "futures": {}, "lots": {},
                                     "underlying": {}}
    return None, empty


def fetch_cm(date=None, walk=7):
    """(date_str, {SYMBOL: bar}) for the latest CM bhavcopy at/ before `date`."""
    return _fetch("cm", date, walk)


def fetch_fo(date=None, walk=7):
    """(date_str, {date, futures, lots, underlying}) for the latest FO bhavcopy."""
    return _fetch("fo", date, walk)


def fetch_fo_text(date=None, walk=7):
    """(date_str, decoded_csv_text) for the latest FO bhavcopy, with the same
    weekend/holiday walk-back as fetch_fo. Used by the EOD option-chain parser,
    which needs the option rows parse_fo discards. (date, None) if none found."""
    for d in _recent_trading_days(date, walk):
        raw = _download(fo_url(d))
        if not raw:
            continue
        try:
            text = _unzip(raw)
        except Exception:
            log.warning("bhavcopy fo unzip failed for %s", d, exc_info=True)
            continue
        if text:
            return d.strftime("%Y-%m-%d"), text
    return None, None


def fetch_sec_delivery(date=None, walk=7):
    """(date_str, {SYMBOL: {delivQty, delivPct, …}}) for the latest sec_bhavdata_full
    delivery file at/ before `date`, with the same weekend/holiday walk-back as the
    bhavcopy. It's a PLAIN CSV (not zipped). (None, {}) if none found."""
    for d in _recent_trading_days(date, walk):
        raw = _download(deliv_url(d))
        if not raw:
            continue
        try:
            parsed = parse_sec_delivery(raw.decode("utf-8", "replace"))
        except Exception:
            log.warning("bhavcopy delivery parse failed for %s", d, exc_info=True)
            continue
        if parsed:
            return d.strftime("%Y-%m-%d"), parsed
    return None, {}


def latest(force=False):
    """Cached most-recent CM+FO bhavcopy (30-min TTL, lock-guarded).

    Returns the shared `_cache` dict; treat it as READ-ONLY. Holding the lock
    across the (rare, ~1-2s) download serializes cold callers so they don't each
    hammer the archive."""
    with _lock:
        if not force and _cache["cm"] and (time.time() - _cache["ts"]) < _LATEST_TTL:
            return _cache
        cm_date, cm = fetch_cm()
        fo_date, fo = fetch_fo()
        _cache.update(ts=time.time(), cm=cm, fo=fo, cmDate=cm_date,
                      foDate=fo_date, date=cm_date or fo_date)
        return _cache


def refresh():
    """Force a fresh download of the latest bhavcopy (ignores the TTL cache)."""
    return latest(force=True)


# ---------------------------------------------------------------------------
# public EOD lookups
# ---------------------------------------------------------------------------
def eod_price_map():
    """{SYMBOL: close} for every equity in the latest CM bhavcopy."""
    return {s: r["close"] for s, r in latest()["cm"].items()
            if r.get("close") is not None}


def eod_close(symbol):
    """Latest EOD close for a symbol (equity close, else its future's spot)."""
    if not symbol:
        return None
    sym = symbol.upper().strip()
    c = latest()
    row = c["cm"].get(sym)
    if row and row.get("close") is not None:
        return row["close"]
    fut = (c["fo"].get("futures") or {}).get(sym)
    if fut:
        return fut.get("underlying") if fut.get("underlying") is not None else fut.get("close")
    return None


def eod_quote(symbol):
    """Full EOD record for a symbol: the CM bar (if any) + its near future."""
    if not symbol:
        return {}
    sym = symbol.upper().strip()
    c = latest()
    row = dict(c["cm"].get(sym) or {})
    fut = (c["fo"].get("futures") or {}).get(sym)
    if fut:
        row["future"] = fut
    row.setdefault("symbol", sym)
    row["date"] = c.get("cmDate") or c.get("foDate")
    return row


def lot_sizes():
    """{SYMBOL: lot} for the whole F&O universe (fallback for fo_mktlots.csv)."""
    return dict((latest()["fo"].get("lots") or {}))


def status(refresh=False):
    """Freshness/coverage of the EOD cache (no secrets). `refresh` forces a pull."""
    if refresh:
        latest(force=True)
    c = _cache
    fo = c.get("fo") or {}
    return {
        "cmDate": c.get("cmDate"),
        "foDate": c.get("foDate"),
        "date": c.get("date"),
        "equities": len(c.get("cm") or {}),
        "futures": len(fo.get("futures") or {}),
        "lots": len(fo.get("lots") or {}),
        "ageSec": round(time.time() - c["ts"], 1) if c.get("ts") else None,
        "ttlSec": _LATEST_TTL,
        "cached": bool(c.get("cm")),
        "source": "nsearchives UDiFF bhavcopy",
    }


# ---------------------------------------------------------------------------
# persistence — broaden the daily-backtest universe
# ---------------------------------------------------------------------------
def ingest_db(date=None):
    """Load a day's bhavcopy into the `eod_bars`/`eod_oi` cache (one CM bar and
    one near-future OI row per symbol), layering in per-symbol **delivery%** from
    the sec_bhavdata_full file (which the UDiFF CM omits) so the EOD scanner /
    delivery strategy work market-wide. Widens the daily backtester's universe to
    the whole market without per-symbol NSE calls. Returns written-row counts."""
    import db
    cm_date, cm = fetch_cm(date)
    fo_date, fo = fetch_fo(date)

    bars = deliv = 0
    if cm:
        rows = []
        for sym, r in cm.items():
            rows.append({**r, "symbol": sym, "iso": r.get("d"), "date": r.get("d")})
        # Layer delivery% for the SAME session (aligned to cm_date, no walk-away).
        if cm_date:
            dd, dmap = fetch_sec_delivery(cm_date, walk=2)
            if dmap and dd == cm_date:
                for row in rows:
                    dv = dmap.get(row["symbol"])
                    if dv:
                        row["delivPct"] = dv.get("delivPct")
                        row["delivQty"] = dv.get("delivQty")
                        deliv += 1
        bars = db.eod_bars_put_bulk(rows)

    oi = 0
    for sym, f in (fo.get("futures") or {}).items():
        row = {
            "d": fo_date, "date": fo_date,
            "close": f.get("close"), "spot": f.get("underlying"),
            "oi": f.get("oi"), "changeOi": f.get("changeOi"),
            "volume": f.get("volume"), "lot": f.get("lot"),
        }
        if row["d"]:
            oi += db.eod_oi_put(sym, f.get("expiry"), [row])

    return {"cmDate": cm_date, "foDate": fo_date, "bars": bars, "oi": oi,
            "deliv": deliv, "equities": len(cm), "futures": len(fo.get("futures") or {})}


def backfill(days=20, progress=None):
    """Ingest the last `days` trading sessions' bhavcopies into eod_bars/eod_oi so
    the EOD scanner + daily backtest have market-wide HISTORY (not just today's
    single day). Idempotent — re-ingesting a day just REPLACEs the same rows.

    Each day is one CM + one FO archive fetch (~1-2s), so this is slow (~1s/day);
    call it from a background thread. Serialized by a lock so two callers can't
    hammer the archive at once. Returns a summary of what landed.
    """
    days = max(1, min(int(days), 250))
    got = {"asked": days, "days": 0, "bars": 0, "oi": 0, "deliv": 0,
           "equities": 0, "dates": []}
    if not _backfill_lock.acquire(blocking=False):
        got["busy"] = True
        return got
    try:
        seen = set()
        for d in _recent_trading_days(n=days):
            res = ingest_db(date=d)
            cm_date = res.get("cmDate")
            # A holiday walks back to the prior session, which we may already have
            # ingested this pass — count each distinct published day once.
            if not cm_date or cm_date in seen or not res.get("bars"):
                continue
            seen.add(cm_date)
            got["days"] += 1
            got["bars"] += res.get("bars", 0)
            got["oi"] += res.get("oi", 0)
            got["deliv"] += res.get("deliv", 0)
            got["equities"] = max(got["equities"], res.get("equities", 0))
            got["dates"].append(cm_date)
            if progress:
                try:
                    progress(dict(got))
                except Exception:
                    pass
        got["dates"].sort()
        return got
    finally:
        _backfill_lock.release()
