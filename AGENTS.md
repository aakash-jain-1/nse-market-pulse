# Project Context — NSE Market Pulse

> This file is the single source of truth for AI agents / future sessions
> working on this project. Read it first. Keep it updated when things change.

## What this project is

**NSE Market Pulse** is a live dashboard + CLI that surfaces which NSE (National
Stock Exchange of India) stocks are **"in demand" right now**, aimed at spotting
intraday momentum and unusual activity. It pulls data from NSE India's public
(unofficial) JSON API and presents it in an auto-refreshing web UI.

- **GitHub:** `git@github.com:aakash-jain-1/nse-market-pulse.git` (branch `main`)
- **Purpose:** educational/research; NOT investment advice.
- **Owner:** aakash-jain-1

## Tech stack

- Python **3.13** (Windows). See "Environment gotchas" for the interpreter path.
- **Flask 3.1.3** — web server + JSON API
- **requests 2.34.2** — NSE HTTP calls (with cookie warm-up)
- **tabulate 0.10.0** — CLI table formatting
- Vanilla HTML/CSS/JS frontend (no build step, no framework)

## File structure

```
NSE/
├── app.py             # Flask server + JSON API endpoints (runs on port 5055)
├── nse_client.py      # NSE session mgmt + data fetching / normalization (CORE)
├── nse_quote.py       # Per-stock quote/chart/depth via NextApi gateway
├── paper.py           # Paper-trading engine (virtual portfolio, JSON-persisted)
├── snapshot_logger.py # Background snapshot logger + backtester (CSV)
├── nse_demand.py      # Standalone CLI scanner (original, still works)
├── templates/
│   └── index.html     # Entire dashboard UI (HTML + CSS + JS inline)
├── data/              # (gitignored) snapshots.csv lives here
├── requirements.txt
├── README.md
├── AGENTS.md          # <- this file
├── .gitignore
└── paper_state.json   # (gitignored) local virtual-portfolio state
```

## How to run

```bash
python app.py            # dashboard at http://127.0.0.1:5055
python nse_demand.py     # CLI: all views (also: gainers/losers/volume/value/volgainers)
```

The Flask app runs in debug mode, so it auto-reloads on `.py` changes and
re-reads `templates/index.html` on every request (no restart needed for UI edits).

## Environment gotchas (IMPORTANT)

- On this Windows machine the bare `python` command sometimes resolves to the
  Microsoft Store shim and fails with *"Python was not found"*. Use the full
  interpreter path when that happens:
  `C:/Users/aakas/AppData/Local/Programs/Python/Python313/python.exe`
- **Port 5000 is contaminated** by a *different* previously-run app (a "BSE
  Corporate Announcements" PWA) whose **service worker** is cached in the
  browser and hijacks `127.0.0.1:5000`. That's why we run on **port 5055**.
  If port 5000 shows the wrong app: hard-refresh (Ctrl+Shift+R) or unregister
  the service worker (F12 → Application → Service Workers → Unregister).

## Architecture notes

### NSE session handling (`nse_client.py`)
NSE blocks plain HTTP requests. We must:
1. Create a `requests.Session` with a browser-like `User-Agent` + `Referer`.
2. **Warm it up** by GETting the homepage and `/market-data/live-equity-market`
   so NSE sets session cookies.
3. Reuse that session for API calls; rebuild it automatically on failure and
   after a TTL (`_SESSION_TTL = 300s`). Guarded by a lock for concurrency.

### Data flow
`nse_client.py` fetches + normalizes each endpoint into clean `list[dict]`.
Both `app.py` (JSON API) and `nse_demand.py` (CLI) consume these functions.
The frontend polls `/api/<view>` and renders tables client-side.

### Working NSE endpoints (verified)
| Purpose | Endpoint |
|---------|----------|
| Top gainers | `/api/live-analysis-variations?index=gainers` |
| Top losers | `/api/live-analysis-variations?index=loosers` (note NSE's misspelling) |
| Most active by volume | `/api/live-analysis-most-active-securities?index=volume` |
| Most active by value | `/api/live-analysis-most-active-securities?index=value` |
| Volume gainers | `/api/live-analysis-volume-gainers` |
| OI spurts (underlyings) | `/api/live-analysis-oi-spurts-underlyings` |
| Most-active stock futures | `/api/liveEquity-derivatives?index=stock_fut` (has underlying+pChange+OI+lastPrice+underlyingValue+expiry; ~20 rows) |
| Intraday chart (OLD, empty) | `/api/chart-databyindex?index=<SYMBOL>EQN` |

### NextApi gateway (NEW — the big unlock, `nse_quote.py`)
The current NSE website uses a newer gateway that DOES work from our warmed
session, **as long as we send a stock-specific Referer**
(`/get-quote/equity/<SYMBOL>`). Base path:

    /api/NextApi/apiClient/GetQuoteApi?functionName=<fn>&...

| Purpose | functionName | Notes |
|---------|--------------|-------|
| Full quote + 5-level market depth | `getSymbolData&marketType=N&series=EQ&symbol=X` | LTP in `tradeInfo.lastPrice`; change/open/high/low in `metaData`; depth in `orderBook`; delivery % in `tradeInfo.deliveryToTradedQuantity` |
| Real intraday chart | `getSymbolChartData&symbol=<X>EQN&days=1D` | `grapthData` = `[[ts_ms, price, phase, ...], ...]` (400+ pts/day) |
| Company meta | `getMetaData&symbol=X` | |
| Option expiries/strikes | `getOptionChainDropdown&symbol=X` | `expiryDates`, `strikePrice` lists |
| Option chain | `getOptionChainData&symbol=X&params=expiryDate=<28-Jul-2026>` | note the `params=expiryDate=...` nested form; `data[].CE/PE` + `underlyingValue` |

This finally gives real charts, per-stock quotes for ANY symbol, and market
depth. `nse_client.get_price()` falls back to `nse_quote.get_ltp()` so paper
trading works for any tradable symbol (not just hot-list names).

**Note on symbol renames:** some underlyings changed tickers (e.g. TATAMOTORS →
`TMPV`); use the current F&O symbol. Non-F&O symbols return "no expiries".

### BLOCKED / unreliable endpoints (do not rely on)
- `/api/quote-equity?symbol=X` → **403 Forbidden** (superseded by NextApi above).
- `/api/chart-databyindex?index=<SYMBOL>EQN` → **empty** (superseded by
  `getSymbolChartData` above). Client-side sparklines are still used as an
  instant fallback in the detail modal while the real chart loads.
- `/api/snapshot-derivatives-equity?index=oi_gainers` → "No Data Found"
  pre-market; only has data during market hours.
- `/api/equity-stockIndices?index=...` → 404 with the names we tried.
- Market depth (`orderBook`) is all-zeros outside market hours (09:15–15:30 IST).

## Feature summary (what's built)

- **Demand Score** — composite ranking combining volume-gainers (volume
  multiple), most-active-by-value (money flow rank), and top-gainers (% gain).
  See `get_demand_score()`.
- **Volume Gainers**, **Top Gainers/Losers**, **Most Active (Volume/Value)**.
- **F&O Open Interest tab** — OI spurts enriched with the underlying's real
  `pChange` (cross-referenced from `stock_fut` + gainers/losers + most-active,
  cached ~20s). Classified server-side into: Long buildup / Short buildup /
  Short covering / Long unwinding, with an honest grey "OI Rising/Falling"
  fallback when the price direction is genuinely unknown (e.g. indices).
- **Option Chain** (`⛓ Options` button, or from the detail modal) — full CE/PE
  grid for any F&O symbol + expiry, with **PCR**, **max pain**, **ATM** highlight,
  ITM shading, and OI-size bars. Backed by `nse_quote.get_option_chain()`.
- **Live sparklines** per row (client-side, accumulate across refreshes).
- **Stock detail modal** on row click — now shows the **real NSE intraday
  chart** (`getSymbolChartData`, with prev-close line), **5-level market depth**
  (`orderBook`), and enriched metrics (delivery %, VWAP, day/52W range). Falls
  back to the session sparkline when the real chart isn't available.
- **Alerts** — desktop notification + sound beep when a stock crosses a
  configurable volume multiple (20x/50x/100x) with a rising price.
- **CSV export** — client-side download of the current view (⬇ CSV button).
- **Paper trading** (`paper.py`) — virtual portfolio starting at Rs 10,00,000.
  Buy/Sell from the stock detail modal; 💼 Portfolio button shows holdings,
  live mark-to-market P&L, and order history. Fills are simulated at the latest
  price from `nse_client.get_price()`, which merges all live lists into a
  symbol->LTP map. **Limitation:** only symbols currently in the hot lists
  (~100-150) have a price, so only those are tradable. State persists to
  `paper_state.json` (gitignored). This is broker-agnostic by design: swapping
  in a real broker feed later only changes the price/fill source.
- **Futures tab** (`get_futures()`) — most-active stock futures with **basis**
  (futures price - spot = premium/discount), basis %, annualized carry (by days
  to expiry), OI, and long/short buildup. OI change is cross-referenced from the
  OI-spurts endpoint (partial coverage -> "OI n/a" when unknown). Note stock_fut
  returns only ~20 (most-active) contracts, not the full F&O universe.
- **Snapshot logging + backtest** (`snapshot_logger.py`) — a daemon thread
  captures the demand board + volume-gainers (25 each) to `data/snapshots.csv`
  every 60s **during market hours only** (Mon-Fri 09:15-15:30 IST). The 📊 Log
  button shows logger status, a manual "Capture now", CSV download, and a simple
  **forward-return backtest** (price move from a symbol's first sighting to its
  latest, with avg return + hit rate). Started in `app.py` guarded by
  `WERKZEUG_RUN_MAIN` so the Flask reloader doesn't run two loggers.

## Known limitations

- Real intraday charts + depth come from the NextApi gateway (per-symbol, needs
  the stock-specific Referer); depth is empty outside market hours.
- OI price-direction coverage is partial pre-market; improves during 09:15–15:30 IST.
- All endpoints are unofficial and can change without notice.
- Data only meaningful during NSE market hours (Mon–Fri, 09:15–15:30 IST).

## Roadmap / ideas (not yet built)

- **Real-time broker feed** (the big one): integrate a free broker API for live
  ticks, working charts, and market depth. User is leaning toward starting with
  **Angel One SmartAPI** or **Upstox v2** (both free) but has no account yet.
  Plan: keep the paper-trading interface, swap `get_price`/fills for the broker
  feed. Needs the user's API credentials.
- Persist sparkline price history (survive page reload) via localStorage.
- Phone/LAN access + optional deploy.
- Options analytics: OI change heatmap, IV skew chart, multi-expiry PCR trend.
- Paper-trade options (buy/sell CE/PE contracts) from the chain grid.
- Use real per-stock quotes to remove the paper-trading hot-list limit fully.
- Consider `jugaad-data` / `nsefeed` as a more robust fallback for the flaky
  bits (quotes, historical). See README/analysis for the API landscape.

## Done recently

- OI % change column + CSV export.
- Paper trading engine (see feature summary).
- Snapshot logging + forward-return backtest (see feature summary).
- Futures tab: basis (premium/discount), annualized carry, OI buildup.
- NextApi gateway integration (`nse_quote.py`): real intraday charts, per-stock
  quotes for any symbol, and 5-level market depth in the detail modal.
- Option chain module: full CE/PE grid + PCR / max pain / ATM analytics.

## Futures roadmap (user wants to trade futures)

- Futures paper trading (lot sizes, margin/leverage, MTM). Needs a lot-size
  source (stock_fut lacks marketLot; NSE publishes an F&O lot-size master).
- Rollover tracker (OI shift current-month -> next-month near expiry).
- Full F&O universe (stock_fut is only ~20 most-active contracts).

## Conventions

- Keep data-fetching logic in `nse_client.py`; keep `app.py` thin (routes only).
- Normalize NSE fields into stable keys (`symbol`, `ltp`, `pChange`, `volume`,
  ...) so the frontend/CLI don't depend on NSE's raw field names.
- No secrets in the repo (`.gitignore` covers `.env`). NSE needs no API key.
- Only commit/push when the user explicitly asks.
```
