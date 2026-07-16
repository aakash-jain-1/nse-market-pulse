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
from datetime import datetime

import angel_feed
import db
import dhan_feed

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
    dicts = ["_watch", "_latest", "_bars", "_sym2sec", "_sec2sym"]
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


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} feed tests passed")


if __name__ == "__main__":
    _main()
