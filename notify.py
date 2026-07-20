"""
Off-screen alerts — Telegram / generic webhook
================================================
The dashboard already has *client-side* alerts (toast + desktop notification +
beep), but those only fire while a browser tab is open. This module adds
*server-side* alerts that reach you when nothing is open — e.g. Telegram on your
phone — by riding the snapshot-logger's existing 60s market-hours loop.

It is strictly opt-in and zero-overhead when unconfigured: `tick()` returns
immediately unless a Telegram bot **or** a webhook is set up. Configure via env
(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, and/or `ALERT_WEBHOOK_URL`) or a
gitignored `notify_config.json` (see notify_config.example.json).

Two alert kinds, both deduped per (IST day, kind, symbol[, direction]) via the
`alert_log` SQLite table so you never get the same alert twice — even across
restarts:
  - "idea"   : a fresh high-conviction trade idea from get_recommendations()
  - "volume" : an unusual-volume spike with a rising price (mirrors the client's
               volume-multiple alert)

No secrets are ever returned by public_status(); messages are best-effort with
short timeouts and never raise into the caller.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("notify")

IST = timezone(timedelta(hours=5, minutes=30))
CONFIG_JSON = os.path.join(os.path.dirname(__file__), "notify_config.json")

# How many alerts of a given kind we'll send in a single cycle. Caps the burst
# the first time alerts are enabled (empty alert_log) without dropping anything
# important — the rest simply fire on subsequent cycles as they stay/re-qualify.
_MAX_PER_CYCLE = 6
_RATING_FLOOR = {"High": 66, "Medium": 40, "All": 0, "Low": 0}
_HTTP_TIMEOUT = 8

# EOD-digest "track record" footer (does confirmation-stacking pay?) — sourced from
# the conviction calibration so the picks you actually see carry their own credibility.
_TRACK_DAYS = 30       # window shown in the footer
_TRACK_MIN = 8         # need >= this many RESOLVED ideas before showing any track record
_TRACK_TIER_MIN = 3    # a confirmation tier needs >= this many resolved to be listed

_DEFAULTS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "webhook_url": "",
    "enabled": True,
    "min_rating": "High",   # High | Medium | All  (idea-alert conviction floor)
    "vol_mult": 50,         # volume-spike threshold (× average), rising price only
}

_config_cache = {"mtime": None, "data": None}
_send_lock = threading.Lock()   # serialize outbound posts (tidy logs, gentle on APIs)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_config():
    """Merged config: JSON defaults (mtime-cached) with env-var overrides on top."""
    try:
        mtime = os.path.getmtime(CONFIG_JSON)
    except OSError:
        mtime = None
    c = _config_cache
    if c["data"] is None or c["mtime"] != mtime:
        data = dict(_DEFAULTS)
        try:
            with open(CONFIG_JSON, encoding="utf-8") as f:
                raw = json.load(f)
            for k in _DEFAULTS:
                if raw.get(k) is not None:
                    data[k] = raw[k]
        except Exception:
            pass  # missing/invalid file → defaults (still overridable by env)
        _config_cache.update(mtime=mtime, data=data)
    data = dict(c["data"])
    # Env overrides win (handy for containers / not committing a file).
    env_map = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
        "webhook_url": "ALERT_WEBHOOK_URL",
    }
    for key, env in env_map.items():
        v = os.environ.get(env)
        if v is not None:
            data[key] = v.strip()
    # Normalize
    for k in ("telegram_bot_token", "telegram_chat_id", "webhook_url", "min_rating"):
        data[k] = (str(data.get(k) or "")).strip()
    try:
        data["vol_mult"] = float(data.get("vol_mult") or 50)
    except (TypeError, ValueError):
        data["vol_mult"] = 50
    data["enabled"] = bool(data.get("enabled", True))
    return data


def _channels(cfg):
    ch = []
    if cfg["telegram_bot_token"] and cfg["telegram_chat_id"]:
        ch.append("telegram")
    if cfg["webhook_url"]:
        ch.append("webhook")
    return ch


def _enabled(cfg):
    return bool(cfg["enabled"] and _channels(cfg))


def public_status():
    """Safe status for the UI/health endpoint — never leaks the token/chat id."""
    cfg = _load_config()
    ch = _channels(cfg)
    return {
        "configured": bool(ch),
        "enabled": bool(cfg["enabled"] and ch),
        "channels": ch,
        "minRating": cfg["min_rating"] or "High",
        "volMult": cfg["vol_mult"],
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _send_telegram(token, chat_id, text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return True
        log.warning("telegram send failed: HTTP %s", r.status_code)
    except Exception:
        log.warning("telegram send error", exc_info=True)
    return False


def _send_webhook(url, text):
    try:
        r = requests.post(url, json={"text": text}, timeout=_HTTP_TIMEOUT)
        if 200 <= r.status_code < 300:
            return True
        log.warning("webhook send failed: HTTP %s", r.status_code)
    except Exception:
        log.warning("webhook send error", exc_info=True)
    return False


def _send(cfg, text):
    """Fan a message out to every configured channel; True if any succeeded."""
    with _send_lock:
        ok = False
        if cfg["telegram_bot_token"] and cfg["telegram_chat_id"]:
            ok = _send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], text) or ok
        if cfg["webhook_url"]:
            ok = _send_webhook(cfg["webhook_url"], text) or ok
        return ok


def _fmt_trackrecord(rep, days=_TRACK_DAYS):
    """Compact 'does confirmation-stacking pay?' footer from
    conviction_calibration.report(): overall win rate + per-confirmation-tier win rate
    over RESOLVED ideas, so the digest's picks carry their own realized track record.
    Returns "" when there isn't enough resolved history yet. Pure/text-only."""
    if not rep:
        return ""
    totals = rep.get("totals") or {}
    resolved = int(totals.get("resolved") or 0)
    if resolved < _TRACK_MIN:
        return ""
    tiers = []
    for b in rep.get("byConfirmations") or []:
        wr = b.get("winRate")
        if wr is not None and (b.get("resolved") or 0) >= _TRACK_TIER_MIN:
            tiers.append(f"{_esc(b.get('bucket'))}\u2713 {wr:.0f}%")
    overall = totals.get("winRate")
    tail = f"overall {overall:.0f}%" if overall is not None else ""
    body = " \u00b7 ".join(x for x in (" \u00b7 ".join(tiers), tail) if x) or "\u2014"
    return f"\U0001f4ca <b>Track record</b> ({days}d, {resolved} resolved): {body}"


def _fmt_digest(board, top=8, trackrecord=""):
    """Format an EOD conviction board (from eod_conviction.board()) into a compact
    off-screen digest — the top LONG and SHORT stacked-conviction picks with their
    confirmation count + the plan, plus an optional realized-track-record footer.
    Pure/text-only so it's unit-testable."""
    date = _esc(board.get("date") or "—")
    longs = (board.get("longs") or [])[:top]
    shorts = (board.get("shorts") or [])[:top]

    def _line(p):
        arrow = "\U0001f7e2" if p.get("direction") == "LONG" else "\U0001f534"
        conf = int(p.get("confirmations") or 0)
        why = _esc((p.get("reasons") or [""])[0])
        return (f"{arrow} <b>{_esc(p.get('symbol'))}</b> "
                f"[{conf}\u2713 · {_esc(p.get('rating'))}] "
                f"@ \u20b9{_fmt_price(p.get('close'))} "
                f"\u2192 SL {_fmt_price(p.get('stop'))} / Tgt {_fmt_price(p.get('target'))}"
                + (f"\n   \u2022 {why}" if why else ""))

    parts = [f"\U0001f3c6 <b>NSE Market Pulse — EOD conviction ({date})</b>"]
    if longs:
        parts.append("\n<b>Longs</b>")
        parts += [_line(p) for p in longs]
    if shorts:
        parts.append("\n<b>Shorts</b>")
        parts += [_line(p) for p in shorts]
    if not longs and not shorts:
        parts.append("\nNo stacked-conviction setups today.")
    if trackrecord:
        parts.append("\n" + trackrecord)
    parts.append("\n<i>Educational — not investment advice.</i>")
    return "\n".join(parts)


def send_digest(board=None, top=8):
    """Send the EOD conviction digest to the configured channel(s). Builds the board
    itself when not supplied. No-op with a clear error when nothing is configured."""
    cfg = _load_config()
    ch = _channels(cfg)
    if not ch:
        return {"ok": False, "channels": [],
                "error": ("No channel configured. Set TELEGRAM_BOT_TOKEN + "
                          "TELEGRAM_CHAT_ID (or ALERT_WEBHOOK_URL), or fill "
                          "notify_config.json, then retry.")}
    if board is None:
        try:
            import eod_conviction
            board = eod_conviction.board()
        except Exception:
            log.warning("notify: building conviction board failed", exc_info=True)
            return {"ok": False, "channels": ch, "error": "Could not build the board."}
    # Best-effort realized track record ("do stacked picks actually win?") — never
    # let a calibration hiccup block the digest itself.
    footer = ""
    try:
        import conviction_calibration
        footer = _fmt_trackrecord(conviction_calibration.report(days=_TRACK_DAYS))
    except Exception:
        log.warning("notify: track-record footer failed", exc_info=True)
    ok = _send(cfg, _fmt_digest(board, top=top, trackrecord=footer))
    return {"ok": bool(ok), "channels": ch,
            "count": (len(board.get("longs") or []) + len(board.get("shorts") or [])),
            "error": None if ok else "Send failed — check the token / chat id / network."}


def send_test():
    """Fire a one-off test message (used by the UI's 🔔 Push button)."""
    cfg = _load_config()
    ch = _channels(cfg)
    if not ch:
        return {"ok": False, "channels": [],
                "error": ("No channel configured. Set TELEGRAM_BOT_TOKEN + "
                          "TELEGRAM_CHAT_ID (or ALERT_WEBHOOK_URL), or fill "
                          "notify_config.json, then retry.")}
    ok = _send(cfg, "\u2705 <b>NSE Market Pulse</b> — test alert. Off-screen "
                    "alerts are wired up correctly.")
    return {"ok": bool(ok), "channels": ch,
            "error": None if ok else "Send failed — check the token / chat id / network."}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_price(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_idea(i):
    d = i.get("direction", "?")
    arrow = "\U0001f7e2 LONG" if d == "LONG" else "\U0001f534 SHORT"
    rr = i.get("rr")
    reasons = i.get("reasons") or []
    why = _esc(reasons[0]) if reasons else ""
    return (
        f"\U0001f4a1 <b>{arrow} {_esc(i.get('symbol'))}</b> "
        f"({_esc(i.get('rating'))} {int(i.get('conviction') or 0)})\n"
        f"Entry {_fmt_price(i.get('entry'))} · SL {_fmt_price(i.get('stop'))} · "
        f"Tgt {_fmt_price(i.get('target'))}"
        + (f" · R:R {rr}" if rr else "")
        + (f"\n{why}" if why else "")
    )


def _fmt_vol(sym, mult, pchg, ltp):
    return (f"\U0001f4c8 <b>{_esc(sym)}</b> — {mult:.0f}× avg volume · "
            f"+{pchg:.2f}% @ \u20b9{_fmt_price(ltp)}")


# ---------------------------------------------------------------------------
# Detection (rides snapshot_logger._run_cycle via tick())
# ---------------------------------------------------------------------------
def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _tick_ideas(cfg):
    import db
    import nse_client
    floor = _RATING_FLOOR.get(cfg["min_rating"] or "High", 66)
    recs = nse_client.get_recommendations()
    ideas = (recs.get("longs") or []) + (recs.get("shorts") or [])
    cands = [i for i in ideas
             if (i.get("conviction") or 0) >= floor and i.get("fresh", True)]
    cands.sort(key=lambda x: x.get("conviction") or 0, reverse=True)
    sent = 0
    for i in cands:
        if sent >= _MAX_PER_CYCLE:
            break
        key = f"{_today()}|idea|{i.get('symbol')}|{i.get('direction')}"
        if db.alert_seen(key):
            continue
        if _send(cfg, _fmt_idea(i)):
            db.alert_mark(key, "idea", i.get("symbol"))
            sent += 1
    return sent


def _tick_volume(cfg, ctx):
    if not ctx:
        return 0
    import db
    thr = cfg["vol_mult"]
    rows = (ctx.get("volgainers") or []) + (ctx.get("scanner") or [])
    sent, seen = 0, set()
    for r in rows:
        sym = r.get("symbol")
        if not sym or sym in seen:
            continue
        mult = r.get("week1volChange") or r.get("volMult")
        pchg = r.get("pChange") or 0
        if not (mult and mult >= thr and pchg > 0):
            continue
        seen.add(sym)
        key = f"{_today()}|vol|{sym}"
        if db.alert_seen(key):
            continue
        if sent >= _MAX_PER_CYCLE:
            break
        if _send(cfg, _fmt_vol(sym, mult, pchg, r.get("ltp"))):
            db.alert_mark(key, "vol", sym)
            sent += 1
    return sent


def tick(ctx=None):
    """One alert pass. Called from snapshot_logger each market-hours cycle.

    No-op (fast return) unless a channel is configured, so users without alerts
    set up pay nothing. Each kind is independently guarded.
    """
    cfg = _load_config()
    if not _enabled(cfg):
        return
    try:
        _tick_ideas(cfg)
    except Exception:
        log.warning("notify: idea tick failed", exc_info=True)
    try:
        _tick_volume(cfg, ctx)
    except Exception:
        log.warning("notify: volume tick failed", exc_info=True)
