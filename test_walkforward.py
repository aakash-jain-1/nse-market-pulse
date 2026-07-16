"""
Unit tests for walkforward.py — out-of-sample / overfit validation.

The whole report is a PURE function over the daily backtest's trade list, so we
build synthetic trades with a KNOWN in-sample→out-of-sample story and assert the
verdicts: expectancy, best-per-regime learning, the a-priori map, the playbook
filter, the per-strategy overfit verdict bands, the adaptive-vs-fixed verdict,
fold splitting, and the full analyze() wiring (holdout + folds + adaptive). run()
is checked with backtest_daily.run stubbed (no network).

Run: python test_walkforward.py   (also works under pytest)
"""

import contextlib

import backtest_daily as bd
import walkforward as wf


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _t(strategy, status, r, opened, regime="Trend-Up"):
    return {"strategy": strategy, "status": status, "rMultiple": r,
            "openedDate": opened, "regimeAtEntry": regime,
            "pnlPct": r * 2.0, "symbol": "X", "direction": "LONG"}


# ---------------------------------------------------------------------------
# _expectancy / _closed
# ---------------------------------------------------------------------------
def test_expectancy():
    ts = [_t("m", "TARGET", 2.0, "d1"), _t("m", "STOP", -1.0, "d1"),
          _t("m", "EXPIRED", 0.5, "d1"), _t("m", "OPEN", 0.0, "d1")]
    e = wf._expectancy(ts)
    assert e["n"] == 3 and e["expectancyR"] == 0.5      # (2-1+0.5)/3, OPEN ignored
    assert e["winRate"] == round(1 / 3 * 100, 1) and e["totalR"] == 1.5
    assert wf._expectancy([])["expectancyR"] is None


# ---------------------------------------------------------------------------
# _best_per_regime / _best_overall / _apriori_map / _apply_playbook
# ---------------------------------------------------------------------------
def test_best_per_regime_respects_min_samples():
    trades = ([_t("momentum", "TARGET", 2.0, "d1")] * 3 +
              [_t("meanrev", "TARGET", 0.5, "d1")] * 3 +
              [_t("delivery", "TARGET", 9.0, "d1")])          # only 1 sample → ignored
    best = wf._best_per_regime(trades, min_samples=3)
    assert best == {"Trend-Up": "momentum"}                  # highest exp with enough n


def test_best_overall():
    trades = [_t("momentum", "TARGET", 2.0, "d1"), _t("momentum", "TARGET", 2.0, "d1"),
              _t("meanrev", "STOP", -1.0, "d1"), _t("meanrev", "STOP", -1.0, "d1")]
    assert wf._best_overall(trades, min_samples=2) == "momentum"
    assert wf._best_overall(trades, min_samples=5) is None    # not enough samples


def test_apriori_map_uses_design():
    m = wf._apriori_map([s["id"] for s in bd.STRATS])
    # momentum's declared regimeFit includes Trend-Up and it's first in STRATS
    assert m["Trend-Up"] == "momentum"


def test_apply_playbook_filters():
    test = [_t("momentum", "TARGET", 2.0, "d1", "Trend-Up"),
            _t("meanrev", "TARGET", 2.0, "d1", "Trend-Up"),
            _t("momentum", "STOP", -1.0, "d1", "Range")]
    kept = wf._apply_playbook(test, {"Trend-Up": "momentum"})
    assert len(kept) == 1 and kept[0]["strategy"] == "momentum"
    assert kept[0]["regimeAtEntry"] == "Trend-Up"


# ---------------------------------------------------------------------------
# verdict bands
# ---------------------------------------------------------------------------
def test_verdict_bands():
    assert wf._verdict(0.5, 0.2, oos_n=2, min_test=8) == "insufficient"
    assert wf._verdict(1.0, 0.8, 20, 8) == "robust"
    assert wf._verdict(1.0, 0.3, 20, 8) == "decaying"
    assert wf._verdict(0.5, -0.2, 20, 8) == "overfit"
    assert wf._verdict(-0.1, -0.2, 20, 8) == "no-edge"
    assert wf._verdict(-0.1, 0.3, 20, 8) == "improving"


def test_adaptive_verdict():
    good = {"n": 20, "expectancyR": 0.5, "winRate": 55}
    weak = {"n": 20, "expectancyR": 0.2, "winRate": 45}
    assert wf._adaptive_verdict(good, 0.3, 8) == "adds-value"
    assert wf._adaptive_verdict(weak, 0.5, 8) == "no-better-than-fixed"
    assert wf._adaptive_verdict(good, None, 8) == "adds-value"
    assert wf._adaptive_verdict({"n": 2, "expectancyR": 9.0}, 0.1, 8) == "insufficient"


# ---------------------------------------------------------------------------
# _split_folds
# ---------------------------------------------------------------------------
def test_split_folds():
    days = [f"d{i}" for i in range(8)]
    chunks = wf._split_folds(days, 4)
    assert len(chunks) == 4 and all(len(c) == 2 for c in chunks)
    assert [d for c in chunks for d in c] == days          # covers every day once
    assert len(wf._split_folds(["a", "b"], 5)) == 2        # folds clamped to len


# ---------------------------------------------------------------------------
# analyze — end to end on a known IS→OOS story
# ---------------------------------------------------------------------------
def _dataset():
    days = [f"2026-07-{d:02d}" for d in range(1, 11)]       # 10 trading days
    trades = []
    for d in days[:6]:                                     # train block
        trades += [_t("momentum", "TARGET", 2.0, d),
                   _t("meanrev", "TARGET", 1.0, d)]
    for d in days[6:]:                                     # out-of-sample block
        trades += [_t("momentum", "TARGET", 2.0, d),       # momentum holds up
                   _t("meanrev", "STOP", -1.0, d)]          # meanrev collapses
    return trades


def test_analyze_flags_overfit_and_robust():
    res = wf.analyze(_dataset(), folds=3, train_frac=0.6,
                     min_test=2, min_regime_samples=2)
    assert res["ok"] is True
    ps = {r["id"]: r for r in res["perStrategy"]}
    assert ps["momentum"]["isExpectancyR"] == 2.0 and ps["momentum"]["oosExpectancyR"] == 2.0
    assert ps["momentum"]["verdict"] == "robust"
    assert ps["meanrev"]["isExpectancyR"] == 1.0 and ps["meanrev"]["oosExpectancyR"] == -1.0
    assert ps["meanrev"]["verdict"] == "overfit"
    assert res["perStrategy"][0]["id"] == "momentum"       # sorted by OOS desc


def test_analyze_adaptive_and_folds():
    res = wf.analyze(_dataset(), folds=3, train_frac=0.6,
                     min_test=2, min_regime_samples=2)
    ad = res["adaptive"]
    assert ad["playbook"]["Trend-Up"]["id"] == "momentum"  # learned the winner
    assert ad["oosExpectancyR"] == 2.0                     # following it OOS
    assert ad["bestFixed"]["id"] == "momentum"
    assert ad["verdict"] == "adds-value"
    wfld = res["walkForward"]
    assert len(wfld["folds"]) == 2 and wfld["adaptivePooledN"] > 0


def test_analyze_insufficient():
    res = wf.analyze([_t("momentum", "TARGET", 2.0, "2026-07-01")],
                     min_test=8)
    assert res["ok"] is False and "closed" in res


# ---------------------------------------------------------------------------
# run — backtest_daily.run stubbed
# ---------------------------------------------------------------------------
def test_run_wires_analyze():
    fake = {"trades": _dataset(), "range": {"from": "2026-07-01", "to": "2026-07-10"},
            "universeWithData": 40}
    with _patch(bd, "run", lambda **k: fake):
        out = wf.run(days=90, folds=3)
    assert out["ok"] is True and out["daysRequested"] == 90
    assert out["range"]["to"] == "2026-07-10" and out["universeWithData"] == 40


def test_run_handles_no_history():
    with _patch(bd, "run", lambda **k: {"message": "No history returned from NSE"}):
        out = wf.run()
    assert out["ok"] is False and "No history" in out["reason"]


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} walkforward tests passed")


if __name__ == "__main__":
    _main()
