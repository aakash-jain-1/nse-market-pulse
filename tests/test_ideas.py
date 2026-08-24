"""
Unit tests for the intrabar-accurate idea outcome pass (AUDIT.md L7).

resolve_outcomes_intrabar() turns real 1-min candles into a sticky TARGET/STOP
verdict for today's unresolved ideas, with the conservative STOP-first tie-break
and a coarse-LTP fallback for symbols with no charting token. We stub the DB +
candle feed so the test is pure/offline.

Run: python test_ideas.py   (also works under pytest)
"""

from datetime import datetime, timezone

from nse_pulse.sim import ideas_journal as ij
from nse_pulse.core import nse_quote


def _bar(hh, mm, o, h, l, c):
    ms = int(datetime(2026, 7, 16, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)
    return {"t": ms, "o": o, "h": h, "l": l, "c": c, "v": 1000}


def _idea(symbol="RELIANCE", direction="LONG", entry=100.0, stop=98.0,
          target=104.0, first_seen="2026-07-16 09:20:00"):
    return {"day": "2026-07-16", "symbol": symbol, "direction": direction,
            "entry": entry, "stop": stop, "target": target,
            "stopPct": 2.0, "targetPct": 4.0, "firstSeenAt": first_seen,
            "outcome": None, "outcomeAt": None, "outcomePct": None, "ltp": entry}


class _Harness:
    """Swap ideas_journal's DB + feed + gates for offline stubs."""
    def __init__(self, rows, candles):
        self.rows = rows
        self.candles = candles
        self.upserted = []
        self._orig = {}

    def __enter__(self):
        ij._last_intrabar = 0.0
        self._orig = {
            "init": ij.db.init, "for_day": ij.db.ideas_for_day,
            "upsert": ij.db.ideas_upsert, "market": ij._market_ish,
            "today": ij._today, "get_ohlc": nse_quote.get_ohlc,
        }
        ij.db.init = lambda: None
        ij.db.ideas_for_day = lambda day: self.rows
        ij.db.ideas_upsert = lambda rows: self.upserted.extend(rows)
        ij._market_ish = lambda: True
        ij._today = lambda: "2026-07-16"
        nse_quote.get_ohlc = lambda s, **k: {"points": self.candles.get(s, []),
                                             "error": None if s in self.candles else "token-not-found"}
        return self

    def __exit__(self, *a):
        ij.db.init = self._orig["init"]
        ij.db.ideas_for_day = self._orig["for_day"]
        ij.db.ideas_upsert = self._orig["upsert"]
        ij._market_ish = self._orig["market"]
        ij._today = self._orig["today"]
        nse_quote.get_ohlc = self._orig["get_ohlc"]


def test_intrabar_target_verdict():
    rows = [_idea()]
    candles = {"RELIANCE": [
        _bar(9, 20, 100, 101, 99.5, 100.5),
        _bar(9, 21, 100.5, 104.5, 100, 104),   # high pierces target 104
    ]}
    with _Harness(rows, candles) as h:
        ij.resolve_outcomes_intrabar()
    assert len(h.upserted) == 1
    assert h.upserted[0]["outcome"] == "TARGET"
    assert h.upserted[0]["outcomePct"] == 4.0        # exact move to the level
    assert h.upserted[0]["outcomeAt"]                 # timestamped


def test_intrabar_stop_first_tie():
    rows = [_idea()]
    candles = {"RELIANCE": [
        _bar(9, 20, 100, 100, 100, 100),
        _bar(9, 21, 100, 105, 97, 100),         # bar hits BOTH stop(98) and target(104)
    ]}
    with _Harness(rows, candles) as h:
        ij.resolve_outcomes_intrabar()
    assert h.upserted and h.upserted[0]["outcome"] == "STOP"


def test_no_token_no_verdict():
    # symbol with no candles (no charting token) keeps the coarse verdict: no write
    rows = [_idea(symbol="NOTOKEN")]
    with _Harness(rows, candles={}) as h:
        ij.resolve_outcomes_intrabar()
    assert h.upserted == []


def test_already_resolved_skipped():
    idea = _idea()
    idea["outcome"] = "TARGET"          # already has a verdict
    with _Harness([idea], candles={"RELIANCE": [_bar(9, 21, 100, 104.5, 100, 104)]}) as h:
        ij.resolve_outcomes_intrabar()
    assert h.upserted == []              # nothing pending -> no fetch/write


def test_throttled_second_call_noops():
    rows = [_idea()]
    candles = {"RELIANCE": [_bar(9, 21, 100, 104.5, 100, 104)]}
    with _Harness(rows, candles) as h:
        ij.resolve_outcomes_intrabar()          # first call resolves
        n_after_first = len(h.upserted)
        ij.resolve_outcomes_intrabar()          # immediate second call is throttled
    assert n_after_first == 1
    assert len(h.upserted) == 1                 # no extra work


# ---------------------------------------------------------------------------
# Settling PAST days' ideas on daily bars (resolve_outcomes_eod)
# ---------------------------------------------------------------------------
# The intrabar pass above only ever looks at TODAY, so an idea that outlived its
# session — every EOD conviction pick, since the board saves them dated to the last
# closed session — could never get a verdict. These cover the daily-bar settler.
def _dbar(d, o, h, l, c):
    return {"d": d, "open": o, "high": h, "low": l, "close": c, "volume": 1000}


def _saved(day="2026-07-17", direction="LONG", entry=100.0, stop=97.0,
           target=106.0, conviction=True):
    reasons = (["🏆 EOD conviction (3 signals)", "📈 Uptrend"] if conviction
               else ["momentum breakout"])
    return {"day": day, "symbol": "AAA", "direction": direction, "entry": entry,
            "stop": stop, "target": target, "reasons": reasons, "outcome": None}


def test_daily_candle_round_trips_the_session_date():
    # intrabar reads epochs back as UTC to recover IST (NSE's baked-epoch trick), so
    # a date pushed through _daily_candle must come back as the SAME session.
    c = ij._daily_candle(_dbar("2026-07-20", 1, 2, 0.5, 1.5))
    assert ij.intrabar.candle_dt(c["t"]).strftime("%Y-%m-%d") == "2026-07-20"


def test_signal_day_bar_is_never_used():
    # The plan came FROM that session's close; scoring it on the same bar is
    # look-ahead. A wild signal-day range must not resolve anything.
    bars = [_dbar("2026-07-17", 100, 500, 1, 100)]     # would hit target AND stop
    assert ij.resolve_idea_on_daily(_saved(), bars) is None


def test_target_and_stop_on_a_later_session():
    bars = [_dbar("2026-07-17", 100, 100, 100, 100),
            _dbar("2026-07-20", 101, 107, 100, 106)]   # high 107 >= target 106
    hit = ij.resolve_idea_on_daily(_saved(), bars)
    assert hit["outcome"] == "TARGET" and hit["outcomePct"] == 6.0
    assert hit["outcomeAt"].startswith("2026-07-20")

    down = [_dbar("2026-07-17", 100, 100, 100, 100),
            _dbar("2026-07-20", 100, 101, 96, 97)]     # low 96 <= stop 97
    assert ij.resolve_idea_on_daily(_saved(), down)["outcome"] == "STOP"


def test_straddling_session_is_a_stop():
    # Same conservative tie-break as every other engine: one bar touching both
    # levels is assumed to have hit the STOP first.
    bars = [_dbar("2026-07-17", 100, 100, 100, 100),
            _dbar("2026-07-20", 100, 110, 90, 100)]
    assert ij.resolve_idea_on_daily(_saved(), bars)["outcome"] == "STOP"


def test_short_direction_is_mirrored():
    bars = [_dbar("2026-07-17", 100, 100, 100, 100),
            _dbar("2026-07-20", 99, 100, 93, 94)]      # falls to the SHORT target
    hit = ij.resolve_idea_on_daily(
        _saved(direction="SHORT", stop=103.0, target=94.0), bars)
    assert hit["outcome"] == "TARGET" and hit["outcomePct"] == 6.0


def test_quiet_run_expires_at_the_horizon():
    bars = [_dbar("2026-07-17", 100, 100, 100, 100)] + [
        _dbar("2026-07-2%d" % i, 100, 101, 99, 100) for i in range(0, 6)]
    out = ij.resolve_idea_on_daily(_saved(), bars, max_sessions=3)
    assert out["outcome"] == "EXPIRED"           # settled, but neither win nor loss
    assert out["outcomePct"] == 0.0


def test_still_inside_its_horizon_stays_open():
    # Fewer sessions than the horizon and no level touched → leave it unresolved
    # rather than calling it expired early.
    bars = [_dbar("2026-07-17", 100, 100, 100, 100),
            _dbar("2026-07-20", 100, 101, 99, 100)]
    assert ij.resolve_idea_on_daily(_saved(), bars, max_sessions=5) is None


def test_incomplete_plan_is_skipped():
    for missing in ("entry", "stop", "target", "day", "direction"):
        idea = _saved()
        idea[missing] = None
        bars = [_dbar("2026-07-17", 100, 100, 100, 100),
                _dbar("2026-07-20", 101, 107, 100, 106)]
        assert ij.resolve_idea_on_daily(idea, bars) is None


def test_horizon_differs_by_where_the_idea_came_from():
    # Reuse each engine's own convention instead of inventing a third.
    assert ij._horizon(_saved(conviction=True)) == ij.EOD_MAX_SESSIONS
    assert ij._horizon(_saved(conviction=False)) == ij.LIVE_MAX_SESSIONS
    assert ij.LIVE_MAX_SESSIONS < ij.EOD_MAX_SESSIONS


def test_daily_settled_marker_protects_live_verdicts():
    # Daily resolution can only stamp a session boundary; the live passes always
    # stamp a wall-clock time. force= must re-settle ours and never theirs.
    assert ij.daily_settled({"outcomeAt": "2026-07-20 00:00:00"}) is True
    assert ij.daily_settled({"outcomeAt": "2026-07-20 14:02:24"}) is False
    assert ij.daily_settled({"outcomeAt": None}) is False


def _temp_ideas_db():
    import contextlib, gc, os, shutil, tempfile

    @contextlib.contextmanager
    def cm():
        from nse_pulse.core import db
        d = tempfile.mkdtemp(prefix="nse_ideas_eod_")
        saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
        db.DATA_DIR, db.DB_FILE, db._initialized = d, os.path.join(d, "market.db"), False
        db.init()
        try:
            yield db
        finally:
            db.DATA_DIR, db.DB_FILE, db._initialized = saved
            gc.collect()
            shutil.rmtree(d, ignore_errors=True)
    return cm()


def test_resolve_outcomes_eod_end_to_end():
    """The regression for the orphaned conviction board: a past-day idea with a
    plan and subsequent bars must come back settled, and today's must be left to
    the intraday resolvers."""
    from nse_pulse.core import corporate_actions as ca
    with _temp_ideas_db() as db:
        past, today = "2026-07-17", "2026-07-24"
        db.ideas_upsert([
            dict(_saved(day=past), symbol="WINNER"),
            dict(_saved(day=past, direction="SHORT", stop=103.0, target=94.0),
                 symbol="LOSER"),
            dict(_saved(day=past), symbol="NOBARS"),
            dict(_saved(day=today), symbol="TODAY"),
        ])
        bars = {
            "WINNER": [_dbar(past, 100, 100, 100, 100),
                       _dbar("2026-07-20", 101, 107, 100, 106)],
            "LOSER": [_dbar(past, 100, 100, 100, 100),
                      _dbar("2026-07-20", 101, 104, 100, 103)],   # stop 103
        }
        orig_today, orig_bars = ij._today, ca.bars_all
        try:
            ij._today, ca.bars_all = (lambda: today), (lambda **k: bars)
            out = ij.resolve_outcomes_eod()
        finally:
            ij._today, ca.bars_all = orig_today, orig_bars

        assert out["settled"] == 2 and out["target"] == 1 and out["stop"] == 1
        assert out["stillOpen"] == 1                 # NOBARS has no history to score
        got = {i["symbol"]: i.get("outcome") for i in db.ideas_all(limit=50)}
        assert got["WINNER"] == "TARGET" and got["LOSER"] == "STOP"
        assert got["NOBARS"] is None and got["TODAY"] is None


def test_resolve_outcomes_eod_leaves_live_verdicts_alone():
    from nse_pulse.core import corporate_actions as ca
    with _temp_ideas_db() as db:
        past, today = "2026-07-17", "2026-07-24"
        live = dict(_saved(day=past), symbol="LIVE", outcome="TARGET",
                    outcomeAt=f"{past} 14:02:24", outcomePct=6.13)
        mine = dict(_saved(day=past), symbol="MINE", outcome="STOP",
                    outcomeAt="2026-07-20 00:00:00", outcomePct=-3.0)
        db.ideas_upsert([live, mine])
        bars = {s: [_dbar(past, 100, 100, 100, 100),
                    _dbar("2026-07-20", 101, 107, 100, 106)] for s in ("LIVE", "MINE")}
        orig_today, orig_bars = ij._today, ca.bars_all
        try:
            ij._today, ca.bars_all = (lambda: today), (lambda **k: bars)
            assert ij.resolve_outcomes_eod()["settled"] == 0      # both already have one
            out = ij.resolve_outcomes_eod(force=True)             # re-settle only ours
        finally:
            ij._today, ca.bars_all = orig_today, orig_bars
        assert out["settled"] == 1
        rows = {i["symbol"]: i for i in db.ideas_all(limit=50)}
        assert rows["LIVE"]["outcomePct"] == 6.13                 # untouched
        assert rows["MINE"]["outcome"] == "TARGET"                # re-scored on the bars


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} ideas tests passed")


if __name__ == "__main__":
    _main()
