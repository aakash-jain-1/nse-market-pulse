"""
Unit tests for angel_feed.py + dhan_feed.py — the live-feed adapters.

They share a public interface, so the shared pure logic (is_market_open,
_market_window, _coarse_error, _to_f, public_status shape, forming-bar folding,
set_watch/snapshot) is checked for BOTH via helpers, plus provider-specific
config precedence (env → json) and depth normalization. Nothing here needs the
broker SDK or the network: config files are redirected to temp paths, env is
controlled, and the instrument master is populated by hand.

Run: python test_feeds.py   (also works under pytest)
"""

import contextlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone

from nse_pulse.feeds import angel_feed
from nse_pulse.core import db
from nse_pulse.feeds import dhan_feed


def _baked(y, mo, d, h, mi):
    """IST wall-clock baked as UTC → epoch ms (matches angel_feed._baked_ms /
    get_ohlc `t`), for asserting candle timestamps."""
    return int(datetime(y, mo, d, h, mi).replace(tzinfo=timezone.utc).timestamp() * 1000)

ANGEL_KEYS = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET"]
DHAN_KEYS = ["DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"]


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _env(clear, **setvals):
    """Set given env vars, delete the rest of `clear`, restore everything after."""
    allkeys = set(clear) | set(setvals)
    saved = {k: os.environ.get(k) for k in allkeys}
    for k in clear:
        os.environ.pop(k, None)
    for k, v in setvals.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _cfg(mod, json_obj=None):
    """Point mod.CONFIG_JSON at a temp file (or a nonexistent path) + reset cache."""
    d = tempfile.mkdtemp(prefix="nse_feed_test_")
    saved_path, saved_cache = mod.CONFIG_JSON, dict(mod._config_cache)
    if json_obj is None:
        mod.CONFIG_JSON = os.path.join(d, "nope.json")
    else:
        p = os.path.join(d, "cfg.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(json_obj, f)
        mod.CONFIG_JSON = p
    mod._config_cache = {"mtime": None, "data": None}
    try:
        yield
    finally:
        mod.CONFIG_JSON, mod._config_cache = saved_path, saved_cache
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _feed_state(mod):
    """Snapshot & clear the in-memory feed store so a test starts blank."""
    # _sec2trad / _index_tokens exist on the angel adapter only (dhan indexes cash
    # equities only), so take whichever the module actually carries.
    dicts = [k for k in ("_watch", "_latest", "_bars", "_sym2sec", "_sec2sym",
                         "_sec2trad", "_index_tokens") if hasattr(mod, k)]
    saved = {k: (dict(getattr(mod, k)) if isinstance(getattr(mod, k), dict)
                 else set(getattr(mod, k))) for k in dicts}
    saved_scalars = {"_scrip_at": mod._scrip_at, "_focus": mod._focus}
    for k in dicts:
        getattr(mod, k).clear()
    mod._scrip_at, mod._focus = 0.0, None
    try:
        yield
    finally:
        for k in dicts:
            getattr(mod, k).clear()
            getattr(mod, k).update(saved[k])
        mod._scrip_at, mod._focus = saved_scalars["_scrip_at"], saved_scalars["_focus"]


# ---------------------------------------------------------------------------
# shared checks
# ---------------------------------------------------------------------------
def _check_market_open(mod):
    assert mod.is_market_open(datetime(2026, 7, 16, 10, 0)) is True
    assert mod.is_market_open(datetime(2026, 7, 16, 9, 14)) is False
    assert mod.is_market_open(datetime(2026, 7, 16, 9, 15)) is True
    assert mod.is_market_open(datetime(2026, 7, 16, 15, 30)) is True
    assert mod.is_market_open(datetime(2026, 7, 16, 15, 31)) is False
    assert mod.is_market_open(datetime(2026, 7, 18, 10, 0)) is False   # Saturday


def _check_market_window(mod):
    assert mod._market_window(datetime(2026, 7, 16, 9, 8)) is True     # pre-open
    assert mod._market_window(datetime(2026, 7, 16, 9, 7)) is False
    assert mod._market_window(datetime(2026, 7, 16, 15, 40)) is True   # closing auction
    assert mod._market_window(datetime(2026, 7, 16, 15, 41)) is False
    assert mod._market_window(datetime(2026, 7, 19, 10, 0)) is False   # Sunday


def _check_coarse(mod):
    assert mod._coarse_error(None) is None
    assert mod._coarse_error("HTTP 401 Unauthorized") == "auth_failed"
    assert mod._coarse_error("429 Too Many Requests") == "rate_limited"
    assert mod._coarse_error("Connection timed out") == "network"
    assert mod._coarse_error("subscription not active") == "data_plan"
    assert mod._coarse_error("weird boom") == "error"
    # never echo secrets back to the UI
    out = mod._coarse_error("login failed jwt=SECRETTOKEN")
    assert out == "auth_failed" and "SECRETTOKEN" not in out


def _check_to_f(mod):
    assert mod._to_f("5") == 5.0 and mod._to_f(None) is None and mod._to_f("x") is None


def _check_public_status(mod, provider, keys):
    with _env(keys), _cfg(mod, None), _feed_state(mod):
        st = mod.public_status()
    assert st["provider"] == provider
    assert st["configured"] is False
    assert isinstance(st["marketOpen"], bool)
    assert st["watching"] == []
    assert set(st) >= {"provider", "configured", "connected", "marketOpen",
                       "error", "watching", "running"}


def _check_set_watch_snapshot(mod):
    with _feed_state(mod):
        mod._sym2sec.update({"RELIANCE": "2885"})
        mod._sec2sym.update({"2885": "RELIANCE"})
        mod._scrip_at = time.time()          # keeps _load_scrip from going to network
        res = mod.set_watch(["reliance", "ghost"])
        assert res["resolved"] == {"RELIANCE": "2885"}
        assert res["unresolved"] == ["GHOST"]
        assert res["watching"] == ["RELIANCE"]
        mod._bars["2885"] = {"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}
        snap = mod.snapshot()
        assert "RELIANCE" in snap and snap["RELIANCE"]["bar"]["o"] == 1


def _check_update_bar(mod):
    with _feed_state(mod):
        mod._sec2sym.update({"T": "TSYM"})
        finalized = []
        with _patch(db, "min_bars_put",
                    lambda sym, pts: (finalized.append((sym, pts)), len(pts))[1]):
            mod._update_bar("T", 60_000_000, 100.0, 1000)
            assert mod._bars["T"] == {"t": 60_000_000, "o": 100.0, "h": 100.0,
                                      "l": 100.0, "c": 100.0, "v": 0, "_sv": 1000}
            mod._update_bar("T", 60_030_000, 105.0, 1200)      # same minute, up
            b = mod._bars["T"]
            assert b["h"] == 105.0 and b["c"] == 105.0 and b["v"] == 200
            mod._update_bar("T", 60_030_001, 95.0, 1250)       # same minute, down
            b = mod._bars["T"]
            assert b["l"] == 95.0 and b["v"] == 250
            mod._update_bar("T", 60_060_000, 110.0, 1300)      # rollover → finalize
            assert finalized and finalized[0][0] == "TSYM"
            assert mod._bars["T"]["o"] == 110.0                # fresh candle


# ---------------------------------------------------------------------------
# angel
# ---------------------------------------------------------------------------
def test_angel_market_open():
    _check_market_open(angel_feed)


def test_angel_market_window():
    _check_market_window(angel_feed)


def test_angel_coarse_error():
    _check_coarse(angel_feed)
    assert angel_feed._coarse_error("invalid totp") == "auth_failed"   # angel-specific


def test_angel_to_f():
    _check_to_f(angel_feed)


def test_angel_px():
    assert angel_feed._px(10000) == 100.0 and angel_feed._px(None) is None


def test_angel_config_env_precedence():
    with _env(ANGEL_KEYS, ANGEL_API_KEY="k", ANGEL_CLIENT_CODE="c",
              ANGEL_MPIN="m", ANGEL_TOTP_SECRET="t"), _cfg(angel_feed, None):
        assert angel_feed._load_config() == {"api_key": "k", "client_code": "c",
                                             "mpin": "m", "totp_secret": "t"}
        assert angel_feed.is_configured() is True


def test_angel_config_json_aliases():
    with _env(ANGEL_KEYS), _cfg(angel_feed, {"api_key": "k", "client_code": "c",
                                             "pin": "1234", "totp": "sec"}):
        c = angel_feed._load_config()
        assert c["mpin"] == "1234" and c["totp_secret"] == "sec"
        assert angel_feed.is_configured() is True


def test_angel_config_none():
    with _env(ANGEL_KEYS), _cfg(angel_feed, None):
        assert angel_feed.is_configured() is False


def test_angel_norm_depth():
    d = angel_feed._norm_depth([{"price": 10000, "quantity": 50}],
                               [{"price": 10100, "quantity": 30}])
    assert d["bids"][0] == {"price": 100.0, "qty": 50}
    assert d["asks"][0] == {"price": 101.0, "qty": 30}


def test_angel_public_status():
    _check_public_status(angel_feed, "angel", ANGEL_KEYS)


def test_angel_set_watch_snapshot():
    _check_set_watch_snapshot(angel_feed)


def test_angel_update_bar():
    _check_update_bar(angel_feed)


# ---------------------------------------------------------------------------
# angel instrument master — NSE cash equities AND indices (same segment, so the
# indices are streamable through the very same subscription)
# ---------------------------------------------------------------------------
SCRIP_ROWS = [
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE",
     "exch_seg": "NSE", "instrumenttype": ""},
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY",
     "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
    {"token": "99926009", "symbol": "Nifty Bank", "name": "BANKNIFTY",
     "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
    {"token": "99926017", "symbol": "India VIX", "name": "INDIA VIX",
     "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
    # noise that must NOT be indexed: another segment, and a non-EQ NSE series
    {"token": "44444", "symbol": "RELIANCE25AUGFUT", "name": "RELIANCE",
     "exch_seg": "NFO", "instrumenttype": "FUTSTK"},
    {"token": "55555", "symbol": "SOMECO-BE", "name": "SOMECO",
     "exch_seg": "NSE", "instrumenttype": ""},
]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@contextlib.contextmanager
def _scrip(rows):
    """Load a fake Angel scrip master into a blank feed state."""
    class _Req:
        @staticmethod
        def get(url, timeout=None):
            return _FakeResp(rows)
    with _feed_state(angel_feed), _patch(angel_feed, "requests", _Req):
        angel_feed._load_scrip(force=True)
        yield


def test_angel_scrip_master_indexes_equities_and_indices():
    with _scrip(SCRIP_ROWS):
        assert angel_feed.resolve("reliance") == "2885"
        assert angel_feed.resolve("NIFTY") == "99926000"
        assert angel_feed.resolve("BANKNIFTY") == "99926009"
        assert angel_feed.resolve("INDIA VIX") == "99926017"
        # other segments + non-EQ series stay out
        assert angel_feed.resolve("RELIANCE25AUGFUT") is None
        assert angel_feed.resolve("SOMECO") is None
        # the exchange tradingsymbol is kept verbatim — an index has no -EQ series
        assert angel_feed._sec2trad["2885"] == "RELIANCE-EQ"
        assert angel_feed._sec2trad["99926009"] == "Nifty Bank"


def test_angel_resolve_accepts_index_aliases():
    with _scrip(SCRIP_ROWS):
        for alias in ("NIFTY 50", "nifty50", "Nifty 50"):
            assert angel_feed.resolve(alias) == "99926000", alias
        for alias in ("NIFTY BANK", "bank nifty", "Nifty Bank"):
            assert angel_feed.resolve(alias) == "99926009", alias
        for alias in ("INDIAVIX", "vix", "India VIX"):
            assert angel_feed.resolve(alias) == "99926017", alias


def test_angel_equity_wins_a_name_collision_with_an_index():
    """A tradable stock must never be shadowed by a same-named index."""
    rows = SCRIP_ROWS + [{"token": "99926099", "symbol": "RELIANCE",
                          "name": "RELIANCE", "exch_seg": "NSE",
                          "instrumenttype": "AMXIDX"}]
    with _scrip(rows):
        assert angel_feed.resolve("RELIANCE") == "2885"
        assert angel_feed.is_index("RELIANCE") is False


def test_angel_is_index_and_index_symbols():
    with _scrip(SCRIP_ROWS):
        assert angel_feed.is_index("BANKNIFTY") is True
        assert angel_feed.is_index("nifty 50") is True     # via an alias
        assert angel_feed.is_index("99926017") is True     # via the raw token
        assert angel_feed.is_index("RELIANCE") is False
        assert angel_feed.is_index("") is False
        assert angel_feed.is_index(None) is False
        assert angel_feed.index_symbols() == ["BANKNIFTY", "INDIA VIX", "NIFTY"]


def test_angel_snapshot_flags_index_records():
    """An index has no volume/OI/order book — the payload must say so, so the UI
    renders '—' instead of a misleading 0 and an empty depth ladder."""
    with _scrip(SCRIP_ROWS):
        angel_feed.set_watch(["RELIANCE", "BANKNIFTY"])
        snap = angel_feed.snapshot()
        assert snap["BANKNIFTY"]["isIndex"] is True
        assert "isIndex" not in snap["RELIANCE"]
        assert "BANKNIFTY" in angel_feed.public_status()["indices"]


#: A real SNAP_QUOTE packet for an index, as observed live on 2026-08-24. Angel
#: fills the traded-instrument fields anyway — volume/ATP zero and a SENTINEL book
#: of price -0.01 / qty -1 — so the handler has to drop them rather than pass off
#: junk as a real order book.
INDEX_TICK = {
    "token": "99926009", "last_traded_price": 5512345,      # paise
    "open_price_of_the_day": 5500000, "high_price_of_the_day": 5520000,
    "low_price_of_the_day": 5495000, "closed_price": 5498000,
    "volume_trade_for_the_day": 0, "average_traded_price": 0, "open_interest": 0,
    "best_5_buy_data": [{"price": -1, "quantity": -1}] * 5,
    "best_5_sell_data": [],
}


def test_angel_index_tick_keeps_the_level_and_drops_the_fake_book():
    with _scrip(SCRIP_ROWS):
        angel_feed._on_data(None, dict(INDEX_TICK))
        rec = angel_feed.snapshot(["BANKNIFTY"])["BANKNIFTY"]
    assert rec["ltp"] == 55123.45 and rec["prevClose"] == 54980.0
    assert rec["open"] == 55000.0 and rec["high"] == 55200.0
    assert rec["isIndex"] is True
    # no volume / OI / VWAP / depth is recorded for an index — not even as zeros
    for k in ("volume", "oi", "atp", "depth"):
        assert k not in rec, k
    # ...but the level still folds into the forming 1-min candle
    assert rec["bar"]["c"] == 55123.45 and rec["bar"]["v"] == 0


def test_angel_equity_tick_still_records_volume_and_depth():
    """The index guard must not touch the cash-equity path."""
    tick = dict(INDEX_TICK, token="2885",
                best_5_buy_data=[{"price": 130590, "quantity": 3092}],
                best_5_sell_data=[{"price": 130600, "quantity": 846}],
                volume_trade_for_the_day=2226917, average_traded_price=131276,
                open_interest=267781000)
    with _scrip(SCRIP_ROWS):
        angel_feed._on_data(None, tick)
        rec = angel_feed.snapshot(["RELIANCE"])["RELIANCE"]
    assert rec["volume"] == 2226917 and rec["oi"] == 267781000
    assert rec["atp"] == 1312.76
    assert rec["depth"]["bids"][0] == {"price": 1305.9, "qty": 3092}
    assert "isIndex" not in rec


def test_angel_rest_quote_uses_the_master_tradingsymbol_for_an_index():
    """An index tradingsymbol ("Nifty Bank") can't be rebuilt by appending -EQ."""
    asked = {}

    class _Ltp:
        def ltpData(self, exch, tsym, tok):
            asked.update(exch=exch, tsym=tsym, tok=tok)
            return {"data": {"ltp": 55123.45, "close": 54980.0}}

    with _scrip(SCRIP_ROWS), _patch(angel_feed, "_smart", _Ltp()):
        q = angel_feed.rest_quote("banknifty")
    assert asked == {"exch": "NSE", "tsym": "Nifty Bank", "tok": "99926009"}
    assert q["ltp"] == 55123.45 and q["symbol"] == "BANKNIFTY"


def test_angel_rest_quote_blanks_traded_only_fields_for_an_index():
    """getMarketData reports 0 volume and a zeroed book for an index — report those
    as absent instead, matching the streaming path (this feeds the Live poll)."""
    class _Full:
        def getMarketData(self, mode, tokens):
            return {"data": {"fetched": [{
                "ltp": 55123.45, "close": 54980.0, "open": 55000.0,
                "high": 55200.0, "low": 54950.0, "tradeVolume": 0, "avgPrice": 0,
                "depth": {"buy": [{"price": 0, "quantity": 0}], "sell": []}}]}}

    with _scrip(SCRIP_ROWS), _patch(angel_feed, "_smart", _Full()):
        idx = angel_feed.rest_quote("BANKNIFTY")
        eq = angel_feed.rest_quote("RELIANCE")
    assert idx["isIndex"] is True and idx["ltp"] == 55123.45
    assert idx["volume"] is None and idx["vwap"] is None
    assert idx["depth"]["bids"][0] == {"price": None, "qty": None}
    # the same payload on an equity token keeps whatever the broker reported
    assert "isIndex" not in eq and eq["volume"] == 0


# ---------------------------------------------------------------------------
# angel F&O legs (options + futures) — a second exchange segment on one socket
# ---------------------------------------------------------------------------
#: NFO rows in the master's real shape: `symbol` is the opaque tradingsymbol, `name`
#: the underlying, `strike` is in paise like every other price, and weekly index
#: expiries list options but no future.
FNO_ROWS = [
    {"token": "61647", "symbol": "NIFTY25AUG2624200CE", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "25AUG2026",
     "strike": "2420000.000000", "lotsize": "65"},
    {"token": "61648", "symbol": "NIFTY25AUG2624200PE", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "25AUG2026",
     "strike": "2420000.000000", "lotsize": "65"},
    {"token": "61649", "symbol": "NIFTY25AUG2624300CE", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "25AUG2026",
     "strike": "2430000.000000", "lotsize": "65"},
    # a LATER expiry, listed FIRST, to prove chronological (not alphabetical) sorting
    {"token": "61650", "symbol": "NIFTY29DEC2624000CE", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "29DEC2026",
     "strike": "2400000.000000", "lotsize": "65"},
    {"token": "61651", "symbol": "NIFTY01SEP2624000CE", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "01SEP2026",
     "strike": "2400000.000000", "lotsize": "65"},
    {"token": "61660", "symbol": "NIFTY25AUG26FUT", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "FUTIDX", "expiry": "25AUG2026",
     "strike": "-1.000000", "lotsize": "65"},
    {"token": "61670", "symbol": "RELIANCE25AUG261400CE", "name": "RELIANCE",
     "exch_seg": "NFO", "instrumenttype": "OPTSTK", "expiry": "25AUG2026",
     "strike": "140000.000000", "lotsize": "500"},
    # malformed rows that must be skipped, not crash: no expiry, an option whose
    # tradingsymbol doesn't end CE/PE, and an instrumenttype we don't trade.
    {"token": "70001", "symbol": "NIFTY24000CE", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "",
     "strike": "2400000.000000", "lotsize": "65"},
    {"token": "70002", "symbol": "NIFTY25AUG2624000XX", "name": "NIFTY",
     "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "25AUG2026",
     "strike": "2400000.000000", "lotsize": "65"},
    {"token": "70003", "symbol": "USDINR25AUG26FUT", "name": "USDINR",
     "exch_seg": "CDS", "instrumenttype": "FUTCUR", "expiry": "25AUG2026",
     "strike": "-1.000000", "lotsize": "1000"},
]

ALL_ROWS = SCRIP_ROWS + FNO_ROWS


def test_angel_expiry_key_sorts_expiries_chronologically():
    keys = sorted(["29DEC2026", "01SEP2026", "25AUG2026"], key=angel_feed._expiry_key)
    assert keys == ["25AUG2026", "01SEP2026", "29DEC2026"]
    # an unparseable value sorts last instead of raising
    assert angel_feed._expiry_key("junk") == (9999, 99, 99)


def test_angel_parse_fno_builds_the_contract_tree():
    """Pure: the whole tree shape is checked without the 37 MB download."""
    sym2tok, meta, tree = angel_feed._parse_fno(FNO_ROWS)
    assert sym2tok["NIFTY25AUG2624200CE"] == "61647"
    # strikes are quoted in paise and must be divided down to rupees
    assert meta["61647"] == {"underlying": "NIFTY", "expiry": "25AUG2026",
                             "strike": 24200.0, "optType": "CE", "lot": 65,
                             "tsym": "NIFTY25AUG2624200CE", "kind": "option"}
    assert meta["61660"]["kind"] == "future" and meta["61660"]["strike"] is None
    assert tree["NIFTY"]["25AUG2026"]["CE"][24200.0] == "61647"
    assert tree["NIFTY"]["25AUG2026"]["PE"][24200.0] == "61648"
    assert tree["NIFTY"]["25AUG2026"]["FUT"] == "61660"
    assert tree["NIFTY"]["25AUG2026"]["lot"] == 65
    # the malformed / other-segment rows are dropped
    assert set(tree) == {"NIFTY", "RELIANCE"}
    for bad in ("70001", "70002", "70003"):
        assert bad not in meta, bad


def test_angel_fno_contracts_do_not_shadow_cash_symbols():
    """~36k contracts must stay OUT of the cash map, or RELIANCE could resolve to an
    option and the equity watchlist would silently break."""
    with _scrip(ALL_ROWS):
        assert angel_feed.resolve("RELIANCE") == "2885"
        assert angel_feed.resolve("NIFTY") == "99926000"
        assert not any(r["symbol"] in angel_feed._sym2sec for r in FNO_ROWS)
        # ...but the tradingsymbol itself resolves, so the watchlist can subscribe it
        assert angel_feed.resolve("nifty25aug2624200ce") == "61647"
        assert angel_feed.is_fno("NIFTY25AUG2624200CE") is True
        assert angel_feed.is_fno("61660") is True
        assert angel_feed.is_fno("RELIANCE") is False
        assert angel_feed.is_fno(None) is False


def test_angel_segment_and_exchange_follow_the_token():
    with _scrip(ALL_ROWS):
        assert angel_feed.segment_of("61647") == angel_feed.NSE_FO
        assert angel_feed.segment_of("2885") == angel_feed.NSE_CM
        assert angel_feed.segment_of("99926009") == angel_feed.NSE_CM
        assert angel_feed.exchange_of("61647") == "NFO"
        assert angel_feed.exchange_of("2885") == "NSE"


def test_angel_subscribe_payload_groups_tokens_by_segment():
    """Cash, indices and F&O share ONE socket, but each token must be listed under
    its own exchangeType or the broker silently drops the subscription."""
    with _scrip(ALL_ROWS):
        assert angel_feed._by_segment({"2885", "61647", "99926009", "61660"}) == [
            {"exchangeType": angel_feed.NSE_CM, "tokens": ["2885", "99926009"]},
            {"exchangeType": angel_feed.NSE_FO, "tokens": ["61647", "61660"]},
        ]


def test_angel_set_watch_subscribes_an_fno_leg_under_nfo():
    calls = []

    class _Sws:
        def subscribe(self, corr, mode, payload):
            calls.append(("sub", payload))

        def unsubscribe(self, corr, mode, payload):
            calls.append(("unsub", payload))

    with _scrip(ALL_ROWS), _patch(angel_feed, "_sws", _Sws()):
        res = angel_feed.set_watch(["RELIANCE", "NIFTY25AUG2624200CE"])
        assert res["unresolved"] == []
        assert res["watching"] == ["NIFTY25AUG2624200CE", "RELIANCE"]
        assert calls == [("sub", [
            {"exchangeType": angel_feed.NSE_CM, "tokens": ["2885"]},
            {"exchangeType": angel_feed.NSE_FO, "tokens": ["61647"]},
        ])]
        calls.clear()
        angel_feed.set_watch(["RELIANCE"])           # drop the leg
        assert calls == [("unsub", [{"exchangeType": angel_feed.NSE_FO,
                                     "tokens": ["61647"]}])]


def test_angel_fno_chain_walks_underlying_expiry_strike():
    with _scrip(ALL_ROWS):
        ch = angel_feed.fno_chain("nifty")
    # expiries chronological; the nearest is the default slice
    assert ch["expiries"] == ["25AUG2026", "01SEP2026", "29DEC2026"]
    assert ch["expiry"] == "25AUG2026" and ch["lot"] == 65
    assert ch["fut"] == "NIFTY25AUG26FUT"
    # each strike carries the tradingsymbol POST /api/live/watch takes directly
    assert ch["strikes"] == [
        {"strike": 24200.0, "ce": "NIFTY25AUG2624200CE", "pe": "NIFTY25AUG2624200PE"},
        {"strike": 24300.0, "ce": "NIFTY25AUG2624300CE", "pe": None},
    ]


def test_angel_fno_chain_honours_an_explicit_expiry():
    with _scrip(ALL_ROWS):
        ch = angel_feed.fno_chain("NIFTY", "29dec2026")
    assert ch["expiry"] == "29DEC2026"
    assert ch["strikes"] == [{"strike": 24000.0, "ce": "NIFTY29DEC2624000CE",
                              "pe": None}]
    assert ch["fut"] is None          # a far expiry here lists no future


def test_angel_fno_chain_and_underlyings_handle_unknown_names():
    with _scrip(ALL_ROWS):
        assert angel_feed.fno_underlyings() == ["NIFTY", "RELIANCE"]
        empty = angel_feed.fno_chain("NOSUCH")
    # a miss carries the SAME keys as a hit, so the caller never special-cases it
    assert empty == {"underlying": "NOSUCH", "expiry": None, "expiries": [],
                     "strikes": [], "fut": None, "lot": None}


def test_angel_fno_meta_reads_by_symbol_or_token():
    with _scrip(ALL_ROWS):
        by_sym = angel_feed.fno_meta("NIFTY25AUG26FUT")
        assert by_sym == angel_feed.fno_meta("61660")
        assert angel_feed.fno_meta("RELIANCE") is None
    assert by_sym["underlying"] == "NIFTY" and by_sym["kind"] == "future"
    assert by_sym["lot"] == 65


def test_angel_fno_tick_keeps_volume_oi_and_carries_its_contract_parts():
    """Unlike an index, an option IS traded — volume, OI and the book are real. The
    record also carries the leg's parts so the UI can label an opaque tradingsymbol."""
    tick = dict(INDEX_TICK, token="61647", last_traded_price=18525,
                volume_trade_for_the_day=4_512_300, open_interest=9_120_750,
                average_traded_price=18400,
                best_5_buy_data=[{"price": 18500, "quantity": 650}],
                best_5_sell_data=[{"price": 18550, "quantity": 1300}])
    with _scrip(ALL_ROWS):
        angel_feed._on_data(None, tick)
        rec = angel_feed.snapshot(["NIFTY25AUG2624200CE"])["NIFTY25AUG2624200CE"]
    assert rec["ltp"] == 185.25 and rec["volume"] == 4_512_300
    assert rec["oi"] == 9_120_750 and rec["atp"] == 184.0
    assert rec["depth"]["bids"][0] == {"price": 185.0, "qty": 650}
    assert "isIndex" not in rec
    assert rec["fno"]["underlying"] == "NIFTY" and rec["fno"]["strike"] == 24200.0
    assert rec["fno"]["optType"] == "CE" and rec["fno"]["lot"] == 65


def test_angel_public_status_counts_fno_contracts_separately():
    with _scrip(SCRIP_ROWS):
        base = angel_feed.public_status()
    with _scrip(ALL_ROWS):
        st = angel_feed.public_status()
    assert base["fnoContracts"] == 0
    assert st["fnoContracts"] == 7          # the malformed/other-segment rows excluded
    # the legs are counted on their own — they must not inflate the cash/index map
    assert st["instruments"] == base["instruments"]


def test_angel_rest_calls_use_the_nfo_exchange_for_a_leg():
    """getMarketData / getCandleData key off `exchange`; an NFO token asked on "NSE"
    returns nothing, so the segment has to follow the token."""
    seen = {}

    class _Smart:
        def getMarketData(self, mode, tokens):
            seen["quote"] = dict(tokens)
            return {"data": {"fetched": [{"ltp": 185.25, "close": 180.0,
                                          "tradeVolume": 4512300}]}}

        def getCandleData(self, params):
            seen.setdefault("candles", []).append(params["exchange"])
            return {"data": [["2026-08-24T09:15:00+05:30", 180, 186, 179, 185, 5000]]}

    with _scrip(ALL_ROWS), _patch(angel_feed, "_smart", _Smart()), \
            _patch(angel_feed, "_candle_cache", {}), \
            _patch(angel_feed, "_candle_calls", angel_feed.collections.deque()):
        q = angel_feed.rest_quote("NIFTY25AUG2624200CE")
        angel_feed.rest_ohlc("NIFTY25AUG2624200CE", interval=15)
        angel_feed.rest_chart("NIFTY25AUG2624200CE")      # 5-min: a distinct cache key
        angel_feed.rest_ohlc("RELIANCE", interval=15)
    assert seen["quote"] == {"NFO": ["61647"]}
    assert seen["candles"] == ["NFO", "NFO", "NSE"]
    # an option is traded, so nothing is blanked the way an index's fields are
    assert q["ltp"] == 185.25 and q["volume"] == 4512300
    assert q["fno"]["optType"] == "CE"


# ---------------------------------------------------------------------------
# angel on-demand REST (stock-detail modal served from the broker, not NSE)
# ---------------------------------------------------------------------------
class _FakeSmart:
    """Minimal SmartConnect stand-in with the documented response shapes."""
    def __init__(self, market=True):
        self._market = market

    def getMarketData(self, mode, tokens):
        assert mode == "FULL"
        return {"data": {"fetched": [{
            "tradingSymbol": "RELIANCE-EQ", "ltp": 1450.5, "open": 1440.0,
            "high": 1460.0, "low": 1435.0, "close": 1442.0, "netChange": 8.5,
            "percentChange": 0.59, "avgPrice": 1448.0, "tradeVolume": 1234567,
            "52WeekHigh": 1600.0, "52WeekLow": 1100.0,
            "exchFeedTime": "2026-07-20 15:30:00",
            "depth": {"buy": [{"price": 1450.4, "quantity": 100, "orders": 3}],
                      "sell": [{"price": 1450.6, "quantity": 80, "orders": 4}]}}]}}

    def getCandleData(self, params):
        assert params["exchange"] == "NSE"
        assert params["interval"] in {"ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE",
                                      "ONE_DAY"}
        return {"data": [["2026-07-20T09:15:00+05:30", 1440, 1445, 1439, 1443, 10000],
                         ["2026-07-20T09:20:00+05:30", 1443, 1448, 1442, 1447, 12000]]}


class _LtpOnlySmart:
    """Older SDK: only ltpData (no getMarketData) → quote without depth."""
    def ltpData(self, exch, tsym, tok):
        assert exch == "NSE" and tsym == "RELIANCE-EQ" and tok == "2885"
        return {"data": {"ltp": 1451.0, "open": 1440.0, "high": 1460.0,
                         "low": 1435.0, "close": 1442.0}}


@contextlib.contextmanager
def _angel_rest(smart):
    with _feed_state(angel_feed):
        angel_feed._sym2sec.update({"RELIANCE": "2885"})
        angel_feed._sec2sym.update({"2885": "RELIANCE"})
        angel_feed._scrip_at = time.time()          # no network
        # Fresh candle cache + rate-limit window so the module-global TTL cache can't
        # leak candle rows between rest_* tests (they all resolve RELIANCE -> 2885).
        with _patch(angel_feed, "_smart", smart), \
                _patch(angel_feed, "_candle_cache", {}), \
                _patch(angel_feed, "_candle_calls", angel_feed.collections.deque()):
            yield


def test_angel_rest_quote_full_market_data():
    with _angel_rest(_FakeSmart()):
        q = angel_feed.rest_quote("reliance")
    assert q["symbol"] == "RELIANCE" and q["source"] == "angel"
    assert q["ltp"] == 1450.5 and q["change"] == 8.5 and q["prevClose"] == 1442.0
    assert q["volume"] == 1234567 and q["yearHigh"] == 1600.0
    assert len(q["depth"]["bids"]) == 5 and len(q["depth"]["asks"]) == 5
    assert q["depth"]["bids"][0] == {"price": 1450.4, "qty": 100}
    assert q["depth"]["asks"][1] == {"price": None, "qty": None}   # padded to 5


def test_angel_rest_quote_ltp_fallback_no_depth():
    with _angel_rest(_LtpOnlySmart()):
        q = angel_feed.rest_quote("RELIANCE")
    assert q["ltp"] == 1451.0 and q["source"] == "angel"
    assert q["change"] == 9.0 and round(q["pChange"], 2) == 0.62
    assert q["depth"]["bids"][0] == {"price": None, "qty": None}   # ltpData has no depth


def test_angel_rest_chart_maps_candles_to_points():
    with _angel_rest(_FakeSmart()):
        c = angel_feed.rest_chart("reliance")
    assert c["symbol"] == "RELIANCE" and c["source"] == "angel"
    assert len(c["points"]) == 2 and c["points"][0]["price"] == 1443
    # timestamps are IST-baked-as-UTC so seeded history lines up with live bars
    assert c["points"][0]["t"] == _baked(2026, 7, 20, 9, 15)


def test_angel_rest_ohlc_maps_candles():
    with _angel_rest(_FakeSmart()):
        o = angel_feed.rest_ohlc("reliance", interval=5)
        d = angel_feed.rest_ohlc("RELIANCE", chart_type="D", days=120)
    assert o["symbol"] == "RELIANCE" and o["source"] == "angel" and o["chartType"] == "I"
    assert o["points"][0] == {"t": _baked(2026, 7, 20, 9, 15), "o": 1440.0,
                              "h": 1445.0, "l": 1439.0, "c": 1443.0, "v": 10000.0}
    assert d["chartType"] == "D" and len(d["points"]) == 2


def test_angel_rest_guards_return_none():
    # no logged-in client → None
    with _feed_state(angel_feed), _patch(angel_feed, "_smart", None):
        assert angel_feed.rest_quote("RELIANCE") is None
        assert angel_feed.rest_chart("RELIANCE") is None
        assert angel_feed.rest_ohlc("RELIANCE") is None
    # unknown symbol → None; a raising client → None (caller falls back to NSE)
    with _angel_rest(_FakeSmart()):
        assert angel_feed.rest_quote("NOSUCH") is None
        assert angel_feed.rest_ohlc("NOSUCH") is None

    class _Boom:
        def getMarketData(self, *a):
            raise RuntimeError("angel down")
        def ltpData(self, *a):
            raise RuntimeError("angel down")
        def getCandleData(self, *a):
            raise RuntimeError("angel down")
    with _angel_rest(_Boom()):
        assert angel_feed.rest_quote("RELIANCE") is None
        assert angel_feed.rest_chart("RELIANCE") is None
        assert angel_feed.rest_ohlc("RELIANCE") is None


class _RateThenOk:
    """Fake SmartConnect.getCandleData: raise Angel's rate-limit text `fails` times,
    then succeed. Mirrors the live finding (bursts trip 'exceeding access rate')."""
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def getCandleData(self, params):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("Access denied because of exceeding access rate")
        return {"data": [["2026-07-20T09:15:00+05:30", 1, 2, 3, 4, 5]]}


@contextlib.contextmanager
def _fresh_candle_state():
    """Reset the sliding-window deque AND the TTL cache so a test starts clean."""
    with _patch(angel_feed, "_candle_calls", angel_feed.collections.deque()), \
            _patch(angel_feed, "_candle_cache", {}):
        yield


def test_angel_get_candles_retries_then_succeeds():
    # 2 rate-limit trips, success on the 3rd → rows, no NSE fallback.
    with _patch(angel_feed.time, "sleep", lambda *a: None), _fresh_candle_state():
        f = _RateThenOk(2)
        rows = angel_feed._get_candles(f, {})
    assert rows and f.calls == 3


def test_angel_get_candles_gives_up_after_retries():
    # persistent rate-limit → None (caller falls back to NSE) after backoff+1 tries
    with _patch(angel_feed.time, "sleep", lambda *a: None), _fresh_candle_state():
        f = _RateThenOk(99)
        assert angel_feed._get_candles(f, {}) is None
        assert f.calls == len(angel_feed._CANDLE_BACKOFF) + 1


def test_angel_get_candles_no_retry_on_other_error():
    # a non-rate error (e.g. bad token) shouldn't burn retries — fail fast to NSE.
    class _Boom:
        def __init__(self):
            self.calls = 0

        def getCandleData(self, p):
            self.calls += 1
            raise RuntimeError("Invalid token")

    with _patch(angel_feed.time, "sleep", lambda *a: None), _fresh_candle_state():
        f = _Boom()
        assert angel_feed._get_candles(f, {}) is None
        assert f.calls == 1


def test_angel_get_candles_caches_within_ttl():
    # Same (token, interval, from-date) within the TTL → one real Angel call, then cache.
    p = {"symboltoken": "2885", "interval": "FIVE_MINUTE",
         "fromdate": "2026-07-15 09:15", "todate": "2026-07-20 11:00"}
    with _patch(angel_feed.time, "sleep", lambda *a: None), _fresh_candle_state():
        f = _RateThenOk(0)
        r1 = angel_feed._get_candles(f, p)
        r2 = angel_feed._get_candles(f, dict(p, todate="2026-07-20 11:05"))  # todate ignored
        assert r1 and r2 == r1 and f.calls == 1


def test_angel_candle_cache_ttl_expiry():
    with _patch(angel_feed, "_candle_cache", {}):
        angel_feed._candle_cache_put("k", [1, 2, 3])
        assert angel_feed._candle_cache_get("k") == [1, 2, 3]
        # force-age the entry beyond the TTL → miss
        angel_feed._candle_cache["k"] = (time.time() - angel_feed._CANDLE_TTL - 1, [1, 2, 3])
        assert angel_feed._candle_cache_get("k") is None


def test_angel_get_candles_does_not_cache_failure():
    p = {"symboltoken": "X", "interval": "ONE_MINUTE",
         "fromdate": "2026-07-19 09:15", "todate": "2026-07-20 11:00"}
    with _patch(angel_feed.time, "sleep", lambda *a: None), _fresh_candle_state():
        f = _RateThenOk(99)
        assert angel_feed._get_candles(f, p) is None
        assert angel_feed._candle_cache == {}   # failures are never cached


def test_angel_candle_throttle_waits_on_full_per_minute_window():
    # A full sliding minute-window must block (Angel's 180/min "accumulation" trap),
    # waiting for the oldest call to age out (~60s) rather than firing and getting banned.
    slept = []
    now = time.time()
    dq = angel_feed.collections.deque([now - 1] * angel_feed._CANDLE_PER_MIN)
    with _patch(angel_feed.time, "sleep", lambda s: slept.append(s)), \
            _patch(angel_feed, "_candle_calls", dq):
        angel_feed._candle_throttle()
    assert slept and max(slept) > 50


def test_angel_baked_iso_to_ms():
    assert angel_feed._baked_iso_to_ms("bad") is None
    assert angel_feed._baked_iso_to_ms(None) is None
    # IST wall-clock is baked in as UTC (not shifted -5:30) so candles align with live
    assert angel_feed._baked_iso_to_ms("2026-07-20T09:15:00+05:30") == _baked(
        2026, 7, 20, 9, 15)


# ---------------------------------------------------------------------------
# dhan
# ---------------------------------------------------------------------------
def test_dhan_market_open():
    _check_market_open(dhan_feed)


def test_dhan_market_window():
    _check_market_window(dhan_feed)


def test_dhan_coarse_error():
    _check_coarse(dhan_feed)


def test_dhan_to_f():
    _check_to_f(dhan_feed)


def test_dhan_config_env_precedence():
    with _env(DHAN_KEYS, DHAN_CLIENT_ID="c", DHAN_ACCESS_TOKEN="t"), _cfg(dhan_feed, None):
        assert dhan_feed._load_config() == ("c", "t")
        assert dhan_feed.is_configured() is True


def test_dhan_config_json():
    with _env(DHAN_KEYS), _cfg(dhan_feed, {"client_id": "c", "access_token": "t"}):
        assert dhan_feed._load_config() == ("c", "t")
        assert dhan_feed.is_configured() is True


def test_dhan_config_none():
    with _env(DHAN_KEYS), _cfg(dhan_feed, None):
        assert dhan_feed.is_configured() is False


def test_dhan_norm_depth():
    d = dhan_feed._norm_depth([{"bid_price": 100.0, "bid_quantity": 50,
                                "ask_price": 101.0, "ask_quantity": 30}])
    assert d["bids"][0] == {"price": 100.0, "qty": 50}
    assert d["asks"][0] == {"price": 101.0, "qty": 30}


def test_dhan_public_status():
    _check_public_status(dhan_feed, "dhan", DHAN_KEYS)


def test_dhan_set_watch_snapshot():
    _check_set_watch_snapshot(dhan_feed)


def test_dhan_update_bar():
    _check_update_bar(dhan_feed)


def test_dhan_rest_stubs_return_none():
    # Dhan's data API isn't wired (paid plan) → safe no-ops so app.py falls back to NSE
    assert dhan_feed.rest_quote("RELIANCE") is None
    assert dhan_feed.rest_chart("RELIANCE") is None
    assert dhan_feed.rest_ohlc("RELIANCE") is None


def test_dhan_index_and_fno_stubs():
    """Both adapters must expose the same interface — this one indexes cash
    equities only, so nothing is ever an index or an F&O leg."""
    assert dhan_feed.is_index("NIFTY") is False
    assert dhan_feed.index_symbols() == []
    assert dhan_feed.is_fno("NIFTY25AUG2624200CE") is False
    assert dhan_feed.fno_meta("NIFTY25AUG2624200CE") is None
    assert dhan_feed.fno_underlyings() == []
    # identical shape across adapters, so the picker never special-cases a provider
    with _scrip(ALL_ROWS):
        angel_miss = angel_feed.fno_chain("NIFTY_NOSUCH")
    assert dhan_feed.fno_chain("NIFTY") == angel_miss | {"underlying": "NIFTY"}
    with _env(DHAN_KEYS), _cfg(dhan_feed, None), _feed_state(dhan_feed):
        st = dhan_feed.public_status()
    assert st["indices"] == [] and st["fnoContracts"] == 0


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} feed tests passed")


if __name__ == "__main__":
    _main()
