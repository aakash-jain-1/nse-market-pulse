"""
Unit tests for ideas_journal.py — the durable Ideas-tab memory.

_move_pct (signed, in the idea's favour), _age_min, _key, the sticky coarse
_resolve_outcome (first-touch TARGET/STOP, no overwrite), and the DB-backed
enrich() (freeze entry+firstSeenAt on first sight, re-price, track MFE/MAE,
resolve outcome, sort fresh-first) + history()/day_ideas()/recent() views.

Runs against a throwaway market.db; the intrabar background pass is disabled so
enrich() never touches the network.

Run: python test_ideas_journal.py   (also works under pytest)
"""

import contextlib
import gc
import os
import shutil
import tempfile
from datetime import datetime, timedelta

import db
import ideas_journal as ij


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _temp_db():
    d = tempfile.mkdtemp(prefix="nse_ideas_test_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = d
    db.DB_FILE = os.path.join(d, "market.db")
    db._initialized = False
    db.init()
    try:
        # keep enrich()'s background candle pass from spawning / hitting network
        with _patch(ij, "_intrabar_due", lambda: False), \
             _patch(ij, "_migrate_json_once", lambda: None):
            yield
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)


def _idea(symbol, direction="LONG", entry=100.0, ltp=100.0):
    return {"symbol": symbol, "direction": direction, "entry": entry, "ltp": ltp,
            "stop": 98.0, "target": 104.0, "stopPct": 2.0, "targetPct": 4.0,
            "rr": 2.0, "conviction": 70, "rating": "High", "reasons": ["r"], "fno": True}


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_move_pct_direction():
    assert ij._move_pct("LONG", 100, 105) == 5.0
    assert ij._move_pct("SHORT", 100, 95) == 5.0      # short profits on drop
    assert ij._move_pct("LONG", 100, 95) == -5.0
    assert ij._move_pct("LONG", None, 105) is None
    assert ij._move_pct("LONG", 100, None) is None


def test_key():
    assert ij._key("ACME", "LONG") == "ACME|LONG"


def test_age_min():
    first = (datetime.now(ij.IST) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert 4 <= ij._age_min(first) <= 6
    assert ij._age_min("garbage") is None


def test_resolve_outcome_target_stop_sticky():
    rec = {"entry": 100, "targetPct": 4, "stopPct": 2}
    ij._resolve_outcome(rec, 5.0, "t")
    assert rec["outcome"] == "TARGET" and rec["outcomePct"] == 5.0
    # sticky: a later stop-side move must NOT overwrite
    ij._resolve_outcome(rec, -3.0, "t2")
    assert rec["outcome"] == "TARGET"

    rec2 = {"entry": 100, "targetPct": 4, "stopPct": 2}
    ij._resolve_outcome(rec2, -3.0, "t")
    assert rec2["outcome"] == "STOP" and rec2["outcomePct"] == -3.0

    rec3 = {"entry": 100, "targetPct": 4, "stopPct": 2}
    ij._resolve_outcome(rec3, None, "t")
    assert "outcome" not in rec3      # no move → untouched


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------
def test_enrich_freezes_entry_and_returns_fresh():
    with _temp_db():
        longs, shorts = ij.enrich([_idea("A")], [])
        assert len(longs) == 1 and shorts == []
        rec = longs[0]
        assert rec["entry"] == 100.0 and rec["fresh"] is True
        assert rec["movePct"] == 0.0 and rec["firstSeenAt"]
        assert db.ideas_for_day(ij._today())[0]["symbol"] == "A"


def test_enrich_tracks_move_and_resolves_target():
    with _temp_db():
        ij.enrich([_idea("A", ltp=100.0)], [])
        # price rips past the +4% target
        longs, _ = ij.enrich([_idea("A", ltp=106.0)], [])
        rec = longs[0]
        assert rec["movePct"] == 6.0 and rec["maxMovePct"] == 6.0
        assert rec["outcome"] == "TARGET"


def test_enrich_uses_price_fn_for_tracked_idea():
    with _temp_db():
        ij.enrich([_idea("A")], [])            # first sight, fresh
        # next poll: A is NOT in the fresh set → re-priced via price_fn
        longs, _ = ij.enrich([], [], price_fn=lambda s: 97.0)
        rec = longs[0]
        assert rec["fresh"] is False
        assert rec["movePct"] == -3.0
        assert rec["outcome"] == "STOP"        # -3% <= -2% stop


def test_enrich_sorts_fresh_first():
    with _temp_db():
        ij.enrich([_idea("OLD")], [])          # becomes tracked next poll
        longs, _ = ij.enrich([_idea("NEW")], [], price_fn=lambda s: 100.0)
        assert longs[0]["symbol"] == "NEW" and longs[0]["fresh"] is True
        assert longs[1]["symbol"] == "OLD" and longs[1]["fresh"] is False


# ---------------------------------------------------------------------------
# history / day_ideas / recent
# ---------------------------------------------------------------------------
def test_history_hitrate():
    with _temp_db():
        day = "2026-07-15"
        db.ideas_upsert([
            {"day": day, "symbol": "A", "direction": "LONG", "outcome": "TARGET",
             "firstSeenAt": f"{day}T09:20:00", "maxMovePct": 4, "minMovePct": 0, "movePct": 4},
            {"day": day, "symbol": "B", "direction": "LONG", "outcome": "STOP",
             "firstSeenAt": f"{day}T09:21:00", "maxMovePct": 0, "minMovePct": -2, "movePct": -2},
        ])
        h = ij.history()
        d0 = h["days"][0]
        assert d0["day"] == day and d0["resolved"] == 2 and d0["hitRate"] == 50.0
        assert h["stats"]["ideas"] == 2


def test_day_ideas():
    with _temp_db():
        day = ij._today()
        db.ideas_upsert([{"day": day, "symbol": "A", "direction": "LONG",
                          "firstSeenAt": f"{day} 09:20:00"}])
        out = ij.day_ideas(day)
        assert out["date"] == day and out["count"] == 1 and out["ideas"][0]["symbol"] == "A"


def test_recent_window_and_rating_filter():
    with _temp_db():
        day = ij._today()
        now = datetime.now(ij.IST)
        fresh_ts = now.strftime("%Y-%m-%d %H:%M:%S")
        old_ts = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        db.ideas_upsert([
            {"day": day, "symbol": "FRESH", "direction": "LONG",
             "firstSeenAt": fresh_ts, "rating": "High"},
            {"day": day, "symbol": "OLD", "direction": "LONG",
             "firstSeenAt": old_ts, "rating": "High"},
            {"day": day, "symbol": "LOWRATE", "direction": "LONG",
             "firstSeenAt": fresh_ts, "rating": "Low"},
        ])
        # 60-min window drops the 3-hour-old idea
        win = ij.recent(window_min=60)
        syms = {r["symbol"] for r in win["ideas"]}
        assert "OLD" not in syms and "FRESH" in syms
        # min rating Medium drops the Low-rated idea
        hi = ij.recent(window_min=0, min_rating="Medium")
        syms2 = {r["symbol"] for r in hi["ideas"]}
        assert "LOWRATE" not in syms2 and "FRESH" in syms2


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} ideas_journal tests passed")


if __name__ == "__main__":
    _main()
