"""
End-to-end tests for sim.take() — dedupe, regime tagging, book separation,
entry-mode gating and the conviction limit (AUDIT.md L8 tail).

Unlike the pure-math tests in test_sim.py, these drive the REAL take() path
against a throwaway SQLite DB + JSON state, with a fake `strat` module so no
network is touched. The fixture repoints db.DB_FILE / sim.STATE_FILE at a temp
dir and restores everything (incl. db._initialized) on exit.

NOTE: mutates process-global module state (db path, sim.strat), so run SERIALLY.

Run: python test_take.py   (also works under pytest, single-process)
"""

import gc
import os
import shutil
import tempfile
import types

import db
import sim


class _TempSim:
    """Point db + sim at a throwaway temp dir with a fake strategy set."""
    def __init__(self, generate, strategies=None):
        self.generate = generate
        self.strategies = strategies or [{"id": "momentum", "name": "Momentum"}]

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="nse_take_test_")
        self._db = (db.DATA_DIR, db.DB_FILE, db._initialized)
        self._sim = (sim.STATE_FILE, sim._migrated, sim.strat)

        db.DATA_DIR = self.dir
        db.DB_FILE = os.path.join(self.dir, "market.db")
        db._initialized = False
        db.init()                                   # build schema in the temp DB

        sim.STATE_FILE = os.path.join(self.dir, "sim_state.json")
        sim._migrated = False
        sim.strat = types.SimpleNamespace(STRATEGIES=self.strategies,
                                           generate=self.generate)
        return self

    def __exit__(self, *a):
        db.DATA_DIR, db.DB_FILE, _ = self._db
        db._initialized = False                     # force real re-init next prod call
        sim.STATE_FILE, sim._migrated, sim.strat = self._sim
        gc.collect()                                # release temp-DB connections (Windows lock)
        shutil.rmtree(self.dir, ignore_errors=True)


def _idea(symbol, direction="LONG", conviction=5, entry=100.0, fno=False):
    stop = entry * (0.98 if direction == "LONG" else 1.02)
    target = entry * (1.06 if direction == "LONG" else 0.94)
    return {"symbol": symbol, "direction": direction, "conviction": conviction,
            "rating": "High", "entry": entry, "ltp": entry,
            "stop": round(stop, 2), "target": round(target, 2),
            "stopPct": 2.0, "targetPct": 6.0, "rr": 3.0, "fno": fno,
            "reasons": ["test"]}


def _ctx(label="Trend Up"):
    return {"regime": {"label": label}}


def test_take_creates_and_dedupes():
    with _TempSim(lambda sid, ctx: [_idea("AAA")]):
        added = sim.take(ctx=_ctx())
        assert added["momentum"] == 1
        assert db.sim_trade_count() == 1
        # same idea, same day -> deduped (no second row)
        assert sim.take(ctx=_ctx())["momentum"] == 0
        assert db.sim_trade_count() == 1
        t = db.sim_all_trades()[0]
        assert t["symbol"] == "AAA" and t["direction"] == "LONG"
        assert t["status"] == "OPEN"


def test_take_regime_tagging():
    with _TempSim(lambda sid, ctx: [_idea("BBB")]):
        sim.take(ctx=_ctx("High Vol"))
        assert db.sim_all_trades()[0]["regimeAtEntry"] == "High Vol"


def test_dedupe_survives_close():
    # Continuous mode must not instantly re-enter a setup the moment it closes:
    # dedupe is against everything opened TODAY, any status.
    with _TempSim(lambda sid, ctx: [_idea("CCC")]):
        sim.take(ctx=_ctx())
        tr = db.sim_all_trades()[0]
        tr["status"], tr["closedDay"] = "CLOSED", sim._today()
        db.sim_insert_trades([tr])
        assert sim.take(ctx=_ctx())["momentum"] == 0
        assert db.sim_trade_count() == 1


def test_long_and_short_same_symbol():
    gen = lambda sid, ctx: [_idea("DDD", "LONG"), _idea("DDD", "SHORT")]
    with _TempSim(gen):
        assert sim.take(ctx=_ctx())["momentum"] == 2
        assert {t["direction"] for t in db.sim_all_trades()} == {"LONG", "SHORT"}


def test_dedupe_is_per_strategy():
    strategies = [{"id": "momentum", "name": "M"}, {"id": "meanrev", "name": "MR"}]
    with _TempSim(lambda sid, ctx: [_idea("ZZZ")], strategies=strategies):
        assert sim.take(ctx=_ctx()) == {"momentum": 1, "meanrev": 1}
        assert db.sim_trade_count() == 2      # same symbol allowed across strategies


def test_open_mode_once_per_day():
    calls = {"n": 0}

    def gen(sid, ctx):
        calls["n"] += 1
        return [_idea("E1" if calls["n"] == 1 else "E2")]   # a fresh idea each call

    with _TempSim(gen):
        st = sim._load(); st["entryMode"] = "open"; sim._save(st)
        assert sim.take(auto=True, ctx=_ctx())["momentum"] == 1
        assert sim.take(auto=True, ctx=_ctx())["momentum"] == 0   # auto gated to once/day
        assert db.sim_trade_count() == 1
        assert calls["n"] == 1                # gate short-circuits BEFORE generate()
        # a MANUAL take bypasses the open-mode gate and takes the fresh idea
        assert sim.take(auto=False, ctx=_ctx())["momentum"] == 1
        assert db.sim_trade_count() == 2


def test_fno_book_filters_and_separates():
    gen = lambda sid, ctx: [_idea("FUTX", fno=True), _idea("CASHY", fno=False)]
    with _TempSim(gen):
        assert sim.take(ctx=_ctx(), book="fno")["momentum"] == 1     # only the fno idea
        assert [t["symbol"] for t in db.sim_all_trades(book="fno")] == ["FUTX"]
        assert sim.take(ctx=_ctx(), book="cash")["momentum"] == 2    # both, separate book
        assert db.sim_trade_count(book="cash") == 2
        assert db.sim_trade_count(book="fno") == 1


def test_limit_takes_top_conviction():
    ideas = [_idea(f"S{i}", conviction=i) for i in range(1, 6)]      # S1..S5, conv 1..5
    with _TempSim(lambda sid, ctx: list(ideas)):
        assert sim.take(ctx=_ctx(), limit=2)["momentum"] == 2
        assert {t["symbol"] for t in db.sim_all_trades()} == {"S5", "S4"}


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} take() tests passed")


if __name__ == "__main__":
    _main()
