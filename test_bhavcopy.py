"""
Unit tests for bhavcopy.py — the NSE EOD bhavcopy ingestion + resilient EOD
source.

The PURE parsers (parse_cm/parse_fo) are driven with hand-built UDiFF CSV text
using the REAL column names, so a future NSE format tweak fails loudly here. The
download/walk-back/caching layer is exercised with a stubbed `_download` (a
url→bytes map), so no network is touched. Integration tests confirm the EOD
close is wired in as nse_client.get_price's last resort and as the lot-size
fallback, and that ingest_db lands rows in the eod_bars/eod_oi cache.

Run: python test_bhavcopy.py   (also works under pytest)
"""

import contextlib
import csv
import gc
import io
import os
import shutil
import tempfile
import zipfile
from datetime import date

import bhavcopy as b


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _reset_cache():
    saved = dict(b._cache)
    b._cache.update(ts=0.0, cm={}, fo={}, cmDate=None, foDate=None, date=None)
    try:
        yield
    finally:
        b._cache.clear()
        b._cache.update(saved)


HEADER = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
          "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric",
          "OptnTp", "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric",
          "LastPric", "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst",
          "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd",
          "SsnId", "NewBrdLotQty", "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]


def _csv(rows):
    """Build UDiFF CSV text (real header) from a list of partial row dicts."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r.get(h, "") for h in HEADER])
    return buf.getvalue()


def _zip(text, name="BhavCopy.csv"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return out.getvalue()


def _eq(sym, close, prev, **kw):
    row = {"TradDt": "2026-07-16", "FinInstrmTp": "STK", "TckrSymb": sym,
           "SctySrs": "EQ", "OpnPric": prev, "HghPric": close, "LwPric": prev,
           "ClsPric": close, "LastPric": close, "PrvsClsgPric": prev,
           "TtlTradgVol": "1000", "TtlTrfVal": "500000", "TtlNbOfTxsExctd": "50"}
    row.update(kw)
    return row


def _fut(sym, close, prev, expiry, tp="STF", **kw):
    row = {"TradDt": "2026-07-16", "FinInstrmTp": tp, "TckrSymb": sym,
           "XpryDt": expiry, "ClsPric": close, "PrvsClsgPric": prev,
           "UndrlygPric": close, "SttlmPric": close, "OpnIntrst": "900000",
           "ChngInOpnIntrst": "21700", "TtlTradgVol": "45", "NewBrdLotQty": "500"}
    row.update(kw)
    return row


def _opt(sym, expiry, strike, ot="CE", lot="500"):
    return {"TradDt": "2026-07-16", "FinInstrmTp": "STO", "TckrSymb": sym,
            "XpryDt": expiry, "StrkPric": strike, "OptnTp": ot,
            "ClsPric": "8", "PrvsClsgPric": "11", "OpnIntrst": "120900",
            "NewBrdLotQty": lot}


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_num():
    assert b._num("110.5") == 110.5
    assert b._num("1,234.5") == 1234.5      # thousands separators tolerated
    assert b._num(" 42 ") == 42.0
    assert b._num("") is None
    assert b._num(None) is None
    assert b._num("-") is None
    assert b._num("abc") is None


def test_pct():
    assert b._pct(110, 100) == 10.0
    assert b._pct(90, 100) == -10.0
    assert b._pct(100, 0) is None
    assert b._pct(None, 100) is None
    assert b._pct(100, None) is None


def test_ymd():
    assert b._ymd(date(2026, 7, 16)) == "20260716"
    assert b._ymd("2026-07-16") == "20260716"
    from datetime import datetime as _dt
    assert b._ymd(_dt(2026, 7, 16, 15, 30)) == "20260716"


def test_urls():
    assert b.cm_url("2026-07-16").endswith("/cm/BhavCopy_NSE_CM_0_0_0_20260716_F_0000.csv.zip")
    assert b.fo_url("2026-07-16").endswith("/fo/BhavCopy_NSE_FO_0_0_0_20260716_F_0000.csv.zip")
    assert b.cm_url("2026-07-16").startswith("https://nsearchives.nseindia.com")


def test_unzip_roundtrip():
    assert b._unzip(_zip("hello,world\n1,2\n")) == "hello,world\n1,2\n"


def test_unzip_empty_raises():
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass
    try:
        b._unzip(empty.getvalue())
        assert False, "expected ValueError on empty zip"
    except ValueError:
        pass


def test_recent_trading_days_defaults_and_datetime():
    from datetime import datetime as _dt
    # no-arg → most recent weekdays up to today (exercises the _today_ist path)
    d = b._recent_trading_days()
    assert len(d) == 7 and all(x.weekday() < 5 for x in d)
    # a datetime is accepted (date() extracted)
    d2 = b._recent_trading_days(_dt(2026, 7, 16, 9, 0), 2)
    assert d2[0] == date(2026, 7, 16)


def test_recent_trading_days_skips_weekend():
    # 2026-07-18 is a Saturday, 07-19 Sunday → newest weekday is Fri 07-17.
    days = b._recent_trading_days(date(2026, 7, 18), 3)
    assert days == [date(2026, 7, 17), date(2026, 7, 16), date(2026, 7, 15)]


def test_recent_trading_days_from_string_and_count():
    days = b._recent_trading_days("2026-07-16", 5)
    assert len(days) == 5 and days[0] == date(2026, 7, 16)
    assert all(d.weekday() < 5 for d in days)


# ---------------------------------------------------------------------------
# parse_cm
# ---------------------------------------------------------------------------
def test_parse_cm_basic():
    cm = b.parse_cm(_csv([_eq("RELIANCE", "110", "100")]))
    r = cm["RELIANCE"]
    assert r["close"] == 110.0 and r["prevClose"] == 100.0
    assert round(r["pChange"], 6) == 10.0
    assert r["volume"] == 1000.0 and r["value"] == 500000.0 and r["trades"] == 50.0
    assert r["d"] == "2026-07-16" and r["series"] == "EQ"


def test_parse_cm_filters_non_equity_series():
    cm = b.parse_cm(_csv([
        _eq("ABC", "10", "9"),
        _eq("GOLDBOND", "14000", "13900", SctySrs="GB"),   # sovereign gold bond
        _eq("GSEC", "100", "100", SctySrs="GS"),           # govt security
    ]))
    assert set(cm) == {"ABC"}


def test_parse_cm_custom_series_filter():
    cm = b.parse_cm(_csv([_eq("SMEX", "10", "9", SctySrs="SM")]),
                    series={"SM"})
    assert set(cm) == {"SMEX"}


def test_parse_cm_prefers_eq_over_duplicate():
    # Same symbol under EQ and BE — EQ must win regardless of row order.
    rows = [_eq("DUP", "50", "48", SctySrs="BE"), _eq("DUP", "51", "48")]
    cm = b.parse_cm(_csv(rows))
    assert cm["DUP"]["series"] == "EQ" and cm["DUP"]["close"] == 51.0
    rows2 = [_eq("DUP", "51", "48"), _eq("DUP", "50", "48", SctySrs="BE")]
    assert b.parse_cm(_csv(rows2))["DUP"]["series"] == "EQ"


def test_parse_cm_skips_non_stk_instrument():
    rows = [_eq("EQROW", "10", "9"),
            _eq("IDXROW", "100", "99", FinInstrmTp="IDX")]
    assert set(b.parse_cm(_csv(rows))) == {"EQROW"}


def test_parse_cm_missing_numbers():
    cm = b.parse_cm(_csv([_eq("NOPREV", "10", "")]))
    assert cm["NOPREV"]["prevClose"] is None
    assert cm["NOPREV"]["pChange"] is None


def test_parse_cm_skips_blank_symbol():
    cm = b.parse_cm(_csv([_eq("", "10", "9"), _eq("OK", "10", "9")]))
    assert set(cm) == {"OK"}


# ---------------------------------------------------------------------------
# parse_fo
# ---------------------------------------------------------------------------
def test_parse_fo_futures_nearest_expiry():
    rows = [
        _fut("ACME", "205", "200", "2026-08-25"),   # far
        _fut("ACME", "202", "200", "2026-07-28"),   # near — should win
    ]
    fo = b.parse_fo(_csv(rows))
    f = fo["futures"]["ACME"]
    assert f["expiry"] == "2026-07-28" and f["close"] == 202.0
    assert f["kind"] == "stock" and f["oi"] == 900000.0
    assert f["changeOi"] == 21700.0 and f["lot"] == 500
    assert round(f["pChange"], 6) == 1.0


def test_parse_fo_index_future_kind():
    fo = b.parse_fo(_csv([_fut("NIFTY", "24000", "23900", "2026-07-28", tp="IDF")]))
    assert fo["futures"]["NIFTY"]["kind"] == "index"


def test_parse_fo_options_only_contribute_lots():
    rows = [_opt("OPTONLY", "2026-07-28", "500", lot="300")]
    fo = b.parse_fo(_csv(rows))
    assert "OPTONLY" not in fo["futures"]      # no future row → not a future
    assert fo["lots"]["OPTONLY"] == 300         # but lot size still captured


def test_parse_fo_lots_and_underlying_maps():
    rows = [_fut("ACME", "202", "200", "2026-07-28", UndrlygPric="199.7")]
    fo = b.parse_fo(_csv(rows))
    assert fo["lots"]["ACME"] == 500
    assert fo["underlying"]["ACME"] == 199.7
    assert fo["date"] == "2026-07-16"


def test_parse_fo_options_builds_chain():
    rows = [
        _opt("ACME", "2026-07-28", "100", ot="CE"),   # ClsPric 8 / prev 11 / OI 120900
        _opt("ACME", "2026-07-28", "100", ot="PE"),
        _opt("ACME", "2026-08-25", "100", ot="CE"),    # far expiry
        {"TradDt": "2026-07-16", "FinInstrmTp": "STF", "TckrSymb": "ACME",
         "XpryDt": "2026-07-28", "ClsPric": "101"},     # a FUTURE row → ignored
        {"TradDt": "2026-07-16", "FinInstrmTp": "STO", "TckrSymb": "ACME",
         "XpryDt": "2026-07-28", "StrkPric": "105", "OptnTp": "",
         "ClsPric": "2", "OpnIntrst": "5"},             # blank OptnTp → skipped
    ]
    # give the near CE row a real underlying + change so we can assert them
    rows[0].update(UndrlygPric="101", PrvsClsgPric="9", ClsPric="8",
                   ChngInOpnIntrst="10", TtlTradgVol="50")
    p = b.parse_fo_options(_csv(rows), "acme")           # case-insensitive filter
    assert p["symbol"] == "ACME"
    assert p["expiries"] == ["2026-07-28", "2026-08-25"]  # sorted ISO, nearest first
    near = p["byExpiry"]["2026-07-28"]
    assert near["underlying"] == 101.0
    assert set(near["rows"]) == {100.0}                   # blank-OptnTp 105 dropped
    ce = near["rows"][100.0]["ce"]
    assert ce["oi"] == 120900.0 and ce["chgOi"] == 10.0
    assert ce["ltp"] == 8.0 and ce["change"] == -1.0      # 8 - 9
    assert near["rows"][100.0]["pe"] is not None


def test_parse_fo_options_symbol_filter_excludes_others():
    rows = [
        _opt("ACME", "2026-07-28", "100", ot="CE"),
        {"TradDt": "2026-07-16", "FinInstrmTp": "IDO", "TckrSymb": "NIFTY",
         "XpryDt": "2026-07-28", "StrkPric": "24000", "OptnTp": "PE",
         "ClsPric": "120", "OpnIntrst": "5000"},
    ]
    p = b.parse_fo_options(_csv(rows), "ACME")
    assert 24000.0 not in p["byExpiry"]["2026-07-28"]["rows"]   # NIFTY filtered out
    # No filter → both index (IDO) and stock (STO) options are parsed.
    p2 = b.parse_fo_options(_csv(rows))
    strikes = p2["byExpiry"]["2026-07-28"]["rows"]
    assert 100.0 in strikes and 24000.0 in strikes


def test_fetch_fo_text_walks_back_and_missing():
    good = _zip(_csv([_opt("ACME", "2026-07-28", "100", ot="CE")]))
    with _patch(b, "_download", lambda url: good if "20260715" in url else None):
        d, text = b.fetch_fo_text(date=date(2026, 7, 16), walk=5)
    assert d == "2026-07-15" and "ACME" in text
    with _patch(b, "_download", lambda url: None):
        d, text = b.fetch_fo_text(date=date(2026, 7, 16), walk=3)
    assert d is None and text is None


def test_parse_fo_skips_blank_symbol_and_keeps_first_nearest():
    # blank symbol row is ignored; a later EQUAL/greater expiry doesn't replace.
    rows = [
        _fut("", "1", "1", "2026-07-28"),               # blank → skipped
        _fut("ACME", "202", "200", "2026-07-28"),        # near — kept
        _fut("ACME", "205", "200", "2026-08-25"),        # far  — ignored
    ]
    fo = b.parse_fo(_csv(rows))
    assert set(fo["futures"]) == {"ACME"}
    assert fo["futures"]["ACME"]["expiry"] == "2026-07-28"


# ---------------------------------------------------------------------------
# _unzip / _download / fetch walk-back
# ---------------------------------------------------------------------------
def test_fetch_cm_walks_back_over_holiday():
    # 07-16 "missing" (holiday → None); 07-15 present.
    good = _zip(_csv([_eq("ACME", "10", "9")]))

    def fake_dl(url):
        return good if "20260715" in url else None

    with _patch(b, "_download", fake_dl):
        d, cm = b.fetch_cm(date=date(2026, 7, 16), walk=5)
    assert d == "2026-07-15" and "ACME" in cm


def test_fetch_cm_all_missing_returns_empty():
    with _patch(b, "_download", lambda url: None):
        d, cm = b.fetch_cm(date=date(2026, 7, 16), walk=3)
    assert d is None and cm == {}


def test_fetch_fo_walks_back_and_shape():
    good = _zip(_csv([_fut("ACME", "10", "9", "2026-07-28")]))

    def fake_dl(url):
        return good if "20260716" in url else None

    with _patch(b, "_download", fake_dl):
        d, fo = b.fetch_fo(date=date(2026, 7, 16), walk=3)
    assert d == "2026-07-16" and "ACME" in fo["futures"]


def test_fetch_fo_all_missing_returns_empty_shape():
    with _patch(b, "_download", lambda url: None):
        d, fo = b.fetch_fo(date=date(2026, 7, 16), walk=2)
    assert d is None and fo["futures"] == {} and fo["lots"] == {}


def test_fetch_skips_corrupt_zip():
    # First candidate returns garbage bytes (unzip raises) → walk on to the next.
    good = _zip(_csv([_eq("ACME", "10", "9")]))

    def fake_dl(url):
        if "20260716" in url:
            return b"not-a-zip"
        return good if "20260715" in url else None

    with _patch(b, "_download", fake_dl):
        d, cm = b.fetch_cm(date=date(2026, 7, 16), walk=5)
    assert d == "2026-07-15" and "ACME" in cm


# ---------------------------------------------------------------------------
# _download transport (404 vs error vs session-rebuild retry)
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status=200, content=b"zip"):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http %d" % self.status_code)


def _fake_nse(get_impl):
    """A stand-in nse_client module exposing BASE/HEADERS + a get_session()."""
    import types
    m = types.SimpleNamespace()
    m.BASE = "https://www.nseindia.com"
    m.HEADERS = {"User-Agent": "UA"}

    class _S:
        def get(self, url, **kw):
            return get_impl(url, **kw)

    m.get_session = lambda force=False: _S()
    return m


@contextlib.contextmanager
def _patch_nse_module(fake):
    import sys
    real = sys.modules.get("nse_client")
    sys.modules["nse_client"] = fake
    try:
        yield
    finally:
        if real is not None:
            sys.modules["nse_client"] = real
        else:
            del sys.modules["nse_client"]


def test_download_ok():
    with _patch_nse_module(_fake_nse(lambda url, **k: _Resp(200, b"OKBYTES"))):
        assert b._download("http://x") == b"OKBYTES"


def test_download_404_returns_none_no_retry():
    with _patch_nse_module(_fake_nse(lambda url, **k: _Resp(404, b""))):
        assert b._download("http://x") is None


def test_download_retries_then_none_on_error():
    calls = {"n": 0}

    def boom(url, **k):
        calls["n"] += 1
        raise RuntimeError("dead session")
    with _patch_nse_module(_fake_nse(boom)):
        assert b._download("http://x") is None
    assert calls["n"] == 2      # first attempt + one force-session retry


# ---------------------------------------------------------------------------
# latest() caching + public lookups
# ---------------------------------------------------------------------------
def _stub_fetch(cm_map, fo_map, date_str="2026-07-16"):
    """Return fake fetch_cm/fetch_fo that count their calls."""
    calls = {"cm": 0, "fo": 0}

    def fcm(dt=None, walk=7):
        calls["cm"] += 1
        return date_str, cm_map

    def ffo(dt=None, walk=7):
        calls["fo"] += 1
        return date_str, {"date": date_str, "futures": fo_map, "lots": {},
                          "underlying": {}}
    return fcm, ffo, calls


def test_latest_caches_within_ttl():
    fcm, ffo, calls = _stub_fetch({"A": {"close": 10.0}}, {})
    with _reset_cache(), _patch(b, "fetch_cm", fcm), _patch(b, "fetch_fo", ffo):
        b.latest()
        b.latest()
        assert calls["cm"] == 1 and calls["fo"] == 1     # second served from cache
        b.latest(force=True)
        assert calls["cm"] == 2                           # force refetches
        b.refresh()
        assert calls["cm"] == 3                           # refresh() == force pull


def test_eod_price_map_and_close():
    fcm, ffo, _ = _stub_fetch(
        {"A": {"close": 10.0, "symbol": "A"}, "B": {"close": None}}, {})
    with _reset_cache(), _patch(b, "fetch_cm", fcm), _patch(b, "fetch_fo", ffo):
        pm = b.eod_price_map()
        assert pm == {"A": 10.0}                 # B (no close) omitted
        assert b.eod_close("a") == 10.0          # case-insensitive
        assert b.eod_close("ZZZ") is None
        assert b.eod_close("") is None


def test_eod_close_futures_underlying_fallback():
    fcm, ffo, _ = _stub_fetch(
        {}, {"FUTONLY": {"underlying": 250.0, "close": 251.0}})
    with _reset_cache(), _patch(b, "fetch_cm", fcm), _patch(b, "fetch_fo", ffo):
        assert b.eod_close("FUTONLY") == 250.0   # spot preferred over fut close


def test_eod_quote_merges_cm_and_future():
    fcm, ffo, _ = _stub_fetch(
        {"ACME": {"close": 10.0, "symbol": "ACME"}},
        {"ACME": {"expiry": "2026-07-28", "close": 10.5}})
    with _reset_cache(), _patch(b, "fetch_cm", fcm), _patch(b, "fetch_fo", ffo):
        q = b.eod_quote("acme")
        assert q["close"] == 10.0 and q["future"]["expiry"] == "2026-07-28"
        assert q["date"] == "2026-07-16"
    with _reset_cache(), _patch(b, "fetch_cm", fcm), _patch(b, "fetch_fo", ffo):
        assert b.eod_quote("") == {}


def test_lot_sizes_from_latest():
    def ffo(dt=None, walk=7):
        return "2026-07-16", {"date": "2026-07-16", "futures": {},
                              "lots": {"ACME": 500}, "underlying": {}}
    with _reset_cache(), _patch(b, "fetch_cm", lambda *a, **k: ("2026-07-16", {})), \
            _patch(b, "fetch_fo", ffo):
        lots = b.lot_sizes()
        assert lots == {"ACME": 500}
        lots["X"] = 1                             # caller mutation must not leak
        assert "X" not in b.lot_sizes()


def test_status_shape_and_refresh():
    fcm, ffo, calls = _stub_fetch({"A": {"close": 10.0}},
                                  {"F": {"expiry": "2026-07-28"}})
    with _reset_cache(), _patch(b, "fetch_cm", fcm), _patch(b, "fetch_fo", ffo):
        st = b.status(refresh=True)
        assert calls["cm"] == 1
        assert st["equities"] == 1 and st["futures"] == 1
        assert st["cmDate"] == "2026-07-16" and st["cached"] is True
        assert st["source"] == "nsearchives UDiFF bhavcopy"
        assert set(st) >= {"cmDate", "foDate", "date", "equities", "futures",
                           "lots", "ageSec", "ttlSec", "cached", "source"}


# ---------------------------------------------------------------------------
# ingest_db → eod_bars / eod_oi
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    import db
    d = tempfile.mkdtemp(prefix="nse_bhav_test_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR = d
    db.DB_FILE = os.path.join(d, "market.db")
    db._initialized = False
    db.init()
    try:
        yield db
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)


def test_ingest_db_populates_bars_and_oi():
    cm = {"ACME": {"symbol": "ACME", "d": "2026-07-16", "open": 9.0, "high": 11.0,
                   "low": 8.5, "close": 10.0, "prevClose": 9.0, "volume": 1000.0,
                   "value": 10000.0, "trades": 50.0},
          "BETA": {"symbol": "BETA", "d": "2026-07-16", "close": 20.0,
                   "prevClose": 19.0}}
    fo = {"date": "2026-07-16", "lots": {}, "underlying": {},
          "futures": {"ACME": {"expiry": "2026-07-28", "close": 10.2,
                               "underlying": 10.0, "oi": 900000.0,
                               "changeOi": 21700.0, "volume": 45.0, "lot": 500}}}
    with _temp_db() as db:
        with _patch(b, "fetch_cm", lambda *a, **k: ("2026-07-16", cm)), \
                _patch(b, "fetch_fo", lambda *a, **k: ("2026-07-16", fo)):
            res = b.ingest_db()
        assert res["bars"] == 2 and res["oi"] == 1
        assert res["equities"] == 2 and res["futures"] == 1
        bars = db.eod_bars_get("ACME")
        assert len(bars) == 1 and bars[0]["close"] == 10.0 and bars[0]["d"] == "2026-07-16"
        oi = db.eod_oi_get("ACME", "2026-07-28")
        assert len(oi) == 1 and oi[0]["oi"] == 900000.0 and oi[0]["spot"] == 10.0


# ---------------------------------------------------------------------------
# backfill — many sessions, dedup, progress, clamp, busy
# ---------------------------------------------------------------------------
def test_backfill_counts_distinct_days_and_progress():
    # 5 calls: a holiday walks back onto 07-16 (dup), and one day is unpublished.
    results = [
        {"cmDate": "2026-07-17", "bars": 2400, "oi": 200, "equities": 2405, "futures": 200},
        {"cmDate": "2026-07-16", "bars": 2400, "oi": 200, "equities": 2400, "futures": 200},
        {"cmDate": "2026-07-16", "bars": 2400, "oi": 200, "equities": 9999, "futures": 200},  # dup → skipped
        {"cmDate": "2026-07-15", "bars": 2400, "oi": 200, "equities": 2400, "futures": 200},
        {"cmDate": None, "bars": 0, "oi": 0, "equities": 0, "futures": 0},
    ]
    it = iter(results)
    progress = []
    with _patch(b, "ingest_db", lambda date=None: next(it)):
        got = b.backfill(days=5, progress=lambda g: progress.append(g["days"]))
    assert got["days"] == 3                          # distinct published sessions
    assert got["bars"] == 2400 * 3 and got["oi"] == 600
    assert got["dates"] == ["2026-07-15", "2026-07-16", "2026-07-17"]  # sorted
    assert got["equities"] == 2405                   # max over COUNTED days (dup's 9999 skipped)
    assert progress == [1, 2, 3]                     # one tick per distinct day


def test_backfill_clamps_days():
    calls = {"n": 0}

    def fake(date=None):
        calls["n"] += 1
        return {"cmDate": None, "bars": 0, "oi": 0, "equities": 0, "futures": 0}

    with _patch(b, "ingest_db", fake):
        b.backfill(days=0)
        assert calls["n"] == 1                        # clamped up to 1
    calls["n"] = 0
    with _patch(b, "ingest_db", fake):
        b.backfill(days=999)
        assert calls["n"] == 250                      # clamped down to 250


def test_backfill_busy_returns_without_running():
    b._backfill_lock.acquire()
    try:
        got = b.backfill(days=5)
    finally:
        b._backfill_lock.release()
    assert got.get("busy") is True and got["days"] == 0


# ---------------------------------------------------------------------------
# integration — wired into nse_client
# ---------------------------------------------------------------------------
def test_get_price_falls_back_to_eod():
    import nse_client as nse
    import nse_quote
    with _patch(nse, "get_price_map", lambda: {}), \
            _patch(nse_quote, "get_ltp", lambda s: None), \
            _patch(b, "eod_close", lambda s: 123.4):
        assert nse.get_price("OBSCURE") == 123.4


def test_get_price_prefers_live_over_eod():
    import nse_client as nse
    import nse_quote
    called = {"eod": 0}

    def eod(s):
        called["eod"] += 1
        return 1.0
    with _patch(nse, "get_price_map", lambda: {}), \
            _patch(nse_quote, "get_ltp", lambda s: 99.0), \
            _patch(b, "eod_close", eod):
        assert nse.get_price("X") == 99.0
        assert called["eod"] == 0            # live quote short-circuits EOD


def test_get_lot_sizes_falls_back_to_bhavcopy():
    import nse_client as nse

    class _EmptyResp:
        text = "UNDERLYING,SYMBOL\n"     # header only → no lots parsed
        def raise_for_status(self):
            pass

    class _S:
        def get(self, *a, **k):
            return _EmptyResp()

    nse._lots_cache.update(ts=0.0, map=None)
    try:
        with _patch(nse, "get_session", lambda *a, **k: _S()), \
                _patch(b, "lot_sizes", lambda: {"ACME": 500}):
            assert nse.get_lot_sizes() == {"ACME": 500}
    finally:
        nse._lots_cache.update(ts=0.0, map=None)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
