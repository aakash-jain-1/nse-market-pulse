"""
NSE In-Demand Dashboard - Flask backend
=======================================
Serves the dashboard UI plus JSON API endpoints that proxy NSE's live data
through our cookie-warmed session.

Run:
    python app.py
Then open http://127.0.0.1:5055

Note: we intentionally avoid port 5000 because a previously-installed
service worker from another local app (a BSE announcements PWA) caches that
origin and hijacks requests. A fresh port sidesteps that stale cache.
"""

import hmac
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

from flask import Flask, g, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import angel_feed
import dhan_feed
import nse_client as nse
import nse_quote
import paper
import snapshot_logger as snaplog

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Runtime config & security posture (see AUDIT.md — H1/H2).
# ---------------------------------------------------------------------------
def _envflag(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# Werkzeug's interactive debugger is effectively a remote code-execution console,
# so it must NEVER be on for a network-bound server. OFF by default; opt in with
# FLASK_DEBUG=1 for local development only.
DEBUG = _envflag("FLASK_DEBUG")
# The reloader (auto-restart on .py edits) is safe and convenient — on by default,
# disable with FLASK_RELOAD=0. Debug implies the reloader.
RELOAD = DEBUG or _envflag("FLASK_RELOAD", "1")
PORT = int(os.environ.get("PORT", "5055"))
# Bind to all interfaces so phones/tablets on the same Wi-Fi can reach the
# dashboard. Set HOST=127.0.0.1 to keep it strictly loopback.
HOST = os.environ.get("HOST", "0.0.0.0")
# Optional shared secret. When NSE_TOKEN is set, every request must present it
# (X-Access-Token header, nse_token cookie, or ?token=… once, which sets the
# cookie). Unset = open (unchanged behaviour) — set it if you expose the app on
# an untrusted network.
ACCESS_TOKEN = (os.environ.get("NSE_TOKEN") or "").strip()
_IS_SERVING = (not RELOAD) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"

# Templates re-read on every request even with the debugger off, so UI edits to
# index.html show on refresh without a restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _setup_logging():
    """Root logging: console (warnings) + rotating file (info) in ./logs.

    Replaces the app's previous silent-failure posture (AUDIT.md M5). Third-party
    libraries stay at WARNING; our own modules log at INFO.
    """
    if getattr(_setup_logging, "_done", False):
        return
    _setup_logging._done = True
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    root.addHandler(console)
    # One process owns the file to avoid two handlers rotating the same file.
    if _IS_SERVING:
        try:
            os.makedirs("logs", exist_ok=True)
            fh = RotatingFileHandler(os.path.join("logs", "app.log"),
                                     maxBytes=2_000_000, backupCount=5,
                                     encoding="utf-8")
            fh.setLevel(logging.INFO)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            pass  # never let logging setup crash the app
    for name in ("app", "nse_client", "nse_quote", "sim", "strategies",
                 "snapshot_logger", "backtest_daily", "backtest_strategies",
                 "angel_feed", "dhan_feed", "db", "ideas_journal", "paper"):
        logging.getLogger(name).setLevel(logging.INFO)


_setup_logging()
log = logging.getLogger("app")


@app.before_request
def _security_guard():
    """CSRF (same-origin on writes) + optional shared-token gate. See AUDIT.md H2."""
    # 1) CSRF: a state-changing request must originate from our own page. This
    #    blocks a malicious website in your browser from POSTing to the app.
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            if urlparse(origin).netloc != request.host:
                return jsonify({"error": "cross-origin request blocked"}), 403
    # 2) Optional shared-token gate (only active when NSE_TOKEN is set).
    if ACCESS_TOKEN:
        q = request.args.get("token") or ""
        supplied = (request.headers.get("X-Access-Token")
                    or request.cookies.get("nse_token") or q)
        if not hmac.compare_digest(str(supplied), ACCESS_TOKEN):
            return jsonify({"error": "unauthorized"}), 401
        if q and hmac.compare_digest(str(q), ACCESS_TOKEN):
            g._set_token_cookie = True


@app.after_request
def _security_headers(resp):
    # Set the auth cookie once when a valid ?token= was supplied via URL.
    if getattr(g, "_set_token_cookie", False):
        resp.set_cookie("nse_token", ACCESS_TOKEN, httponly=True, samesite="Strict")
    # Defense-in-depth headers. The UI is inline JS/CSS + inline handlers, so
    # script/style must allow 'unsafe-inline'; we still lock down external
    # sources, framing, objects and outbound connections.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'self'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# Live realtime feed provider (see angel_feed.py / dhan_feed.py). Angel One's
# SmartAPI feed is FREE; Dhan's is a paid ₹499/mo data plan — so prefer whichever
# is configured, Angel first. If neither is set up we still point at Angel so the
# Live tab shows the recommended (free) provider's setup card.
def _select_live_feed():
    if angel_feed.is_configured():
        return angel_feed
    if dhan_feed.is_configured():
        return dhan_feed
    return angel_feed


live_feed = _select_live_feed()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/gainers")
def api_gainers():
    return jsonify(nse.get_variations("gainers"))


@app.route("/api/losers")
def api_losers():
    return jsonify(nse.get_variations("losers"))


@app.route("/api/volume")
def api_volume():
    return jsonify(nse.get_most_active("volume"))


@app.route("/api/value")
def api_value():
    return jsonify(nse.get_most_active("value"))


@app.route("/api/volgainers")
def api_volgainers():
    return jsonify(nse.get_volume_gainers())


@app.route("/api/oispurts")
def api_oispurts():
    return jsonify(nse.get_oi_spurts())


@app.route("/api/futures")
def api_futures():
    return jsonify(nse.get_futures())


@app.route("/api/futures/all")
def api_futures_all():
    return jsonify(nse.get_all_futures())


@app.route("/api/futures/<symbol>")
def api_futures_symbol(symbol):
    return jsonify(nse_quote.get_symbol_futures(symbol))


@app.route("/api/scanner")
def api_scanner():
    def fnum(name):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    return jsonify(nse.get_scanner(
        direction=request.args.get("direction", "any"),
        min_abs_change=fnum("minChange"),
        min_vol_mult=fnum("minVolMult"),
        min_value_cr=fnum("minValueCr"),
        oi=request.args.get("oi", "any"),
        fno_only=request.args.get("fno") == "1",
    ))


@app.route("/api/recommendations")
def api_recommendations():
    return jsonify(nse.get_recommendations(
        fno_only=request.args.get("fno") == "1",
    ))


@app.route("/api/ideas/history")
def api_ideas_history():
    """Per-day summary of journaled ideas (durable, multi-session)."""
    import ideas_journal
    try:
        limit = int(request.args.get("limit", 60))
    except (TypeError, ValueError):
        limit = 60
    return jsonify(ideas_journal.history(limit=limit))


@app.route("/api/ideas/day")
def api_ideas_day():
    """Every idea journaled on ?date=YYYY-MM-DD, with its intraday outcome."""
    import ideas_journal
    return jsonify(ideas_journal.day_ideas(request.args.get("date", "")))


@app.route("/api/ideas/recent")
def api_ideas_recent():
    """Today's ideas newest-first (rolling feed). ?window=min &min=High &limit=N."""
    import ideas_journal
    try:
        window = int(request.args.get("window", 60))
    except (TypeError, ValueError):
        window = 60
    try:
        limit = int(request.args.get("limit", 40))
    except (TypeError, ValueError):
        limit = 40
    min_rating = request.args.get("min") or None
    return jsonify(ideas_journal.recent(window_min=window, limit=limit,
                                        min_rating=min_rating))


@app.route("/api/deepdive/<symbol>")
def api_deepdive(symbol):
    return jsonify(nse.get_stock_deepdive(symbol))


def _broker_connected():
    """True when the selected live feed (Angel/Dhan) has a live session — so we can
    serve the stock-detail modal from the broker's REST instead of hitting NSE."""
    try:
        return bool(live_feed.public_status().get("connected"))
    except Exception:
        return False


def _broker_quote(symbol):
    """Per-stock quote from the broker (Angel REST), or None. Only when connected;
    swallows every error so the caller cleanly falls back to NSE."""
    if not _broker_connected():
        return None
    try:
        fn = getattr(live_feed, "rest_quote", None)
        return fn(symbol) if fn else None
    except Exception:
        return None


@app.route("/api/quote/<symbol>")
def api_quote(symbol):
    """Live per-stock quote + 5-level depth. Prefers the broker (Angel) REST when the
    live feed is connected — no NSE hit — then NSE's NextApi, and finally the resilient
    EOD bhavcopy close during a WAF cooldown (clearly marked `stale`) so the modal always
    works."""
    import nse_client as nse
    q = _broker_quote(symbol)      # broker-first: dodges NSE's Akamai entirely
    if q is not None and q.get("ltp") is not None:
        return jsonify(q)
    if not nse.blocked_for():
        try:
            return jsonify(nse_quote.get_quote(symbol))
        except Exception:
            pass                     # fall through to the EOD close
    import bhavcopy
    q = bhavcopy.eod_quote(symbol) or {}
    c, pc = q.get("close"), q.get("prevClose")
    q.update(stale=True, source="eod-bhavcopy", blockedForSec=nse.blocked_for())
    if c is not None:
        q["ltp"] = c
        if pc:
            q.setdefault("change", round(c - pc, 2))
            q.setdefault("pChange", round((c / pc - 1) * 100, 2))
    else:
        q["error"] = "NSE temporarily rate-limited and no EOD close cached yet."
    return jsonify(q)


@app.route("/api/depth")
def api_depth():
    """Batch order-book imbalance stats (scanner "depth demand" rank).

    User-initiated + capped in nse_quote.get_book_stats so it can't stampede NSE.
    """
    syms = (request.args.get("symbols") or "").split(",")
    return jsonify({"symbols": nse_quote.get_book_stats(syms)})


@app.route("/api/alerts/status")
def api_alerts_status():
    """Are off-screen (Telegram/webhook) alerts configured? (never returns secrets)."""
    import notify
    return jsonify(notify.public_status())


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """Send a one-off test alert to the configured channel(s)."""
    import notify
    return jsonify(notify.send_test())


@app.route("/api/eod/status")
def api_eod_status():
    """Freshness/coverage of the EOD bhavcopy cache (?refresh=1 forces a pull)."""
    import bhavcopy
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    return jsonify(bhavcopy.status(refresh=refresh))


@app.route("/api/eod/price/<symbol>")
def api_eod_price(symbol):
    """Latest EOD close for ANY listed symbol (resilient off-hours price)."""
    import bhavcopy
    return jsonify({"symbol": symbol.upper(), "close": bhavcopy.eod_close(symbol),
                    "date": bhavcopy.status().get("cmDate")})


@app.route("/api/eod/quote/<symbol>")
def api_eod_quote(symbol):
    """Full EOD record for a symbol (CM bar + near future) from the bhavcopy."""
    import bhavcopy
    return jsonify(bhavcopy.eod_quote(symbol))


@app.route("/api/eod/refresh", methods=["POST"])
def api_eod_refresh():
    """Ingest a day's bhavcopy into the eod_bars/eod_oi cache (broadens the
    daily-backtest universe to the whole market). Defaults to the latest day."""
    import bhavcopy
    body = request.get_json(silent=True) or {}
    return jsonify(bhavcopy.ingest_db(date=body.get("date") or None))


@app.route("/api/eod/scan")
def api_eod_scan():
    """Full-market EOD/swing scanner over the ingested bhavcopy history (works
    off-hours). ?view=&limit=&minPrice=&minValueCr=&fno=1&deals=1&rollover=0."""
    import eod_scanner

    def fnum(name, default):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    return jsonify(eod_scanner.scan(
        view=request.args.get("view", "setups"),
        limit=int(fnum("limit", 50)),
        min_price=fnum("minPrice", 20.0),
        min_value_cr=fnum("minValueCr", 1.0),
        fno_only=request.args.get("fno") == "1",
        with_deals=request.args.get("deals") == "1",
        with_rollover=request.args.get("rollover") != "0",  # on by default (F&O names)
    ))


@app.route("/api/eod/sectors")
def api_eod_sectors():
    """Sector relative-strength (rotation) board over the ingested EOD history:
    ranks sectors by RS vs the market and surfaces the leading names. Off-hours,
    no network. ?minPrice=&minValueCr=&namesPerSector=&leadSectors=."""
    import sector_scan

    def fnum(name, default):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    return jsonify(sector_scan.scan(
        min_price=fnum("minPrice", 20.0),
        min_value_cr=fnum("minValueCr", 2.0),
        names_per_sector=int(fnum("namesPerSector", 5)),
        lead_sectors=int(fnum("leadSectors", 4)),
    ))


@app.route("/api/eod/deals")
def api_eod_deals():
    """Latest bulk/block deals (institutional footprint), market-wide and off-hours.
    ?kind=bulk|block&limit=200 ; ?status=1 for freshness/coverage only."""
    import deals
    if request.args.get("status") == "1":
        return jsonify(deals.status(refresh=request.args.get("refresh") == "1"))
    try:
        limit = int(request.args.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    return jsonify(deals.recent(kind=request.args.get("kind", "bulk"), limit=limit))


@app.route("/api/eod/conviction")
def api_eod_conviction():
    """Stacked-conviction board ('tomorrow's watchlist'): fuses breakout + delivery%
    + bulk/block deals + F&O OI buildup + sector RS + option chain, ranked by how many
    independent signals agree. Off-hours.
    ?limit=&minPrice=&minValueCr=&minPillars=&fno=1&deals=0&options=0&adaptive=1."""
    import eod_conviction

    def fnum(name, default):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    return jsonify(eod_conviction.board(
        limit=int(fnum("limit", 25)),
        min_price=fnum("minPrice", 20.0),
        min_value_cr=fnum("minValueCr", 2.0),
        min_pillars=int(fnum("minPillars", 2)),
        fno_only=request.args.get("fno") == "1",
        with_deals=request.args.get("deals") != "0",     # on by default
        with_options=request.args.get("options") != "0",  # on by default
        with_rollover=request.args.get("rollover") != "0",  # on by default
        adaptive=request.args.get("adaptive") == "1",     # off by default
    ))


@app.route("/api/eod/conviction/save", methods=["POST"])
def api_eod_conviction_save():
    """Persist the current conviction board into the Ideas history (dated to the EOD
    session; never clobbers a live idea). Body/query: same filters as the board."""
    import eod_conviction
    body = request.get_json(silent=True) or {}

    def num(name, default):
        v = body.get(name, request.args.get(name))
        try:
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    b = eod_conviction.board(
        limit=int(num("limit", 25)), min_price=num("minPrice", 20.0),
        min_value_cr=num("minValueCr", 2.0), min_pillars=int(num("minPillars", 2)),
        fno_only=(body.get("fno") or request.args.get("fno")) == "1",
        with_deals=(body.get("deals", request.args.get("deals")) != "0"),
        with_options=(body.get("options", request.args.get("options")) != "0"),
        with_rollover=(body.get("rollover", request.args.get("rollover")) != "0"),
        adaptive=(body.get("adaptive", request.args.get("adaptive")) == "1"))
    return jsonify(eod_conviction.save(b))


@app.route("/api/eod/conviction/digest", methods=["POST"])
def api_eod_conviction_digest():
    """Push the EOD conviction digest to the configured off-screen channel(s)
    (Telegram/webhook). Builds the board itself."""
    import notify
    return jsonify(notify.send_digest())


@app.route("/api/eod/conviction/calibration")
def api_eod_conviction_calibration():
    """Does the board's confirmation-stacking pay? Scores realized TARGET/STOP
    outcomes of the saved conviction ideas, bucketed by pillar count / rating /
    direction / individual pillar / option-chain warning. ?days=N optional."""
    import conviction_calibration
    days = request.args.get("days")
    try:
        days = int(days) if days not in (None, "") else None
    except ValueError:
        days = None
    return jsonify(conviction_calibration.report(days=days))


@app.route("/api/eod/rollover")
def api_eod_rollover():
    """Futures rollover tracker from the EOD FO bhavcopy: near-vs-next month rollover%
    + roll cost (spread) + basis + OI-state, cross-sectionally ranked. Off-hours.
    ?minPrice=&minValueCr=&limit=&sort=rollover|rollcost|basis|dte."""
    import rollover

    def fnum(name, default):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    return jsonify(rollover.board(
        min_price=fnum("minPrice", 20.0),
        min_value_cr=fnum("minValueCr", 0.5),
        limit=int(fnum("limit", 50)),
        sort=request.args.get("sort", "rollover"),
    ))


@app.route("/api/eod/scheduler")
def api_eod_scheduler():
    """State of the auto post-close EOD refresh (enabled? when? last run + result)."""
    import eod_scheduler
    return jsonify(eod_scheduler.status())


@app.route("/api/eod/scheduler/run", methods=["POST"])
def api_eod_scheduler_run():
    """Trigger the post-close refresh now (backfill → deals → optional digest).
    Runs off-thread since a backfill is dozens of archive fetches; poll
    /api/eod/scheduler for the result."""
    import eod_scheduler
    import threading
    days = request.args.get("days")
    kw = {"days": int(days)} if days else {}
    if eod_scheduler._state.get("running"):
        return jsonify({"started": False, "reason": "already running",
                        "status": eod_scheduler.status()})
    threading.Thread(target=eod_scheduler.run_job, kwargs=kw, daemon=True).start()
    return jsonify({"started": True, "status": eod_scheduler.status()})


# Backfill runs off-thread (dozens of ~1-2s archive fetches); the UI polls the
# GET for progress. Module state is fine — a single serving worker, and the
# heavy work is serialized inside bhavcopy.backfill()'s own lock.
_eod_backfill = {"running": False, "startedAt": 0.0, "days": 0, "result": None}


@app.route("/api/eod/backfill", methods=["GET", "POST"])
def api_eod_backfill():
    """POST {days} to load the last N sessions' bhavcopies into eod_bars (gives the
    scanner market-wide HISTORY). Returns immediately; GET reports progress."""
    import bhavcopy
    import threading
    if request.method == "GET":
        return jsonify(dict(_eod_backfill))
    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days") or 20)
    except (TypeError, ValueError):
        days = 20
    days = max(1, min(days, 120))
    if _eod_backfill["running"]:
        return jsonify({**_eod_backfill, "busy": True})
    _eod_backfill.update(running=True, startedAt=time.time(), days=days, result=None)

    def _job():
        try:
            def _progress(snap):
                _eod_backfill["result"] = snap   # live day/bar counts for the poller
            _eod_backfill["result"] = bhavcopy.backfill(days=days, progress=_progress)
        except Exception:
            log.warning("EOD backfill failed", exc_info=True)
            _eod_backfill["result"] = {"error": "backfill failed"}
        finally:
            _eod_backfill["running"] = False

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({**_eod_backfill, "started": True})


@app.route("/api/eod/optionchain/<symbol>")
def api_eod_option_chain(symbol):
    """Resilient EOD option chain from the FO bhavcopy (works off-hours / when the
    live NextApi is blocked). Same shape as /api/optionchain plus {eod:true,date}."""
    import eod_options
    return jsonify(eod_options.chain(symbol, request.args.get("expiry")))


@app.route("/api/eod/optionchain/<symbol>/summary")
def api_eod_option_summary(symbol):
    """PCR / max-pain / OI per expiry from the EOD FO bhavcopy."""
    import eod_options
    return jsonify(eod_options.summary(symbol))


@app.route("/api/chart/<symbol>")
def api_chart(symbol):
    """Intraday chart points. Prefers the broker (Angel) candles when connected — no
    NSE hit — else NSE's NextApi chart feed."""
    if _broker_connected():
        try:
            fn = getattr(live_feed, "rest_chart", None)
            c = fn(symbol) if fn else None
            if c and c.get("points"):
                return jsonify(c)
        except Exception:
            pass                     # fall back to NSE
    return jsonify(nse_quote.get_chart(symbol))


def _broker_ohlc(symbol, interval, chart_type, days):
    """OHLCV candles from the broker (Angel) when connected, else None. Only for the
    window-less (frontend chart) case; explicit from/to windows stay on NSE."""
    if not _broker_connected():
        return None
    try:
        fn = getattr(live_feed, "rest_ohlc", None)
        c = fn(symbol, interval=interval, chart_type=chart_type, days=days) if fn else None
        return c if (c and c.get("points")) else None
    except Exception:
        return None


@app.route("/api/ohlc/<symbol>")
def api_ohlc(symbol):
    interval = int(request.args.get("interval", 1))
    chart_type = "D" if request.args.get("type") == "D" else "I"
    days = request.args.get("days")
    frm = request.args.get("from")
    to = request.args.get("to")
    days_i = int(days) if days else None
    # Broker-first for the plain (window-less) chart request — no NSE hit. An explicit
    # from/to window (e.g. the backtester's exact holding period) always uses NSE.
    if frm is None and to is None:
        c = _broker_ohlc(symbol, interval, chart_type, days_i)
        if c is not None:
            return jsonify(c)
    return jsonify(nse_quote.get_ohlc(
        symbol, interval=interval, chart_type=chart_type, days=days_i,
        from_ts=int(frm) if frm else None,
        to_ts=int(to) if to else None))


# ---------------------------------------------------------------------------
# Live realtime feed — Angel One / Dhan, chosen at startup as `live_feed`.
# ---------------------------------------------------------------------------
@app.route("/api/live/config")
def api_live_config():
    """Is the live feed configured/connected? (safe: never returns secrets)."""
    return jsonify(live_feed.public_status())


@app.route("/api/live/watch", methods=["POST"])
def api_live_watch():
    """Replace the watched symbol set; the feed subscribes/unsubscribes the delta."""
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or []
    focus = body.get("focus")
    return jsonify(live_feed.set_watch(symbols, focus))


@app.route("/api/live/seed/<symbol>")
def api_live_seed(symbol):
    """Historical candles to seed the live chart. Broker-first (Angel candles) when
    connected — no NSE hit — else NSE's OHLCV feed."""
    interval = request.args.get("interval", "1")
    daily = interval == "D"
    days = int(request.args.get("days", 120)) if daily else None
    iv = 1 if daily else int(interval)
    ct = "D" if daily else "I"
    c = _broker_ohlc(symbol, iv, ct, days)
    if c is not None:
        return jsonify(c)
    if daily:
        return jsonify(nse_quote.get_ohlc(symbol, chart_type="D", days=days))
    return jsonify(nse_quote.get_ohlc(symbol, interval=iv))


@app.route("/api/live/snapshot")
def api_live_snapshot():
    """One-shot latest tick data (poll fallback when SSE isn't available)."""
    ids = request.args.get("ids", "")
    syms = [s for s in ids.split(",") if s] or None
    return jsonify({"quotes": live_feed.snapshot(syms),
                    "status": live_feed.public_status()})


@app.route("/api/live/stream")
def api_live_stream():
    """Server-Sent Events: push the watched set's latest ticks ~1x/second.

    The broker socket updates an in-memory store in realtime; this just samples
    it for the browser so the page never talks to a socket directly. One
    EventSource per open Live tab; the client changes what's streamed via
    POST /api/live/watch.
    """
    def gen():
        import json as _json
        yield "retry: 3000\n\n"
        try:
            while True:
                payload = {"quotes": live_feed.snapshot(),
                           "status": live_feed.public_status(),
                           "ts": int(time.time() * 1000)}
                yield "data: " + _json.dumps(payload) + "\n\n"
                time.sleep(1.0)
        except GeneratorExit:  # client disconnected
            return

    return app.response_class(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.route("/api/optionchain/<symbol>")
def api_optionchain(symbol):
    expiry = request.args.get("expiry")
    return jsonify(nse_quote.get_option_chain(symbol, expiry))


@app.route("/api/optionchain/<symbol>/summary")
def api_optionchain_summary(symbol):
    return jsonify(nse_quote.get_option_summary(symbol))


@app.route("/api/fno/universe")
def api_fno_universe():
    return jsonify(nse.get_fno_universe())


@app.route("/api/demand")
def api_demand():
    return jsonify(nse.get_demand_score())


@app.route("/api/paper/portfolio")
def api_paper_portfolio():
    return jsonify(paper.portfolio())


@app.route("/api/paper/order", methods=["POST"])
def api_paper_order():
    body = request.get_json(silent=True) or {}
    ok, msg, order = paper.place_order(
        body.get("symbol"), body.get("side"), body.get("qty")
    )
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg, "order": order}), status


@app.route("/api/paper/option_order", methods=["POST"])
def api_paper_option_order():
    body = request.get_json(silent=True) or {}
    ok, msg, order = paper.place_option_order(
        body.get("underlying"), body.get("expiry"), body.get("strike"),
        body.get("optType"), body.get("side"),
        body.get("lots", body.get("qty")),
    )
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg, "order": order}), status


@app.route("/api/sim/strategies")
def api_sim_strategies():
    import strategies
    return jsonify({"strategies": strategies.strategy_meta()})


def _book():
    b = (request.args.get("book") or "cash").lower()
    return "fno" if b == "fno" else "cash"


@app.route("/api/sim/summary")
def api_sim_summary():
    import sim
    return jsonify(sim.summary(strategy_id=request.args.get("strategy"), book=_book()))


@app.route("/api/sim/daily")
def api_sim_daily():
    import sim
    book = _book()
    return jsonify({**sim.daily_matrix(book=book), "perf": sim.daily_performance(book=book)})


@app.route("/api/sim/day")
def api_sim_day():
    import sim
    return jsonify(sim.day_trades(request.args.get("date", ""), book=_book()))


@app.route("/api/sim/leaderboard")
def api_sim_leaderboard():
    import sim
    return jsonify(sim.leaderboard_bundle(book=_book()))


@app.route("/api/sim/performance")
def api_sim_performance():
    import sim
    return jsonify(sim.performance(book=_book()))


@app.route("/api/sim/analytics")
def api_sim_analytics():
    """Per-strategy & portfolio equity curve / drawdown / R-distribution (charts)."""
    import sim
    return jsonify(sim.analytics(book=_book()))


@app.route("/api/sim/backtest")
def api_sim_backtest():
    import backtest_strategies as bt
    days = request.args.get("days")
    since = None
    if days:
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        since = (datetime.now(ist) - timedelta(days=int(days))).strftime("%Y-%m-%d")
    resolve = request.args.get("resolve", "intrabar")
    if resolve not in ("intrabar", "ltp"):
        resolve = "intrabar"
    return jsonify(bt.run(
        since_day=since,
        max_sessions=int(request.args.get("maxSessions", 3)),
        entry_mode=request.args.get("entryMode", "continuous"),
        resolve=resolve,
    ))


@app.route("/api/sim/backtest_daily")
def api_sim_backtest_daily():
    """Daily-bar historical backtest. `source=eod` runs the WHOLE ingested
    bhavcopy universe from SQLite (off-hours, thousands of trades); the default
    `source=live` pulls a curated universe from NSE. minPrice/minValueCr filter
    the EOD universe."""
    import backtest_daily as btd
    resolve = request.args.get("resolve", "daily")
    source = "eod" if request.args.get("source") == "eod" else "live"
    default_uni = 2500 if source == "eod" else 40

    def fnum(name, default):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    return jsonify(btd.run(
        days=int(request.args.get("days", 30)),
        universe_size=int(request.args.get("universe", default_uni)),
        max_hold=int(request.args.get("maxHold", 5)),
        force=request.args.get("refresh") in ("1", "true", "yes"),
        resolve="intrabar" if resolve == "intrabar" else "daily",
        source=source,
        min_price=fnum("minPrice", btd.EOD_MIN_PRICE),
        min_value_cr=fnum("minValueCr", btd.EOD_MIN_VALUE_CR),
    ))


@app.route("/api/sim/regime")
def api_sim_regime():
    import sim
    return jsonify(sim.current_regime())


@app.route("/api/sim/strategy_of_day")
def api_sim_strategy_of_day():
    """Today's live regime + the historically best strategy for that regime.
    `source=eod` reads the full-market bhavcopy leaderboard (far more samples)."""
    import backtest_daily as btd
    source = "eod" if request.args.get("source") == "eod" else "live"
    default_uni = 2500 if source == "eod" else 60
    return jsonify(btd.strategy_of_day(
        days=int(request.args.get("days", 60)),
        universe_size=int(request.args.get("universe", default_uni)),
        source=source,
    ))


@app.route("/api/sim/walkforward")
def api_sim_walkforward():
    """Walk-forward out-of-sample validation: does each strategy's (and the
    regime-adaptive selection's) edge survive out-of-sample, or is it curve-fit?
    `source=eod` validates over the full-market bhavcopy universe."""
    import walkforward as wf
    source = "eod" if request.args.get("source") == "eod" else "live"
    default_uni = 2500 if source == "eod" else 60
    return jsonify(wf.run(
        days=int(request.args.get("days", 120)),
        universe_size=int(request.args.get("universe", default_uni)),
        max_hold=int(request.args.get("maxHold", 5)),
        folds=int(request.args.get("folds", 4)),
        source=source,
    ))


@app.route("/api/sim/portfolio")
def api_sim_portfolio():
    """Portfolio-level backtest: replay the daily-backtest trades through a REAL book
    (finite capital, a cap on concurrent positions, risk/equal sizing) to get an
    equity curve + CAGR / max-drawdown / Sharpe — not just per-trade R. `source=eod`
    runs the whole ingested bhavcopy universe (off-hours)."""
    import portfolio_backtest as pbk
    import backtest_daily as btd
    source = "eod" if request.args.get("source") == "eod" else "live"
    default_uni = 2500 if source == "eod" else 40

    def fnum(name, default):
        v = request.args.get(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    return jsonify(pbk.run(
        days=int(request.args.get("days", 90)),
        universe_size=int(request.args.get("universe", default_uni)),
        source=source,
        start_capital=fnum("capital", 1_000_000.0),
        max_positions=int(request.args.get("maxPositions", 5)),
        risk_pct=fnum("riskPct", 1.0),
        sizing="equal" if request.args.get("sizing") == "equal" else "risk",
        max_alloc_pct=fnum("maxAllocPct", 25.0),
        max_hold=int(request.args.get("maxHold", 5)),
        per_strategy=request.args.get("perStrategy") != "0",
        min_price=fnum("minPrice", btd.EOD_MIN_PRICE) if source == "eod" else None,
        min_value_cr=fnum("minValueCr", btd.EOD_MIN_VALUE_CR) if source == "eod" else None,
    ))


@app.route("/api/sim/take", methods=["POST"])
def api_sim_take():
    import sim
    body = request.get_json(silent=True) or {}
    book = "fno" if (body.get("book") or "cash").lower() == "fno" else "cash"
    strat = body.get("strategy")
    ids = [strat] if strat else None
    added = sim.take(strategy_ids=ids, book=book)
    out = sim.summary(strategy_id=strat, book=book)
    out["added"] = added
    return jsonify(out)


@app.route("/api/sim/auto", methods=["POST"])
def api_sim_auto():
    import sim
    body = request.get_json(silent=True) or {}
    return jsonify({"auto": sim.set_auto(bool(body.get("on")))})


@app.route("/api/sim/mode", methods=["POST"])
def api_sim_mode():
    import sim
    body = request.get_json(silent=True) or {}
    return jsonify({"entryMode": sim.set_entry_mode(body.get("entryMode"))})


@app.route("/api/sim/reset", methods=["POST"])
def api_sim_reset():
    import sim
    body = request.get_json(silent=True) or {}
    # A specific book clears only that book; omit to wipe everything + settings.
    raw = (body.get("book") or "").lower()
    book = "fno" if raw == "fno" else ("cash" if raw == "cash" else None)
    sim.reset(book=book)
    return jsonify(sim.summary(book=book or "cash"))


@app.route("/api/paper/futures_order", methods=["POST"])
def api_paper_futures_order():
    body = request.get_json(silent=True) or {}
    ok, msg, order = paper.place_futures_order(
        body.get("symbol"), body.get("side"), body.get("lots"),
    )
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg, "order": order}), status


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    paper.reset()
    return jsonify({"ok": True, "message": "Portfolio reset"})


@app.route("/api/log/status")
def api_log_status():
    return jsonify(snaplog.status())


@app.route("/api/log/health")
def api_log_health():
    return jsonify(snaplog.health())


@app.route("/api/health")
def api_health():
    """Consolidated liveness for monitoring: capture loop + feed + DB + posture.

    Lets you tell "blank data because market is closed" from "capture stalled"
    or "NSE session dead" without reading logs (AUDIT.md M5).
    """
    import db
    import eod_scheduler
    import nse_client as nse
    h = snaplog.health()
    sch = eod_scheduler.status()
    try:
        dbsize = os.path.getsize(db.DB_FILE) if os.path.exists(db.DB_FILE) else 0
    except Exception:
        dbsize = 0
    feed = live_feed.public_status()
    # Healthy during market hours if the loop is ticking; idle outside is fine.
    ok = bool(h.get("healthy") or not h.get("marketHours"))
    return jsonify({
        "ok": ok,
        "time": int(time.time() * 1000),
        "logger": h,
        "feed": {"provider": feed.get("provider"),
                 "connected": feed.get("connected"),
                 "configured": feed.get("configured")},
        # WAF cooldown (Akamai "Access Denied") + request pacer stats. blockedForSec>0
        # → live NSE is paused and the app is serving cached / EOD data (powers the
        # dashboard's "NSE cooling down" banner). blockCount/reqLastMin surface how hard
        # we're backing off and how much NSE traffic the pacer is currently letting out.
        "nse": nse.pacer_stats(),
        # Auto post-close EOD refresh (bhavcopy + deals + optional digest).
        "autoEod": {"enabled": sch.get("enabled"), "runAt": sch.get("runAt"),
                    "lastRunDate": sch.get("lastRunDate"), "running": sch.get("running")},
        "db": {"path": db.DB_FILE, "bytes": dbsize,
               "mb": round(dbsize / 1_048_576, 1)},
        "posture": {"debug": DEBUG, "host": HOST,
                    "authRequired": bool(ACCESS_TOKEN)},
    })


@app.route("/api/log/snapshot", methods=["POST"])
def api_log_snapshot():
    n = snaplog.capture_snapshot()
    return jsonify({"ok": n > 0, "rowsWritten": n, "status": snaplog.status()})


@app.route("/api/log/backtest")
def api_log_backtest():
    view = request.args.get("view", "demand")
    return jsonify(snaplog.backtest(view))


@app.route("/api/log/iv", methods=["POST"])
def api_log_iv():
    n = snaplog.capture_iv()
    return jsonify({"ok": n > 0, "rowsWritten": n, "status": snaplog.status()})


@app.route("/api/iv/rank/<symbol>")
def api_iv_rank(symbol):
    return jsonify(snaplog.iv_rank(symbol))


@app.route("/api/log/download")
def api_log_download():
    import db
    st = snaplog.status()
    if not st["totalRows"]:
        return jsonify({"error": "No snapshots logged yet"}), 404
    out = os.path.join(db.DATA_DIR, "snapshots_export.csv")
    db.export_snapshots_csv(out)
    return send_file(out, as_attachment=True, download_name="nse_snapshots.csv")


@app.route("/favicon.ico")
def favicon():
    # No icon to serve; 204 avoids a 404/500 and the browser console noise.
    return ("", 204)


@app.errorhandler(Exception)
def handle_error(e):
    # Preserve real HTTP status codes (404/405/...) instead of masking every
    # error as 500 — otherwise a missing route like /favicon.ico shows up as a
    # 500 in the console.
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    # Log the real cause server-side; never leak internals to the client
    # (AUDIT.md H1 — str(e) can expose paths/state).
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return jsonify({"error": "Internal server error"}), 500


def _lan_ip():
    """Best-effort primary LAN IP (no traffic actually sent — just picks the
    egress interface). Returns None if it can't be determined."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


if __name__ == "__main__":
    # With the reloader on, Flask spawns a watcher parent + a worker child. Only
    # the worker (which actually serves) sets WERKZEUG_RUN_MAIN, so start the
    # background threads there to avoid two loggers writing the same files.
    if _IS_SERVING:
        # Sims run automatically from startup — force auto ON regardless of any
        # persisted state, so it never waits for the user to flip the toggle.
        import sim
        sim.set_auto(True)

        # Clean Ctrl+C: quiet the two benign shutdown-time races from daemon/server
        # threads. (1) A daemon intrabar resolver can enter a ThreadPoolExecutor just
        # as the interpreter starts finalizing → RuntimeError("cannot schedule new
        # futures after interpreter shutdown"). (2) On Windows, select() on the
        # just-closed dev-server socket raises OSError(WinError 10038). Neither is a
        # real failure. We flip a stop flag at exit so the resolvers bail BEFORE
        # starting a pool, and drop just those two in the thread excepthook (delegating
        # everything else so genuine errors still surface).
        import atexit as _atexit
        import threading as _threading
        _prev_excepthook = _threading.excepthook

        def _quiet_shutdown_excepthook(args):
            e = args.exc_value
            if isinstance(e, RuntimeError) and "interpreter shutdown" in str(e):
                return
            if isinstance(e, OSError) and getattr(e, "winerror", None) == 10038:
                return
            _prev_excepthook(args)

        _threading.excepthook = _quiet_shutdown_excepthook

        def _graceful_stop():
            import ideas_journal as _ij
            for m in (sim, _ij):
                try:
                    m.request_stop()
                except Exception:
                    pass
            try:
                snaplog.stop()
            except Exception:
                pass

        _atexit.register(_graceful_stop)
        snaplog.start()
        # Prune the reproducible time-series once per startup so market.db stays
        # bounded (AUDIT.md M6). Durable ledgers + the EOD cache are kept. Daemon
        # thread so a large first sweep never delays the dashboard coming up.
        import db as _db
        import threading as _th
        def _retain():
            try:
                deleted = _db.retention()
                if any(deleted.values()):
                    log.info("DB retention pruned: %s", deleted)
            except Exception:
                log.warning("DB retention failed", exc_info=True)
        _th.Thread(target=_retain, daemon=True).start()
        # Live realtime feed (Angel One or Dhan). No-op unless credentials + SDK
        # are present, so the app runs unchanged for users who haven't set it up.
        live_feed.start()
        # Pre-warm the strategy-of-the-day leaderboard AND the walk-forward
        # robustness overlay (both EOD-cached backtests over the SAME daily bars, so
        # the second is ~pure CPU) in a daemon thread, so the Sim tab's card is ready
        # — with robustness verdicts — without stalling the first poll on a cold run.
        import threading
        import backtest_daily as _btd

        def _warm_sim():
            for fn in (_btd.cached_regime_leaderboard, _btd.cached_walkforward):
                try:
                    fn()
                except Exception:
                    log.warning("sim pre-warm failed (%s)", fn.__name__, exc_info=True)

        threading.Thread(target=_warm_sim, daemon=True).start()

        # Pre-warm the EOD bhavcopy cache (2 static archive files) so the last-
        # resort price fallback + EOD status are instant, and off-hours pricing
        # for any listed symbol works from the first request.
        def _warm_eod():
            try:
                import bhavcopy
                bhavcopy.latest()
            except Exception:
                log.warning("EOD bhavcopy pre-warm failed", exc_info=True)

        threading.Thread(target=_warm_eod, daemon=True).start()

        # Auto EOD backfill after the 15:30 close: one paced, block-aware refresh
        # (bhavcopy + deals + optional digest) per trading day, so the EOD scanner /
        # conviction board / backtests are fresh without clicking "Load EOD". Opt-out
        # via NSE_EOD_AUTO=0. Persists its last-run date, so reloader restarts don't
        # re-trigger it.
        try:
            import eod_scheduler
            eod_scheduler.start()
        except Exception:
            log.warning("auto-EOD scheduler failed to start", exc_info=True)

        # Show the phone-friendly URLs once (in the serving worker).
        ip = _lan_ip() if HOST == "0.0.0.0" else HOST
        print("\n" + "=" * 60)
        print(" NSE Market Pulse — dashboard is live")
        print(f"   Local:    http://127.0.0.1:{PORT}")
        if HOST == "0.0.0.0" and ip:
            print(f"   Network:  http://{ip}:{PORT}   <-- open this on your phone")
            print("   (phone must be on the same Wi-Fi; allow Python through")
            print("    the Windows firewall if prompted)")
        if HOST != "127.0.0.1" and not ACCESS_TOKEN:
            print("   ⚠  Reachable on your LAN with NO access token. Anyone on")
            print("      this network can use every endpoint. To lock it down set")
            print("      NSE_TOKEN=<secret> (then open the app with ?token=<secret>),")
            print("      or bind loopback-only with HOST=127.0.0.1.")
        if DEBUG:
            print("   ⚠  FLASK_DEBUG=1 — interactive debugger ENABLED (dev only).")
        print("=" * 60 + "\n")
    # threaded so a long request (e.g. a full-universe daily backtest, ~2-3 min)
    # doesn't block the dashboard's auto-refresh polling. The interactive
    # debugger stays OFF unless FLASK_DEBUG=1 (AUDIT.md H1); the reloader is
    # independent so .py edits still auto-restart during development.
    try:
        app.run(host=HOST, port=PORT, threaded=True,
                debug=DEBUG, use_reloader=RELOAD, use_debugger=DEBUG)
    except KeyboardInterrupt:
        print("\nShutting down NSE Market Pulse…")
