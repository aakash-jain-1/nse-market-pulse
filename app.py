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

from flask import Flask, jsonify, render_template

import nse_client as nse

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


@app.route("/api/demand")
def api_demand():
    return jsonify(nse.get_demand_score())


@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5055)
