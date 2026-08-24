"""
Unit tests for intrabar.resolve — the minute-candle trade resolver.
Run: python test_intrabar.py   (also works under pytest)
"""

from datetime import datetime, timezone

from nse_pulse.core import intrabar

RISK = 2000.0


def _bar(y, mo, d, hh, mm, o, h, l, c, v=1000):
    # Build ms so that candle_dt (tz-aware UTC read) round-trips to this wall
    # clock exactly the way NSE bakes IST into the epoch as UTC.
    ms = int(datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)
    return {"t": ms, "o": o, "h": h, "l": l, "c": c, "v": v}


def _trade(direction="LONG", entry=100.0, stop=98.0, target=104.0, qty=1000.0,
           opened="2026-07-09T09:15:00", day="2026-07-09", max_sessions=3):
    return {
        "symbol": "X", "direction": direction, "entry": entry, "stop": stop,
        "target": target, "qty": qty, "openedTs": opened, "openedDay": day,
        "maxSessions": max_sessions, "status": "OPEN", "ltp": entry,
        "pnl": 0.0, "pnlPct": 0.0, "rMultiple": 0.0, "mfePct": 0.0, "maePct": 0.0,
        "minsToExit": None, "exitPrice": None, "closedTs": None, "closedDay": None,
    }


def test_long_target():
    t = _trade()
    bars = [
        _bar(2026, 7, 9, 9, 15, 100, 101, 99.5, 100.5),
        _bar(2026, 7, 9, 9, 16, 100.5, 104.2, 100.4, 104.0),  # target 104 hit
        _bar(2026, 7, 9, 9, 17, 104, 105, 103, 104.5),
    ]
    assert intrabar.resolve(t, bars, RISK) == "TARGET"
    assert t["exitPrice"] == 104.0
    assert t["pnl"] == 4000.0            # 1000 * (104 - 100)
    assert t["rMultiple"] == 2.0         # 4000 / 2000
    assert t["minsToExit"] == 1


def test_long_stop():
    t = _trade()
    bars = [
        _bar(2026, 7, 9, 9, 15, 100, 100.5, 99, 99.5),
        _bar(2026, 7, 9, 9, 16, 99.5, 100, 97.5, 98.0),      # stop 98 hit
    ]
    assert intrabar.resolve(t, bars, RISK) == "STOP"
    assert t["exitPrice"] == 98.0
    assert t["pnl"] == -2000.0
    assert t["rMultiple"] == -1.0


def test_straddle_defaults_to_stop():
    t = _trade()
    # single bar pierces BOTH stop and target -> conservative stop-first
    bars = [_bar(2026, 7, 9, 9, 16, 100, 105, 97, 101)]
    assert intrabar.resolve(t, bars, RISK) == "STOP"
    assert t["exitPrice"] == 98.0


def test_straddle_tie_target():
    t = _trade()
    bars = [_bar(2026, 7, 9, 9, 16, 100, 105, 97, 101)]
    assert intrabar.resolve(t, bars, RISK, tie="target") == "TARGET"
    assert t["exitPrice"] == 104.0


def test_horizon_expiry():
    t = _trade(max_sessions=1)
    # one full session, never touches target/stop -> EXPIRED at last close
    bars = [
        _bar(2026, 7, 9, 9, 15, 100, 101, 99.5, 100.5),
        _bar(2026, 7, 9, 15, 29, 100.5, 101, 99.5, 100.8),
    ]
    assert intrabar.resolve(t, bars, RISK) == "EXPIRED"
    assert t["exitPrice"] == 100.8


def test_runs_out_stays_open():
    t = _trade(max_sessions=3)
    # only one day of bars but horizon is 3 sessions -> still OPEN
    bars = [
        _bar(2026, 7, 9, 9, 15, 100, 101, 99.5, 100.5),
        _bar(2026, 7, 9, 9, 16, 100.5, 101, 100, 100.7),
    ]
    assert intrabar.resolve(t, bars, RISK) == "OPEN"
    assert t["closedTs"] is None
    assert t["ltp"] == 100.7


def test_short_target_and_stop():
    long_target = _trade(direction="SHORT", entry=100, stop=102, target=96)
    bars_t = [_bar(2026, 7, 9, 9, 16, 100, 100.5, 95.8, 96.0)]  # low <= 96
    assert intrabar.resolve(long_target, bars_t, RISK) == "TARGET"
    assert long_target["pnl"] == 4000.0   # short gains as price falls

    s = _trade(direction="SHORT", entry=100, stop=102, target=96)
    bars_s = [_bar(2026, 7, 9, 9, 16, 100, 102.3, 99.5, 101.5)]  # high >= 102
    assert intrabar.resolve(s, bars_s, RISK) == "STOP"
    assert s["pnl"] == -2000.0


def test_no_bars_returns_none():
    t = _trade()
    assert intrabar.resolve(t, [], RISK) is None
    # bars entirely before entry are also unusable
    old = [_bar(2026, 7, 9, 9, 0, 100, 101, 99, 100)]
    assert intrabar.resolve(t, old, RISK) is None


def test_mfe_mae_tracked():
    t = _trade(target=999)  # never hit target so we scan the whole path
    bars = [
        _bar(2026, 7, 9, 9, 15, 100, 103, 100, 102),   # +3% high
        _bar(2026, 7, 9, 9, 16, 102, 102, 98.5, 99),   # -1.5% low (98.5)
    ]
    intrabar.resolve(t, bars, RISK, max_sessions=1)
    assert t["mfePct"] == 3.0
    assert t["maePct"] == -1.5


def test_resolve_point_long():
    # single-price coarse resolver shared by the live sim + context backtest
    assert intrabar.resolve_point("LONG", 100, 98, 104, 104.5) == ("TARGET", 104)
    assert intrabar.resolve_point("LONG", 100, 98, 104, 97.0) == ("STOP", 98)
    assert intrabar.resolve_point("LONG", 100, 98, 104, 101.0) == (None, None)


def test_resolve_point_short():
    assert intrabar.resolve_point("SHORT", 100, 102, 96, 95.0) == ("TARGET", 96)
    assert intrabar.resolve_point("SHORT", 100, 102, 96, 103.0) == ("STOP", 102)
    assert intrabar.resolve_point("SHORT", 100, 102, 96, 99.0) == (None, None)


def test_resolve_point_mutually_exclusive():
    # A single price can never be beyond BOTH levels (they straddle entry), so the
    # tie-break is unreachable and target/stop order can't change the outcome.
    for px in (90, 95, 100, 105, 110):
        s_stop = intrabar.resolve_point("LONG", 100, 98, 104, px, tie="stop")
        s_tgt = intrabar.resolve_point("LONG", 100, 98, 104, px, tie="target")
        assert s_stop == s_tgt


def test_resolve_point_missing_levels():
    assert intrabar.resolve_point("LONG", 100, None, 104, 104.5) == ("TARGET", 104)
    assert intrabar.resolve_point("LONG", 100, 98, None, 97.0) == ("STOP", 98)
    assert intrabar.resolve_point("LONG", 100, None, None, 200.0) == (None, None)


def test_a_zero_priced_bar_cannot_fake_a_stop():
    """A hole in the candle feed reads as low=0, which is below ANY long stop. Left
    unguarded that writes a phantom STOP (at -100% MAE) into the ledger permanently,
    where it then skews expectancy, the regime leaderboards and the adaptive pick."""
    t = _trade()
    bars = [
        _bar(2026, 7, 9, 9, 15, 100, 100.5, 99.6, 100.2),
        _bar(2026, 7, 9, 9, 16, 0, 0, 0, 0, v=0),            # the hole
        _bar(2026, 7, 9, 9, 17, 100.2, 100.7, 100.1, 100.6),
    ]
    assert intrabar.resolve(t, bars, RISK) == "OPEN"          # NOT "STOP"
    assert t["status"] == "OPEN" and t["exitPrice"] is None
    assert t["maePct"] > -1.0                                 # not -100%
    # a genuine stop on a clean bar still resolves
    t2 = _trade()
    assert intrabar.resolve(t2, bars[:2] + [
        _bar(2026, 7, 9, 9, 17, 99.5, 100, 97.5, 98.0)], RISK) == "STOP"


def test_unusable_bars_are_skipped_not_trusted():
    t = _trade()
    good = _bar(2026, 7, 9, 9, 15, 100, 100.4, 99.7, 100.1)
    for bad in (
        _bar(2026, 7, 9, 9, 16, None, None, None, None),      # empty
        _bar(2026, 7, 9, 9, 16, 100, 99.0, 101.0, 100),       # inverted (low > high)
        _bar(2026, 7, 9, 9, 16, -1, -1, -1, -1),             # negative
        {"t": None, "o": 1, "h": 1, "l": 1, "c": 1},          # no timestamp
    ):
        t = _trade()
        assert intrabar.resolve(t, [good, bad], RISK) == "OPEN"
        assert t["status"] == "OPEN"
    # nothing usable at all → None, so the caller keeps its own LTP path
    assert intrabar.resolve(_trade(), [_bar(2026, 7, 9, 9, 16, 0, 0, 0, 0)], RISK) is None


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} intrabar tests passed")


if __name__ == "__main__":
    _main()
