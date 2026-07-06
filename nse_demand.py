"""
NSE "In-Demand" Scanner
=======================
Pulls live data from NSE India's JSON API to find which stocks are hot right now:
  - Top Gainers / Top Losers
  - Most Active by Volume
  - Most Active by Value (traded amount)
  - Volume Gainers (trading far above their average volume)

NSE blocks plain requests, so we first hit the homepage to grab session
cookies, then reuse that session for the API calls.

Usage:
    python nse_demand.py              # show everything
    python nse_demand.py gainers      # only top gainers
    python nse_demand.py volume       # only most active by volume
    python nse_demand.py value        # only most active by value
    python nse_demand.py volgainers   # volume gainers (unusual activity)
    python nse_demand.py losers       # top losers
"""

import sys
import requests
from tabulate import tabulate

BASE = "https://www.nseindia.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

ENDPOINTS = {
    "gainers": "/api/live-analysis-variations?index=gainers",
    "losers": "/api/live-analysis-variations?index=loosers",
    "volume": "/api/live-analysis-most-active-securities?index=volume",
    "value": "/api/live-analysis-most-active-securities?index=value",
    "volgainers": "/api/live-analysis-volume-gainers",
}


def get_session():
    """Create a session and warm it up so NSE hands us the cookies."""
    s = requests.Session()
    s.headers.update(HEADERS)
    # Warm-up requests to collect cookies.
    s.get(BASE, timeout=15)
    s.get(BASE + "/market-data/live-equity-market", timeout=15)
    return s


def fetch(session, path):
    r = session.get(BASE + path, timeout=15)
    r.raise_for_status()
    return r.json()


def show_variations(session, key, title):
    """Gainers / losers come nested under NIFTY / allSec buckets."""
    data = fetch(session, ENDPOINTS[key])
    # Prefer the broad 'allSec' bucket; fall back to any available list.
    bucket = data.get("allSec") or data.get("NIFTY") or {}
    rows = bucket.get("data", [])[:15]
    table = [
        [
            r.get("symbol"),
            r.get("ltp"),
            r.get("perChange"),
            r.get("prev_price"),
            r.get("trade_quantity"),
        ]
        for r in rows
    ]
    print(f"\n=== {title} ===")
    print(
        tabulate(
            table,
            headers=["Symbol", "LTP", "% Change", "Prev Close", "Volume"],
            tablefmt="github",
        )
    )


def show_most_active(session, key, title):
    data = fetch(session, ENDPOINTS[key])
    rows = data.get("data", [])[:15]
    table = [
        [
            r.get("symbol"),
            r.get("lastPrice"),
            r.get("pChange"),
            r.get("totalTradedVolume"),
            r.get("totalTradedValue"),
        ]
        for r in rows
    ]
    print(f"\n=== {title} ===")
    print(
        tabulate(
            table,
            headers=["Symbol", "LTP", "% Change", "Volume", "Value (Rs)"],
            tablefmt="github",
        )
    )


def show_volume_gainers(session, title):
    data = fetch(session, ENDPOINTS["volgainers"])
    rows = data.get("data", [])[:15]

    def times(x):
        try:
            return f"{float(x):.1f}x"
        except (TypeError, ValueError):
            return "-"

    table = [
        [
            r.get("symbol"),
            r.get("ltp"),
            r.get("pChange"),
            r.get("volume"),
            r.get("week1AvgVolume"),
            times(r.get("week1volChange")),
        ]
        for r in rows
    ]
    print(f"\n=== {title} ===")
    print(
        tabulate(
            table,
            headers=[
                "Symbol",
                "LTP",
                "% Change",
                "Today Vol",
                "1W Avg Vol",
                "vs 1W Avg",
            ],
            tablefmt="github",
        )
    )


def main():
    what = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    session = get_session()

    try:
        if what in ("all", "gainers"):
            show_variations(session, "gainers", "TOP GAINERS")
        if what in ("all", "losers"):
            show_variations(session, "losers", "TOP LOSERS")
        if what in ("all", "volume"):
            show_most_active(session, "volume", "MOST ACTIVE BY VOLUME")
        if what in ("all", "value"):
            show_most_active(session, "value", "MOST ACTIVE BY VALUE (money flow)")
        if what in ("all", "volgainers"):
            show_volume_gainers(session, "VOLUME GAINERS (unusual activity)")
    except requests.HTTPError as e:
        print(f"HTTP error from NSE: {e}")
    except Exception as e:
        print(f"Something went wrong: {e}")


if __name__ == "__main__":
    main()
