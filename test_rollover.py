"""
Unit tests for rollover.py — the futures rollover tracker.

The maths is PURE and driven with hand-built futures dicts:
  - _days_between / _oi_state       : calendar gap + price×OI quadrant
  - _metrics                        : rollover% / roll-cost / annualized / basis
  - _percentile_ranks / _median     : cross-sectional ranking helpers
board() is exercised against a STUBBED eod_options._fo_text (a tiny synthetic FO
UDiFF CSV), so nothing touches NSE or the network.

Run: python test_rollover.py   (also works under pytest)
"""

import contextlib
import csv
import io

import rollover as R


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_COLS = ["TradDt", "TckrSymb", "FinInstrmTp", "OptnTp", "StrkPric", "XpryDt",
         "ClsPric", "PrvsClsgPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
         "TtlTradgVol", "TtlTrfVal", "UndrlygPric", "NewBrdLotQty"]


def _fut(sym, exp, cls, prev, oi, chg, spot, val=5e8, tp="STF"):
    return {"TradDt": "2026-07-18", "TckrSymb": sym, "FinInstrmTp": tp, "OptnTp": "",
            "StrkPric": "", "XpryDt": exp, "ClsPric": cls, "PrvsClsgPric": prev,
            "SttlmPric": cls, "OpnIntrst": oi, "ChngInOpnIntrst": chg,
            "TtlTradgVol": 9999, "TtlTrfVal": val, "UndrlygPric": spot,
            "NewBrdLotQty": 100}


def _opt(sym, exp, strike):
    r = _fut(sym, exp, 2, 2, 9000, 0, 100, val=1)
    r.update(FinInstrmTp="STO", OptnTp="CE", StrkPric=strike)
    return r


def _text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


@contextlib.contextmanager
def _fo(rows, date="2026-07-18"):
    """Stub eod_options._fo_text with a synthetic FO CSV + reset rollover's cache."""
    import eod_options
    text = _text(rows)
    orig = eod_options._fo_text
    eod_options._fo_text = lambda force=False: (date, text)
    R._cache.update(ts=0.0, key=None, board=None)
    try:
        yield
    finally:
        eod_options._fo_text = orig
        R._cache.update(ts=0.0, key=None, board=None)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_days_between():
    assert R._days_between("2026-07-30", "2026-08-27") == 28
    assert R._days_between("2026-07-18", "2026-07-30") == 12
    assert R._days_between(None, "2026-08-27") is None
    assert R._days_between("garbage", "2026-08-27") is None


def test_oi_state_quadrants():
    assert R._oi_state(100, True) == ("long buildup", True)
    assert R._oi_state(100, False) == ("short buildup", False)
    assert R._oi_state(-100, True) == ("short covering", True)
    assert R._oi_state(-100, False) == ("long unwinding", False)
    assert R._oi_state(None, True) == (None, None)


def test_metrics_contango_long_buildup():
    fut = {"symbol": "AAA", "kind": "stock",
           "expiries": ["2026-07-30", "2026-08-27"],
           "byExpiry": {
               "2026-07-30": {"expiry": "2026-07-30", "close": 105.0, "prevClose": 100.0,
                              "oi": 200000, "changeOi": -50000, "value": 5e8,
                              "underlying": 104.0, "pChange": 5.0},
               "2026-08-27": {"expiry": "2026-08-27", "close": 106.0, "prevClose": 101.0,
                              "oi": 800000, "changeOi": 120000, "value": 4e8,
                              "underlying": 104.0, "pChange": 4.95}}}
    m = R._metrics(fut, "2026-07-18")
    assert m["rolloverPct"] == 80.0                 # 800k / (200k+800k)
    assert m["rollCostPct"] == round((106 - 105) / 105 * 100, 2)   # +0.95 contango
    assert m["annualizedRollPct"] == round(m["rollCostPct"] * 365 / 28, 1)
    assert m["basisPct"] == round((105 - 104) / 104 * 100, 2)
    assert m["daysToExpiry"] == 12
    assert m["netChgOi"] == 70000 and m["freshOi"] is True
    assert m["oiState"] == "long buildup" and m["bullish"] is True


def test_metrics_needs_two_usable_expiries():
    one = {"symbol": "X", "expiries": ["2026-07-30"],
           "byExpiry": {"2026-07-30": {"expiry": "2026-07-30", "close": 100,
                                       "oi": 1000, "underlying": 100}}}
    assert R._metrics(one, "2026-07-18") is None
    # two expiries but next-month OI missing → None
    miss = {"symbol": "Y", "expiries": ["2026-07-30", "2026-08-27"],
            "byExpiry": {
                "2026-07-30": {"expiry": "2026-07-30", "close": 100, "oi": 1000,
                               "underlying": 100, "changeOi": 0, "pChange": 0},
                "2026-08-27": {"expiry": "2026-08-27", "close": 101, "oi": None,
                               "underlying": 100, "changeOi": 0, "pChange": 0}}}
    assert R._metrics(miss, "2026-07-18") is None


def test_percentile_ranks():
    assert R._percentile_ranks([]) == []
    assert R._percentile_ranks([5]) == [50.0]
    r = R._percentile_ranks([10, 20, 30])
    assert r[0] == 0.0 and r[2] == 100.0                # min→0, max→100
    # ties share the mean rank
    assert R._percentile_ranks([7, 7]) == [50.0, 50.0]


def test_median():
    assert R._median([]) is None
    assert R._median([5]) == 5
    assert R._median([10, 20, 30]) == 20
    assert R._median([10, 20, 30, 40]) == 25.0
    assert R._median([None, 10, None, 30]) == 20


def test_mode_str():
    assert R._mode_str(["a", "a", "b", None]) == "a"
    assert R._mode_str([None, ""]) is None


# ---------------------------------------------------------------------------
# board() against a stubbed FO bhavcopy
# ---------------------------------------------------------------------------
def test_board_ranks_filters_and_ignores_options():
    rows = [
        _fut("HIGH", "2026-07-30", 105, 100, 200000, -50000, 104),
        _fut("HIGH", "2026-08-27", 106, 101, 800000, 120000, 104),   # 80%
        _fut("LOW", "2026-07-30", 98, 100, 800000, -10000, 99),
        _fut("LOW", "2026-08-27", 97, 99, 200000, 5000, 99),          # 20%
        _fut("PENNY", "2026-07-30", 5, 5, 50000, 100, 5),             # below min price
        _fut("PENNY", "2026-08-27", 5, 5, 60000, 100, 5),
        _fut("SOLO", "2026-07-30", 400, 395, 100000, 100, 400),       # one expiry only
        _opt("HIGH", "2026-07-30", 105),                              # option row → ignored
    ]
    with _fo(rows):
        b = R.board(min_price=20, min_value_cr=0.0)
    syms = [r["symbol"] for r in b["rows"]]
    assert syms == ["HIGH", "LOW"]                       # rollover-desc, PENNY/SOLO gone
    assert b["universe"] == 4                            # HIGH/LOW/PENNY/SOLO parsed
    assert b["count"] == 2
    hi = b["rows"][0]
    assert hi["symbol"] == "HIGH" and hi["rolloverPct"] == 80.0
    assert hi["rolloverRank"] == 100.0 and hi["carrying"] is True
    assert b["rows"][1]["shedding"] is True
    assert b["medianRollover"] == 50.0
    assert b["nearExpiry"] == "2026-07-30" and b["daysToExpiry"] == 12


def test_board_sort_by_rollcost():
    rows = [
        _fut("CONTANGO", "2026-07-30", 100, 100, 100000, 0, 100),
        _fut("CONTANGO", "2026-08-27", 103, 100, 100000, 0, 100),     # +3%
        _fut("BACKWARD", "2026-07-30", 100, 100, 100000, 0, 100),
        _fut("BACKWARD", "2026-08-27", 98, 100, 100000, 0, 100),      # -2%
    ]
    with _fo(rows):
        b = R.board(min_price=20, min_value_cr=0.0, sort="rollcost")
    assert [r["symbol"] for r in b["rows"]] == ["CONTANGO", "BACKWARD"]


def test_board_note_when_expiry_far():
    rows = [
        _fut("AAA", "2026-08-27", 100, 100, 100000, 0, 100),
        _fut("AAA", "2026-09-24", 101, 100, 100000, 0, 100),
    ]
    with _fo(rows, date="2026-07-18"):                   # ~40 days to near expiry
        b = R.board(min_price=20, min_value_cr=0.0)
    assert b["note"] and "expiry" in b["note"].lower()


def test_board_empty_without_text():
    import eod_options
    orig = eod_options._fo_text
    eod_options._fo_text = lambda force=False: (None, None)
    R._cache.update(ts=0.0, key=None, board=None)
    try:
        b = R.board()
        assert b["rows"] == [] and b["count"] == 0 and b["note"]
    finally:
        eod_options._fo_text = orig
        R._cache.update(ts=0.0, key=None, board=None)


# ---------------------------------------------------------------------------
# rank_map() — the market-wide {sym: metrics+rank} the conviction board folds in
# ---------------------------------------------------------------------------
def test_rank_map_keys_by_symbol_over_full_universe():
    rows = [
        _fut("HIGH", "2026-07-30", 105, 100, 200000, -50000, 104),
        _fut("HIGH", "2026-08-27", 106, 101, 800000, 120000, 104),      # 80%
        _fut("LOW", "2026-07-30", 98, 100, 800000, -10000, 99),
        _fut("LOW", "2026-08-27", 97, 99, 200000, 5000, 99),             # 20%
        _fut("PENNY", "2026-07-30", 5, 5, 50000, 100, 5),                # NOT filtered here
        _fut("PENNY", "2026-08-27", 5, 5, 60000, 100, 5),
        _fut("SOLO", "2026-07-30", 400, 395, 100000, 100, 400),          # 1 expiry → dropped
    ]
    import eod_options
    orig = eod_options._fo_text
    eod_options._fo_text = lambda force=False: ("2026-07-18", _text(rows))
    R._map_cache.update(ts=0.0, date=None, map=None)
    try:
        date, m = R.rank_map()
    finally:
        eod_options._fo_text = orig
        R._map_cache.update(ts=0.0, date=None, map=None)
    assert date == "2026-07-18"
    # unlike board(), NO price/value filter — every 2-expiry name is present + ranked
    assert set(m) == {"HIGH", "LOW", "PENNY"}
    assert m["HIGH"]["carrying"] is True and m["HIGH"]["rolloverRank"] == 100.0
    assert m["LOW"]["shedding"] is True and m["LOW"]["rolloverRank"] == 0.0
    assert m["HIGH"]["bullish"] is True and m["LOW"]["bullish"] is False


def test_rank_map_empty_without_text():
    import eod_options
    orig = eod_options._fo_text
    eod_options._fo_text = lambda force=False: (None, None)
    R._map_cache.update(ts=0.0, date=None, map=None)
    try:
        date, m = R.rank_map()
        assert m == {}
    finally:
        eod_options._fo_text = orig
        R._map_cache.update(ts=0.0, date=None, map=None)


def test_status_shape():
    s = R.status()
    for k in ("cached", "date", "ageSec", "ttlSec", "source"):
        assert k in s


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print("ok  ", fn.__name__)
        except Exception as e:
            fails += 1
            print("FAIL", fn.__name__, "->", repr(e))
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
