"""
Unit tests for eod_options.py — the resilient EOD option chain from the FO bhavcopy.

The pure analytics (`_fmt_expiry` / `_atm` / `_walls` / `_assemble`) are asserted
against hand-built parsed chains, and `chain()` / `summary()` are driven end-to-end
with a stubbed `bhavcopy.fetch_fo_text` (a hand-built FO UDiFF CSV), so nothing
touches the network. Max-pain is delegated to `nse_quote._max_pain`, so these also
lock in that the EOD chain matches the LIVE chain's shape.

Run: python test_eod_options.py   (also works under pytest)
"""

import contextlib
import csv
import io

from nse_pulse.eod import eod_options as eo

HEADER = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
          "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric",
          "OptnTp", "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric",
          "LastPric", "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst",
          "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd",
          "SsnId", "NewBrdLotQty", "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _reset_caches():
    tsaved = dict(eo._text_cache)
    csaved = dict(eo._chain_cache)
    msaved = dict(eo._map_cache)
    eo._text_cache.update(ts=0.0, text=None, date=None)
    eo._chain_cache.clear()
    eo._map_cache.update(ts=0.0, map=None, date=None)
    try:
        yield
    finally:
        eo._text_cache.clear()
        eo._text_cache.update(tsaved)
        eo._chain_cache.clear()
        eo._chain_cache.update(csaved)
        eo._map_cache.clear()
        eo._map_cache.update(msaved)


def _csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r.get(h, "") for h in HEADER])
    return buf.getvalue()


def _opt(sym, exp, strike, ot, close, oi, prev=None, chg=None, vol=None, spot=None):
    return {"TradDt": "2026-07-16", "FinInstrmTp": "IDO" if sym == "NIFTY" else "STO",
            "TckrSymb": sym, "XpryDt": exp, "StrkPric": strike, "OptnTp": ot,
            "ClsPric": close, "PrvsClsgPric": "" if prev is None else prev,
            "OpnIntrst": oi, "ChngInOpnIntrst": "" if chg is None else chg,
            "TtlTradgVol": "" if vol is None else vol,
            "UndrlygPric": "" if spot is None else spot}


def _acme_chain_text():
    """A small ACME chain: spot 100, strikes 90-110, near + far expiry."""
    ce_oi = {90: 100, 95: 200, 100: 900, 105: 500, 110: 1200}
    pe_oi = {90: 1500, 95: 800, 100: 700, 105: 150, 110: 50}
    rows = []
    for k in (90, 95, 100, 105, 110):
        rows.append(_opt("ACME", "2026-07-28", k, "CE", max(1, 105 - k), ce_oi[k],
                         prev=max(1, 106 - k), chg=10, vol=50, spot=100))
        rows.append(_opt("ACME", "2026-07-28", k, "PE", max(1, k - 95), pe_oi[k],
                         prev=max(1, k - 94), chg=-5, vol=40, spot=100))
    rows.append(_opt("ACME", "2026-08-25", 100, "CE", 8, 300, spot=100))
    rows.append(_opt("NIFTY", "2026-07-28", 24000, "CE", 120, 5000))  # other symbol
    return _csv(rows)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_fmt_expiry():
    assert eo._fmt_expiry("2026-07-28") == "28-Jul-2026"
    assert eo._fmt_expiry("garbage") == "garbage"
    assert eo._fmt_expiry(None) is None


def test_atm_picks_nearest_strike():
    rows = [{"strike": 90.0}, {"strike": 100.0}, {"strike": 110.0}]
    assert eo._atm(rows, 102.0) == 100.0
    assert eo._atm(rows, 106.0) == 110.0
    assert eo._atm(rows, None) is None
    assert eo._atm([], 100.0) is None


def test_walls_top_oi():
    rows = [
        {"strike": 90.0, "pe": {"oi": 1500}, "ce": {"oi": 100}},
        {"strike": 100.0, "pe": {"oi": 700}, "ce": {"oi": 900}},
        {"strike": 110.0, "pe": {"oi": 50}, "ce": {"oi": 1200}},
    ]
    sup = eo._walls(rows, "pe", n=2)
    assert [x["strike"] for x in sup] == [90.0, 100.0]        # biggest PUT OI first
    res = eo._walls(rows, "ce", n=2)
    assert [x["strike"] for x in res] == [110.0, 100.0]       # biggest CALL OI first


def test_norm_leg_fills_missing_live_fields():
    leg = eo._norm_leg({"oi": 900, "ltp": 5})
    assert leg["iv"] is None and leg["bid"] is None and leg["ask"] is None
    assert leg["pChgOi"] is None
    assert eo._norm_leg(None) is None


# ---------------------------------------------------------------------------
# _assemble (pure over a parsed dict)
# ---------------------------------------------------------------------------
def _parsed_acme():
    from nse_pulse.eod import bhavcopy
    return bhavcopy.parse_fo_options(_acme_chain_text(), "ACME")


def test_assemble_matches_live_shape():
    ch = eo._assemble(_parsed_acme(), "2026-07-16", "ACME")
    # same keys the live get_option_chain returns (so the UI renderer is reused)
    assert set(ch) >= {"symbol", "expiry", "expiries", "underlying", "rows",
                       "ceTotOI", "peTotOI", "pcr", "maxPain", "atmStrike",
                       "support", "resistance", "lotSize"}
    assert ch["eod"] is True and ch["date"] == "2026-07-16"
    assert ch["expiry"] == "28-Jul-2026"                      # nearest, display fmt
    assert ch["expiries"] == ["28-Jul-2026", "25-Aug-2026"]
    assert ch["underlying"] == 100.0
    assert [r["strike"] for r in ch["rows"]] == [90.0, 95.0, 100.0, 105.0, 110.0]
    assert ch["ceTotOI"] == 2900.0 and ch["peTotOI"] == 3200.0
    assert ch["pcr"] == round(3200 / 2900, 2)
    assert ch["maxPain"] == 100.0 and ch["atmStrike"] == 100.0
    assert [x["strike"] for x in ch["support"]][0] == 90.0    # biggest PUT OI
    assert [x["strike"] for x in ch["resistance"]][0] == 110.0  # biggest CALL OI
    leg = ch["rows"][2]["ce"]
    assert leg["iv"] is None and leg["bid"] is None           # bhavcopy has no IV/quote


def test_assemble_selects_requested_expiry_iso_or_display():
    for want in ("2026-08-25", "25-Aug-2026"):
        ch = eo._assemble(_parsed_acme(), "2026-07-16", "ACME", expiry=want)
        assert ch["expiry"] == "25-Aug-2026"
        assert [r["strike"] for r in ch["rows"]] == [100.0]


def test_assemble_empty_when_no_options():
    ch = eo._assemble({"byExpiry": {}, "expiries": []}, "2026-07-16", "ZZZ")
    assert ch["rows"] == [] and ch["eod"] is True
    assert "error" in ch


# ---------------------------------------------------------------------------
# chain() / summary() end-to-end (stubbed fetch)
# ---------------------------------------------------------------------------
def test_chain_end_to_end_and_text_cache():
    from nse_pulse.eod import bhavcopy
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return "2026-07-16", _acme_chain_text()

    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text", fake_fetch):
        ch = eo.chain("acme")                    # case-insensitive
        assert ch["symbol"] == "ACME" and ch["pcr"] == round(3200 / 2900, 2)
        assert ch["maxPain"] == 100.0 and ch["eod"] is True
        eo.chain("ACME")                          # cached chain (same key)
        eo.chain("ACME", "25-Aug-2026")           # different expiry, still one fetch
        assert calls["n"] == 1                    # FO text fetched once (text cache)


def test_chain_guards():
    from nse_pulse.eod import bhavcopy
    assert eo.chain("")["error"] == "no symbol"
    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text", lambda: (None, None)):
        ch = eo.chain("ACME")
        assert ch["rows"] == [] and "unavailable" in ch["error"]
    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text",
                                 lambda: ("2026-07-16", _acme_chain_text())):
        miss = eo.chain("NOSUCH")
        assert miss["rows"] == [] and "error" in miss


def test_summary_per_expiry():
    from nse_pulse.eod import bhavcopy
    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text",
                                 lambda: ("2026-07-16", _acme_chain_text())):
        s = eo.summary("ACME")
    assert s["symbol"] == "ACME" and s["eod"] is True and s["underlying"] == 100.0
    exps = {e["expiry"]: e for e in s["expiries"]}
    assert set(exps) == {"28-Jul-2026", "25-Aug-2026"}
    assert exps["28-Jul-2026"]["maxPain"] == 100.0
    assert exps["28-Jul-2026"]["ceTotOI"] == 2900.0
    assert exps["25-Aug-2026"]["ceTotOI"] == 300.0


def test_summary_empty_when_no_text():
    from nse_pulse.eod import bhavcopy
    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text", lambda: (None, None)):
        s = eo.summary("ACME")
    assert s["expiries"] == [] and s["eod"] is True


# ---------------------------------------------------------------------------
# oi_map() — market-wide analytics in one parse (the conviction-board fuse)
# ---------------------------------------------------------------------------
def test_oi_map_all_underlyings_one_parse():
    from nse_pulse.eod import bhavcopy
    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text",
                                 lambda: ("2026-07-16", _acme_chain_text())):
        date, m = eo.oi_map()
    assert date == "2026-07-16"
    assert {"ACME", "NIFTY"} <= set(m)               # every underlying, one pass
    a = m["ACME"]
    assert a["expiry"] == "28-Jul-2026"              # nearest, display fmt
    assert a["maxPain"] == 100.0 and a["pcr"] == round(3200 / 2900, 2)
    assert a["resistance"][0]["strike"] == 110.0     # biggest CALL OI = resistance
    assert a["support"][0]["strike"] == 90.0         # biggest PUT OI = support


def test_oi_map_cached_then_empty_without_text():
    from nse_pulse.eod import bhavcopy
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return "2026-07-16", _acme_chain_text()

    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text", fake):
        eo.oi_map()
        eo.oi_map()
        assert calls["n"] == 1                        # 15-min map cache → one parse
    with _reset_caches(), _patch(bhavcopy, "fetch_fo_text", lambda: (None, None)):
        date, m = eo.oi_map()
        assert m == {}                                # no text → empty, no crash


def test_status_shape():
    with _reset_caches():
        st = eo.status()
    assert st["cached"] is False and st["ttlSec"] == eo._TEXT_TTL
    assert "bhavcopy" in st["source"].lower()


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
