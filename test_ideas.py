"""
Unit tests for the intrabar-accurate idea outcome pass (AUDIT.md L7).

resolve_outcomes_intrabar() turns real 1-min candles into a sticky TARGET/STOP
verdict for today's unresolved ideas, with the conservative STOP-first tie-break
and a coarse-LTP fallback for symbols with no charting token. We stub the DB +
candle feed so the test is pure/offline.

Run: python test_ideas.py   (also works under pytest)
"""

from datetime import datetime, timezone

import ideas_journal as ij
import nse_quote


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


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} ideas tests passed")


if __name__ == "__main__":
    _main()
