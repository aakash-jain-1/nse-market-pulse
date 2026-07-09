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
| Daily history (OHLC+vol+delivery%) | `/api/historicalOR/generateSecurityWiseHistoricalData?from=DD-MM-YYYY&to=DD-MM-YYYY&symbol=<SYM>&type=priceVolumeDeliverable&series=EQ` — Referer `/get-quote/equity?symbol=<SYM>`. **Caps at ~70 trading days from `to`**, so `get_stock_history()` fetches back-to-back windows and merges. Powers the deep-dive. |
| Historical F&O OI-over-time + lot size | `/api/historicalOR/foCPV?from=DD-MM-YYYY&to=DD-MM-YYYY&instrumentType=FUTSTK&symbol=<SYM>&year=YYYY&expiryDate=DD-MON-YYYY` (UPPERCASE month) — Referer `/report-detail/fo_eq_security`. **Works.** Returns daily `FH_OPEN_INT`, `FH_CHANGE_IN_OI`, `FH_CLOSING_PRICE`, `FH_UNDERLYING_VALUE`, `FH_MARKET_LOT`. `get_futures_oi_history()` powers the deep-dive OI chart. Use `instrumentType=OPTSTK&optionType=CE/PE&strikePrice=` for option OI. NOTE: near-month OI inflates ~10 sessions before/after rollover, so trend reads use a short (~5-session) window. (The `/api/historical/...` path 503s — must be `historicalOR`.) |

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
| Per-symbol futures + options | `getSymbolDerivativesData&symbol=X` | `data[]` of contracts; futures = `instrumentType` FUTSTK/FUTIDX (has lastPrice, OI, chgOI, volume, underlyingValue). Covers the WHOLE F&O universe, unlike the 20-row `stock_fut` feed. Referer: `/get-quote/derivatives?symbol=X`. |
| Option chain | `getOptionChainData&symbol=X&params=expiryDate=<28-Jul-2026>` | note the `params=expiryDate=...` nested form; `data[].CE/PE` + `underlyingValue`. Works for **indices too** (NIFTY/BANKNIFTY/…) with the same call. |
| Full F&O universe | `/api/underlying-information` | 5 indices + ~210 stock underlyings (also `/api/master-quote`). Cached 1h in `get_fno_universe()`. |

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

- **Stock Deep-Dive** (`🔬 Analyze` header button, or from the detail modal) —
  type any NSE symbol → `get_stock_deepdive()` (`/api/deepdive/<sym>`). Pulls
  ~90 trading days of daily history (`get_stock_history()`, chunked), computes
  30/60/90-day returns, 20/50-DMA, 90-day high/low + distance, volume ratio,
  avg + trend of delivery %, annualized volatility. Adds the live futures
  (basis/OI/signal) and options (PCR/max-pain/support-resistance) snapshot, then
  `_analyze_stock()` synthesizes a bias (score -100..100), plain-English "what to
  watch today" notes and key support/resistance levels. Price+volume chart in a
  modal. Educational, not advice.
- **Column/param tooltips** — hover any table header or metric label for a
  plain-English meaning incl. whether up = bullish/bearish (`COL_INFO` map +
  `annotateInfo()` in the frontend; dotted underline affordance).
- **Trade Ideas** (`💡 Ideas`) — ranked LONG/SHORT setups from
  `get_recommendations()` (`/api/recommendations?fno=1`). Builds on the scanner
  aggregate: `_build_idea()` scores a bull/bear net from price momentum + OI
  buildup signal + unusual volume + multi-signal breadth, emits a conviction
  (0-99, High/Med/Low), plain-English reasons, and an entry/stop/target plan
  (stop scales with the day's move, target = 2× risk → 1:2 R:R). Two columns of
  cards, click to open the detail modal. Educational only — NOT advice.
- **Scanner** (`🔎 Scanner`) — the one-stop "in-demand right now"
  board. `get_scanner()` aggregates every cheap hot list (volume gainers,
  most-active value/volume, gainers/losers, OI spurts, futures) into a per-symbol
  composite score with human-readable **tags** (⭐ Multi-signal, 🔥 Unusual
  volume, 💰 Money flow, 📈/📉 Momentum, 🟢/🔴 OI buildup). Client-side filter
  bar → `/api/scanner?direction=&minChange=&minVolMult=&minValueCr=&oi=&fno=1`.
- **Demand Score** — composite ranking combining volume-gainers (volume
  multiple), most-active-by-value (money flow rank), and top-gainers (% gain).
  See `get_demand_score()`.
- **Volume Gainers**, **Top Gainers/Losers**, **Most Active (Volume/Value)**.
- **Futures tab** — near-month basis / premium-discount, annualized carry, and
  OI buildup. Two modes via a toggle: **Most active** (fast, NSE's 20-row
  `stock_fut` feed) and **All F&O** (`get_all_futures()` — a concurrent
  per-symbol sweep of the whole ~215-name universe via `getSymbolDerivativesData`,
  6 workers, cached 90s; `/api/futures/all`). Per-symbol via `/api/futures/<sym>`.
- **F&O Open Interest tab** — OI spurts enriched with the underlying's real
  `pChange` (cross-referenced from `stock_fut` + gainers/losers + most-active,
  cached ~20s). Classified server-side into: Long buildup / Short buildup /
  Short covering / Long unwinding, with an honest grey "OI Rising/Falling"
  fallback when the price direction is genuinely unknown (e.g. indices).
- **Option Chain** (`⛓ Options` button, or from the detail modal) — full CE/PE
  grid for any F&O symbol + expiry, with **PCR**, **max pain**, **ATM** highlight,
  ITM shading, and OI-size bars. Backed by `nse_quote.get_option_chain()`. Also:
  - **Support/Resistance** — top-3 PUT-OI strikes (support) and CALL-OI strikes
    (resistance), with % distance from spot.
  - **OI-change chart** (CE vs PE chg-OI bars around ATM) + **IV-skew chart**
    (call/put IV vs strike) — client-side SVG, spot marker on both.
  - **All-expiry summary** — PCR / max-pain / OI per expiry with a bull/bear
    bias flag, via `get_option_summary()` (`/api/optionchain/<sym>/summary`).
  - **Full F&O universe picker** — the symbol box autocompletes across all ~215
    F&O names (`get_fno_universe()` / `/api/fno/universe`), with one-click
    **index chips** (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50). Index
    option chains work through the same equity endpoint.
  - **Greeks** — Black-Scholes delta/gamma/theta/vega computed per leg from
    spot/strike/DTE/IV (`_bs_greeks`, r≈6.5%). Grid has an **OI ⇄ Greeks** toggle.
  - **IV Rank** — shown in the summary strip when history exists. The snapshot
    logger captures ATM IV for indices + most-active F&O every 5 min into
    `data/iv_log.csv`; `snapshot_logger.iv_rank()` (`/api/iv/rank/<sym>`) turns
    that into an IV rank/percentile. Meaningful once history accumulates.
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
 symbol->LTP map. **Options too:** `place_option_order()` fills CE/PE at the
 live premium (from the option chain) via a trade box in the ⛓ Options modal;
 option positions are tracked per-contract and re-priced live in the portfolio.
 **Options are sized in LOTS** — `place_option_order(...lots)` multiplies by the
 underlying's market lot (`nse_client.get_lot_size()`, from NSE's `fo_mktlots.csv`,
 215 names, cached a day); the trade box shows lot size + total units + est. cost,
 and the portfolio/orders show lots. **Futures too:** `place_futures_order(sym,
 side, lots)` is margin-based (~15% of notional, `FUT_MARGIN_RATE`), supports
 LONG **and** SHORT with proper netting/flip-through-zero, realizes P&L + releases
 margin on close, and marks to market on the live near-month price. Traded from a
 "Paper trade FUTURES" box in the detail modal (shown only for F&O names).
 **Limitation:** only symbols currently in the hot lists
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
- Phone/LAN access + optional deploy.
- Consider `jugaad-data` / `nsefeed` as a more robust fallback for the flaky
  bits (quotes, historical). See README/analysis for the API landscape.

## Done recently

- **Futures paper trading** (`place_futures_order()`): margin-based (~15% of
  notional), long **and** short with netting/flip-through-zero, MTM on live
  near-month price. New route `/api/paper/futures_order`; traded from the detail
  modal's "Paper trade FUTURES" box (F&O names only).
- **Lot-size enforcement in paper options**: options now trade in lots
  (`get_lot_size()` from `fo_mktlots.csv`); trade box + portfolio show lots/units.
- **One-click deep-dive** (🔬) from every table row, Ideas card and momentum row.
- **Stock Deep-Dive** (`get_stock_deepdive()` / `get_stock_history()` /
  `_analyze_stock()`): 30/60/90-day history + delivery/volume/volatility stats,
  live F&O/options snapshot, and a synthesized bias + levels + today's read.
  Discovered the working daily-history endpoint (`generateSecurityWiseHistorical
  Data`, capped ~70 trading days/req → fetched in chunks).
- **Historical futures OI** (`get_futures_oi_history()` via `historicalOR/foCPV`):
  real OI-over-time chart + lot size in the deep-dive; short-window OI/price read
  to avoid rollover false signals.
- **Column tooltips** (`COL_INFO` / `annotateInfo`): hover any header/metric for
  its meaning + up=good/bad guidance.
- **Futures tab is now first + the default** landing tab.
- **Trade Ideas tab** (`get_recommendations()` / `_build_idea()`): ranked
  LONG/SHORT setups with conviction score, reasons and entry/stop/target.
- **Futures Momentum panel**: on the Futures tab, two columns ranking the
  strongest bullish/bearish movers (price move × OI activity), client-side.
- **Intraday chart crosshair**: hover the detail-modal chart for price/%chg/time
  tooltip. Fixed the +5:30h label bug (NSE bakes IST into the epoch as UTC — read
  UTC components via `istTime()` instead of `toLocaleTimeString`).
- OI % change column + CSV export.
- Paper trading engine (see feature summary).
- Snapshot logging + forward-return backtest (see feature summary).
- Futures tab: basis (premium/discount), annualized carry, OI buildup.
- NextApi gateway integration (`nse_quote.py`): real intraday charts, per-stock
  quotes for any symbol, and 5-level market depth in the detail modal.
- Option chain module: full CE/PE grid + PCR / max pain / ATM analytics.
- Options analytics: support/resistance walls, OI-change + IV-skew charts,
  all-expiry PCR/max-pain summary.
- Sparkline history + alert state persisted to localStorage (intraday-only, so
  a browser refresh no longer wipes the client-side sparklines).
- Unified Scanner tab (`get_scanner()`): ranked in-demand board with filters
  (direction / min %chg / min vol×avg / min value Cr / OI buildup / F&O only)
  and explanatory tags. Now the default landing tab.
- Full F&O universe (`get_fno_universe()`): searchable option-chain picker for
  all ~215 F&O names + one-click index option chains.
- All-F&O futures coverage: per-symbol futures via getSymbolDerivativesData and
  a cached concurrent full-universe sweep behind the Futures-tab "All F&O" toggle.
- Option Greeks (Black-Scholes) with an OI/Greeks grid toggle in the chain.
- Paper-trade options: buy/sell CE/PE at live premium from the ⛓ Options modal.
- IV logging (ATM IV → data/iv_log.csv, every 5 min) + IV rank/percentile in the
  option chain summary.

## Futures roadmap (user wants to trade futures)

- Rollover tracker (OI shift current-month -> next-month near expiry).
- Full F&O universe (stock_fut is only ~20 most-active contracts).

## Conventions

- Keep data-fetching logic in `nse_client.py`; keep `app.py` thin (routes only).
- Normalize NSE fields into stable keys (`symbol`, `ltp`, `pChange`, `volume`,
  ...) so the frontend/CLI don't depend on NSE's raw field names.
- No secrets in the repo (`.gitignore` covers `.env`). NSE needs no API key.
- Only commit/push when the user explicitly asks.
```
