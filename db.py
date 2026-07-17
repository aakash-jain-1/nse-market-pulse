"""
SQLite store for the append-only time-series logs
==================================================
The small, document-shaped state (sim_state.json, paper_state.json) stays as
JSON — it's tiny and rewritten atomically. But the *time-series* logs grow fast
(~16k snapshot rows/day) and the old CSV readers loaded the WHOLE file into
memory on every backtest()/iv_rank() call. SQLite fixes that: one file, no
server, indexed queries, millions of rows, concurrent reads (WAL).

Tables:
  snapshots     — the demand / volume-gainers board, one row per symbol/snapshot.
  iv_log        — ATM implied-volatility captures.
  context_log   — a trimmed, gzipped snapshot of the FULL strategy context each
                  cycle (scanner / gainers / losers / oi / quotes / index). This
                  is what lets us replay all strategies offline (backtest).
  sim_trades    — the durable multi-strategy sim ledger.
  eod_bars /    — persistent daily OHLCV+delivery / futures-OI history cache for
  eod_oi /        the daily backtest. Past bars are immutable, so we keep them
  eod_meta        forever and only re-hit NSE per a freshness TTL (eod_meta).

Everything is stdlib (sqlite3 + gzip + json). DB lives in data/market.db
(gitignored via *.db). On first run we import any existing snapshots.csv /
iv_log.csv so no history is lost.
"""

import csv
import gzip
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_FILE = os.path.join(DATA_DIR, "market.db")

_init_lock = threading.Lock()
# Serializes ALL writers (AUDIT.md L4). WAL lets readers run concurrently, but
# funnelling writes through one process-level lock avoids "database is locked"
# retries when the daily backtest's 6 workers, the capture loop and the sim all
# write at once.
_write_lock = threading.Lock()
_initialized = False

IST = timezone(timedelta(hours=5, minutes=30))

SNAPSHOT_COLS = [
    "ts", "view", "rank", "symbol", "ltp", "pChange",
    "score", "signalCount", "volMult", "week1volChange", "volume", "value",
]
IV_COLS = [
    "ts", "symbol", "expiry", "atmStrike", "atmIV", "ceIV", "peIV",
    "pcr", "underlying",
]
SIM_TRADE_COLS = [
    "id", "book", "strategy", "symbol", "direction", "conviction", "rating", "reasons",
    "fno", "entry", "stop", "target", "stopPct", "targetPct", "rr", "qty",
    "notional", "risk", "status", "ltp", "mfePct", "maePct", "pnl", "pnlPct",
    "rMultiple", "openedAt", "openedDate", "regimeAtEntry", "volAtEntry",
    "exitPrice", "closedAt", "closedDay", "minsToExit",
]
_SIM_TEXT = {"id", "book", "strategy", "symbol", "direction", "rating", "status",
             "openedAt", "openedDate", "regimeAtEntry", "volAtEntry",
             "closedAt", "closedDay"}
EOD_BAR_COLS = [
    "symbol", "d", "date", "iso", "open", "high", "low", "close",
    "prevClose", "vwap", "volume", "value", "trades", "delivQty", "delivPct",
]
_EOD_BAR_TEXT = {"symbol", "d", "date", "iso"}
EOD_OI_COLS = [
    "symbol", "expiry", "d", "date", "close", "spot", "oi", "changeOi",
    "volume", "lot",
]
_EOD_OI_TEXT = {"symbol", "expiry", "d", "date"}
IDEA_COLS = [
    "day", "symbol", "direction", "entry", "stop", "target", "stopPct", "targetPct",
    "rr", "conviction", "rating", "reasons", "fno", "pChange", "firstSeenAt",
    "lastSeenAt", "ltp", "movePct", "maxMovePct", "minMovePct",
    "outcome", "outcomeAt", "outcomePct",
]
_IDEA_TEXT = {"day", "symbol", "direction", "rating", "reasons", "firstSeenAt",
              "lastSeenAt", "outcome", "outcomeAt"}


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init():
    """Create tables/indexes once; import legacy CSVs if the tables are empty."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    ts TEXT, view TEXT, rank INTEGER, symbol TEXT,
                    ltp REAL, pChange REAL, score REAL, signalCount INTEGER,
                    volMult REAL, week1volChange REAL, volume REAL, value REAL
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_snap_view_ts ON snapshots(view, ts)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_snap_symbol ON snapshots(symbol)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS iv_log (
                    ts TEXT, symbol TEXT, expiry TEXT, atmStrike REAL,
                    atmIV REAL, ceIV REAL, peIV REAL, pcr REAL, underlying REAL
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_iv_symbol_ts ON iv_log(symbol, ts)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS context_log (
                    ts TEXT PRIMARY KEY, day TEXT, regime TEXT,
                    niftyPct REAL, payload BLOB
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_ctx_day ON context_log(day)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS sim_trades (
                    id TEXT PRIMARY KEY, book TEXT DEFAULT 'cash',
                    strategy TEXT, symbol TEXT, direction TEXT,
                    conviction REAL, rating TEXT, reasons TEXT, fno INTEGER,
                    entry REAL, stop REAL, target REAL, stopPct REAL, targetPct REAL,
                    rr REAL, qty REAL, notional REAL, risk REAL,
                    status TEXT, ltp REAL, mfePct REAL, maePct REAL,
                    pnl REAL, pnlPct REAL, rMultiple REAL,
                    openedAt TEXT, openedDate TEXT, regimeAtEntry TEXT,
                    volAtEntry TEXT, exitPrice REAL, closedAt TEXT,
                    closedDay TEXT, minsToExit INTEGER
                )""")
            # Migrate pre-existing ledgers: add the `book` column (all legacy trades
            # belong to the all-market 'cash' book) so the F&O book is additive,
            # and the `volAtEntry` column (volatility regime at entry; NULL for
            # trades opened before the volatility-aware regime board shipped).
            cols = {r[1] for r in c.execute("PRAGMA table_info(sim_trades)")}
            if "book" not in cols:
                c.execute("ALTER TABLE sim_trades ADD COLUMN book TEXT DEFAULT 'cash'")
                c.execute("UPDATE sim_trades SET book='cash' WHERE book IS NULL")
            if "volAtEntry" not in cols:
                c.execute("ALTER TABLE sim_trades ADD COLUMN volAtEntry TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_status ON sim_trades(status)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_strat_day ON sim_trades(strategy, openedDate)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_regime ON sim_trades(regimeAtEntry, strategy)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_book ON sim_trades(book, status)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS eod_bars (
                    symbol TEXT, d TEXT, date TEXT, iso TEXT,
                    open REAL, high REAL, low REAL, close REAL, prevClose REAL,
                    vwap REAL, volume REAL, value REAL, trades REAL,
                    delivQty REAL, delivPct REAL,
                    PRIMARY KEY (symbol, d)
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_eod_bars_sym ON eod_bars(symbol)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS eod_oi (
                    symbol TEXT, expiry TEXT, d TEXT, date TEXT,
                    close REAL, spot REAL, oi REAL, changeOi REAL,
                    volume REAL, lot REAL,
                    PRIMARY KEY (symbol, expiry, d)
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_eod_oi_sym ON eod_oi(symbol, expiry)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS eod_meta (
                    symbol TEXT, kind TEXT, fetched_at TEXT, last_d TEXT, n INTEGER,
                    PRIMARY KEY (symbol, kind)
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS min_bars (
                    symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                    PRIMARY KEY (symbol, t)
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_min_bars_sym ON min_bars(symbol)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS ideas (
                    day TEXT, symbol TEXT, direction TEXT,
                    entry REAL, stop REAL, target REAL,
                    stopPct REAL, targetPct REAL, rr REAL,
                    conviction REAL, rating TEXT, reasons TEXT, fno INTEGER,
                    pChange REAL, firstSeenAt TEXT, lastSeenAt TEXT,
                    ltp REAL, movePct REAL, maxMovePct REAL, minMovePct REAL,
                    outcome TEXT, outcomeAt TEXT, outcomePct REAL,
                    PRIMARY KEY (day, symbol, direction)
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_ideas_day ON ideas(day)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS alert_log (
                    key TEXT PRIMARY KEY, kind TEXT, symbol TEXT, fired_at TEXT
                )""")
        _import_legacy_csv()
        _initialized = True


# ----------------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------------
def insert_snapshots(rows):
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT INTO snapshots ({','.join(SNAPSHOT_COLS)}) "
            f"VALUES ({','.join('?' * len(SNAPSHOT_COLS))})",
            [tuple(_num_or_none(r.get(col)) if col not in ("ts", "view", "symbol")
                   else r.get(col) for col in SNAPSHOT_COLS) for r in rows],
        )
    return len(rows)


def snapshot_rows(view):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM snapshots WHERE view=? ORDER BY ts", (view,))]


def snapshot_stats():
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"]
        times = c.execute(
            "SELECT MIN(ts) a, MAX(ts) b, COUNT(DISTINCT ts) n FROM snapshots"
        ).fetchone()
    return {"total": total, "distinct": times["n"] or 0,
            "first": times["a"], "last": times["b"]}


def export_snapshots_csv(path):
    with _conn() as c, open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SNAPSHOT_COLS)
        for r in c.execute(f"SELECT {','.join(SNAPSHOT_COLS)} FROM snapshots ORDER BY ts"):
            w.writerow([r[col] for col in SNAPSHOT_COLS])
    return path


# ----------------------------------------------------------------------------
# IV log
# ----------------------------------------------------------------------------
def insert_iv(rows):
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT INTO iv_log ({','.join(IV_COLS)}) "
            f"VALUES ({','.join('?' * len(IV_COLS))})",
            [tuple(r.get(col) for col in IV_COLS) for r in rows],
        )
    return len(rows)


def iv_series(symbol):
    with _conn() as c:
        return [(r["ts"], r["atmIV"]) for r in c.execute(
            "SELECT ts, atmIV FROM iv_log WHERE symbol=? ORDER BY ts", (symbol,))]


def iv_stats():
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(DISTINCT ts) t, COUNT(DISTINCT symbol) s, MAX(ts) last "
            "FROM iv_log").fetchone()
    return {"snapshots": r["t"] or 0, "symbols": r["s"] or 0, "last": r["last"]}


# ----------------------------------------------------------------------------
# Alert dedupe log (server-side off-screen alerts — notify.py)
# ----------------------------------------------------------------------------
def alert_seen(key):
    """True if this alert key was already fired (dedupe across cycles/restarts)."""
    init()
    with _conn() as c:
        return c.execute("SELECT 1 FROM alert_log WHERE key=?", (key,)).fetchone() is not None


def alert_mark(key, kind, symbol=None):
    """Record that an alert fired. Keys are IST-day-scoped so they self-expire."""
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO alert_log (key, kind, symbol, fired_at) VALUES (?,?,?,?)",
            (key, kind, symbol, datetime.now(IST).isoformat(timespec="seconds")))


# ----------------------------------------------------------------------------
# Context log (gzipped JSON blob per cycle)
# ----------------------------------------------------------------------------
def insert_context(ts, day, regime, nifty_pct, payload):
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO context_log (ts, day, regime, niftyPct, payload) "
            "VALUES (?,?,?,?,?)", (ts, day, regime, nifty_pct, blob))
    return len(blob)


def context_cycles(since_day=None, limit=None):
    """Yield stored cycles in time order: {ts, day, regime, niftyPct, ctx}."""
    q = "SELECT ts, day, regime, niftyPct, payload FROM context_log"
    args = []
    if since_day:
        q += " WHERE day >= ?"
        args.append(since_day)
    q += " ORDER BY ts"
    if limit:
        q += f" LIMIT {int(limit)}"
    out = []
    with _conn() as c:
        for r in c.execute(q, args):
            try:
                ctx = json.loads(gzip.decompress(r["payload"]).decode("utf-8"))
            except Exception:
                continue
            out.append({"ts": r["ts"], "day": r["day"], "regime": r["regime"],
                        "niftyPct": r["niftyPct"], "ctx": ctx})
    return out


def context_stats():
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT day) d, MIN(ts) a, MAX(ts) b, "
            "SUM(LENGTH(payload)) bytes FROM context_log").fetchone()
    return {"cycles": r["n"] or 0, "days": r["d"] or 0,
            "first": r["a"], "last": r["b"], "bytes": r["bytes"] or 0}


def regime_by_day():
    """{day: {label, niftyPct}} using the LAST regime detected each day.
    Last-write-wins matches the sim's per-day rollup, so views agree."""
    with _conn() as c:
        rows = c.execute(
            "SELECT c.day day, c.regime regime, c.niftyPct niftyPct "
            "FROM context_log c JOIN ("
            "  SELECT day, MAX(ts) mts FROM context_log "
            "  WHERE regime IS NOT NULL AND regime != '' GROUP BY day"
            ") m ON c.day = m.day AND c.ts = m.mts")
        return {r["day"]: {"label": r["regime"], "niftyPct": r["niftyPct"]}
                for r in rows}


# ----------------------------------------------------------------------------
# Sim trades ledger (durable, queryable — replaces the JSON trades blob)
# ----------------------------------------------------------------------------
def _trade_to_row(t):
    row = []
    for col in SIM_TRADE_COLS:
        if col == "reasons":
            row.append(json.dumps(t.get("reasons") or []))
        elif col == "fno":
            row.append(1 if t.get("fno") else 0)
        elif col == "book":
            row.append(t.get("book") or "cash")
        elif col in _SIM_TEXT:
            row.append(t.get(col))
        else:
            row.append(_num_or_none(t.get(col)))
    return tuple(row)


def _row_to_trade(r):
    t = {col: r[col] for col in SIM_TRADE_COLS}
    try:
        t["reasons"] = json.loads(r["reasons"]) if r["reasons"] else []
    except Exception:
        t["reasons"] = []
    t["fno"] = bool(r["fno"])
    t["book"] = t.get("book") or "cash"
    if t["minsToExit"] is not None:
        t["minsToExit"] = int(t["minsToExit"])
    return t


def sim_insert_trades(trades):
    """Insert or update (by id) a batch of trade dicts."""
    if not trades:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO sim_trades ({','.join(SIM_TRADE_COLS)}) "
            f"VALUES ({','.join('?' * len(SIM_TRADE_COLS))})",
            [_trade_to_row(t) for t in trades],
        )
    return len(trades)


def sim_all_trades(book=None):
    q, args = "SELECT * FROM sim_trades", []
    if book is not None:
        q += " WHERE book=?"
        args.append(book)
    q += " ORDER BY openedAt"
    with _conn() as c:
        return [_row_to_trade(r) for r in c.execute(q, args)]


def sim_open_trades(book=None):
    q = "SELECT * FROM sim_trades WHERE status='OPEN'"
    args = []
    if book is not None:
        q += " AND book=?"
        args.append(book)
    q += " ORDER BY openedAt"
    with _conn() as c:
        return [_row_to_trade(r) for r in c.execute(q, args)]


def sim_trades_where(strategy=None, opened_date=None, book=None):
    q, args = "SELECT * FROM sim_trades WHERE 1=1", []
    if book is not None:
        q += " AND book=?"
        args.append(book)
    if strategy is not None:
        q += " AND strategy=?"
        args.append(strategy)
    if opened_date is not None:
        q += " AND openedDate=?"
        args.append(opened_date)
    q += " ORDER BY openedAt"
    with _conn() as c:
        return [_row_to_trade(r) for r in c.execute(q, args)]


def sim_trade_count(book=None):
    q, args = "SELECT COUNT(*) n FROM sim_trades", []
    if book is not None:
        q += " WHERE book=?"
        args.append(book)
    with _conn() as c:
        return c.execute(q, args).fetchone()["n"]


def sim_clear(book=None):
    with _write_lock, _conn() as c:
        if book is None:
            c.execute("DELETE FROM sim_trades")
        else:
            c.execute("DELETE FROM sim_trades WHERE book=?", (book,))


# ----------------------------------------------------------------------------
# EOD daily-bar / OI cache (persistent history for the daily backtest)
# ----------------------------------------------------------------------------
def _eod_bar_row(symbol, b):
    out = []
    for col in EOD_BAR_COLS:
        if col == "symbol":
            out.append(symbol)
        elif col in _EOD_BAR_TEXT:
            out.append(b.get(col))
        else:
            out.append(_num_or_none(b.get(col)))
    return tuple(out)


def _eod_oi_row(symbol, expiry, r):
    out = []
    for col in EOD_OI_COLS:
        if col == "symbol":
            out.append(symbol)
        elif col == "expiry":
            out.append(expiry)
        elif col in _EOD_OI_TEXT:
            out.append(r.get(col))
        else:
            out.append(_num_or_none(r.get(col)))
    return tuple(out)


def eod_bars_get(symbol):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM eod_bars WHERE symbol=? ORDER BY d", (symbol.upper(),))]


def eod_bars_all(since=None):
    """ALL eod bars grouped {SYMBOL: [bar,...]} (each list ascending by date), in
    ONE connection. `since` (YYYY-MM-DD, inclusive) trims to recent history so a
    market-wide scan doesn't load years of rows. Used by the EOD scanner to price
    ~2400 names without a per-symbol query each."""
    q, args = "SELECT * FROM eod_bars", ()
    if since:
        q += " WHERE d >= ?"
        args = (since,)
    q += " ORDER BY symbol, d"
    out = {}
    with _conn() as c:
        for r in c.execute(q, args):
            out.setdefault(r["symbol"], []).append(dict(r))
    return out


def eod_latest_date():
    """Most recent trading date present in eod_bars (YYYY-MM-DD), or None."""
    with _conn() as c:
        r = c.execute("SELECT MAX(d) z FROM eod_bars").fetchone()
    return (r["z"] if r else None) or None


def eod_oi_symbols():
    """Distinct symbols that have futures OI history (the ingested F&O universe)."""
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT symbol FROM eod_oi")]


def eod_bars_put(symbol, bars):
    """Upsert daily bars (each needs a clean 'd' YYYY-MM-DD). Past bars are
    immutable; REPLACE handles the occasional revision + the newest day."""
    symbol = symbol.upper()
    rows = [_eod_bar_row(symbol, b) for b in bars if b.get("d")]
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO eod_bars ({','.join(EOD_BAR_COLS)}) "
            f"VALUES ({','.join('?' * len(EOD_BAR_COLS))})", rows)
    return len(rows)


def eod_bars_put_bulk(bars):
    """Upsert daily bars for MANY symbols in ONE transaction. Each bar dict must
    carry its own 'symbol' and a clean 'd' (YYYY-MM-DD). Used by the bhavcopy
    ingest to load the whole cash market (~2400 rows) in a single write instead
    of a per-symbol transaction."""
    rows = [_eod_bar_row((b.get("symbol") or "").upper(), b)
            for b in bars if b.get("d") and b.get("symbol")]
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO eod_bars ({','.join(EOD_BAR_COLS)}) "
            f"VALUES ({','.join('?' * len(EOD_BAR_COLS))})", rows)
    return len(rows)


def eod_oi_get(symbol, expiry):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM eod_oi WHERE symbol=? AND expiry=? ORDER BY d",
            (symbol.upper(), (expiry or "").upper()))]


def eod_oi_put(symbol, expiry, rows_in):
    symbol, expiry = symbol.upper(), (expiry or "").upper()
    rows = [_eod_oi_row(symbol, expiry, r) for r in rows_in if r.get("d")]
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO eod_oi ({','.join(EOD_OI_COLS)}) "
            f"VALUES ({','.join('?' * len(EOD_OI_COLS))})", rows)
    return len(rows)


def eod_meta_get(symbol, kind):
    with _conn() as c:
        r = c.execute(
            "SELECT fetched_at, last_d, n FROM eod_meta WHERE symbol=? AND kind=?",
            (symbol.upper(), kind)).fetchone()
    return dict(r) if r else None


def eod_meta_set(symbol, kind, fetched_at, last_d, n):
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO eod_meta (symbol, kind, fetched_at, last_d, n) "
            "VALUES (?,?,?,?,?)", (symbol.upper(), kind, fetched_at, last_d, n))


def eod_stats():
    with _conn() as c:
        b = c.execute("SELECT COUNT(*) rows, COUNT(DISTINCT symbol) syms, "
                      "MIN(d) a, MAX(d) z FROM eod_bars").fetchone()
        o = c.execute("SELECT COUNT(*) rows, COUNT(DISTINCT symbol) syms "
                      "FROM eod_oi").fetchone()
        m = c.execute("SELECT COUNT(*) rows, COUNT(DISTINCT symbol) syms "
                      "FROM min_bars").fetchone()
    return {
        "bars": {"rows": b["rows"] or 0, "symbols": b["syms"] or 0,
                 "from": b["a"], "to": b["z"]},
        "oi": {"rows": o["rows"] or 0, "symbols": o["syms"] or 0},
        "min": {"rows": m["rows"] or 0, "symbols": m["syms"] or 0},
    }


def eod_clear():
    with _write_lock, _conn() as c:
        c.execute("DELETE FROM eod_bars")
        c.execute("DELETE FROM eod_oi")
        c.execute("DELETE FROM eod_meta")
        c.execute("DELETE FROM min_bars")


# ----------------------------------------------------------------------------
# Minute-bar cache (1-min OHLCV for intrabar-accurate historical resolution)
# ----------------------------------------------------------------------------
def min_bars_get(symbol, from_t=None, to_t=None):
    """Ascending 1-min candles {t,o,h,l,c,v} (t = ms) in the optional [from_t, to_t]."""
    q, args = "SELECT t,o,h,l,c,v FROM min_bars WHERE symbol=?", [symbol.upper()]
    if from_t is not None:
        q += " AND t>=?"; args.append(int(from_t))
    if to_t is not None:
        q += " AND t<=?"; args.append(int(to_t))
    q += " ORDER BY t"
    with _conn() as c:
        return [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"],
                 "c": r["c"], "v": r["v"]} for r in c.execute(q, args)]


def min_bars_put(symbol, points):
    symbol = symbol.upper()
    rows = [(symbol, int(p["t"]), _num_or_none(p.get("o")), _num_or_none(p.get("h")),
             _num_or_none(p.get("l")), _num_or_none(p.get("c")), _num_or_none(p.get("v")))
            for p in points if p.get("t") is not None]
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO min_bars (symbol,t,o,h,l,c,v) "
            "VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def min_bars_span(symbol):
    """(min_t, max_t, count) for a symbol's cached minute bars."""
    with _conn() as c:
        r = c.execute("SELECT MIN(t) a, MAX(t) z, COUNT(*) n FROM min_bars "
                      "WHERE symbol=?", (symbol.upper(),)).fetchone()
    return (r["a"], r["z"], r["n"] or 0)


# ----------------------------------------------------------------------------
# Ideas journal (durable daily record of every idea shown on the Ideas tab)
# ----------------------------------------------------------------------------
def _idea_to_row(d):
    row = []
    for col in IDEA_COLS:
        if col == "reasons":
            row.append(json.dumps(d.get("reasons") or []))
        elif col == "fno":
            row.append(1 if d.get("fno") else 0)
        elif col in _IDEA_TEXT:
            row.append(d.get(col))
        else:
            row.append(_num_or_none(d.get(col)))
    return tuple(row)


def _row_to_idea(r):
    d = {col: r[col] for col in IDEA_COLS}
    try:
        d["reasons"] = json.loads(r["reasons"]) if r["reasons"] else []
    except Exception:
        d["reasons"] = []
    d["fno"] = bool(r["fno"])
    return d


def ideas_upsert(rows):
    """Insert/replace today's idea records (keyed by day+symbol+direction)."""
    if not rows:
        return 0
    with _write_lock, _conn() as c:
        c.executemany(
            f"INSERT OR REPLACE INTO ideas ({','.join(IDEA_COLS)}) "
            f"VALUES ({','.join('?' * len(IDEA_COLS))})",
            [_idea_to_row(d) for d in rows])
    return len(rows)


def ideas_for_day(day):
    with _conn() as c:
        return [_row_to_idea(r) for r in c.execute(
            "SELECT * FROM ideas WHERE day=? ORDER BY firstSeenAt", (day,))]


def ideas_days(limit=60):
    """Per-day summary (newest first) for the Ideas history table."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT day, COUNT(*) n, "
            "SUM(CASE WHEN direction='LONG' THEN 1 ELSE 0 END) longs, "
            "SUM(CASE WHEN direction='SHORT' THEN 1 ELSE 0 END) shorts, "
            "SUM(CASE WHEN outcome='TARGET' THEN 1 ELSE 0 END) targets, "
            "SUM(CASE WHEN outcome='STOP' THEN 1 ELSE 0 END) stops, "
            "AVG(maxMovePct) avgBest, AVG(minMovePct) avgWorst, AVG(movePct) avgMove "
            "FROM ideas GROUP BY day ORDER BY day DESC LIMIT ?", (int(limit),))]


def ideas_stats():
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT day) d, "
                      "MIN(day) a, MAX(day) z FROM ideas").fetchone()
    return {"ideas": r["n"] or 0, "days": r["d"] or 0, "first": r["a"], "last": r["z"]}


# ----------------------------------------------------------------------------
# Retention — keep market.db from growing without bound (AUDIT.md M6)
# ----------------------------------------------------------------------------
def retention(snapshots_days=90, iv_days=120, context_days=60,
              min_bars_days=45, vacuum=False):
    """Prune the high-volume, *reproducible* time-series so the DB stays bounded.

    Deliberately KEPT forever: the durable ledgers (`sim_trades`, `ideas` — your
    real performance history) and the immutable EOD cache (`eod_bars`/`eod_oi` —
    past bars never change and re-fetching is what we're avoiding). Only the
    re-derivable logs are trimmed. Pass a window of 0/None to skip that table.

    `min_bars.t` is NSE's IST-baked-as-UTC epoch; comparing to a real-UTC cutoff
    is off by ~5.5h, which is immaterial at a multi-day horizon. Returns a dict of
    rows deleted per table. VACUUM (off by default) reclaims file space but locks
    the DB, so run it manually/rarely.
    """
    init()
    now = datetime.now(IST)

    def _iso(n):
        return (now - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")

    def _day(n):
        return (now - timedelta(days=n)).strftime("%Y-%m-%d")

    deleted = {}
    with _write_lock, _conn() as c:
        if snapshots_days:
            deleted["snapshots"] = c.execute(
                "DELETE FROM snapshots WHERE ts < ?", (_iso(snapshots_days),)).rowcount
        if iv_days:
            deleted["iv_log"] = c.execute(
                "DELETE FROM iv_log WHERE ts < ?", (_iso(iv_days),)).rowcount
        if context_days:
            deleted["context_log"] = c.execute(
                "DELETE FROM context_log WHERE day < ?", (_day(context_days),)).rowcount
        if min_bars_days:
            cutoff_ms = int((time.time() - min_bars_days * 86400) * 1000)
            deleted["min_bars"] = c.execute(
                "DELETE FROM min_bars WHERE t < ?", (cutoff_ms,)).rowcount
        # Alert dedupe keys are day-scoped and tiny; keep ~14 days for safety.
        deleted["alert_log"] = c.execute(
            "DELETE FROM alert_log WHERE fired_at < ?", (_iso(14),)).rowcount
    if vacuum:
        with _write_lock, _conn() as c:
            c.execute("VACUUM")
        deleted["vacuum"] = True
    return deleted


# ----------------------------------------------------------------------------
# Helpers / migration
# ----------------------------------------------------------------------------
def _num_or_none(x):
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _import_legacy_csv():
    """One-time import of pre-existing snapshots.csv / iv_log.csv into the DB."""
    snap_csv = os.path.join(DATA_DIR, "snapshots.csv")
    iv_csv = os.path.join(DATA_DIR, "iv_log.csv")
    with _conn() as c:
        have_snap = c.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"]
        have_iv = c.execute("SELECT COUNT(*) n FROM iv_log").fetchone()["n"]

    if not have_snap and os.path.exists(snap_csv):
        with open(snap_csv, newline="", encoding="utf-8") as f:
            insert_snapshots(list(csv.DictReader(f)))
    if not have_iv and os.path.exists(iv_csv):
        with open(iv_csv, newline="", encoding="utf-8") as f:
            insert_iv(list(csv.DictReader(f)))
