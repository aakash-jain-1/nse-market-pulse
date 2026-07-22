"""
Unit tests for db.py — the SQLite time-series + ledger store.

Every test runs against a fresh throwaway market.db (DATA_DIR/DB_FILE are
repointed at a temp dir and db.init() re-run), so nothing touches the real
data/market.db. Covers snapshots, IV log, alert dedupe, context log (gzip
round-trip + regime_by_day last-write-wins), the sim-trades ledger (insert/query/
filter/clear + reasons & fno round-trip + book default), the EOD bar/OI/meta
cache, minute bars, the ideas journal (+ day aggregation), retention (prunes the
reproducible logs, KEEPS the durable ledgers), and _num_or_none.

Run: python test_db.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile
from datetime import datetime, timedelta

from nse_pulse.core import db


@contextlib.contextmanager
def _temp_db():
    d = tempfile.mkdtemp(prefix="nse_db_test_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = d
    db.DB_FILE = os.path.join(d, "market.db")
    db._initialized = False
    db.init()
    try:
        yield
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved
        gc.collect()   # release lingering sqlite handles so Windows can rmtree
        shutil.rmtree(d, ignore_errors=True)


def _now_ist():
    return datetime.now(db.IST)


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------
def test_snapshots_insert_query_stats():
    with _temp_db():
        rows = [
            {"ts": "2026-07-16 09:20:00", "view": "demand", "rank": 1, "symbol": "A",
             "ltp": "100", "pChange": "2", "score": "9.5", "signalCount": "3",
             "volMult": "10", "week1volChange": "10", "volume": "999", "value": "1e7"},
            {"ts": "2026-07-16 09:21:00", "view": "demand", "rank": 1, "symbol": "A",
             "ltp": "101", "pChange": "2.5"},
        ]
        assert db.insert_snapshots(rows) == 2
        got = db.snapshot_rows("demand")
        assert len(got) == 2 and got[0]["symbol"] == "A" and got[0]["ltp"] == 100.0
        st = db.snapshot_stats()
        assert st["total"] == 2 and st["distinct"] == 2
        assert st["first"] == "2026-07-16 09:20:00"


def test_snapshots_export_csv():
    with _temp_db():
        db.insert_snapshots([{"ts": "t1", "view": "demand", "symbol": "A", "ltp": "1"}])
        out = os.path.join(db.DATA_DIR, "x.csv")
        db.export_snapshots_csv(out)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        assert "symbol" in text and "A" in text


def test_insert_snapshots_empty():
    with _temp_db():
        assert db.insert_snapshots([]) == 0


# ---------------------------------------------------------------------------
# iv log
# ---------------------------------------------------------------------------
def test_iv_insert_series_stats():
    with _temp_db():
        db.insert_iv([
            {"ts": "t1", "symbol": "NIFTY", "atmIV": 14.0, "pcr": 0.9},
            {"ts": "t2", "symbol": "NIFTY", "atmIV": 15.0, "pcr": 1.0},
        ])
        assert db.iv_series("NIFTY") == [("t1", 14.0), ("t2", 15.0)]
        st = db.iv_stats()
        assert st["snapshots"] == 2 and st["symbols"] == 1 and st["last"] == "t2"


# ---------------------------------------------------------------------------
# alert dedupe
# ---------------------------------------------------------------------------
def test_alert_seen_mark():
    with _temp_db():
        assert db.alert_seen("k1") is False
        db.alert_mark("k1", "idea", "ACME")
        assert db.alert_seen("k1") is True
        db.alert_mark("k1", "idea", "ACME")   # INSERT OR IGNORE — no error
        assert db.alert_seen("k2") is False


# ---------------------------------------------------------------------------
# context log
# ---------------------------------------------------------------------------
def test_context_roundtrip_and_regime_by_day():
    with _temp_db():
        payload = {"scanner": [{"symbol": "A"}], "n": 1}
        db.insert_context("2026-07-16 09:20:00", "2026-07-16", "Trend-Up", 0.8, payload)
        db.insert_context("2026-07-16 15:20:00", "2026-07-16", "Range", 0.1, {"x": 2})
        cycles = db.context_cycles()
        assert len(cycles) == 2 and cycles[0]["ctx"] == payload
        st = db.context_stats()
        assert st["cycles"] == 2 and st["days"] == 1
        # last-write-wins for the day's regime
        assert db.regime_by_day()["2026-07-16"]["label"] == "Range"


def test_context_cycles_since_day_filter():
    with _temp_db():
        db.insert_context("2026-07-10 09:20:00", "2026-07-10", "Range", 0.0, {"a": 1})
        db.insert_context("2026-07-16 09:20:00", "2026-07-16", "Range", 0.0, {"a": 2})
        got = db.context_cycles(since_day="2026-07-15")
        assert len(got) == 1 and got[0]["day"] == "2026-07-16"


# ---------------------------------------------------------------------------
# sim trades ledger
# ---------------------------------------------------------------------------
def _trade(tid, status="OPEN", book="cash", strategy="momentum",
           opened_date="2026-07-16", **kw):
    t = {"id": tid, "book": book, "strategy": strategy, "symbol": "A",
         "direction": "LONG", "status": status, "openedAt": f"2026-07-16T09:2{tid}:00",
         "openedDate": opened_date, "reasons": ["r1", "r2"], "fno": True,
         "entry": 100.0, "qty": 10}
    t.update(kw)
    return t


def test_sim_insert_and_roundtrip():
    with _temp_db():
        assert db.sim_insert_trades([_trade("1")]) == 1
        t = db.sim_all_trades()[0]
        assert t["id"] == "1" and t["reasons"] == ["r1", "r2"] and t["fno"] is True
        assert t["book"] == "cash" and t["entry"] == 100.0


def test_sim_vol_at_entry_roundtrip():
    with _temp_db():
        db.sim_insert_trades([_trade("1", regimeAtEntry="Trend-Up", volAtEntry="Elevated"),
                              _trade("2")])                    # no volAtEntry supplied
        rows = {t["id"]: t for t in db.sim_all_trades()}
        assert rows["1"]["volAtEntry"] == "Elevated"
        assert rows["2"]["volAtEntry"] is None                # column present, NULL default


def test_sim_book_default_when_missing():
    with _temp_db():
        tr = _trade("1")
        tr.pop("book")
        db.sim_insert_trades([tr])
        assert db.sim_all_trades()[0]["book"] == "cash"


def test_sim_open_and_where_filters():
    with _temp_db():
        db.sim_insert_trades([
            _trade("1", status="OPEN", strategy="momentum"),
            _trade("2", status="TARGET", strategy="momentum"),
            _trade("3", status="OPEN", strategy="vwap", book="fno"),
        ])
        assert {t["id"] for t in db.sim_open_trades()} == {"1", "3"}
        assert {t["id"] for t in db.sim_open_trades(book="fno")} == {"3"}
        assert {t["id"] for t in db.sim_trades_where(strategy="momentum")} == {"1", "2"}
        assert db.sim_trade_count() == 3
        assert db.sim_trade_count(book="fno") == 1


def test_sim_clear_by_book_and_all():
    with _temp_db():
        db.sim_insert_trades([_trade("1", book="cash"), _trade("2", book="fno")])
        db.sim_clear(book="fno")
        assert db.sim_trade_count() == 1 and db.sim_trade_count(book="cash") == 1
        db.sim_clear()
        assert db.sim_trade_count() == 0


def test_sim_insert_updates_by_id():
    with _temp_db():
        db.sim_insert_trades([_trade("1", status="OPEN")])
        db.sim_insert_trades([_trade("1", status="TARGET", pnl=50.0)])
        rows = db.sim_all_trades()
        assert len(rows) == 1 and rows[0]["status"] == "TARGET" and rows[0]["pnl"] == 50.0


# ---------------------------------------------------------------------------
# EOD cache
# ---------------------------------------------------------------------------
def test_eod_bars_oi_meta():
    with _temp_db():
        db.eod_bars_put("acme", [{"d": "2026-07-15", "close": "100", "volume": "5"},
                                 {"d": "2026-07-16", "close": "101"},
                                 {"close": "999"}])  # no 'd' → skipped
        bars = db.eod_bars_get("ACME")
        assert len(bars) == 2 and bars[0]["d"] == "2026-07-15" and bars[1]["close"] == 101.0
        db.eod_oi_put("acme", "31-jul-2026", [{"d": "2026-07-16", "oi": "1000"}])
        assert db.eod_oi_get("ACME", "31-JUL-2026")[0]["oi"] == 1000.0
        db.eod_meta_set("ACME", "bars", "2026-07-16T18:00", "2026-07-16", 2)
        assert db.eod_meta_get("ACME", "bars")["n"] == 2
        stats = db.eod_stats()
        assert stats["bars"]["rows"] == 2 and stats["oi"]["rows"] == 1
        db.eod_clear()
        assert db.eod_bars_get("ACME") == []


def test_eod_bars_put_bulk():
    with _temp_db():
        n = db.eod_bars_put_bulk([
            {"symbol": "acme", "d": "2026-07-16", "close": "100", "volume": "5"},
            {"symbol": "BETA", "d": "2026-07-16", "close": "200"},
            {"symbol": "NOD", "close": "9"},        # no 'd' → skipped
            {"d": "2026-07-16", "close": "1"},       # no symbol → skipped
        ])
        assert n == 2
        assert db.eod_bars_get("ACME")[0]["close"] == 100.0   # symbol upper-cased
        assert db.eod_bars_get("BETA")[0]["close"] == 200.0
        # Re-ingesting the same day REPLACEs (immutable past, revised newest).
        assert db.eod_bars_put_bulk(
            [{"symbol": "ACME", "d": "2026-07-16", "close": "111"}]) == 1
        assert db.eod_bars_get("ACME")[0]["close"] == 111.0
        assert db.eod_bars_put_bulk([]) == 0


def test_eod_bars_all_grouped_and_since():
    with _temp_db():
        db.eod_bars_put("ACME", [{"d": "2026-06-01", "close": 90},
                                 {"d": "2026-07-15", "close": 100}])
        db.eod_bars_put("BETA", [{"d": "2026-07-15", "close": 200}])
        allb = db.eod_bars_all()
        assert set(allb) == {"ACME", "BETA"}
        assert [r["close"] for r in allb["ACME"]] == [90.0, 100.0]  # ascending by d
        # `since` trims older rows (and can drop a symbol entirely).
        recent = db.eod_bars_all(since="2026-07-01")
        assert [r["close"] for r in recent["ACME"]] == [100.0]
        assert db.eod_bars_all(since="2027-01-01") == {}


def test_eod_latest_date_and_oi_symbols():
    with _temp_db():
        assert db.eod_latest_date() is None            # empty DB
        assert db.eod_oi_symbols() == []
        db.eod_bars_put("ACME", [{"d": "2026-07-14", "close": 1},
                                 {"d": "2026-07-16", "close": 2}])
        db.eod_bars_put("BETA", [{"d": "2026-07-15", "close": 3}])
        assert db.eod_latest_date() == "2026-07-16"
        db.eod_oi_put("ACME", "31-JUL-2026", [{"d": "2026-07-16", "oi": 5}])
        assert db.eod_oi_symbols() == ["ACME"]         # only names with futures OI


def test_eod_oi_all_grouped_and_since():
    with _temp_db():
        assert db.eod_oi_all() == {}                    # empty DB
        # Two expiries for ACME (a rollover) → one continuous series per symbol.
        db.eod_oi_put("ACME", "31-JUL-2026",
                      [{"d": "2026-07-15", "oi": 100, "changeOi": 10},
                       {"d": "2026-07-30", "oi": 120, "changeOi": 5}])
        db.eod_oi_put("ACME", "28-AUG-2026",
                      [{"d": "2026-08-01", "oi": 130, "changeOi": 8}])
        db.eod_oi_put("BETA", "31-JUL-2026", [{"d": "2026-07-15", "oi": 55, "changeOi": 5}])
        allo = db.eod_oi_all()
        assert set(allo) == {"ACME", "BETA"}
        # ACME rows span BOTH expiries, ascending by date (continuous near-month).
        assert [r["d"] for r in allo["ACME"]] == ["2026-07-15", "2026-07-30", "2026-08-01"]
        # `since` trims older rows across every expiry.
        recent = db.eod_oi_all(since="2026-08-01")
        assert [r["d"] for r in recent["ACME"]] == ["2026-08-01"]
        assert "BETA" not in recent


# ---------------------------------------------------------------------------
# minute bars
# ---------------------------------------------------------------------------
def test_min_bars_put_get_span():
    with _temp_db():
        pts = [{"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
               {"t": 2000, "o": 1.5, "h": 2.5, "l": 1, "c": 2, "v": 200},
               {"t": 3000, "c": 3},
               {"o": 9}]  # no t → skipped
        assert db.min_bars_put("acme", pts) == 3
        assert len(db.min_bars_get("ACME")) == 3
        mid = db.min_bars_get("ACME", from_t=1500, to_t=2500)
        assert len(mid) == 1 and mid[0]["t"] == 2000
        assert db.min_bars_span("ACME") == (1000, 3000, 3)


# ---------------------------------------------------------------------------
# ideas journal
# ---------------------------------------------------------------------------
def _idea(symbol, direction="LONG", day="2026-07-16", outcome=None, **kw):
    d = {"day": day, "symbol": symbol, "direction": direction, "entry": 100.0,
         "conviction": 70, "rating": "High", "reasons": ["x"], "fno": True,
         "firstSeenAt": f"{day}T09:20:00", "maxMovePct": 2.0, "minMovePct": -0.5,
         "movePct": 1.0, "outcome": outcome}
    d.update(kw)
    return d


def test_ideas_upsert_and_query():
    with _temp_db():
        db.ideas_upsert([_idea("A")])
        rows = db.ideas_for_day("2026-07-16")
        assert len(rows) == 1 and rows[0]["reasons"] == ["x"] and rows[0]["fno"] is True
        # upsert same key updates in place
        db.ideas_upsert([_idea("A", outcome="TARGET")])
        assert db.ideas_for_day("2026-07-16")[0]["outcome"] == "TARGET"


def test_ideas_days_aggregation():
    with _temp_db():
        db.ideas_upsert([
            _idea("A", "LONG", outcome="TARGET"),
            _idea("B", "LONG", outcome="STOP"),
            _idea("C", "SHORT"),
        ])
        day = db.ideas_days()[0]
        assert day["n"] == 3 and day["longs"] == 2 and day["shorts"] == 1
        assert day["targets"] == 1 and day["stops"] == 1
        st = db.ideas_stats()
        assert st["ideas"] == 3 and st["days"] == 1


# ---------------------------------------------------------------------------
# retention — prune reproducible logs, keep durable ledgers
# ---------------------------------------------------------------------------
def test_retention_prunes_old_keeps_recent_and_durable():
    with _temp_db():
        now = _now_ist()
        old_ts = (now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        new_ts = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        old_day = (now - timedelta(days=200)).strftime("%Y-%m-%d")
        new_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        db.insert_snapshots([{"ts": old_ts, "view": "demand", "symbol": "OLD"},
                             {"ts": new_ts, "view": "demand", "symbol": "NEW"}])
        db.insert_iv([{"ts": old_ts, "symbol": "NIFTY", "atmIV": 1},
                      {"ts": new_ts, "symbol": "NIFTY", "atmIV": 2}])
        db.insert_context(old_ts, old_day, "Range", 0.0, {"a": 1})
        db.insert_context(new_ts, new_day, "Range", 0.0, {"a": 2})
        db.min_bars_put("A", [
            {"t": int((now.timestamp() - 200 * 86400) * 1000), "c": 1},
            {"t": int((now.timestamp() - 1 * 86400) * 1000), "c": 2},
        ])
        # durable rows that must survive
        db.sim_insert_trades([_trade("1")])
        db.ideas_upsert([_idea("A")])
        db.eod_bars_put("A", [{"d": "2020-01-01", "close": 1}])
        # an old alert row (inserted directly with an old timestamp)
        with db._conn() as c:
            c.execute("INSERT INTO alert_log (key,kind,symbol,fired_at) VALUES (?,?,?,?)",
                      ("old", "vol", "A",
                       (now - timedelta(days=30)).isoformat(timespec="seconds")))

        deleted = db.retention()

        assert deleted["snapshots"] == 1 and deleted["iv_log"] == 1
        assert deleted["context_log"] == 1 and deleted["min_bars"] == 1
        assert deleted["alert_log"] == 1
        assert [r["symbol"] for r in db.snapshot_rows("demand")] == ["NEW"]
        # durable ledgers untouched
        assert db.sim_trade_count() == 1
        assert db.ideas_stats()["ideas"] == 1
        assert len(db.eod_bars_get("A")) == 1


def test_retention_skips_disabled_windows():
    with _temp_db():
        old_ts = (_now_ist() - timedelta(days=500)).strftime("%Y-%m-%d %H:%M:%S")
        db.insert_snapshots([{"ts": old_ts, "view": "demand", "symbol": "OLD"}])
        deleted = db.retention(snapshots_days=0)   # 0 → skip snapshots
        assert "snapshots" not in deleted
        assert db.snapshot_stats()["total"] == 1


# ---------------------------------------------------------------------------
# _num_or_none
# ---------------------------------------------------------------------------
def test_num_or_none():
    assert db._num_or_none("") is None
    assert db._num_or_none(None) is None
    assert db._num_or_none("3.5") == 3.5
    assert db._num_or_none("x") is None
    assert db._num_or_none(5) == 5.0


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} db tests passed")


if __name__ == "__main__":
    _main()
