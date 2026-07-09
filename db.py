"""
SQLite store for the append-only time-series logs
==================================================
The small, document-shaped state (sim_state.json, paper_state.json) stays as
JSON — it's tiny and rewritten atomically. But the *time-series* logs grow fast
(~16k snapshot rows/day) and the old CSV readers loaded the WHOLE file into
memory on every backtest()/iv_rank() call. SQLite fixes that: one file, no
server, indexed queries, millions of rows, concurrent reads (WAL).

Three tables:
  snapshots     — the demand / volume-gainers board, one row per symbol/snapshot.
  iv_log        — ATM implied-volatility captures.
  context_log   — a trimmed, gzipped snapshot of the FULL strategy context each
                  cycle (scanner / gainers / losers / oi / quotes / index). This
                  is what lets us replay all strategies offline (backtest).

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
    "id", "strategy", "symbol", "direction", "conviction", "rating", "reasons",
    "fno", "entry", "stop", "target", "stopPct", "targetPct", "rr", "qty",
    "notional", "risk", "status", "ltp", "mfePct", "maePct", "pnl", "pnlPct",
    "rMultiple", "openedAt", "openedDate", "regimeAtEntry", "exitPrice",
    "closedAt", "closedDay", "minsToExit",
]
_SIM_TEXT = {"id", "strategy", "symbol", "direction", "rating", "status",
             "openedAt", "openedDate", "regimeAtEntry", "closedAt", "closedDay"}


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
                    id TEXT PRIMARY KEY, strategy TEXT, symbol TEXT, direction TEXT,
                    conviction REAL, rating TEXT, reasons TEXT, fno INTEGER,
                    entry REAL, stop REAL, target REAL, stopPct REAL, targetPct REAL,
                    rr REAL, qty REAL, notional REAL, risk REAL,
                    status TEXT, ltp REAL, mfePct REAL, maePct REAL,
                    pnl REAL, pnlPct REAL, rMultiple REAL,
                    openedAt TEXT, openedDate TEXT, regimeAtEntry TEXT,
                    exitPrice REAL, closedAt TEXT, closedDay TEXT, minsToExit INTEGER
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_status ON sim_trades(status)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_strat_day ON sim_trades(strategy, openedDate)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_sim_regime ON sim_trades(regimeAtEntry, strategy)")
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


def sim_all_trades():
    with _conn() as c:
        return [_row_to_trade(r) for r in c.execute(
            "SELECT * FROM sim_trades ORDER BY openedAt")]


def sim_open_trades():
    with _conn() as c:
        return [_row_to_trade(r) for r in c.execute(
            "SELECT * FROM sim_trades WHERE status='OPEN' ORDER BY openedAt")]


def sim_trades_where(strategy=None, opened_date=None):
    q, args = "SELECT * FROM sim_trades WHERE 1=1", []
    if strategy is not None:
        q += " AND strategy=?"
        args.append(strategy)
    if opened_date is not None:
        q += " AND openedDate=?"
        args.append(opened_date)
    q += " ORDER BY openedAt"
    with _conn() as c:
        return [_row_to_trade(r) for r in c.execute(q, args)]


def sim_trade_count():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM sim_trades").fetchone()["n"]


def sim_clear():
    with _conn() as c:
        c.execute("DELETE FROM sim_trades")


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
