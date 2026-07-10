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

import os

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import nse_client as nse
import nse_quote
import paper
import snapshot_logger as snaplog

app = Flask(__name__)


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


@app.route("/api/deepdive/<symbol>")
def api_deepdive(symbol):
    return jsonify(nse.get_stock_deepdive(symbol))


@app.route("/api/quote/<symbol>")
def api_quote(symbol):
    return jsonify(nse_quote.get_quote(symbol))


@app.route("/api/chart/<symbol>")
def api_chart(symbol):
    return jsonify(nse_quote.get_chart(symbol))


@app.route("/api/ohlc/<symbol>")
def api_ohlc(symbol):
    interval = int(request.args.get("interval", 1))
    chart_type = "D" if request.args.get("type") == "D" else "I"
    days = request.args.get("days")
    frm = request.args.get("from")
    to = request.args.get("to")
    return jsonify(nse_quote.get_ohlc(
        symbol, interval=interval, chart_type=chart_type,
        days=int(days) if days else None,
        from_ts=int(frm) if frm else None,
        to_ts=int(to) if to else None))


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


@app.route("/api/sim/summary")
def api_sim_summary():
    import sim
    return jsonify(sim.summary(strategy_id=request.args.get("strategy")))


@app.route("/api/sim/daily")
def api_sim_daily():
    import sim
    return jsonify({**sim.daily_matrix(), "perf": sim.daily_performance()})


@app.route("/api/sim/day")
def api_sim_day():
    import sim
    return jsonify(sim.day_trades(request.args.get("date", "")))


@app.route("/api/sim/leaderboard")
def api_sim_leaderboard():
    import sim
    return jsonify(sim.leaderboard_bundle())


@app.route("/api/sim/performance")
def api_sim_performance():
    import sim
    return jsonify(sim.performance())


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
    """Daily-bar historical backtest over REAL NSE end-of-day history."""
    import backtest_daily as btd
    resolve = request.args.get("resolve", "daily")
    return jsonify(btd.run(
        days=int(request.args.get("days", 30)),
        universe_size=int(request.args.get("universe", 40)),
        max_hold=int(request.args.get("maxHold", 5)),
        force=request.args.get("refresh") in ("1", "true", "yes"),
        resolve="intrabar" if resolve == "intrabar" else "daily",
    ))


@app.route("/api/sim/regime")
def api_sim_regime():
    import sim
    return jsonify(sim.current_regime())


@app.route("/api/sim/strategy_of_day")
def api_sim_strategy_of_day():
    """Today's live regime + the historically best strategy for that regime."""
    import backtest_daily as btd
    return jsonify(btd.strategy_of_day(
        days=int(request.args.get("days", 60)),
        universe_size=int(request.args.get("universe", 60)),
    ))


@app.route("/api/sim/take", methods=["POST"])
def api_sim_take():
    import sim
    body = request.get_json(silent=True) or {}
    strat = body.get("strategy")
    ids = [strat] if strat else None
    added = sim.take(strategy_ids=ids)
    out = sim.summary(strategy_id=strat)
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
    sim.reset()
    return jsonify(sim.summary())


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
    return jsonify({"error": str(e)}), 500


DEBUG = True
PORT = int(os.environ.get("PORT", "5055"))
# Bind to all interfaces so phones/tablets on the same Wi-Fi can reach the
# dashboard. Override with HOST=127.0.0.1 to keep it local-only.
HOST = os.environ.get("HOST", "0.0.0.0")


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
    # In debug mode Flask spawns a reloader parent + a worker child. Only the
    # worker (which actually serves) sets WERKZEUG_RUN_MAIN, so start the
    # background logger there to avoid two loggers writing the same file.
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        # Sims run automatically from startup — force auto ON regardless of any
        # persisted state, so it never waits for the user to flip the toggle.
        import sim
        sim.set_auto(True)
        snaplog.start()
        # Pre-warm the strategy-of-the-day leaderboard (a cheap, EOD-cached
        # backtest) in a daemon thread so the Sim tab's card is ready without
        # stalling the first poll with a cold ~30s computation.
        import threading
        import backtest_daily as _btd
        threading.Thread(target=_btd.cached_regime_leaderboard,
                         daemon=True).start()

        # Show the phone-friendly URLs once (in the serving worker).
        ip = _lan_ip() if HOST == "0.0.0.0" else HOST
        print("\n" + "=" * 60)
        print(" NSE Market Pulse — dashboard is live")
        print(f"   Local:    http://127.0.0.1:{PORT}")
        if HOST == "0.0.0.0" and ip:
            print(f"   Network:  http://{ip}:{PORT}   <-- open this on your phone")
            print("   (phone must be on the same Wi-Fi; allow Python through")
            print("    the Windows firewall if prompted)")
        print("=" * 60 + "\n")
    # threaded so a long request (e.g. a full-universe daily backtest, ~2-3 min)
    # doesn't block the dashboard's auto-refresh polling.
    app.run(debug=DEBUG, host=HOST, port=PORT, threaded=True)
