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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_FILE = os.path.join(DATA_DIR, "market.db")

_init_lock = threading.Lock()
# serializes EOD-cache writes: the daily backtest hits these from 6 worker
# threads, so we funnel writes through one lock to avoid "database is locked".
_write_lock = threading.Lock()
_initialized = False

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
    "rMultiple", "openedAt", "openedDate", "regimeAtEntry", "exitPrice",
    "closedAt", "closedDay", "minsToExit",
]
_SIM_TEXT = {"id", "book", "strategy", "symbol", "direction", "rating", "status",
             "openedAt", "openedDate", "regimeAtEntry", "closedAt", "closedDay"}
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
                    exitPrice REAL, closedAt TEXT, closedDay TEXT, minsToExit INTEGER
                )""")
            # Migrate pre-existing ledgers: add the `book` column (all legacy trades
            # belong to the all-market 'cash' book) so the F&O book is additive.
            cols = {r[1] for r in c.execute("PRAGMA table_info(sim_trades)")}
            if "book" not in cols:
                c.execute("ALTER TABLE sim_trades ADD COLUMN book TEXT DEFAULT 'cash'")
                c.execute("UPDATE sim_trades SET book='cash' WHERE book IS NULL")
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
        _import_legacy_csv()
        _initialized = True


# ----------------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------------
def insert_snapshots(rows):
    if not rows:
        return 0
    with _conn() as c:
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
    with _conn() as c:
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
# Context log (gzipped JSON blob per cycle)
# ----------------------------------------------------------------------------
def insert_context(ts, day, regime, nifty_pct, payload):
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    with _conn() as c:
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
    with _conn() as c:
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
    with _conn() as c:
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
