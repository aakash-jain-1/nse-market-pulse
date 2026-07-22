"""
Walk-forward out-of-sample validation
======================================
The Sim leaderboard and `strategy_of_day` pick the strategy with the best
HISTORICAL edge. That number is measured IN-SAMPLE (on the same window used to
pick it), so it flatters every strategy and — worse — flatters the *selection*
itself. This module answers the only question that matters before trusting it
with real money: **does the edge survive out-of-sample?**

Two complementary views, both computed as PURE functions over the daily
backtest's trade list (each trade carries `openedDate`, `regimeAtEntry`,
`strategy`, `status`, `rMultiple`), so they're fully unit-testable offline:

1. Holdout split (headline) — cut the timeline once (`train_frac`, default 0.6):
   train = earlier, test = later (never seen when picking). For every fixed
   strategy we report in-sample vs out-of-sample expectancy + an overfit verdict
   (robust / decaying / overfit / no-edge / insufficient).

2. The adaptive-selection test (the real overfit check) — a fixed strategy has
   no fitted parameters, but the "which strategy to trust per regime" CHOICE is
   fit on the train window. So we build the best-per-regime playbook from TRAIN,
   then FOLLOW it on TEST, and compare that out-of-sample result to (a) the best
   single fixed strategy OOS and (b) the a-priori regimeFit design. If following
   the fitted playbook doesn't beat a fixed strategy out-of-sample, the
   regime-switching edge was curve-fit — reported honestly.

3. Walk-forward folds (robustness) — repeat the train→test selection across
   several anchored folds so the verdict isn't hostage to one arbitrary cut.

Educational validation of signal quality — NOT investment advice.
"""

from nse_pulse.backtest import backtest_daily as bd

_CLOSED = ("TARGET", "STOP", "EXPIRED")


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ----------------------------------------------------------------------------
def _closed(trades):
    return [t for t in trades if t.get("status") in _CLOSED]


def _expectancy(trades):
    """Expectancy (mean R), win% and sample size over a set of CLOSED trades."""
    cl = _closed(trades)
    n = len(cl)
    if not n:
        return {"n": 0, "expectancyR": None, "winRate": None, "totalR": 0.0}
    rs = [t.get("rMultiple") or 0.0 for t in cl]
    wins = sum(1 for t in cl if t.get("status") == "TARGET")
    return {"n": n, "expectancyR": round(sum(rs) / n, 3),
            "winRate": round(wins / n * 100, 1), "totalR": round(sum(rs), 2)}


def _by_strategy(trades):
    by = {}
    for t in trades:
        by.setdefault(t.get("strategy"), []).append(t)
    return by


def _best_per_regime(trades, min_samples):
    """{regime: strategy_id} — the strategy with the best expectancy (>= min_samples
    closed trades) in each regime, learned from the given (train) trades."""
    agg = {}
    for t in _closed(trades):
        key = (t.get("regimeAtEntry") or "?", t.get("strategy"))
        a = agg.setdefault(key, {"n": 0, "sumR": 0.0})
        a["n"] += 1
        a["sumR"] += t.get("rMultiple") or 0.0
    best = {}
    for (rg, sid), a in agg.items():
        if a["n"] < min_samples:
            continue
        exp = a["sumR"] / a["n"]
        cur = best.get(rg)
        if cur is None or exp > cur[1]:
            best[rg] = (sid, exp)
    return {rg: sid for rg, (sid, _e) in best.items()}


def _best_overall(trades, min_samples):
    """The single strategy with the best overall expectancy on `trades`
    (>= min_samples closed), or None."""
    best_sid, best_exp = None, None
    for sid, ts in _by_strategy(trades).items():
        e = _expectancy(ts)
        if e["n"] >= min_samples and e["expectancyR"] is not None:
            if best_exp is None or e["expectancyR"] > best_exp:
                best_sid, best_exp = sid, e["expectancyR"]
    return best_sid


def _apriori_map(ids):
    """{regime: strategy_id} from the a-priori DESIGN: the first strategy (in STRATS
    order) whose declared regimeFit covers that regime. No look at outcomes."""
    out = {}
    for rg in bd._REGIME_ORDER:
        for sid in ids:
            if rg in bd._regime_fit(sid):
                out[rg] = sid
                break
    return out


def _apply_playbook(test_trades, regime_map):
    """Keep only the test trades the playbook would have TAKEN: those whose
    strategy is the chosen one for the regime they were entered in."""
    return [t for t in test_trades
            if regime_map.get(t.get("regimeAtEntry")) == t.get("strategy")]


def _verdict(is_r, oos_r, oos_n, min_test):
    """Overfit verdict for one fixed strategy from in-sample vs out-of-sample R."""
    if oos_n < min_test or is_r is None or oos_r is None:
        return "insufficient"
    if is_r <= 0:
        return "improving" if oos_r > 0 else "no-edge"
    if oos_r <= 0:
        return "overfit"                     # positive in-sample, negative live
    if oos_r >= 0.6 * is_r:
        return "robust"                      # edge largely persists
    return "decaying"                        # positive but materially weaker


def _adaptive_verdict(ada, best_fixed_oos, min_test):
    if ada["n"] < min_test or ada["expectancyR"] is None:
        return "insufficient"
    if best_fixed_oos is None:
        return "adds-value" if ada["expectancyR"] > 0 else "weak"
    if ada["expectancyR"] >= best_fixed_oos:
        return "adds-value"                  # regime-switching generalises OOS
    return "no-better-than-fixed"            # the selection was curve-fit


def _split_folds(days, folds):
    """Split an ascending list of trading days into `folds` contiguous chunks."""
    folds = max(2, min(int(folds), len(days)))
    size = len(days) / folds
    out, i = [], 0
    for k in range(folds):
        j = len(days) if k == folds - 1 else int(round((k + 1) * size))
        out.append(days[i:j])
        i = j
    return [c for c in out if c]


# ----------------------------------------------------------------------------
# Analysis (pure over trades) + public run (adds the one network call)
# ----------------------------------------------------------------------------
def analyze(trades, folds=4, train_frac=0.6, min_test=8, min_regime_samples=4):
    """The full walk-forward report. Pure: give it the daily backtest's trade list."""
    ids = [s["id"] for s in bd.STRATS]
    names = {s["id"]: s["name"] for s in bd.STRATS}
    cl = sorted(_closed(trades), key=lambda t: t.get("openedDate") or "")
    days = sorted({t["openedDate"] for t in cl if t.get("openedDate")})

    if len(days) < 4 or len(cl) < min_test * 2:
        return {"ok": False,
                "reason": "Not enough closed daily-backtest history for walk-forward "
                          "yet — widen the window (more days / larger universe).",
                "closed": len(cl), "days": len(days)}

    # ---- 1) single holdout split: earlier = train, later = out-of-sample ----
    idx = max(1, min(len(days) - 1, round(len(days) * train_frac)))
    cut = days[idx]
    train = [t for t in cl if t["openedDate"] < cut]
    test = [t for t in cl if t["openedDate"] >= cut]

    per_strategy = []
    for sid in ids:
        tr = _expectancy([t for t in train if t["strategy"] == sid])
        te = _expectancy([t for t in test if t["strategy"] == sid])
        per_strategy.append({
            "id": sid, "name": names[sid],
            "isExpectancyR": tr["expectancyR"], "isN": tr["n"], "isWinRate": tr["winRate"],
            "oosExpectancyR": te["expectancyR"], "oosN": te["n"], "oosWinRate": te["winRate"],
            "verdict": _verdict(tr["expectancyR"], te["expectancyR"], te["n"], min_test),
        })
    per_strategy.sort(key=lambda r: (r["oosExpectancyR"] if r["oosExpectancyR"] is not None
                                     else -1e9), reverse=True)

    # ---- 2) adaptive selection on the holdout ----
    best_map = _best_per_regime(train, min_regime_samples)
    apri_map = _apriori_map(ids)
    ada = _expectancy(_apply_playbook(test, best_map))
    apri = _expectancy(_apply_playbook(test, apri_map))
    best_fixed = next((r for r in per_strategy
                       if r["oosN"] >= min_test and r["oosExpectancyR"] is not None), None)
    bf_oos = best_fixed["oosExpectancyR"] if best_fixed else None
    adaptive = {
        "playbook": {rg: {"id": sid, "name": names.get(sid, sid)}
                     for rg, sid in best_map.items()},
        "oosExpectancyR": ada["expectancyR"], "oosN": ada["n"], "oosWinRate": ada["winRate"],
        "aprioriOosExpectancyR": apri["expectancyR"], "aprioriN": apri["n"],
        "bestFixed": ({"id": best_fixed["id"], "name": best_fixed["name"],
                       "oosExpectancyR": bf_oos} if best_fixed else None),
        "verdict": _adaptive_verdict(ada, bf_oos, min_test),
    }

    # ---- 3) walk-forward folds (anchored/expanding train → next fold as test) ----
    chunks = _split_folds(days, folds)
    fold_rows, ada_pooled = [], []
    for k in range(1, len(chunks)):
        train_days = set().union(*chunks[:k])
        test_days = set(chunks[k])
        tr_tr = [t for t in cl if t["openedDate"] in train_days]
        te_tr = [t for t in cl if t["openedDate"] in test_days]
        bmap = _best_per_regime(tr_tr, min_regime_samples)
        ada_k = _apply_playbook(te_tr, bmap)
        ada_pooled += ada_k
        bf_sid = _best_overall(tr_tr, min_regime_samples)
        bf_te = _expectancy([t for t in te_tr if t["strategy"] == bf_sid]) if bf_sid else {"expectancyR": None, "n": 0}
        ax = _expectancy(ada_k)
        fold_rows.append({
            "fold": k + 1, "from": chunks[k][0], "to": chunks[k][-1],
            "trainN": len(_closed(tr_tr)), "testN": len(_closed(te_tr)),
            "adaptiveR": ax["expectancyR"], "adaptiveN": ax["n"],
            "bestTrainStrategy": bf_sid,
            "bestTrainStrategyName": names.get(bf_sid),
            "bestFixedTestR": bf_te["expectancyR"],
        })
    wf = _expectancy(ada_pooled)

    return {
        "ok": True,
        "closed": len(cl),
        "days": len(days),
        "trainCut": cut,
        "trainN": len(_closed(train)),
        "testN": len(_closed(test)),
        "perStrategy": per_strategy,
        "adaptive": adaptive,
        "walkForward": {"folds": fold_rows,
                        "adaptivePooledR": wf["expectancyR"],
                        "adaptivePooledN": wf["n"],
                        "adaptivePooledWinRate": wf["winRate"]},
        "params": {"folds": len(chunks), "trainFrac": train_frac,
                   "minTest": min_test, "minRegimeSamples": min_regime_samples},
    }


def run(days=120, universe_size=60, max_hold=5, folds=4, train_frac=0.6,
        resolve="daily", source="live"):
    """Public entry: ONE long daily backtest (cached), then the pure analysis.
    `source="eod"` validates over the full-market bhavcopy universe (far more
    out-of-sample trades → a much stronger overfit check)."""
    bt = bd.run(days=days, universe_size=universe_size, max_hold=max_hold,
                resolve=resolve, _collect=True, source=source)
    if bt.get("message"):
        return {"ok": False, "reason": bt["message"]}
    out = analyze(list(bt.get("trades") or []), folds=folds, train_frac=train_frac)
    out["range"] = bt.get("range")
    out["universeWithData"] = bt.get("universeWithData")
    out["daysRequested"] = days
    out["resolve"] = resolve
    out["source"] = source
    out["generatedAt"] = bd._now()
    return out
