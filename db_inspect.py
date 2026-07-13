"""
db_inspect.py — peek into data/market.db without any GUI or CLI install
=======================================================================
This machine has no `sqlite3` CLI, so this is the zero-install way to look at
what the app has stored. It opens the DB **read-only** (mode=ro), so it's safe
to run while `app.py` is live — it can never lock or mutate anything.

Usage
-----
  python db_inspect.py                     # overview: every table, row count, time span
  python db_inspect.py <table> [N]         # last N rows of a table (default 20) + schema
  python db_inspect.py sql "SELECT ..."    # run an arbitrary read-only query

Examples
  python db_inspect.py ideas 10
  python db_inspect.py sql "SELECT symbol,outcome,movePct FROM ideas WHERE day='2026-07-13'"
  python db_inspect.py sql "SELECT strategy,COUNT(*) FROM sim_trades GROUP BY strategy"
"""

import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta

import db  # reuse the canonical DB_FILE path

# Windows consoles default to cp1252; force UTF-8 so any symbol/name prints fine.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))
MAXW = 24  # truncate cell display to this many chars

# One-line description per table (mirrors db.py's module docstring).
DESCRIPTIONS = {
    "snapshots": "demand / volume-gainers board — one row per symbol per capture",
    "iv_log": "ATM implied-volatility captures",
    "context_log": "gzipped full strategy context per cycle (offline backtest replay)",
    "sim_trades": "the multi-strategy sim ledger (cash + fno books)",
    "eod_bars": "daily OHLCV + delivery% history (daily-backtest cache)",
    "eod_oi": "daily futures OI history (daily-backtest cache)",
    "eod_meta": "EOD cache freshness bookkeeping",
    "min_bars": "cached 1-min OHLCV candles (intrabar-accurate resolution)",
    "ideas": "ideas journal — every idea shown, entry+timestamp frozen, outcome tracked",
}
# The column to sort/tail by + report a span for (per table).
TIME_COL = {
    "snapshots": "ts", "iv_log": "ts", "context_log": "ts", "sim_trades": "openedAt",
    "eod_bars": "d", "eod_oi": "d", "eod_meta": "fetched_at", "min_bars": "t",
    "ideas": "day",
}


def _connect():
    path = db.DB_FILE
    if not os.path.exists(path):
        print(f"No DB yet at {path}\nRun the app first (python app.py) so it can create + populate it.")
        sys.exit(1)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only: safe while live
    con.row_factory = sqlite3.Row
    return con, path


def _tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _span(con, table):
    col = TIME_COL.get(table)
    if not col:
        return ""
    try:
        r = con.execute(f"SELECT MIN({col}) a, MAX({col}) b FROM {table}").fetchone()
    except sqlite3.Error:
        return ""
    a, b = r["a"], r["b"]
    if a is None:
        return ""
    if table == "min_bars":  # epoch-ms (IST baked as UTC) → readable IST wall clock
        a = datetime.fromtimestamp(a / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        b = datetime.fromtimestamp(b / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    else:
        a, b = str(a)[:16], str(b)[:16]
    return f"{a}  ->  {b}" if a != b else a


def _cell(v):
    if isinstance(v, (bytes, bytearray)):
        return f"<blob {len(v)}B>"
    s = "" if v is None else str(v)
    return s if len(s) <= MAXW else s[:MAXW - 3] + "..."


def _print_rows(rows):
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    data = [[_cell(r[c]) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in data)) for i, c in enumerate(cols)]
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(header)
    print("-" * len(header))
    for row in data:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(cols))))


def overview():
    con, path = _connect()
    size = os.path.getsize(path) / 1e6
    tabs = _tables(con)
    w = max((len(t) for t in tabs), default=5)
    print(f"DB: {path}   ({size:.1f} MB, {len(tabs)} tables)\n")
    print(f"{'table'.ljust(w)}  {'rows':>12}  span")
    print("-" * (w + 50))
    for t in tabs:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t.ljust(w)}  {n:>12,}  {_span(con, t)}")
    print()
    for t in tabs:
        if t in DESCRIPTIONS:
            print(f"  {t.ljust(w)}  {DESCRIPTIONS[t]}")
    print('\nMore:  python db_inspect.py <table> [N]   |   '
          'python db_inspect.py sql "SELECT ..."')


def dump(table, limit=20):
    con, _ = _connect()
    tabs = _tables(con)
    if table not in tabs:
        print(f"No such table: {table}\nAvailable: {', '.join(tabs)}")
        sys.exit(1)
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    schema = [c["name"] for c in con.execute(f"PRAGMA table_info({table})")]
    print(f"{table}: {n:,} rows  ({len(schema)} cols: {', '.join(schema)})")
    span = _span(con, table)
    if span:
        print(f"span: {span}")
    print()
    tcol = TIME_COL.get(table)
    if tcol:  # tail: newest N, shown oldest→newest
        rows = list(con.execute(
            f"SELECT * FROM {table} ORDER BY {tcol} DESC LIMIT {int(limit)}"))[::-1]
        print(f"(last {min(int(limit), n)} by {tcol})")
    else:
        rows = list(con.execute(f"SELECT * FROM {table} LIMIT {int(limit)}"))
        print(f"(first {min(int(limit), n)})")
    _print_rows(rows)


def run_sql(query):
    con, _ = _connect()
    q = query.strip()
    if not q.lower().startswith(("select", "with", "pragma", "explain")):
        print("Read-only tool: only SELECT / WITH / PRAGMA / EXPLAIN are allowed.")
        sys.exit(1)
    try:
        rows = list(con.execute(q))
    except sqlite3.Error as e:
        print("SQL error:", e)
        sys.exit(1)
    print(f"{len(rows)} row(s)\n")
    _print_rows(rows)


def main(argv):
    if len(argv) <= 1:
        overview()
        return
    cmd = argv[1]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    if cmd == "sql":
        if len(argv) < 3:
            print('Usage: python db_inspect.py sql "SELECT ..."')
            return
        run_sql(argv[2])
        return
    limit = int(argv[2]) if len(argv) > 2 and argv[2].lstrip("-").isdigit() else 20
    dump(cmd, limit)


if __name__ == "__main__":
    main(sys.argv)
