# NSE Market Pulse

A live dashboard that surfaces which NSE (National Stock Exchange of India)
stocks are **in demand right now** — built for spotting intraday momentum and
unusual activity. It pulls data straight from NSE India's public JSON API and
presents it in a clean, auto-refreshing web UI.

> **Disclaimer:** This project is for **educational and research purposes only**.
> It uses NSE India's unofficial/public endpoints and is **not affiliated with
> NSE**. Nothing here is investment advice. Intraday trading is high-risk —
> always use stop-losses and proper risk management.

## Features

- **Demand Score board** — a composite ranking that floats stocks appearing
  across multiple hot lists (volume spikes + money flow + price gains) to the top.
- **Volume Gainers** — stocks trading far above their average volume (the classic
  "something's happening" signal).
- **F&O Open Interest spurts** — where derivatives traders are building
  positions, auto-classified as *long buildup / short buildup / short covering /
  long unwinding* using the underlying's price direction.
- **Top Gainers / Losers** and **Most Active by Volume / Value**.
- **Live sparklines** — mini price trend per stock, built up live while the page
  is open.
- **Click any stock** for a detail modal with a larger live price chart and key
  metrics.
- **Alerts** — desktop notification + sound when a stock crosses a configurable
  volume multiple (e.g. 50x average) with a rising price.

## Tech stack

- **Python 3.13**, **Flask** for the backend + JSON API
- **requests** for NSE data (with session cookie warm-up to get past NSE's
  anti-bot protection)
- Vanilla HTML/CSS/JS frontend (no build step)

## Getting started

```bash
# 1. Clone
git clone git@github.com:aakash-jain-1/nse-market-pulse.git
cd nse-market-pulse

# 2. (Optional) create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the dashboard
python app.py
```

Then open **http://127.0.0.1:5055** in your browser.

> Tip: for best results during market hours (09:15–15:30 IST), set
> **Auto-refresh** to 15s and turn on **Sound alerts** at 50x volume.

## Command-line scanner

There's also a terminal-only scanner if you don't want the web UI:

```bash
python nse_demand.py            # show everything
python nse_demand.py gainers    # only top gainers
python nse_demand.py volume     # most active by volume
python nse_demand.py value      # most active by value
python nse_demand.py volgainers # volume gainers (unusual activity)
python nse_demand.py losers     # top losers
```

## Project structure

```
nse-market-pulse/
├── app.py            # Flask server + JSON API endpoints
├── nse_client.py     # NSE session handling + data fetching / normalization
├── nse_demand.py     # Standalone CLI scanner
├── templates/
│   └── index.html    # Dashboard UI
├── requirements.txt
└── README.md
```

## How it works

NSE blocks plain HTTP requests, so `nse_client.py` first visits the homepage to
collect session cookies, then reuses that session for the API calls (rebuilding
it automatically when it expires). Each endpoint's response is normalized into
clean lists of dicts that both the CLI and the Flask API consume.

## Notes & limitations

- NSE's per-stock quote and intraday chart endpoints are heavily anti-bot
  protected, so live charts are built **client-side** by accumulating prices
  across refreshes while the page is open.
- Data availability depends on market hours; outside 09:15–15:30 IST you'll see
  the last snapshot or empty lists.
- Endpoint behavior can change without notice since these are unofficial APIs.
