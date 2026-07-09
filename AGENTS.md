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
├── strategies.py      # Strategy library (7 generators) + market-regime detector
├── sim.py             # Multi-strategy forward-tester (per-strategy sims + daily rollup)
├── backtest_strategies.py # Offline backtester: replays strategies over stored context
├── db.py              # SQLite store (snapshots / IV / strategy-context time-series)
├── snapshot_logger.py # Background logger (snapshots + IV + strategy-context) → SQLite
├── nse_demand.py      # Standalone CLI scanner (original, still works)
├── templates/
│   └── index.html     # Entire dashboard UI (HTML + CSS + JS inline)
├── data/              # (gitignored) market.db (SQLite) + any legacy *.csv
├── requirements.txt
├── README.md
├── AGENTS.md          # <- this file
├── .gitignore
└── paper_state.json   # (gitignored) local virtual-portfolio state
```

## Data storage (IMPORTANT)

- **Time-series → SQLite** (`db.py`, `data/market.db`, gitignored via `*.db`).
  Tables: `snapshots` (demand/volgainers board), `iv_log` (ATM IV), `context_log`
  (a trimmed+gzipped snapshot of the full strategy context each cycle, ~6 KB/cycle).
  WAL mode for concurrent reads; indexed by view/ts/symbol/day. Reads no longer
  slurp whole CSVs into memory. On first run any legacy `snapshots.csv` /
  `iv_log.csv` is auto-imported (`db._import_legacy_csv`).
- **Small state → JSON** (`sim_state.json`, `paper_state.json`). Tiny, document-
  shaped, rewritten atomically — no DB needed. Don't "upgrade" these to SQLite.
- Not Postgres/Mongo/Timescale — those need a server and are overkill for a
  single-user local tool. If analytical backtests ever get huge, DuckDB is the
  drop-in upgrade, but we're nowhere near that.

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
| Real intraday chart (price-only) | `getSymbolChartData&symbol=<X>EQN&days=1D` | `grapthData` = `[[ts_ms, price, phase, ...], ...]` (400+ pts/day). **No volume** — superseded by the OHLCV feed below for the modal chart; kept as fallback. |
| Company meta | `getMetaData&symbol=X` | |
| Option expiries/strikes | `getOptionChainDropdown&symbol=X` | `expiryDates`, `strikePrice` lists |
| Per-symbol futures + options | `getSymbolDerivativesData&symbol=X` | `data[]` of contracts; futures = `instrumentType` FUTSTK/FUTIDX (has lastPrice, OI, chgOI, volume, underlyingValue). Covers the WHOLE F&O universe, unlike the 20-row `stock_fut` feed. Referer: `/get-quote/derivatives?symbol=X`. |
| Option chain | `getOptionChainData&symbol=X&params=expiryDate=<28-Jul-2026>` | note the `params=expiryDate=...` nested form; `data[].CE/PE` + `underlyingValue`. Works for **indices too** (NIFTY/BANKNIFTY/…) with the same call. |
| Full F&O universe | `/api/underlying-information` | 5 indices + ~210 stock underlyings (also `/api/master-quote`). Cached 1h in `get_fno_universe()`. |

This finally gives real charts, per-stock quotes for ANY symbol, and market
depth. `nse_client.get_price()` falls back to `nse_quote.get_ltp()` so paper
trading works for any tradable symbol (not just hot-list names).

### Real OHLCV candles (`charting.nseindia.com`) — `nse_quote.get_ohlc()`
Separate host that serves proper **OHLC + VOLUME** candles (the NextApi chart is
price-only). Keyed by an internal `scripcode` **token**, resolved once per symbol
and cached (`get_token()`), then fetched on demand and cached ~30s.

| Purpose | Endpoint |
|---------|----------|
| Symbol → token lookup | `/v1/exchanges/symbolsDynamic?symbol=<SYM>&exchange=NSE` → match `data[].symbol == "<SYM>-EQ"`, take `scripcode` |
| OHLCV candles | `/v1/charts/symbolHistoricalData?token=<t>&fromDate=<epoch_s>&toDate=<epoch_s>&symbol=<SYM>-EQ&symbolType=Equity&chartType=<I\|D>&timeInterval=<min>` |

- `chartType=I` = intraday (`timeInterval` = 1/5/15 min); `chartType=D` = daily.
- `fromDate=0` returns everything NSE retains (~30–40 days of 1-min). Intraday
  default = current session start (09:15 IST); daily default = ~120–180 days.
- **We do NOT store these ourselves** — NSE is the historical store; we query the
  window we need on demand. (Our SQLite `db.py` only persists things NSE does NOT
  keep: composite scores, hot-list rankings, strategy context, ATM IV.)
- Uses `Referer: https://charting.nseindia.com/`; reuses the warmed session.
- Exposed at `GET /api/ohlc/<symbol>?interval=<n>&type=<I|D>&days=<n>`.
- Frontend: detail-modal chart is now **candlesticks + a volume histogram** with
  a 1m/5m/15m/1D selector and OHLCV+time hover; falls back to the price-only line
  chart when a symbol has no token (e.g. renamed/non-equity).

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
- **Stock detail modal** on row click — shows a **real OHLCV candlestick chart
  with a volume histogram** (`charting.nseindia.com`, 1m/5m/15m/1D selector,
  OHLCV+time hover), **5-level market depth** (`orderBook`), and enriched metrics
  (delivery %, VWAP, day/52W range). Falls back to the price-only line chart
  (`getSymbolChartData`) and then the session sparkline when unavailable.
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

- **Multi-strategy Sim + regime-aware daily comparison** (`strategies.py` +
  `sim.py`, 🧪 Sim tab): the Sim now forward-tests **4 strategies in parallel**,
  each with its **own ledger**, so we can see which one fits which market day.
  - **Strategies** (`strategies.py`, 7): `momentum` (the original multi-signal
    engine), `oi_smart` (F&O OI positioning), `meanrev` (contrarian oversold
    bounce / fade), `vol_breakout` (≥5× volume explosions), `high52w`
    (nearness-to-52-week-high momentum, George-Hwang), `vwap` (price vs the
    institutional VWAP benchmark), `delivery` (high delivery% = accumulation/
    distribution). Each is `{id,name,description,regimeFit,generate(ctx)}`
    returning ideas in `_build_idea` shape. `build_context()` fetches all live
    lists ONCE — including a bounded, concurrent per-symbol quote fetch
    (`ctx["quotes"]`, ~45 liquid names) that feeds VWAP / 52wH / delivery — and
    every generator reuses it.
  - **Regime detector** (`detect_regime`): tags each day Trend-Up / Trend-Down /
    Recovery / Pullback / Range / Mixed from NIFTY %change + advance-decline
    breadth (`nse.get_index_snapshot()` → `/api/allIndices`, cached 30s) + the
    prior session's move.
  - **Per-strategy sims** (`sim.py` v2, `sim_state.json` version-gated): `take()`
    snapshots each strategy's ideas (risk-based sizing via `size_position()`:
    each trade risks a fixed ₹2,000 to its stop, notional-capped at ₹5L; dedup
    one entry per symbol+direction per strategy per day);
    `update()` marks to market and closes on target/stop **or a multi-day
    horizon** (`maxSessions`, default 3, then time-expire); entry mode is
    **selectable** — `continuous` (auto-take all day) or `open` (one snapshot/
    day). `daily_rollup()` stores each day's regime + per-strategy win-rate/P&L →
    `daily_matrix()` powers a **day × strategy heatmap**.
  - **Risk-based sizing + expectancy**: `sim.size_position(entry, stop)` sizes
    every trade to a fixed ₹2,000 risk (per-share risk = |entry-stop|), so each
    trade's outcome is measured in **R-multiples** (+1R = made what you risked,
    -1R = hit the stop). Scorecards expose `expectancyR` (avg R/trade) — the
    honest cross-strategy comparison — alongside rupee P&L. The backtester shares
    the exact same sizing.
  - **Regime leaderboard + strategy-of-the-day** (`regime_leaderboard()`,
    `strategy_of_the_day()`, `equity_curves()` → `leaderboard_bundle()`):
    aggregates every trade by **regime-at-entry × strategy** (avg %/trade, win%,
    #trades), flags the best strategy per regime (⭐), and picks the one to lean
    on today (best history in the current regime, ≥3 closed trades, else the
    design-fit strategy). Per-strategy **equity curves** (cumulative realized ₹)
    render as sparklines. This is the accumulating forward-test (a true offline
    backtest isn't possible — snapshots.csv only logs demand/volgainers, not the
    per-strategy inputs like VWAP/delivery/52wH/OI).
  - **UI**: regime banner, ⭐ strategy-of-the-day card, strategy cards (click to
    expand that strategy's open/closed tables), the **regime leaderboard** grid
    (+ equity sparklines), and the daily comparison heatmap. **Sim alerts** toast/
    beep/notify when a strategy takes new ideas or a trade hits target/stop
    (diffs per-strategy counts across polls). Controls: Take all, entry-mode
    dropdown, Auto, Reset.
  - **Offline backtest** (`backtest_strategies.py`, `/api/sim/backtest`): replays
    the SAME generators over the archived `context_log` on a virtual clock —
    reprice open trades (target/stop/multi-day-expiry), take fresh ideas (one per
    symbol+direction per day; `open` vs `continuous` entry), forward-price from
    later cycles. Returns per-strategy scorecards + equity curves + a regime
    leaderboard. This is the real backtest (unlike the live forward-sim, it
    doesn't need days to accumulate — but it needs the logger to have archived
    context, which it does every ~5 min during market hours). UI: "⏮ Backtest
    history" button in the Sim tab.
  - Routes: `/api/sim/{strategies,summary[?strategy=],daily,leaderboard,backtest,
    regime,take,auto,mode,reset}`. Still SEPARATE from the manual paper account.
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
- **OHLCV candlesticks + volume**: detail-modal chart is real candles with a
  volume histogram (1m/5m/15m/1D), hover shows O/H/L/C/%chg/volume/time.
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
