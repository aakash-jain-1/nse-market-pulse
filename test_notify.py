"""
Unit tests for notify.py — off-screen Telegram/webhook alerts.

Covers, with zero network:
  - config precedence (defaults < notify_config.json < env) + normalisation
  - channel detection / enabled gating / public_status leaks NO secrets
  - HTML-safe message formatting (_esc / _fmt_idea / _fmt_vol)
  - transport fan-out (_send: true-if-any, false-if-all-fail)
  - send_test (no-channel vs configured)
  - detection: idea floor by min_rating, fresh filter, per-cycle cap, dedupe;
    volume threshold + rising-price filter + dedupe — all against a TEMP SQLite DB
  - tick() is a no-op when unconfigured (never calls the network)

Mutates process-global module state (notify.CONFIG_JSON / env / db path / patched
transports), so run SERIALLY. Run: python test_notify.py  (also works under pytest)
"""

import contextlib
import gc
import json
import os
import shutil
import tempfile

import db
import notify


# ---------------------------------------------------------------------------
# Isolation fixtures
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _temp_db():
    d = tempfile.mkdtemp(prefix="nse_notify_db_")
    saved = (db.DATA_DIR, db.DB_FILE, db._initialized)
    db.DATA_DIR, db.DB_FILE, db._initialized = d, os.path.join(d, "market.db"), False
    db.init()
    try:
        yield
    finally:
        db.DATA_DIR, db.DB_FILE, db._initialized = saved
        db._initialized = False
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _config(env=None, json_cfg=None):
    """Isolate notify config: clear the mtime cache, point CONFIG_JSON at a temp
    file (or a non-existent path for defaults), and set env overrides."""
    keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALERT_WEBHOOK_URL"]
    saved_env = {k: os.environ.get(k) for k in keys}
    saved_json, saved_cache = notify.CONFIG_JSON, dict(notify._config_cache)
    d = tempfile.mkdtemp(prefix="nse_notify_cfg_")
    if json_cfg is not None:
        p = os.path.join(d, "notify_config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(json_cfg, f)
        notify.CONFIG_JSON = p
    else:
        notify.CONFIG_JSON = os.path.join(d, "__none__.json")
    for k in keys:
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    notify._config_cache.update(mtime=None, data=None)
    try:
        yield
    finally:
        notify.CONFIG_JSON = saved_json
        notify._config_cache.clear()
        notify._config_cache.update(saved_cache)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _patch_send(result=True):
    """Record messages passed to notify._send; report `result` as delivery status."""
    sent, orig = [], notify._send
    notify._send = lambda cfg, text: (sent.append(text) or bool(result))
    try:
        yield sent
    finally:
        notify._send = orig


@contextlib.contextmanager
def _patch_transport(tg=True, wh=True):
    """Record raw telegram/webhook posts; report the given success flags."""
    tg_calls, wh_calls = [], []
    o1, o2 = notify._send_telegram, notify._send_webhook
    notify._send_telegram = lambda tok, chat, text: (tg_calls.append(text) or bool(tg))
    notify._send_webhook = lambda url, text: (wh_calls.append(text) or bool(wh))
    try:
        yield tg_calls, wh_calls
    finally:
        notify._send_telegram, notify._send_webhook = o1, o2


@contextlib.contextmanager
def _patch_recs(longs=None, shorts=None, boom=False):
    import nse_client
    orig = nse_client.get_recommendations
    if boom:
        def fake(*a, **k):
            raise AssertionError("get_recommendations must NOT be called when unconfigured")
    else:
        def fake(*a, **k):
            return {"longs": list(longs or []), "shorts": list(shorts or [])}
    nse_client.get_recommendations = fake
    try:
        yield
    finally:
        nse_client.get_recommendations = orig


def _idea(sym, direction="LONG", conviction=80, rating="High", fresh=True):
    return {"symbol": sym, "direction": direction, "conviction": conviction,
            "rating": rating, "entry": 100.0, "stop": 98.0, "target": 106.0,
            "rr": 3.0, "reasons": ["test reason"], "fno": False, "fresh": fresh}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_defaults_when_unconfigured():
    with _config():
        cfg = notify._load_config()
        assert cfg["telegram_bot_token"] == "" and cfg["telegram_chat_id"] == ""
        assert cfg["webhook_url"] == ""
        assert cfg["enabled"] is True
        assert cfg["min_rating"] == "High" and cfg["vol_mult"] == 50.0
        assert notify._channels(cfg) == []
        assert notify._enabled(cfg) is False


def test_env_configures_telegram():
    with _config(env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}):
        cfg = notify._load_config()
        assert notify._channels(cfg) == ["telegram"]
        assert notify._enabled(cfg) is True


def test_webhook_channel():
    with _config(env={"ALERT_WEBHOOK_URL": "https://hook.example/x"}):
        cfg = notify._load_config()
        assert notify._channels(cfg) == ["webhook"]
        assert notify._enabled(cfg) is True


def test_json_then_env_precedence():
    with _config(json_cfg={"telegram_bot_token": "jtok", "telegram_chat_id": "jchat",
                           "vol_mult": 30, "min_rating": "Medium"},
                 env={"TELEGRAM_BOT_TOKEN": "etok"}):
        cfg = notify._load_config()
        assert cfg["telegram_bot_token"] == "etok"   # env wins
        assert cfg["telegram_chat_id"] == "jchat"     # json fills the rest
        assert cfg["vol_mult"] == 30.0 and cfg["min_rating"] == "Medium"


def test_enabled_flag_disables_even_when_configured():
    with _config(json_cfg={"telegram_bot_token": "t", "telegram_chat_id": "c",
                           "enabled": False}):
        cfg = notify._load_config()
        assert notify._channels(cfg) == ["telegram"]   # channel present...
        assert notify._enabled(cfg) is False            # ...but master switch off
        s = notify.public_status()
        assert s["configured"] is True and s["enabled"] is False


def test_public_status_leaks_no_secrets():
    with _config(env={"TELEGRAM_BOT_TOKEN": "SECRET_TOKEN_123",
                      "TELEGRAM_CHAT_ID": "SECRET_CHAT_456"}):
        s = notify.public_status()
        assert s["configured"] is True and "telegram" in s["channels"]
        blob = json.dumps(s)
        assert "SECRET_TOKEN_123" not in blob and "SECRET_CHAT_456" not in blob


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def test_esc_escapes_html():
    assert notify._esc("x<y>&z") == "x&lt;y&gt;&amp;z"


def test_fmt_idea_long():
    s = notify._fmt_idea(_idea("RELIANCE"))
    assert "LONG RELIANCE" in s and "(High 80)" in s
    assert "Entry 100.00" in s and "SL 98.00" in s and "Tgt 106.00" in s
    assert "R:R 3.0" in s and "test reason" in s


def test_fmt_idea_short():
    s = notify._fmt_idea(_idea("TCS", direction="SHORT"))
    assert "SHORT TCS" in s


def test_fmt_idea_escapes_symbol():
    s = notify._fmt_idea(_idea("X<Y"))
    assert "X&lt;Y" in s          # angle bracket escaped, not injected raw


def test_fmt_vol():
    s = notify._fmt_vol("TCS", 62.0, 3.2, 3890.4)
    assert "TCS" in s and "62" in s and "+3.20%" in s and "3,890.40" in s


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def test_send_fans_out_to_both_channels():
    with _config(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
                      "ALERT_WEBHOOK_URL": "https://hook/x"}), \
         _patch_transport() as (tg, wh):
        assert notify._send(notify._load_config(), "hi") is True
        assert len(tg) == 1 and len(wh) == 1


def test_send_true_if_any_channel_succeeds():
    with _config(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
                      "ALERT_WEBHOOK_URL": "https://hook/x"}), \
         _patch_transport(tg=False, wh=True):
        assert notify._send(notify._load_config(), "hi") is True


def test_send_false_if_all_fail():
    with _config(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
                      "ALERT_WEBHOOK_URL": "https://hook/x"}), \
         _patch_transport(tg=False, wh=False):
        assert notify._send(notify._load_config(), "hi") is False


def test_send_test_no_channel():
    with _config():
        r = notify.send_test()
        assert r["ok"] is False and "No channel" in r["error"]


def test_send_test_ok():
    with _config(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}), \
         _patch_transport(tg=True):
        r = notify.send_test()
        assert r["ok"] is True and r["channels"] == ["telegram"]


# ---------------------------------------------------------------------------
# Detection — ideas
# ---------------------------------------------------------------------------
def _tg():
    return {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}


def test_tick_ideas_floor_and_dedupe():
    with _config(env=_tg()), _temp_db(), _patch_send() as sent, \
         _patch_recs(longs=[_idea("HI", conviction=80), _idea("LO", conviction=30)]):
        cfg = notify._load_config()
        notify._tick_ideas(cfg)
        assert sent == [notify._fmt_idea(_idea("HI"))]     # LO below High floor (66)
        assert db.alert_seen(f"{notify._today()}|idea|HI|LONG") is True
        # second pass: already alerted -> nothing new
        notify._tick_ideas(cfg)
        assert len(sent) == 1


def test_tick_ideas_min_rating_medium():
    with _config(json_cfg={"telegram_bot_token": "t", "telegram_chat_id": "c",
                           "min_rating": "Medium"}), _temp_db(), _patch_send() as sent, \
         _patch_recs(longs=[_idea("MED", conviction=50, rating="Medium")]):
        notify._tick_ideas(notify._load_config())
        assert len(sent) == 1          # conviction 50 clears the Medium floor (40)


def test_tick_ideas_fresh_filter():
    with _config(env=_tg()), _temp_db(), _patch_send() as sent, \
         _patch_recs(longs=[_idea("STALE", fresh=False)]):
        notify._tick_ideas(notify._load_config())
        assert sent == []              # not fresh -> skipped


def test_tick_ideas_per_cycle_cap():
    many = [_idea(f"S{i}", conviction=90) for i in range(notify._MAX_PER_CYCLE + 5)]
    with _config(env=_tg()), _temp_db(), _patch_send() as sent, _patch_recs(longs=many):
        notify._tick_ideas(notify._load_config())
        assert len(sent) == notify._MAX_PER_CYCLE


# ---------------------------------------------------------------------------
# Detection — volume
# ---------------------------------------------------------------------------
def test_tick_volume_threshold_and_rising():
    ctx = {"volgainers": [
        {"symbol": "UP", "volMult": 60, "pChange": 3.0, "ltp": 100},    # sent
        {"symbol": "DOWN", "volMult": 60, "pChange": -1.0, "ltp": 100},  # falling -> no
        {"symbol": "LOW", "volMult": 10, "pChange": 3.0, "ltp": 100},    # below 50x -> no
    ], "scanner": []}
    with _config(env=_tg()), _temp_db(), _patch_send() as sent:
        notify._tick_volume(notify._load_config(), ctx)
        assert sent == [notify._fmt_vol("UP", 60, 3.0, 100)]


def test_tick_volume_prefers_week1_and_dedupes():
    ctx = {"volgainers": [{"symbol": "W", "week1volChange": 70, "pChange": 2.0, "ltp": 50}],
           "scanner": []}
    with _config(env=_tg()), _temp_db(), _patch_send() as sent:
        cfg = notify._load_config()
        notify._tick_volume(cfg, ctx)
        notify._tick_volume(cfg, ctx)          # same day -> deduped
        assert len(sent) == 1
        assert db.alert_seen(f"{notify._today()}|vol|W") is True


def test_tick_volume_no_ctx():
    with _config(env=_tg()), _temp_db(), _patch_send() as sent:
        assert notify._tick_volume(notify._load_config(), None) == 0
        assert sent == []


# ---------------------------------------------------------------------------
# tick() gating + integration
# ---------------------------------------------------------------------------
def test_tick_noop_when_unconfigured():
    # No channel -> tick must return immediately WITHOUT touching the network.
    with _config(), _temp_db(), _patch_send() as sent, _patch_recs(boom=True):
        notify.tick({"volgainers": [{"symbol": "X", "volMult": 99, "pChange": 5, "ltp": 1}]})
        assert sent == []


def test_tick_end_to_end():
    ctx = {"volgainers": [{"symbol": "VOLX", "volMult": 80, "pChange": 4.0, "ltp": 200}],
           "scanner": []}
    with _config(env=_tg()), _temp_db(), _patch_transport() as (tg, wh), \
         _patch_recs(longs=[_idea("IDEAX", conviction=90)]):
        notify.tick(ctx)
        # one idea alert + one volume alert delivered over telegram
        assert len(tg) == 2
        assert db.alert_seen(f"{notify._today()}|idea|IDEAX|LONG") is True
        assert db.alert_seen(f"{notify._today()}|vol|VOLX") is True


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} notify tests passed")


if __name__ == "__main__":
    _main()
