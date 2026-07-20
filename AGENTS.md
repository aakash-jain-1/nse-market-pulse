# Project Context — NSE Market Pulse

> Project guide for AI agents / future sessions: conventions, file tree, roadmap,
> "Done recently". Read it, keep it updated when things change.
>
> **Also read `CONTEXT.md`** (the living memory: current state + dated findings
> log) and the enforced rules in **`.cursor/rules/`** — notably: **never spawn
> subagents**, **testing is the top priority**, and **keep README + all docs in
> sync**. Only commit/push when the user explicitly asks.

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
- **Flask 3.1.3** — web server + JSON API (+ SSE for the live feed)
- **requests 2.34.2** — NSE HTTP calls (with cookie warm-up)
- **tabulate 0.10.0** — CLI table formatting
- **smartapi-python 1.5.5 + pyotp 2.10.0** — Angel One SmartAPI SDK (+TOTP) for the
  optional live WebSocket feed. **FREE** (Angel charges only ₹20/order brokerage, not
  for data). Undeclared SDK deps `logzero`/`websocket-client` are pinned too.
- **dhanhq 2.2.0** — alternative live-feed SDK. Works, but Dhan's *Data API* is a paid
  ₹499+GST/mo subscription, so **Angel One is the default** provider.
- Vanilla HTML/CSS/JS frontend (no build step, no framework). **First external JS
  dependency:** TradingView **Lightweight Charts** (Apache-2.0), loaded from CDN
  for the Live tab (vendorable into `static/vendor/` for offline use).

## File structure

```
NSE/
├── app.py             # Flask server + JSON API endpoints (runs on port 5055)
├── nse_client.py      # NSE session mgmt + data fetching / normalization (CORE)
├── nse_quote.py       # Per-stock quote/chart/depth (NextApi) + OHLCV candles (charting)
├── bhavcopy.py        # EOD UDiFF bhavcopy ingest (static archive) + sec_bhavdata_full delivery% — resilient price/universe fallback + backfill(days)
├── deals.py           # Bulk/block deals (institutional footprint) from nsearchives CSV — parse/cache, by_symbol/recent/status
├── eod_scanner.py     # Full-market EOD/swing scanner over db.eod_bars (breakouts/gaps/vol/MA/NR7/delivery + bulk-deal + sector-RS + futures-rollover xref) — off-hours, pure
├── eod_conviction.py  # EOD conviction board — fuses breakout+delivery+deals+OI+sector RS+option chain+futures rollover, ranks by #signals agreeing; save→ideas / digest→notify
├── eod_options.py     # Resilient EOD option chain from FO bhavcopy (PCR/max-pain/OI walls) — matches live shape; oi_map() = market-wide analytics in one parse (the Conviction option fuse)
├── eod_scheduler.py   # Auto post-close EOD refresh — pure should_run() + block-aware daemon (backfill→deals→optional digest)
├── sectors.py         # Curated NSE symbol→sector map (17 sectors, ~303 names) — static data + sector_of()/all_sectors()
├── sector_scan.py     # Sector relative-strength board over db.eod_bars — cross-sectional RS vs market, ranks sectors + leaders/laggards; strength_map/context = the reusable sector pillar the EOD Scan + Conviction boards fold in
├── conviction_calibration.py # Does confirmation-stacking pay? Scores realized TARGET/STOP of saved conviction ideas — win rate by pillar count / per-pillar lift / option-⚠️ impact + verdict; pillar_weights() feeds that edge back into board scoring (adaptive)
├── rollover.py        # Futures rollover tracker off the FO bhavcopy — near-vs-next month rollover% / roll cost (contango·backwardation) / basis / net-OI state, cross-sectionally ranked; board() + rank_map() (the market-wide {sym:metrics} the Conviction board folds in as a pillar); reuses eod_options' cached FO text (off-hours)
├── angel_feed.py      # Live feed adapter — Angel One SmartAPI WebSocket (FREE) → tick store; + rest_quote/rest_chart/rest_ohlc (detail modal + Live-tab seed from the broker, not NSE)
├── dhan_feed.py       # Live feed adapter — Dhan WebSocket (paid data plan); same interface
├── paper.py           # Paper-trading engine (virtual portfolio, JSON-persisted)
├── strategies.py      # Strategy library (17 generators) + market-regime detector
├── sim.py             # Multi-strategy forward-tester (per-strategy sims + daily rollup)
├── intrabar.py        # Minute-candle trade resolver (target/stop/MFE/MAE) — pure funcs
├── backtest_strategies.py # Offline backtester: replays archived context, resolves on OHLCV
├── backtest_daily.py      # Daily-bar historical backtest, 9 strategies — source="live" (curated NSE) or "eod" (whole bhavcopy universe from SQLite, off-hours)
├── walkforward.py         # Walk-forward out-of-sample / overfit validation (pure over trades)
├── portfolio_backtest.py  # Portfolio-level backtest: replay bd trades through a real book (finite capital, max concurrent, conviction-ranked sizing) → equity curve + CAGR/DD/Sharpe
├── test_*.py          # 753 unit tests across 34 suites (see below)
│   ├── test_intrabar.py / test_sim.py / test_sim_views.py / test_take.py   # sim + intrabar
│   ├── test_backtest.py / test_backtest_daily.py / test_backtest_strategies.py / test_walkforward.py
│   ├── test_portfolio_backtest.py                  # portfolio book: sizing (risk/equal), slot+capital gating, DD/CAGR/Sharpe, equity curve, shorts, run() wiring
│   ├── test_bhavcopy.py                             # EOD UDiFF + sec_bhavdata_full delivery parsers + walk-back + backfill (pacing/abort-on-block) + delivery-merge
│   ├── test_deals.py                                # bulk/block deal parse (incl. NO-RECORDS) + cached fetch/recent/by_symbol/status + keep-cache-during-block
│   ├── test_eod_scanner.py                          # full-market swing scanner: features/tags/score/views/scan/status + delivery view + deals xref
│   ├── test_eod_conviction.py                       # conviction board: OI-state quadrants / pillars / 2R plan / stacked ranking / save-skip
│   ├── test_conviction_calibration.py               # calibration: pillar_of / bucket stats / per-pillar lift / verdict / adaptive pillar_weights (gate/clamp/shrink) / report() on temp DB
│   ├── test_rollover.py                             # futures rollover: days/oi-state/metrics/percentile/median + board() rank/filter/sort/far-expiry-note/empty (stubbed FO text)
│   ├── test_eod_options.py                          # resilient EOD option chain: parse/assemble/chain/summary/analytics
│   ├── test_eod_scheduler.py                        # auto post-close refresh: pure should_run gating / job orchestration / tick recording / routes
│   ├── test_sectors.py                              # sector map integrity: reverse index / canonicalisation / coverage / first-wins
│   ├── test_sector_scan.py                          # sector RS: _ret/_blended/_median/_percentiles/_aggregate + seeded scan (leaders vs laggards)
│   ├── test_client.py / test_client_fetchers.py     # nse_client normalizers + raw parsers
│   ├── test_quote.py / test_quote_more.py / test_book.py   # nse_quote math + parsers + depth
│   ├── test_ideas.py / test_ideas_journal.py / test_strategies.py / test_paper.py
│   ├── test_app.py (middleware) / test_app_routes.py (every endpoint via test client)
│   └── test_db.py / test_logger.py / test_feeds.py / test_notify.py / test_fetch_cache.py
├── db.py              # SQLite store (snapshots / IV / context / sim_trades + EOD & 1-min bar cache)
├── notify.py          # Off-screen alerts (Telegram/webhook) — rides the snapshot logger, opt-in; EOD digest carries a calibration-sourced track-record footer (does stacking pay?)
├── snapshot_logger.py # Background logger (snapshots + IV + strategy-context + alerts) → SQLite
├── db_inspect.py      # Read-only SQLite inspector CLI (overview / tail / SQL)
├── nse_demand.py      # Standalone CLI scanner (original, still works)
├── templates/
│   └── index.html     # Entire dashboard UI (HTML + CSS + JS inline)
├── static/vendor/     # (optional) self-hosted Lightweight Charts for offline use
├── angel_config.example.json # Template → copy to angel_config.json (gitignored)
├── dhan_config.example.json  # Template → copy to dhan_config.json (gitignored)
├── notify_config.example.json # Template → copy to notify_config.json (gitignored) for alerts
├── data/              # (gitignored) market.db (SQLite) + any legacy *.csv
├── requirements.txt
├── README.md
├── AGENTS.md          # <- this file
├── AUDIT.md           # Deep code audit (round 1): findings, severities, fixes
├── AUDIT2.md          # Deep audit round 2: financial-correctness deep-dive + concurrency
├── .gitignore
├── paper_state.json   # (gitignored) local virtual-portfolio state
├── angel_config.json  # (gitignored) Angel One creds (api_key/client_code/mpin/totp_secret)
└── dhan_config.json   # (gitignored) Dhan client_id + access_token (alternative feed)
```

## Data storage (IMPORTANT)

- **Time-series + the sim ledger → SQLite** (`db.py`, `data/market.db`, gitignored
  via `*.db`). Tables: `snapshots` (demand/volgainers board), `iv_log` (ATM IV),
  `context_log` (a trimmed+gzipped snapshot of the full strategy context each cycle,
  ~6 KB/cycle), and `sim_trades` (the durable strategy-sim ledger — every trade
  every strategy ever took, all sessions; indexed on `status`,
  `(strategy, openedDate)`, `(regimeAtEntry, strategy)`). WAL mode for concurrent
  reads; indexed by view/ts/symbol/day. Reads no longer slurp whole CSVs into
  memory. On first run any legacy `snapshots.csv` / `iv_log.csv` is auto-imported
  (`db._import_legacy_csv`), and trades embedded in an older `sim_state.json` are
  auto-migrated into `sim_trades` (`sim._ensure_migrated`).
- **Small state → JSON** (`sim_state.json`, `paper_state.json`). `sim_state.json`
  now holds ONLY settings (auto/entryMode/maxSessions/lastAutoDate) + the bounded
  per-day rollup — the trades live in SQLite. Tiny, document-shaped, rewritten
  atomically. Don't move these small blobs to SQLite, and don't put trades back in
  the JSON.
- Not Postgres/Mongo/Timescale — those need a server and are overkill for a
  single-user local tool. If analytical backtests ever get huge, DuckDB is the
  drop-in upgrade, but we're nowhere near that.

## How to run

```bash
python app.py            # dashboard at http://127.0.0.1:5055
python nse_demand.py     # CLI: all views (also: gainers/losers/volume/value/volgainers)
python db_inspect.py     # peek into data/market.db (no sqlite3 CLI / GUI needed)
python -m pytest -q      # 753 unit tests (client/quote/paper/strategies/sim/backtests/walkforward/portfolio/eod*/sectors/convictioncalibration/rollover/db/app+routes/feeds/…)
```

`db_inspect.py` opens the DB **read-only** (safe while the app is live):
`python db_inspect.py` (overview: tables, row counts, spans),
`python db_inspect.py <table> [N]` (last N rows + schema),
`python db_inspect.py sql "SELECT ..."` (arbitrary read-only query).

The Flask app **auto-reloads** on `.py` changes and re-reads
`templates/index.html` on every request (no restart needed for UI edits). Since
the security audit the **interactive debugger is OFF by default** (it was an
RCE surface on the LAN) — set `FLASK_DEBUG=1` only for local dev if you want the
traceback console. Other env knobs: `HOST=127.0.0.1` (loopback-only),
`FLASK_RELOAD=0` (disable auto-restart), `NSE_TOKEN=<secret>` (require a token on
every request — open the app once with `?token=<secret>` to set the cookie).
Health/liveness is at `GET /api/health`. See `AUDIT.md` for the full posture.

### Phone / LAN access
The server binds `0.0.0.0` by default, so any device on the same Wi-Fi can open
`http://<your-PC-LAN-IP>:5055` (the LAN URL is printed in a banner on startup;
`_lan_ip()` auto-detects it). Override with env vars: `HOST=127.0.0.1` for
local-only, `PORT=xxxx` for a different port. **Gotcha:** Werkzeug's reloader
pins the listening socket at the *parent* launch, so changing `HOST`/`PORT` needs
a **full restart** (Ctrl+C + `python app.py`), not a hot reload. Windows may
prompt to allow Python through the firewall the first time (allow it for private
networks). The UI is mobile-responsive — wide tables scroll horizontally
(`#tableWrap`), the tab bar becomes a horizontal scroller, and chrome/padding
compact under `@media (max-width: 640px)`.

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
- **AI agents: do NOT spawn subagents for this project.** The owner's Cursor is on
  a team plan where **Max Mode is disabled by the admin**, so the Task/subagent
  tool silently falls back to **Composer 2.5 Fast** regardless of the main chat
  model (Opus 4.8) — subagents cannot inherit Opus here. Do deep / quality-
  sensitive work (audits, multi-file refactors, complex debugging) **inline in the
  main chat**, sequentially, one module at a time. (Cursor's subagent-model control
  lives at Settings → Agents → Subagents → "Explore subagent model", and Opus
  needs Max Mode — both blocked by the admin, so this is not fixable client-side.)

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

### Live realtime feed (`angel_feed.py` / `dhan_feed.py`) — the only true stream (OPTIONAL)
Everything above is HTTP **polling**. The **📈 Live** tab adds a genuine
tick-by-tick source over WebSocket, pushed to the browser via **Server-Sent
Events (SSE)** and drawn with **TradingView Lightweight Charts**. Fully optional —
with no creds the app is unchanged.

- **Provider-agnostic:** `app.py` picks a feed at startup into `live_feed`
  (`_select_live_feed()`): **Angel One first, then Dhan**, defaulting to Angel when
  neither is configured (so the setup card shows the free provider). Every
  `/api/live/*` route + the SSE loop call `live_feed.*`; both adapters expose the
  SAME interface — `PROVIDER`, `is_configured`, `sdk_available`, `start`, `stop`,
  `set_watch`, `snapshot`, `public_status` (which now includes `provider`). Adding a
  third broker = one new module, zero route changes.
- **Angel One (default, FREE):** `smartapi-python` + `pyotp`. Config (never
  committed): `ANGEL_API_KEY/CLIENT_CODE/MPIN/TOTP_SECRET` env or `angel_config.json`.
  Login is a TOTP session — `SmartConnect.generateSession(client, mpin, pyotp.TOTP(secret).now())`
  → jwt + feed token; the daily refresh is automatic (we hold the TOTP *secret*, not
  a fixed token). `SmartWebSocketV2` streams **SNAP_QUOTE** (mode 3, exchangeType
  1=NSE cash): LTP + day OHLC/volume + OI + **best-5 depth**. Prices arrive in
  **paise (×100)** — divide by 100. Scrip master `OpenAPIScripMaster.json` filtered
  to NSE `-EQ` → `resolve → token` (RELIANCE → `2885`, same NSE ids as Dhan). The
  SDK spews per-tick INFO to `./logs/<date>/app.log`; `_quiet_logs()` mutes logzero
  to WARNING. (NSE's Apr-2026 static-IP rule is for *order* APIs; **market data has
  no such requirement** and we only stream data.)
- **Dhan (alternative, PAID data plan):** `dhanhq==2.2.0`. Config `DHAN_CLIENT_ID/
  ACCESS_TOKEN` or `dhan_config.json`. `MarketFeed` v2 **Full** packet (mode 21,
  segment 1) = LTP + day OHLC/volume + OI + 5-level depth. Works, but Dhan's Data
  API is **₹499+GST/mo** — unpaid, the socket connects then drops with code **806**
  ("Subscribe to Data APIs"). Uses the SDK's pull API (`run_forever`+`get_data`) so
  reconnects use our backoff, not its tight ~1s loop.
- **Both** (identical lifecycle): a supervisor thread holds the socket ONLY during a
  **market window** (~09:08–15:40 IST) and reconnects with **exponential backoff
  (5→60s)** — this kills the outside-hours reconnect storm that trips connection
  rate limits (we hit Dhan **HTTP 429** before adding it). `start()` is a **no-op**
  without creds/SDK (called in the `WERKZEUG_RUN_MAIN` guard beside `snaplog.start()`).
  `set_watch` drives the live subscription delta from Flask request threads.
- **In-memory store (identical shape both adapters):** `_latest[token]` (symbol, ltp,
  open/high/low, prevClose, volume, oi, atp, depth=`{bids,asks}`) + a **forming
  1-min candle** (`_bars`) whose `t` uses the **same IST-baked-as-UTC epoch ms** as
  `get_ohlc`, so live bars align with seeded ones. Finished minutes are written to
  `db.min_bars` — the Live feed thus **warms the backtester's minute cache** for free.
- **Endpoints (all under `/api/live/`):**
  - `GET config` — `public_status()`: configured/connected/marketOpen + watchlist
    (never returns secrets).
  - `POST watch` — `{symbols:[…], focus}` → subscribe/unsubscribe the delta.
  - `GET seed/<sym>?interval=1|5|15|D` — historical candles to seed the chart
    (reuses `nse_quote.get_ohlc`).
  - `GET stream` — **SSE**, yields `{quotes, status, ts}` ~1×/s (headers:
    `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`; server
    is already `threaded=True`). Breaks cleanly on `GeneratorExit` (client close).
  - `GET snapshot?ids=<csv>` — one-shot poll fallback.
- **Frontend:** the Live tab owns a **persistent** chart + one `EventSource`; it's
  built once on entry and torn down on leave (NOT rebuilt by the poll timer). Baked
  epoch ms → `Math.floor(t/1000)` for Lightweight Charts (UTC render == IST, same
  trick as `istTime()`). The status chip reads **● LIVE** only when connected AND
  market-open; the setup card (`liveSetupCard`) is provider-aware. Watchlist persists
  in `localStorage` (`nseLiveWatch.v1`).
  The lib loads from CDN; `static/vendor/lightweight-charts.standalone.production.js`
  is an offline fallback (browser `onerror`).
- **NSE polled fallback (no/again-offline broker):** the Live tab is no longer
  broker-only. When the feed is **unconfigured** (`liveShellHtml` shows an NSE banner)
  OR configured **but not connected** (after hours / token issue / SSE hiccup — see
  `liveApply`'s `!st.connected` branch and the `es.onerror` net), a ~12s poll
  (`liveStartNsePoll`/`liveNsePollOnce`) fetches `/api/quote/<sym>` for the watchlist
  and reuses the SAME renderers (`liveRenderWatch/Header/Depth`, chart-fold) via
  `nseQuoteToRec`. So the **5-level depth ladder + quotes work straight from NSE**
  (`nse_quote.get_quote().depth`, the NextApi `getSymbolData` order book) with no
  broker at all. A connected broker always **supersedes** it (real-time > polled; the
  poll is stopped in `liveApply`). Chip: **● NSE · ~12s** / **● NSE (broker offline) ·
  …** / **· market closed** (depth is empty outside 09:15–15:30 IST either way). The
  per-stock **detail modal already renders NSE depth** the same way (`loadDepth`).

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
- **EOD Scan** (`🌐 EOD Scan`, `eod_scanner.py`) — the *market-wide, off-hours*
  counterpart to the live Scanner. Instead of the ~100–150 hot lists it scans the
  whole ingested EOD universe (up to ~2400 cash names + the F&O set) from
  `db.eod_bars` for swing setups: breakouts/breakdowns of the recent N-day high/low,
  gaps, unusual volume vs the trailing 20-day avg, trend vs the 20/50-day MAs,
  NR7 squeezes, and **high delivery% accumulation** — each row carrying `tags`,
  a **Deliv%** column (with a "+Npp vs avg" spike hint) + a bullish-setup `score`.
  View selector (setups/breakout/breakdown/gainers/losers/unusual/squeeze/value/
  **delivery**) + price/value/limit/F&O filters + a **🐋 deals** toggle that
  cross-references the latest bulk/block deals →
  `/api/eod/scan?view=&limit=&minPrice=&minValueCr=&fno=1&deals=1`.
  Needs history: **⬇ Backfill** loads recent bhavcopies via `bhavcopy.backfill(days)`
  (`POST /api/eod/backfill`, GET polls; also merges delivery%). Prices are the last
  EOD **close** (not live). Works nights/weekends since it touches no live API.
- **Bulk/Block deals (`deals.py`)** — the latest session's institutional footprint
  (funds/HNIs/promoters, legally disclosed) from NSE's nsearchives CSVs, parsed +
  cached 30 min. `/api/eod/deals?kind=bulk|block&limit=` (+ `?status=1`). Used to
  flag scanner rows a big player just traded (🐋 badge + score bonus).
- **🏆 Conviction board (`eod_conviction.py`)** — the synthesis: fuses the independent
  EOD signals (breakout of the N-day high, delivery% accumulation, bulk/block-deal
  footprint, F&O OI buildup, volume, trend, leading/lagging sector, option chain and
  **futures rollover** — positions carried into next month) into ONE ranked "tomorrow's
  watchlist" via **confirmation stacking** — ranked by how many independent signals AGREE,
  then the blended score, so a 4-way-confirmed name beats a lone strong signal. Each pick
  gets a volatility-scaled 2R plan. **Save** persists the board to the Ideas history (dated
  to the EOD session, never clobbers a live idea); **Send digest** pushes the top picks
  off-screen via `notify.send_digest()`. 🏆 Conviction tab; `/api/eod/conviction[/save|
  /digest]` (`?rollover=0`/`?options=0`/`?deals=0` disable a fuse; `?adaptive=1` weights
  pillars by realized edge). The option + rollover fuses share ONE cached FO-bhavcopy
  parse. Off-hours.
- **📊 Conviction calibration + ⚖️ adaptive weighting (`conviction_calibration.py`)** —
  the honest scorecard for the board above, then the feedback loop: reads back the saved
  conviction ideas, scores their candle-accurate `TARGET`/`STOP` outcomes, and reports
  whether stacking actually pays — win rate by **pillar count** (4-signal vs 2-signal), by
  rating/direction, the **per-pillar lift** (win rate WITH vs WITHOUT each pillar) and the
  **option-⚠️ warning impact**, plus a one-line verdict. `pillar_weights()` then turns each
  measured lift into a clamped, sample-shrunk scoring multiplier the board applies with
  `board(adaptive=True)` — re-ordering WITHIN a confirmation tier without touching the
  stacking count. 📊 Calibration modal + ⚖️ Adaptive toggle on the Conviction tab;
  `/api/eod/conviction/calibration?days=N`, `/api/eod/conviction?adaptive=1`.
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
  - **EOD fallback** (`eod_options.py`) — when the live NextApi chain is empty/
    blocked (off-hours, 403), the loader auto-switches to the FO-bhavcopy chain
    (`/api/eod/optionchain/<sym>`) — same PCR/max-pain/OI-walls, shown with a 🌐 EOD
    badge (no IV/bid-ask/Greeks in the bhavcopy). Works nights/weekends.
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
 symbol->LTP map. **Options too (long AND short/writing):** `place_option_order()`
 fills CE/PE at the live premium (from the option chain) via a trade box in the ⛓
 Options modal; positions use SIGNED qty (long +, short −). `BUY` = buy-to-open long /
 buy-to-cover short; `SELL` = sell-to-close long / **sell-to-open a written short**
 (you do NOT need to hold a long). Long pays the premium (no margin, max loss =
 premium); **short (written) RECEIVES the premium but POSTS margin**
 (`OPT_SHORT_MARGIN_RATE=0.15` × underlying-spot notional) since the risk is
 futures-like — covering frees margin proportionally + realizes P&L. `portfolio()`
 marks written options as margin-based (`ltp*qty signed + margin`, so the received
 premium isn't double-counted).
 **Options are sized in LOTS** — `place_option_order(...lots)` multiplies by the
 underlying's market lot (`nse_client.get_lot_size()`, from NSE's `fo_mktlots.csv`,
 215 names, cached a day); the trade box shows lot size + total units + est. cost,
 and the portfolio/orders show lots. **Futures too:** `place_futures_order(sym,
 side, lots)` is margin-based (~15% of notional, `FUT_MARGIN_RATE`), supports
 LONG **and** SHORT with proper netting/flip-through-zero, realizes P&L + releases
 margin on close, and marks to market on the live near-month price. Traded from a
 "Paper trade FUTURES" box in the detail modal (shown only for F&O names).
 **Pricing:** hot-list LTP → per-stock NextApi quote → **EOD bhavcopy close**
  (`bhavcopy.py`), so ANY listed symbol is tradable (live during hours, last close
  otherwise). State persists to `paper_state.json` (gitignored). This is
  broker-agnostic by design: swapping in a real broker feed later only changes the
  price/fill source.
- **Futures tab** (`get_futures()`) — most-active stock futures with **basis**
  (futures price - spot = premium/discount), basis %, annualized carry (by days
  to expiry), OI, and long/short buildup. OI change is cross-referenced from the
  OI-spurts endpoint (partial coverage -> "OI n/a" when unknown). Note stock_fut
  returns only ~20 (most-active) contracts, not the full F&O universe.
- **Snapshot logging + backtest** (`snapshot_logger.py`) — a daemon thread
  captures the demand board + volume-gainers (25 each) into SQLite every 60s
  **during market hours only** (Mon-Fri 09:15-15:30 IST). The 📊 Log button shows
  logger status, a manual "Capture now", CSV download, and a simple
  **forward-return backtest** (price move from a symbol's first sighting to its
  latest, with avg return + hit rate). Started in `app.py` guarded by
  `WERKZEUG_RUN_MAIN` so the Flask reloader doesn't run two loggers.
  - **Reliability / self-healing**: the loop runs unattended every market day.
    Each cycle isolates its sub-tasks (`_run_cycle`: snapshot / IV / sim /
    context) so one NSE failure can't skip the rest; a heartbeat (`_last_tick`),
    `_cycles` and `_consecutive_errors` feed `health()`; after `REBUILD_AFTER`
    failed cycles it force-rebuilds the NSE session; and a **watchdog thread**
    revives the worker if it dies (`_restarts`). `health()` (`/api/log/health`,
    also nested in `/api/log/status`) reports `healthy/stalled/threadAlive/
    watchdogAlive/secondsSinceTick/cycles/restarts` — the Log modal shows a
    green "healthy" / amber "stalled" / red "down" dot + last tick + restarts.

## Known limitations

- Real intraday charts + depth come from the NextApi gateway (per-symbol, needs
  the stock-specific Referer); depth is empty outside market hours.
- OI price-direction coverage is partial pre-market; improves during 09:15–15:30 IST.
- All endpoints are unofficial and can change without notice.
- Data only meaningful during NSE market hours (Mon–Fri, 09:15–15:30 IST).
- The Live tab is optional and needs the user's own broker credentials. **Angel One
  SmartAPI is the free default** (auto TOTP login, no manual token step); **Dhan** is
  an alternative but its Data API is a paid ₹499+GST/mo subscription. Either way it
  currently streams **NSE cash equities only**.

## Roadmap / ideas (not yet built)

- **Real-time broker feed** — ✅ **done for charts/quotes/depth** via a
  provider-agnostic adapter: **Angel One SmartAPI (free, default)** or **Dhan (paid
  data plan)** — `angel_feed.py` / `dhan_feed.py`, 📈 Live tab; see the live-feed
  architecture note. Still open: route paper-trading fills/`get_price` through the
  broker feed too, and extend the Live tab to index/F&O instruments (currently NSE
  cash equities only).
- Phone/LAN access + optional deploy.
- ✅ *(done — see below)* `jugaad-data`/`nsefeed`-style fallback for the flaky bits:
  implemented natively as `bhavcopy.py` (EOD UDiFF ingest), no third-party dep.
- ✅ *(done — see below)* market-wide EOD scanner over the full bhavcopy universe
  (`eod_scanner.py` + 🌐 EOD Scan tab).
- ✅ *(done — see below)* resilient EOD option chain (max-pain/PCR/OI walls) from FO
  bhavcopy options (`eod_options.py`).
- ✅ *(done — see below)* full-universe EOD backtest (`backtest_daily.py source="eod"`)
  — the 9 EOD strategies over the whole ingested bhavcopy universe from SQLite, so the
  leaderboards / `strategy_of_day` / walk-forward are statistically trustworthy.
- ✅ *(done — see below)* delivery% + bulk/block deals market-wide — `sec_bhavdata_full`
  merged into `eod_bars` (re-activates the delivery strategy) + `deals.py` institutional
  footprint feed + an Accumulation scanner view.
- ✅ *(done — see below)* EOD conviction board (`eod_conviction.py`) — fuses breakout +
  delivery + deals + OI buildup into one confirmation-stacked "tomorrow's watchlist";
  save→Ideas history + off-screen digest. ✅ scheduled/auto EOD backfill + auto-digest
  after close (`eod_scheduler.py`, see Done recently).
- ✅ *(done — see below)* futures rollover tracker (`rollover.py` + 🔄 Rollover tab) —
  near→next month rollover% / roll cost / basis / OI-state, cross-sectionally ranked.
- ✅ *(done — see below)* portfolio-level backtest (`portfolio_backtest.py`) — replays the
  daily-backtest trades through a real book (finite capital, concurrent-position cap,
  risk/equal sizing, **conviction-ranked** same-day picks, **daily mark-to-market**) →
  equity curve + CAGR / max-DD / Sharpe. *Feature complete.*
- ✅ *(done — see below)* conviction calibration + adaptive weighting (`conviction_calibration.py`)
  — scores the saved conviction ideas' realized TARGET/STOP outcomes to test whether
  confirmation-stacking pays (win rate by pillar count, per-pillar lift, option-⚠️ impact +
  verdict), then feeds each pillar's measured edge back into board scoring (`board(adaptive=True)`).

## Done recently

- **Adaptive auto-refresh (throttle/pause the last foreground NSE hit)** — the 30s movers
  refresh is the only foreground NSE call left (no broker offers market-wide movers/OI), so
  instead of a blind `setInterval` it's now a self-scheduling `setTimeout` loop that **pauses
  when the tab is backgrounded** (Page Visibility; resumes + refreshes on return), **pauses
  during a WAF block** (wakes as the cooldown clears), and **slows to ≥5 min when the market
  is closed** (`logger.marketHours` from `/api/health`). Frontend-only (`index.html`); no new
  test function, but `test_health_reports_nse_block` now locks that `/api/health` exposes
  `logger.marketHours`. Suite stays **753**; JS `node --check` clean.
- **Live-tab chart seed + `/api/ohlc` served from the broker too** — finishes the broker-first
  migration. The Live tab still seeded its candles from NSE (`/api/live/seed`) and its 12s poll
  fallback used `/api/ohlc`, so opening the tab hit NSE even with Angel connected. Added
  `angel_feed.rest_ohlc()` (SmartConnect `getCandleData` → `nse_quote.get_ohlc` shape,
  interval keyworded 1→ONE_MINUTE … D→ONE_DAY); `/api/live/seed` + `/api/ohlc` are now
  **broker-first when connected → NSE**, but an explicit `from/to` window (backtester) stays
  on NSE. Fixed the candle timestamp to be **IST-baked-as-UTC** (`_baked_iso_to_ms`, also
  applied to `rest_chart`) so seeded history lines up with the live forming bar. `dhan_feed`
  gets a `rest_ohlc` no-op stub. Tests **+3**; suite **750 → 753**.
- **Stock-detail modal served from the broker (Angel), not NSE** — answers the "aren't we
  using Angel/Dhan?" question. The app is a deliberate hybrid: NSE for market-wide discovery
  (movers/OI/scanner/option-chain/EOD — no broker offers those), broker for live ticks on
  symbols you drill into. The detail modal was still hitting NSE per row-click, so added
  `angel_feed.rest_quote()` / `rest_chart()` (SmartConnect `getMarketData` FULL → LTP+OHLC+
  depth, `ltpData` fallback; `getCandleData` → points) mapped to the `nse_quote` shapes, and
  made `/api/quote` + `/api/chart` **broker-first when connected → NSE → EOD** (every hop
  guarded, so a miss falls back — safe before Angel is even live-verified). `dhan_feed` gets
  safe no-op `rest_*` stubs. Tests **+10**; suite **740 → 750**.
- **🔄 Rollover in the EOD Scan tab** — the rollover signal was only actionable on the
  Conviction board; now the market-wide scanner carries it too. `eod_scanner._rollover_map()`
  (reuses `rollover.rank_map()`'s cached FO text) + `_attach_rollover()` tag F&O rows that
  are CARRYING positions into next month with a **🔄 carrying N%** badge; `_score()` adds +6
  on the bull side. `scan(with_rollover=…)`, `/api/eod/scan?rollover=0`, UI checkbox default
  **on** (cash-only names untouched). Tests **+5**; suite **735 → 740**.
- **📊 Digest trust footer** — the off-screen EOD digest (`notify.send_digest`) listed picks
  but gave no reason to trust them; this appends the realized track record from the
  conviction calibration. `notify._fmt_trackrecord()` (pure) → `📊 Track record (30d, N
  resolved): 2✓ 44% · 3✓ 58% · 4✓ 71% · overall 57%`. **Gated**: hidden until ≥8 resolved
  ideas, a tier shown only with ≥3 resolved. `send_digest()` computes it best-effort
  (`report(days=30)`), appends before the disclaimer, and never lets a calibration hiccup
  block the digest. Tests **+5**; suite **730 → 735**.
- **🔄 Rollover → Conviction pillar** — made the rollover signal ACTIONABLE inside the
  board (it was a standalone tab). `rollover.rank_map()` returns the market-wide
  `{SYMBOL: metrics + cross-sectional rolloverRank/carrying/shedding}`, ranked over the
  WHOLE futures universe (no price/value filter, so any pick can look up its standing),
  cached 15-min on the same FO text the option fuse uses (one parse for both).
  `eod_conviction._roll_pillar()` fires ONLY when a name is CARRYING (rollover% in the top
  fifth today) AND its net near+next OI direction matches the trade side — a breakout on a
  name whose positions are being carried into next month outranks one on shrinking OI.
  Threaded through `_pick` → `board(with_rollover=True)`; echoes `withRollover`;
  `?rollover=0` disables it. Adaptive weighting recognizes it (new `rollover` key in
  `conviction_calibration._PILLARS`), the board legend gains 🔄 and the Calibration modal a
  "🔄 Rollover carry" row. Confirmation COUNT discipline preserved — it's just one more
  independent pillar. Tests **+7** (rank_map; `_roll_pillar` gating; pillar/`_pick`/board
  fuse; `with_rollover=False` skips the fetch; calibration label→key); suite **723 → 730**;
  verified e2e through the route.
- **🔄 Futures rollover tracker (`rollover.py`)** — a genuinely new F&O signal. Near expiry
  traders roll positions from the near to the next month; HOW MUCH rolls (conviction to carry)
  and at WHAT spread (contango/backwardation) is a real read the FO bhavcopy already carries.
  `bhavcopy.parse_fo_futures_all()` keeps ALL STF/IDF expiries per symbol (`parse_fo` keeps
  only the nearest). `rollover.board()` computes per name **rollover%** (nextOI/(near+next) —
  rising into expiry = positions CARRIED), **roll cost** (next−near spread) + annualized,
  near-month **basis** to spot, and the net-(near+next)-OI **state** (long/short buildup vs
  covering/unwinding), then ranks the F&O universe with a CROSS-SECTIONAL `rolloverRank`
  (percentile vs the market median today — meaningful with no rollover history) and
  `sort` ∈ rollover/rollcost/basis/dte. Reuses `eod_options._fo_text()` so the FO file is
  fetched/parsed ONCE for both the option and rollover views; works off-hours / when live is
  blocked. `/api/eod/rollover` + a **🔄 Rollover** tab (rollover% + vs-median bar, roll cost,
  annualized, basis, OI-state chip, 🟢 carrying / 🔴 shedding). Tests **+14** (`test_rollover.py`
  12 + parse test + route arg); suite **709 → 723**; lint clean.
- **⚖️ Adaptive pillar weighting (calibration → scoring)** — the calibration report *measured*
  each pillar's edge but was read-only; this feeds it back so the board learns from its own
  realized results. `conviction_calibration.pillar_weights()` maps each pillar's win-rate lift
  → a scoring multiplier **clamped `[0.5,1.5]`, shrunk toward 1.0 by sample size, neutral until
  ≥5 resolved on both sides** (`_mult_from_lift`, pure). `pillar_of()` is now the ONE shared
  label→key classifier (the parser refactored onto it so it can't drift from the weighter).
  `eod_conviction.board(adaptive=True)` scales pillar weights via `_apply_weights` (option
  bonus too) but **leaves the confirmation COUNT alone** — weighting only re-orders within a
  tier, never overriding how many signals agree. `?adaptive=1` on the board + save routes +
  a **⚖️ Adaptive** toggle (OFF by default); the board echoes applied non-neutral weights and
  the 📊 Calibration modal gains a "→ weight" column. Tests **+11** (suite **698 → 709**); lint clean.
- **📊 Conviction calibration / hit-rate report** — closes the confirmation-stacking loop with
  DATA: does a 4-pillar pick actually beat a 2-pillar one, and does each pillar add or subtract
  edge? `conviction_calibration.py` reads back the saved conviction ideas (`db.ideas_all`, new —
  newest-day-first + optional `since` floor), tag-filters to board picks, and scores each by its
  candle-accurate `TARGET`/`STOP` outcome. Pure helpers (`_confirmations_of` parses "(N signals)",
  `_pillars_in` maps reason labels → the 8 pillar keys, `_bucket_stats` = win rate over RESOLVED +
  MFE/MAE over ALL, `_lift` = WITH vs WITHOUT a pillar, `_verdict` = one honest sentence). Report
  buckets by pillar count / rating / direction / per-pillar / option-⚠️. `/api/eod/conviction/
  calibration?days=N`; a **📊 Calibration** button on the 🏆 Conviction tab opens a modal (verdict +
  totals + "win rate by pillar count" + per-pillar lift + warning-impact tables). Tests **+20**
  (`test_conviction_calibration.py` 19 + 1 route arg test; suite **678 → 698**); lint clean.
- **🎯 Option chain fused into the Conviction board** — max-pain / PCR / OI walls (already
  computed off the FO bhavcopy) now confirm or **soft-veto** each directional pick. New
  `bhavcopy.parse_fo_options_all()` (one pass → every underlying's per-expiry chain) +
  `eod_options.oi_map()` (cached `{SYMBOL: nearest-expiry maxPain/PCR/ATM/OI-walls}`) so the
  board parses the big FO file **once**, not per name. `eod_conviction._option_overlay()`:
  a long UNDER max-pain / short OVER it, with room to the nearest OI wall, and a supportive
  PCR → one extra **🎯 pillar** (+12); a target that runs **into** a call/put OI wall or a
  price **pinned against** max-pain → a **⚠️ warning** that shaves conviction (`_OPT_WARN`) but
  is shown, not silently dropped. `board(with_options=True)` builds the map once; picks carry
  `options` + `warnings`; saved ideas keep the ⚠️ lines. UI: 🎯 max-pain/PCR chip + red ⚠️
  block on each card. Tests **+11** (suite **667 → 678**); lint clean.
- **🧭 Sector RS wired into the Conviction board + EOD scanner** — the new sector strength is
  now an **extra confirmation pillar** everywhere, so a breakout **in a leading sector ranks
  higher** than the same breakout in a laggard. `sector_scan.py` grew a reusable
  `strength_map(grouped, …)` (sector→{rank, rs, strength, total}) + `context(smap, symbol)`
  (flags leading ≥67th / lagging ≤33rd percentile), both built from the **already-loaded**
  bars (no extra DB pass; lazy import breaks the sector_scan↔eod_scanner cycle).
  `eod_conviction.board()` adds a 🧭 leading-sector pillar to longs / lagging-sector pillar to
  shorts (weight 14) and carries `pick["sector"]`; `eod_scanner.scan()` tags rows `🧭 <sector>
  #<rank>` and nudges `_score` (+8 leading / −6 lagging). UI: coloured sector chip on each
  conviction card + the badge on scanner rows. Tests **+12** (suite **655 → 667**); lint clean.
- **🧭 Sector relative-strength (rotation) board** — individual breakouts are stronger when
  the whole *sector* is being bought, but we had no sector awareness. New `sectors.py` (a
  curated, dependency-free symbol→sector map: **17 sectors, ~303 names**) + `sector_scan.py`,
  which mines `db.eod_bars` for **cross-sectional** RS: each name's blended (20/60-day) return
  minus the **market median** (the bhavcopy has no index history, so the market IS the
  universe), aggregated to a per-sector median RS. Sectors are ranked, the top names inside the
  strongest sectors become the **leader board** (downtrends excluded), the weakest sector's
  names are the **laggards**. Pure maths (`_ret`/`_blended`/`_median`/`_percentiles`/
  `_aggregate`); `scan()` is one `eod_bars_all` query reusing `eod_scanner._features`.
  `GET /api/eod/sectors` + a **🧭 Sectors** tab (ranked table with a centre-zero RS bar +
  Leaders/Laggards). First live run: Realty strongest (RS +16.5). Tests **+24** (suite
  **631 → 655**); lint clean.
- **🗓️ Auto EOD backfill after close** — the EOD scanner / conviction board / backtests
  read the ingested bhavcopy universe, which only refreshed on a manual "Load EOD". New
  `eod_scheduler.py` runs **one paced, block-aware refresh** (bhavcopy → deals → optional
  digest) shortly after the 15:30 close on trading days. The decision `should_run(now,
  last_run, blocked)` is a **pure function** (weekday + ≥16:00 IST + not done today + not
  in a WAF cooldown), so it's unit-testable without sleeping/NSE; the last-run date persists
  in `db.eod_meta` so the dev reloader's restarts don't re-trigger it, and a block mid-run
  leaves the day un-recorded to retry after cooldown. `GET /api/eod/scheduler` +
  `POST /api/eod/scheduler/run`, an `autoEod` block in `/api/health`, opt-out via
  `NSE_EOD_AUTO=0`. Tests **+13** (suite **618 → 631**); lint clean.
- **🛡️ Block-resilience UX** — closes the loop on the Akamai incident. The backoff
  already stopped us *re-earning* a block, but the UI still silently showed stale numbers
  and the stock modal 403'd mid-cooldown. Now `/api/health` reports `nse.blockedForSec`;
  the dashboard shows a **live countdown banner** ("NSE has temporarily rate-limited this
  network… showing cached/EOD… auto-resuming in m:ss", auto-hides when clear); and
  **`/api/quote/<sym>` falls back to the EOD bhavcopy close** while blocked (`stale:true`,
  `source:"eod-bhavcopy"`) — never touching NSE. Scanner lists already serve stale
  `_fetch` cache, so the app stays useful. Tests **+2** (suite **616 → 618**); lint clean.
- **📉 Portfolio mark-to-market** — open positions were held at **cost**, so the equity
  curve only stepped on exits and hid intra-trade heat. `bd.run(_collect=True)` now also
  returns traded symbols' daily `closes`; `simulate(closes=…)` marks each open position to
  market daily (reserve + unrealized P&L) over the full trading calendar. Honest result:
  max-DD **4.6% → 5.5%**, Sharpe 0.76 → 0.60, daily curve; realized end-capital unchanged.
  Portfolio engine **feature-complete**. Tests **+1** (suite **615 → 616**); lint clean.
- **🎯 Conviction-ranked portfolio picks** — the portfolio book took an *arbitrary* 74 of
  5,712 signals with 5 slots (neutral order) and lost. Now every `backtest_daily` trade
  carries an entry-time **conviction `score` (0-100)** from its own trigger magnitude
  (move×volume, extreme size, delivery%, breakout distance, OI%, gap size, RS…), **no
  look-ahead** (`_conv` scaler; `_signals` returns `(id,dir,score)`; `_trade` stores it).
  `portfolio_backtest.run()` ranks same-day contention by it. Same 5 slots, same signals,
  best-picked: **−2.5% → +2.2%**, CAGR −9.9% → **+9.1%**, Sharpe −0.98 → **+0.76**, PF
  0.87 → **1.08** (`oi_smart` +18.7% standout). Tests **+3** (suite **612 → 615**); lint clean.
- **📈 Portfolio-level backtest (`portfolio_backtest.py`)** — the daily backtest reported
  per-trade **R**; that answers "does this signal have an edge?" but not "could I have
  traded it?". This replays the exact `backtest_daily` trades through a REAL book:
  finite capital, a cap on **concurrent positions**, and position **sizing** (fixed-%
  risk off current equity, or equal-weight) — so surplus same-day signals get **skipped**
  when slots/capital run out. Output is an **equity curve** + CAGR / max-drawdown /
  Sharpe / profit-factor / exposure, overall and **per strategy** (which one compounds
  capital, not just which has the best R). `simulate()` is pure (fully unit-tested);
  `run()` pulls trades from `bd.run(_collect=True)` (live or full EOD universe).
  `/api/sim/portfolio` + a **📈 Portfolio backtest** button with an SVG equity curve.
  Immediately useful finding on the EOD universe: **5,712 signals but only 74 taken**
  with 5 slots — exactly the reality the per-trade view hides. Tests **+17** (suite
  **595 → 612**); lint clean.
- **🛡️ Akamai/WAF block backoff + gentle backfill pacing** — the user hit
  **"Access Denied … edgesuite.net"** in Chrome: NSE's edge had temporarily blocked
  their IP, caused by our own bursty automated traffic (repeated full-history
  backfills + live polling), and made *self-perpetuating* by a failure path that
  rebuilt the session (2 extra homepage GETs) on every 403. Added a **shared 10-min
  cooldown** in `nse_client` (`blocked_for`/`note_block`/`is_blocked_response`): the
  first 403 pauses ALL NSE traffic — `_fetch` serves stale cache / fails fast (no
  rebuild), `get_session` reuses the stale session, `bhavcopy._download` returns None
  without retrying, and **every per-stock call in `nse_quote` (via a new block-aware
  `_sget()`)** does the same (the user's log showed `/api/quote/…` still 403-ing — that
  gap is now closed). `backfill(pace=0.5)` now spaces days with a jittered pause and
  **aborts early** if the WAF fires mid-run; `deals` keeps prior data during a block
  instead of caching an empty list. Can't un-block an IP (time/new network does) but
  stops us re-earning it. Tests **+17** (suite **578 → 595**); lint clean.
- **🏆 EOD conviction board (`eod_conviction.py`)** — the synthesis of all the EOD work:
  fuses the independent market-wide signals (breakout of the N-day high, delivery%
  accumulation, bulk/block-deal footprint, F&O OI buildup, volume, trend) into ONE
  ranked "tomorrow's watchlist" via **confirmation stacking** (ranked by how many
  independent signals agree, then blended conviction). Pure pillar logic + OI-quadrant
  classification (`_oi_state`) + a volatility-scaled 2R plan. **Save** persists the
  board to the Ideas history (dated to the EOD session, never clobbers a live idea);
  **Send digest** pushes the top picks off-screen via `notify.send_digest()`. New 🏆
  Conviction tab (min-signals selector, filters, card layout with confirmation badges +
  stacked reasons + plan). `/api/eod/conviction[/save|/digest]`. Verified e2e on ~3,300
  real names (HIRECT = breakout + 26.9× vol + 🐋 bulk deal; save persisted 24 picks;
  digest formatted cleanly). Tests **+23** (suite **555 → 578**); lint + JS clean.
- **🚚 Delivery% + bulk/block deals market-wide (`bhavcopy` delivery merge + `deals.py`)**
  — the full-universe EOD backtest had found the **Delivery% strategy going quiet (0
  trades)**: the UDiFF CM bhavcopy we ingest **omits the delivery column**. NSE ships
  delivery in a separate **`sec_bhavdata_full`** plain CSV, so `ingest_db()` now pulls
  it (`parse_sec_delivery`/`fetch_sec_delivery` — handles the file's leading-space
  headers, `-` sentinels, EQ-wins dedup) and merges per-symbol `delivPct`/`delivQty`
  **for the same session only** (guards against a walked-back day). **Real e2e:** a
  23-session backfill merged delivery on **72,549/72,549 bars (100%)** and the delivery
  strategy now **fires 44 trades, regime-gated +0.23R** (was 0). New `deals.py` fetches
  NSE **bulk/block deals** (`parse_deals` handles the "NO RECORDS" sentinel; 30-min
  cache; 102 real bulk deals pulled live). The EOD scanner gains an **Accumulation
  (high delivery%)** view, `avgDelivPct`/`delivVsAvg` features, a **Deliv%** UI column,
  🚚/🐋 tags, and a **🐋 deals** toggle (`?deals=1`) that cross-references deals (+score
  bonus). API: `/api/eod/deals`, `/api/eod/scan?...&deals=1`, backfill reports `deliv`.
  Tests **+25** (suite **530 → 555**), all green; lint + JS clean.
- **📊 Full-universe EOD backtest (`backtest_daily.py source="eod"`)** — the daily
  backtest (and everything it feeds: regime/vol leaderboards, `strategy_of_day`,
  walk-forward) ran over a curated ~40–260-name universe pulled one symbol at a time
  from NSE — slow, network-bound, and a *flattering* sample. Now `source="eod"` reads
  the WHOLE ingested bhavcopy universe straight from SQLite (`db.eod_bars`/`db.eod_oi`,
  via `bhavcopy.backfill`): **no network, off-hours, thousands of trades** (verified:
  1561 liquid names → 5144 trades in 0.3s vs 156 on the curated 40 — and the
  whole-market expectancy honestly sits near breakeven where the curated run looked
  rosy). Data layer split into `_load_live` / `_load_eod` sharing the whole analysis
  pipeline; new `db.eod_oi_all()` builds a continuous near-month OI% series across
  rollovers; liquidity floors (`minPrice`/`minValueCr`) keep the sample tradable;
  `resolve` forced to daily (minute re-resolution needs per-symbol NSE fetches).
  `?source=eod` on `/api/sim/backtest_daily|strategy_of_day|walkforward`; Sim-tab
  **Backtest source** selector (Live NSE ↔ Full-market EOD) with a source badge +
  coverage/thin-history hint. Trade-off at the time: Delivery% went quiet (bhavcopy
  omits it) — **since fixed** by the delivery-merge entry above. Tests **+7**
  (suite **523 → 530**).
- **🔄 Futures rollover (`rollover.py`)** — near-vs-next month futures from the EOD FO
  bhavcopy. Near expiry, traders roll positions from the near to the next month; how much
  rolls (conviction to carry a view) + at what spread (contango/backwardation) is a real
  read the bhavcopy already carries (near/next/far OI + close + settle + spot per contract).
  `bhavcopy.parse_fo_futures_all()` (pure) keeps ALL expiries per symbol; `rollover.board()`
  computes **rollover%** = nextOI/(near+next), **roll cost** = (next−near)/near + annualized,
  near **basis** to spot, and a net-(near+next)-OI **state** (buildup/covering/unwinding),
  then adds a CROSS-SECTIONAL `rolloverRank` (percentile vs the market median today, so it's
  meaningful *without* a rollover history) and 🟢 carrying / 🔴 shedding flags. `sort` ∈
  rollover/rollcost/basis/dte; a note flags when the near expiry is >12 days out (rollover is
  sharpest in the expiry week). Reuses `eod_options._fo_text()` (one cached FO fetch for both
  the option + rollover views); off-hours / block-resilient. `/api/eod/rollover` + 🔄 Rollover tab.
- **⛓ EOD option chain (`eod_options.py`)** — the live chain rides NSE's anti-bot
  NextApi (403s intermittently, empty/stale off-hours). The FO bhavcopy carries every
  contract's EOD OI/close/volume in a plain static ZIP, so this rebuilds the chain +
  analytics resiliently. `bhavcopy.parse_fo_options()` (pure) extracts the option rows
  (STO/IDO) `parse_fo` drops; `eod_options.chain()/summary()` assemble them into the
  **same shape** as `nse_quote.get_option_chain` (rows/pcr/maxPain/atm/support/
  resistance) + `{eod,date}` — **max-pain delegated to `nse_quote._max_pain`** (one
  impl). No IV/bid-ask in the bhavcopy → those legs are None. The ⛓ Option-Chain UI
  now **auto-falls-back** to EOD when the live chain is empty/blocked, with a 🌐 EOD
  badge (expiry dropdown + all-expiry summary stay in EOD mode; IV-rank skipped).
  Verified live (RELIANCE: 3 expiries, PCR 0.59, max-pain 1320). `/api/eod/optionchain/
  <sym>[?expiry]` + `/summary`. Tests **+16** (suite **507 → 523**).
- **🌐 Full-market EOD / swing scanner (`eod_scanner.py`)** — the live scanner only
  sees NSE's ~100–150 intraday hot lists and reads all-zeros off-hours, but we
  already persist whole-market daily bars in `db.eod_bars` (from bhavcopy). This new
  module mines that history for end-of-day setups so the app has a **market-wide**
  board that also works nights/weekends. Ranks names by proximity to / break of the
  recent **N-day high/low** (breakout/breakdown), **gap**, **unusual volume** vs the
  trailing 20-day avg, **trend** vs the 20/50-day MAs, and an **NR7 squeeze** (a
  *genuine* contraction — strictly narrower than each prior session; a flat series
  is not a squeeze). All feature math (`_features`/`_tags`/`_score` + per-view
  predicate & sort key) is **pure** → fully unit-tested; `scan()` does one grouped
  `db.eod_bars_all(since=…)` read, filters (min price / turnover / F&O-only) and
  ranks. Signals **degrade gracefully** with history depth. New `bhavcopy.backfill(days)`
  bulk-loads recent sessions for market-wide *history* (lock-guarded, idempotent,
  dedups holiday walk-backs); DB gains `eod_bars_all`/`eod_latest_date`/
  `eod_oi_symbols`. `/api/eod/scan` + background `/api/eod/backfill` (POST starts,
  GET polls); new **🌐 EOD Scan** tab (setup selector + price/value/limit/F&O
  filters + ⬇ Backfill w/ live progress). Prices are the last EOD **close**
  (labelled — not live). Tests **+32** (suite **475 → 507**).
- **🎯 Vol-conditioned strategy selection** — closed the loop on the volatility
  axis: `strategy_of_day` and the live adaptive playbook (`_regime_playbook_pick`)
  now pick using a **blend of the regime-bucket and vol-bucket marginal
  expectancies** (`blendedR = 0.6·regimeR + 0.4·volR`, `backtest_daily._blend_r` /
  `_vol_cells`; `cached_regime_leaderboard` now exposes `volLeaderboard`/`volDist`).
  We blend the two **marginal** leaderboards rather than keying on a joint
  regime×vol bucket (which would starve sample sizes). The pick is still
  walk-forward-gated; the SoD card shows a 🌊 "vol agrees/disagrees → blended R"
  line. Backward compatible: with no vol overlay `blendedR == regimeR`. Tests +5
  (suite **470 → 475**).
- **🌊 Volatility-aware regime board (India VIX axis)** — the regime engine was
  momentum-only (NIFTY %, breadth, prior-day move; VIX never fetched, PCR unused).
  Added an **orthogonal volatility axis** kept *separate* from the 6 directional
  labels so per-regime sample sizes / leaderboard / walk-forward keys stay stable:
  `get_index_snapshot` now pulls **INDIA VIX** (+ `yearHigh`/`yearLow`);
  `detect_regime` emits `vix`/`vixPctile`/`volState` (**Calm** <13 / **Normal**
  13–18 / **Elevated** ≥18). The daily backtest mirrors it with a VIX-free
  realized-vol proxy (`_stdev` → rolling stdev of the median move, percentile-
  bucketed) so `_regime_map` days carry `realVol`/`volState`; every sim + backtest
  trade is tagged **`volAtEntry`** (new additive `sim_trades` column) and a
  **volatility × strategy** leaderboard (`_vol_leaderboard`, result `volLeaderboard`/
  `volDist`) shows which edges hold up in calm vs elevated tape. UI: 🌊 VIX badge on
  the regime banner + Strategy-of-the-Day card and a vol leaderboard heat matrix.
  *Instrumentation, not selection yet* — the axis is surfaced/attributed now so
  vol-conditioned selection can be data-driven later. Tests +14 (suite **456 → 470**).
- **✍️ Paper option writing / short-selling (`paper.py`)** — options only did
  buy-to-open / sell-to-**close** (a user hit "Cannot sell… you hold 0 lots"). Now
  `place_option_order` uses signed qty like futures: `SELL` opens a **written short**
  (no long needed) that RECEIVES the premium and POSTS margin
  (`OPT_SHORT_MARGIN_RATE=0.15` × underlying-spot notional); `BUY` covers it (frees
  margin, realizes P&L). `portfolio()` marks written options margin-based
  (`ltp*qty + margin`, no premium double-count). UI shows SHORT/LONG + margin and a
  **Sell / Write** button. Tests +5 −1 (suite **452 → 456**). Paper money only.
- **🗄 Data resilience + broaden universe (`bhavcopy.py`)** — the live NSE JSON is
  anti-bot/flaky and only ~100-150 hot-list names had a price. NSE also ships the
  daily **UDiFF Common Bhavcopy** as STATIC ZIP/CSV on `nsearchives.nseindia.com`
  (no anti-bot gate). New `bhavcopy.py` parses the CM (cash, ~3100 equities) + FO
  (derivatives, ~215 futures + lot sizes) files — pure `parse_cm`/`parse_fo`
  (`TradDt` already `YYYY-MM-DD`), best-effort `_download` (404 → skip; one
  force-session retry) with weekend/holiday **walk-back** and a 30-min lock-guarded
  `latest()` cache. Wired as the **last-resort price** in `nse_client.get_price()`
  (hot-list → NextApi live → **EOD close**, so ANY listed symbol is priceable off-
  hours + when the live API is down) and a **lot-size fallback** in `get_lot_sizes()`.
  `db.eod_bars_put_bulk` + `ingest_db()` bulk-load the whole market into
  `eod_bars`/`eod_oi`, widening the daily-backtest universe. `/api/eod/status|price|
  quote|refresh` + startup pre-warm; Sim-tab **⬇ Load EOD (whole market)** button +
  freshness pill. Dependency-free. Tests +42, module **99 %** (suite **410 → 452**).
- **🛡 Walk-forward robustness overlay on strategy-of-the-day** — the regime
  leaderboard / strategy-of-the-day used to pick the best **in-sample** edge (curve-fit
  risk). It now **prefers a walk-forward-robust** strategy and **skips ones flagged
  overfit** out-of-sample. `backtest_daily` gained `cached_walkforward()` (memoised
  ≤1/6h), `peek_walkforward()` (non-blocking, for the hot path), `robustness_map()` and
  `_prefer_robust()` (`UNTRUSTED_VERDICTS={overfit,no-edge}`). `strategy_of_day()`
  returns `pick.robustness`, `ranked[].robustness`, `walkForward`, and `skippedOverfit`;
  the live `gen_adaptive` picks robustly via the non-blocking peek and annotates its
  reasons. UI: a colour-coded `WF: <verdict>` badge + "↩ Skipped …" note. Tests +5
  (suite **405 → 410**).
- **➕ Seven new strategies (library 10 → 17)** — added `fut_basis` (Futures
  Basis / Cost-of-Carry), `rel_strength` (Relative Strength vs NIFTY), `squeeze`
  (NR7 volatility squeeze), `gap` (Gap-and-Go / Fade), `pcr_extreme` (PCR
  contrarian), `max_pain` (expiry pin), and `pdhl` (prior-day high/low break). Each
  is a standard `gen_*` (returns `_mk_idea` shapes + `regimeFit`), so they run in
  the parallel sim and get tracked per-regime. `build_context()` gained two bounded,
  cached loaders — `ctx["daily"]` (session-cached recent daily bars) and
  `ctx["chains"]` (5-min-TTL per-stock PCR/max-pain for a small F&O subset). The
  EOD-computable `rel_strength`/`gap`/`squeeze` are also reconstructed in
  `backtest_daily` (STRATS 6 → 9; `_backtest_symbol` now takes `day_regime`) so
  **walk-forward validates them automatically**; the live-only edges are listed in
  `NOT_COVERED`. Tests +28 (suite **377 → 405**).
- **🧪 Walk-forward out-of-sample validation (`walkforward.py`)** — the overfit
  guard the Sim leaderboard was missing. A **pure** analysis (100 % covered) over the
  daily backtest's trade list: a holdout train/test split gives every fixed strategy
  an **in-sample vs OOS expectancy** + verdict (`robust` / `decaying` / `overfit` /
  `no-edge` / `improving`); the headline **adaptive-selection test** learns the
  best-per-regime playbook on train, follows it on test, and compares to the best
  fixed strategy + a-priori design (`adds-value` / `no-better-than-fixed`); anchored
  **walk-forward folds** pool re-learn→re-test so it's not one lucky cut.
  `backtest_daily.run(_collect=True)` now exposes raw trades; **`/api/sim/walkforward`**
  + Sim-tab **🧪** card (`renderWalkforward`). Suite **363 → 377**.
- **🧭 Project rules + living context** — added `.cursor/rules/` (always-apply):
  `00-testing` (extensive testing first), `10-no-subagents` (never use the Task
  tool — Max Mode is admin-disabled so subagents fall back to Composer 2.5 Fast),
  `20-context-file` (read+update `CONTEXT.md`), `30-documentation` (keep README +
  AGENTS + AUDIT + roadmap in sync). Created **`CONTEXT.md`** as the living memory.
- **🧪 Tests for the new features** — `test_book.py` (order-book imbalance/spread
  math, sanitisation/dedupe/cap, error isolation) + `test_notify.py` (config
  precedence, no-secret status, HTML-safe formatting, transport fan-out, and the
  full idea/volume detection + dedupe + gating against a temp DB). Suite **62 → 98**.
- **📖 Order-book intelligence (depth-derived signals)** — the 5-level depth we
  already fetch (via `nse_quote.getSymbolData`) now drives a **buy/sell pressure
  imbalance** signal everywhere depth is present: a green/red pressure bar +
  "Buy/Sell N% · spread X bps" on the **Live depth panel** and the **stock-detail
  modal**, and a per-row **right-edge stripe** on the Live watchlist (green=bid-heavy,
  red=ask-heavy, tooltip has the exact %). All zero added load (depth was already
  on-screen). The **Scanner** gains an optional **⚖ Order-book scan** button + "Book"
  column: one *user-initiated*, pool-fanned, **capped-at-30** batch of live depth
  (`/api/depth` → `nse_quote.get_book_stats`, reusing the 12 s quote cache) — no
  polling, so it can't stampede NSE. Shared JS helpers `depthStats()`/`obiBarHtml()`.
- **🔔 Off-screen alerts (Telegram / webhook) — `notify.py`** — server-side alerts
  that reach your phone when **no tab is open** (the old alerts were client-only).
  Rides the snapshot logger's existing 60 s market-hours cycle: fires on **fresh
  high-conviction ideas** (`get_recommendations`, conviction floor by `min_rating`)
  and **unusual-volume spikes with a rising price** (mirrors the client volume
  alert). **Opt-in and zero-overhead when unconfigured** (`tick()` fast-returns
  unless a channel is set). Deduped per (IST-day, kind, symbol[, direction]) via a
  new `alert_log` SQLite table (survives restarts; pruned at 14 d), capped per
  cycle. Config via env (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`ALERT_WEBHOOK_URL`)
  or gitignored `notify_config.json` (see `notify_config.example.json`). UI: header
  **🔔 Push** pill shows status + sends a test; endpoints `/api/alerts/status`
  (no secrets) + `/api/alerts/test`.
- **⚡ Deduplicated NSE hot-list fetches (`nse_client._fetch`)** — the list getters
  (`get_variations`/`get_most_active`/`get_volume_gainers`/`get_oi_spurts`/`get_futures`)
  were uncached, so `get_scanner()`, `strategies.build_context()` and
  `get_demand_score()` re-hit the SAME endpoints many times per 60s logger cycle
  (and frontend polls piled on top). Added a small **path-keyed 15s TTL micro-cache**
  in `_fetch` (only successful JSON; `ttl=0` forces live; size-capped). Measured
  **29 → 8 GETs per build-context+demand cycle (~72% fewer)**, with zero meaningful
  freshness change (reco already 12s, price 20s, index 30s). Tests in
  `test_fetch_cache.py` (hit/expiry/distinct-paths/ttl-0/error-not-cached/cap).
- **🛠 Audit findings implemented (all P0/P1/P2)** — see the dated **Remediation
  status** table in [`AUDIT.md`](AUDIT.md#1a-remediation-status-2026-07-16). Highlights:
  - **Security:** debugger off by default (`FLASK_DEBUG` opt-in), generic error
    handler, **CSRF same-origin check** on all writes, optional **`NSE_TOKEN`**
    gate, CSP + security headers, LAN warning at startup. LAN + auto-reload
    preserved (non-breaking).
  - **Robustness:** `logging` → `logs/app.log` + consolidated **`/api/health`**;
    NSE session rebuilt outside the lock; single-flight on `backtest_daily.run`
    and the futures sweep; `db.retention()` prunes reproducible logs at startup;
    all DB writers now hold `_write_lock`.
  - **Correctness:** one shared `intrabar.resolve_point()` for the coarse exit
    paths (stop-first); sim trades expire even when price is unavailable (no
    immortal `OPEN`); business-day hold horizon (survives app-offline sessions).
  - **Fixed the broken "⏮ Backtest history" button** (`host is not defined`).
  - Feeds now expose only coarse error categories; `escapeHtml()` + input
    sanitisation on the user-typed sinks; capped `nse_quote._cache`; config files
    cached by mtime; tz-aware UTC in `intrabar`.
  - Verified: all modules import, Flask test-client confirms CSRF(403)/CSP/health.
    **No behavioural regressions to the sims.**
- **🎯 Intrabar-accurate idea verdicts (audit L7)** — `ideas_journal.resolve_outcomes_intrabar()`
  now re-scores today's *unresolved* ideas against real 1-min candles from each
  idea's `firstSeenAt` via the canonical `intrabar.resolve` (STOP-first), so an
  idea's TARGET/STOP verdict matches the backtesters instead of depending on poll
  timing. Load-conscious: **throttled ~3 min** (race-safe), **market-hours gated**,
  one **batched, token-gated, 30 s-cached** fetch per symbol on a **background
  thread** (never blocks the poll); coarse LTP stays as the labelled fallback for
  tokenless symbols. Covered by `test_ideas.py`.
- **🧪 Test suite for the financial math (audit L8)** — `test_sim.py` +
  `test_backtest.py` + `test_ideas.py` + `test_take.py` join `test_intrabar.py`:
  **56 tests** (`python -m pytest -q`) covering risk-based sizing (+ notional cap /
  no-stop fallback), %-move, the business-day hold horizon, the coarse exit path
  incl. the M4 no-price expiry, the STOP-first daily tie-break + MFE/MAE, scorecard
  R/expectancy/win-rate, the L7 intrabar idea verdicts (tie/fallback/throttle), and
  the end-to-end `take()` ingest — dedupe (symbol×direction×strategy×day×book,
  surviving a same-day close), regime tagging, F&O-book filtering, `open`-mode
  once-per-day gating and the conviction limit — on a throwaway temp DB (no network).
  These lock in the exact numbers so a future refactor can't silently drift them.
- **🔍 Deep code audit → [`AUDIT.md`](AUDIT.md)** — whole-repo read-through
  (security, concurrency, financial-logic correctness, DB/persistence, feeds,
  frontend). Read it before hardening work. Severity is stated *in context*
  (loopback = mostly Low; the `0.0.0.0` default flips several to High). Top items
  for a future session, in priority order:
  - **H1/H2 (deploy):** `debug=True` + `host=0.0.0.0` + no-auth/no-CSRF → Werkzeug
    debugger/RCE + source disclosure on the LAN, and any device/website can reset
    ledgers or place paper trades. Default `DEBUG` off + `HOST=127.0.0.1`.
  - **M1 (bug):** `templates/index.html:4053` — dead `host.innerHTML` line throws
    `ReferenceError: host is not defined`, so the "⏮ Backtest history" button is
    stuck disabled. Delete that one line (the next line already renders it).
  - **M4 (sim):** trades on symbols that leave the hot list skip MTM **and**
    expiry (`sim.py:259-261`) → can hang `OPEN`; evaluate expiry in the
    `px is None` branch.
  - **M5 (observability):** pervasive `except Exception: pass`, no `logging` →
    silent blank data. Add logging + a `/api/health` off the existing heartbeats.
  - **M2/M3 (load):** session rebuild holds its lock across network I/O; heavy
    backtests have no single-flight → NSE stampede.
  - **M9 (correctness):** three divergent exit engines (live coarse checks
    target-before-stop; intrabar/daily are stop-first). Unify via one
    `resolve_exit()`.
  - Full list (M6/M7/M8 + L1–L9) and the prioritised P0/P1/P2 roadmap are in
    `AUDIT.md`. No source was changed by the audit.
- **🔔 New-idea alerts (any tab)** — an always-on client poller (`ideaAlertTick`,
  20s, market-hours gated) pings the moment a *new* idea appears: in-app toast
  (click → detail) + desktop Notification + beep. Seeds silently on the first
  non-empty poll so the existing backlog never floods; a `💡 New ideas` header
  dropdown filters by conviction (Off / High / High+Med / All, default High) and
  a 5-per-tick cap prevents bursts. Backend: `get_recommendations` now caches its
  (unfiltered) enriched set ~12s so the Ideas tab + the alert poll share ONE
  scanner sweep (the F&O toggle just filters the cached view). No new endpoint.
- **💡 Ideas journal → durable + historical view** (`ideas_journal.py`, now
  SQLite-backed via the new `ideas` table in `db.py`). The Ideas tab was
  stateless (entry == current price every poll, no memory). The journal now
  freezes each idea's **entry + `firstSeenAt`** on first sight, re-prices the
  whole day's set every poll (`movePct` + best/worst MFE/MAE), and records a
  **sticky first-touch verdict** (`outcome` = TARGET / STOP + `outcomeAt`, no
  look-ahead). Records persist across restarts and accumulate day by day, so
  `/api/ideas/history` (per-day summary: **market regime** [last detection,
  via `db.regime_by_day()` off `context_log`], n, L/S, ✓Tgt/✗Stop, Hit%, avg
  best/worst) and `/api/ideas/day?date=` (that day's ideas + outcomes) drive a
  new **📅 Ideas history** table under the live cards (click a day to expand its
  trades; rows deep-dive-able). Live cards show a `✓ target HH:MM` / `✗ stop`
  badge. A one-time import folds any pre-existing `ideas_journal.json` (today)
  into the DB so the migration loses nothing. `nse_client.get_recommendations`
  calls `ideas_journal.enrich(...)` with a cached `price_fn` (no per-symbol
  network fetch). Educational — NOT advice.
- **📈 Live feed → Angel One SmartAPI (FREE), provider-agnostic** — discovered
  Dhan's live *Data API* is a paid ₹499+GST/mo plan (socket connects then drops with
  code 806 unpaid), so added `angel_feed.py` using Angel One's **free** SmartAPI
  WebSocket (SNAP_QUOTE = LTP + day OHLC/vol + OI + best-5 depth; auto TOTP login via
  `pyotp`, prices in paise). `app.py` now selects `live_feed` at startup (Angel first,
  Dhan fallback) behind the unchanged `/api/live/*` routes; `public_status()` carries
  `provider`; the setup card is provider-aware. Also **hardened both feeds**: only
  connect during a market window + exponential backoff (fixes an outside-hours
  reconnect storm that tripped Dhan's HTTP 429), and the chip shows ● LIVE only when
  connected AND market-open.
- **📈 Live realtime tab (Dhan WebSocket + TradingView Lightweight Charts)** —
  the project's first *true stream* and first external JS dependency. New
  `dhan_feed.py` holds a Dhan `MarketFeed` (Full packets) on a supervisor thread,
  resolves symbols via the cached scrip master, keeps an in-memory tick store +
  forming 1-min candle (baked-IST epoch, persisted to `db.min_bars`), and is
  exposed via `/api/live/{config,watch,seed,stream(SSE),snapshot}`. Frontend adds a
  full-width workspace (persistent candlestick+volume chart, streaming watchlist,
  5-level depth ladder, quote header, 1m/5m/15m/1D). Fully optional + gitignored
  creds (`dhan_config.json`); no-op without a token. See the live-feed note above.
- **Two parallel books — 🧪 Sim (cash) + 🎯 F&O Sim** (`book` tag on every
  `sim_trades` row; `cash` | `fno`): both books run the SAME 17 strategies off the
  SAME live context each cycle, but the `fno` book only takes F&O-eligible ideas
  (`idea['fno']`, via `strategies._is_fno` = lot-size lookup). Identical risk-based
  sizing (₹2k/trade) → the two scorecards are directly comparable. The auto loop
  (`snapshot_logger`) calls `sim.take(..., book="cash")` **and** `sim.take(...,
  book="fno")`. Every read view takes `book=` (default `cash`): `summary`,
  `daily_matrix`, `daily_performance`, `day_trades`, `regime_leaderboard`,
  `leaderboard_bundle`, `performance`. DB: `sim_all_trades/open_trades/trades_where/
  clear/trade_count(book=)`, index `ix_sim_book`, and an `ALTER TABLE … ADD COLUMN
  book DEFAULT 'cash'` migration (all legacy trades → cash). Trade `id` is prefixed
  with the book so both books can hold the same setup. Routes accept `?book=fno`
  (GET) / `{book}` (take & reset POST bodies); `reset(book)` clears just that book,
  `reset(None)` wipes everything + settings. **UI:** a top-level **🎯 F&O Sim** tab
  mirrors the whole Sim view for `book=fno` (`window._simBook` drives fetches +
  per-book state: `_simSel`, `_dayOpen/_dayCache` keyed `book|date`, alert diffs in
  `_simPrevByBook`). Adaptive/strategy-of-the-day stays backtest-driven (book-shared).
- **Multi-strategy Sim + regime-aware daily comparison** (`strategies.py` +
  `sim.py`, 🧪 Sim tab): the Sim now forward-tests **17 strategies in parallel**,
  each with its **own ledger**, so we can see which one fits which market day.
  - **Strategies** (`strategies.py`, 17): `momentum` (the original multi-signal
    engine), `oi_smart` (F&O OI positioning), `meanrev` (contrarian oversold
    bounce / fade), `vol_breakout` (≥5× volume explosions), `high52w`
    (nearness-to-52-week-high momentum, George-Hwang), `vwap` (price vs the
    day's cumulative VWAP), `delivery` (high delivery% = accumulation/
    distribution), `orb` (Opening-Range Breakout: break of the 09:15-09:30 range
    with volume), `ivwap` (Intraday VWAP Reclaim: true session VWAP from minute
    candles), `fut_basis` (Futures Basis / Cost-of-Carry: rich premium+rising OI =
    LONG, discount+rising OI = SHORT — reads the spot↔future price gap), `rel_strength`
    (Relative Strength vs NIFTY: leaders LONG / laggards SHORT), `squeeze` (NR7
    volatility contraction → expansion break), `gap` (Gap-and-Go / Fade, regime-
    tilted), `pcr_extreme` (per-stock PCR contrarian — live-only), `max_pain`
    (expiry pin toward max pain — live-only, expiry-gated), `pdhl` (prior-day
    high/low break — live-only), `adaptive` (**Regime-Adaptive meta-strategy** —
    see below). Each is `{id,name,description,regimeFit,generate(ctx)}` returning
    ideas in `_build_idea` shape. `build_context()` fetches all live lists ONCE —
    including a bounded, concurrent per-symbol quote fetch (`ctx["quotes"]`, ~45
    liquid names, feeds VWAP/52wH/delivery/gap) AND 5-min candles for the same set
    (`ctx["candles"]`, feeds orb/ivwap) — plus two bounded, cached loaders:
    **`ctx["daily"]`** (recent daily bars, session-cached — immutable intraday;
    feeds `squeeze`/`pdhl`) and **`ctx["chains"]`** (per-stock PCR/max-pain for a
    small F&O subset, 5-min TTL; feeds `pcr_extreme`/`max_pain`). Every generator
    reuses the bundle. **`orb`/`ivwap`/`squeeze`/`pdhl`/`pcr_extreme`/`max_pain`
    depend on candles/daily/chains, which are NOT archived in `context_log`
    (`_trim_context`), so they run in the live forward-sim but are inert in the
    offline context-replay backtest.** The EOD-computable `rel_strength`/`gap`/
    `squeeze` ARE reconstructed in `backtest_daily` (and thus walk-forward-vetted).
  - **Regime-Adaptive** (`gen_adaptive` + `_regime_playbook_pick`): a
    meta-strategy that generates no signals itself — each session it delegates to
    the base strategy with the best HISTORICAL edge in today's regime (the
    strategy-of-the-day), tagging each idea's `reasons` with the playbook choice.
    The pick uses `backtest_daily.peek_regime_leaderboard()` (a NON-blocking cache
    read — lazy import to dodge the `strategies`↔`backtest_daily` cycle; never
    triggers a cold compute in the per-minute hot path), then **prefers a
    walk-forward-robust** strategy via `btd._prefer_robust` + `btd.robustness_map(
    btd.peek_walkforward())` (also non-blocking): among the regime's candidates
    sorted by in-sample edge, it takes the first whose out-of-sample verdict isn't
    `overfit`/`no-edge` (falls back to the raw best when no walk-forward is warm),
    and appends that verdict to the idea's reasons. Falls back to the first
    strategy whose `regimeFit` covers the regime. It's a LIVE-sim track only —
    deliberately absent from `backtest_daily.STRATS` so `cached_regime_leaderboard`
    → `run()` can't recurse — and forward-tests whether "follow the playbook" beats
    any single fixed strategy.
  - **Regime-conditioned position sizing** (adaptive only): `conviction_mult`
    combines two signals into a risk multiplier in **[0.5, 1.5]**. (1) The
    delegated strategy's *historical edge* — `_conviction_mult` maps its leaderboard
    cell (expectancy R + sample size in today's regime) to a base band: size up on a
    strong, well-sampled edge (≥0.30R·≥10 trades → 1.5×), down when weak/negative
    (<0 → 0.5×) or on a-priori fit (0.75×). (2) The *live regime clarity* —
    `regime_strength(regime)`∈[0,1] scores how textbook-clear today is (decisive
    NIFTY move + lopsided breadth for trends; tight+balanced for Range; sharp
    counter-move for Recovery/Pullback; Mixed≈0.3). The band is tilted ±20%
    (`factor = 0.8 + 0.4·strength`) and re-clamped, so a strong edge on a *borderline*
    regime day is trimmed (e.g. Trend-Up +0.87%/40:10 → strength 0.45 → 1.5×→1.47×)
    while a decisive day keeps the full bet. Emergent bonus: the 🔥 high-conviction
    alert (≥1.5×) now needs BOTH a strong edge AND a clear regime. Each
    idea carries `sizeMult`; `sim._open_trade` sets `risk = RISK_PER_TRADE × sizeMult`
    and `size_position(entry, stop, risk=...)` scales qty accordingly. Fixed
    strategies never set `sizeMult` (stay 1.0), so cross-strategy comparability is
    intact. Because `rMultiple` is normalized to each trade's OWN risk, expectancy R
    stays size-agnostic; the sizing payoff shows only in the **capital-weighted
    expectancy** `weightedR = ΣPnL/Σrisk` (closed trades) vs equal-weight
    `expectancyR` — surfaced in the Playbook scoreboard. `riskSum`/`weightedR` are
    added to each `_scorecard`; summary exposes `riskPerTrade`.
  - **Regime detector** (`detect_regime`): tags each day Trend-Up / Trend-Down /
    Recovery / Pullback / Range / Mixed from NIFTY %change + advance-decline
    breadth (`nse.get_index_snapshot()` → `/api/allIndices`, cached 30s) + the
    prior session's move. Plus an **orthogonal volatility axis** from **India VIX**
    (also on `/api/allIndices`): `volState` = **Calm** <13 / **Normal** 13–18 /
    **Elevated** ≥18, with a 52-week `vixPctile` from the index's year hi/lo. The
    directional label is unchanged (vol is a *tint*, not a 7th label — this keeps
    per-regime sample sizes and the leaderboard/walk-forward keys stable). Every
    trade is tagged `volAtEntry` (live sim + backtest) so vol-conditioned selection
    can later be learned from data.
  - **Per-strategy sims** (`sim.py` v2; trades in SQLite `sim_trades`, settings +
    daily rollup in `sim_state.json`): `take()` snapshots each strategy's ideas
    (risk-based sizing via `size_position()`: each trade risks a fixed ₹2,000 to
    its stop, notional-capped at ₹5L; dedup one entry per symbol+direction per
    strategy per day, checked via `db.sim_trades_where`); `update()` loads open
    trades with `db.sim_open_trades()`, marks to market and closes on target/stop
    **or a multi-day horizon** (`maxSessions`, default 3, then time-expire), then
    writes back with `db.sim_insert_trades()` (INSERT-OR-REPLACE by id). Entry mode
    is **selectable** — `continuous` (auto-take all day) or `open` (one snapshot/
    day). `daily_rollup()` stores each day's regime + per-strategy win-rate/P&L →
    `daily_matrix()` powers a **day × strategy heatmap**. All aggregate reads
    (`summary`, `regime_leaderboard`, `equity_curves`, `performance`) group
    `db.sim_all_trades()` in Python — same logic as before, just a durable source.
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
    render as sparklines. This is the accumulating forward-test.
  - **All-time performance** (`sim.performance()` → `/api/sim/performance`, 🧪 Sim
    tab "Performance (all-time)" table): one ranked row per strategy over the whole
    `sim_trades` ledger — expectancy R, total R, win%, realized ₹, profit factor,
    avg hold (mins), #trading-days — plus a portfolio total. Ranked by expectancy
    R. This is the durable cross-session scorecard (survives restarts).
  - **Daily P&L / "Today"** (`sim.daily_performance()`, folded into
    `/api/sim/daily` as `perf`): ledger-backed date-wise realized P&L across ALL
    strategies — per day: trades opened (by `openedDate`), trades CLOSED that day
    (by `closedDay`/`closedAt`) with realized ₹, summed R and target/stop/expiry
    split. The `today` card also carries the whole live open book + its unrealised
    MTM (open MTM is 'now', not a past day). Regime/NIFTY per day merged from the
    rollup log. Renders as a prominent **📅 Today** card near the top of the Sim tab
    + a **Daily P&L by date** table above the win-rate heatmap. Deliberately does
    NOT call `update()` (summary()'s per-poll reprice already refreshes open MTM —
    repeating it would double a heavy sweep on a big open book).
    Each Daily-P&L row is **clickable** → expands into that day's individual trades
    via `sim.day_trades(date)` / `/api/sim/day?date=YYYY-MM-DD` (trades CLOSED that
    day + trades OPENED that day still running, newest first, ≤400 each, tagged with
    strategy display name). Front-end keeps expanded dates in `window._dayOpen` (Set)
    and fetched trades in `window._dayCache` so the drill-down survives the tab's
    per-poll re-render; `toggleDay()` re-fetches on each expand so "today" reflects
    the latest closes. Each drilled-down trade row carries `data-sym` (row click →
    quick-detail modal, via the shared wiring) plus a `🔬` `drillBtn` (→ full
    deep-dive), same as every other table/card.
  - **UI**: regime banner, ⭐ strategy-of-the-day card, strategy cards (click to
    expand that strategy's open/closed tables), the **regime leaderboard** grid
    (+ equity sparklines), and the daily comparison heatmap. **Sim alerts** toast/
    beep/notify when a strategy takes new ideas or a trade hits target/stop
    (diffs per-strategy counts across polls). Two adaptive-specific alerts ride on
    the `summary().adaptive` block (`{regime, via, viaName, basis, sizeMult}` from
    `_regime_playbook_pick`/`_conviction_mult`): a **🎯 playbook flip** when the
    adaptive track switches which strategy it follows (regime rotated the pick),
    and a **🔥 high-conviction** variant when adaptive fires while today's size is
    ≥1.5×. The scoreboard shows a live "Following X · <regime> · conviction ×N"
    line as the visible counterpart (`simAlerts` fires only while the Sim tab is
    open). Controls: Take all, entry-mode dropdown, Auto, Reset.
  - **Offline backtest** (`backtest_strategies.py`, `/api/sim/backtest`): two
    passes. Pass 1 replays the SAME generators over the archived `context_log`
    and OPENS trades only (one per symbol+direction per day; `open` vs
    `continuous` entry). Pass 2 resolves exits against **real 1-min OHLCV**
    (`intrabar.resolve`, see below), fetched once per unique symbol. Returns
    per-strategy scorecards (win%, expectancy R, avg MFE/MAE, median mins-to-exit)
    + equity curves + a regime leaderboard, plus `resolve` mode and a
    `{intrabar, ltpFallback}` count. `?resolve=ltp` reverts to the old coarse
    per-cycle LTP resolution. UI: "⏮ Backtest history" button in the Sim tab.
  - **Daily-bar historical backtest** (`backtest_daily.py`, `/api/sim/backtest_daily`,
    "📅 Daily backtest" button): answers "how would the strategies have done over
    the last N days?" *today*, without needing archived context. Pulls REAL NSE EOD
    history (`nse.get_stock_history` daily OHLCV+delivery% + near-month futures OI
    via `nse.get_futures_oi_history`/`foCPV`, near expiry from `nse.get_futures`),
    reconstructs the **9 EOD-computable strategies** (momentum, meanrev, delivery,
    high52w=52w-proxy, vol_breakout, oi_smart=rising-OI buildup, rel_strength=5-day
    move vs an equal-weight market proxy, gap=open-vs-prev-close regime-tilted,
    squeeze=NR7 contraction→break) over a selectable universe — `LIQUID` names
    first, extended with a spread sample of the rest; the UI offers Top 40/80/150 or
    **All F&O (~210)** (`universe` param, capped 260, `_universe()` clamps to the
    live count). Enters at the signal day's close, resolves on subsequent daily
    high/low (stop-first on straddles), same `size_position` R sizing. Concurrency =
    6 workers. VWAP/ORB/iVWAP + the live-only F&O edges (fut_basis, pcr_extreme,
    max_pain, pdhl) are in `NOT_COVERED`. This is a daily-bar APPROXIMATION (lower
    fidelity than the live sim / context backtest) — the UI says so; don't conflate
    its numbers with the intraday strategies.
    **Full-universe EOD source** (`?source=eod`, Sim-tab "Backtest source" selector):
    instead of the per-symbol NSE pull above, read the WHOLE ingested bhavcopy
    universe straight from SQLite (`db.eod_bars`/`db.eod_oi`, populated by
    `bhavcopy.backfill`). No network, works off-hours, and runs **thousands of
    trades** (~1500 liquid names → ~5k trades in <1s) so the regime/vol leaderboards,
    `strategy_of_day` and walk-forward become statistically trustworthy — the curated
    ~40-name run flatters the strategies; the whole market is the honest test. The
    data layer is split into `_load_live` / `_load_eod` (both return `(hist, ois,
    meta)`) so the entire analysis pipeline is shared. `_load_eod` applies liquidity
    floors (`minPrice`/`minValueCr`), keeps the top-N by turnover, and builds a
    continuous near-month OI% series via `db.eod_oi_all()`. `resolve` is forced to
    daily (minute re-resolution needs per-symbol fetches); **Delivery% goes quiet**
    (the UDiFF CM bhavcopy has no delivery column). `source` also threads through
    `strategy_of_day`/`walkforward` (`?source=eod`), keyed separately in their caches.
    **Persistent EOD cache** (`db.eod_bars` / `eod_oi` / `eod_meta`): daily bars are
    immutable once a session closes, so we store them in SQLite forever and only
    re-hit NSE per a freshness TTL (`CACHE_TTL_HOURS=12`, tracked in `eod_meta`).
    First full-universe run ≈ ~840 requests / ~3 min; after that repeat runs are
    ~instant (all cache hits) and wider look-backs (60/90d) need ZERO extra fetches
    because ~8 months of history is already stored. `_cached_bars`/`_cached_oi_rows`
    are the read-through wrappers; `?refresh=1` (or the "force refresh" checkbox)
    bypasses the cache. `run()` reports `cache:{barsHit,barsFetched,ttlHours,store}`.
    **Minute-accurate mode** (`?resolve=intrabar`, "minute-accurate" checkbox): a
    second pass re-resolves every daily trade on REAL 1-min candles via
    `intrabar.resolve` (true intraday path — which-came-first, wick timing, MFE/MAE)
    instead of the daily high/low; entry stays at the signal-day close. Minute bars
    are pulled once per symbol for the window (`_prefetch_minutes` → `nse_quote.get_ohlc`)
    and cached in `db.min_bars` (PK symbol,t-ms; 12h TTL via `eod_meta` kind `min`).
    Trades older than NSE's ~30-40d minute retention keep the daily resolution;
    `run()` reports `resolve` + `resolved:{intrabar,daily}` + `cache.minCache`. In
    practice it closely CONFIRMS the daily numbers here (stop/target 9% apart → ~0%
    same-day both-touched), so it's a fidelity/validation toggle, not a rewrite.
    **OI gate (tightened):** `oi_smart` now needs `OI_MIN_PCT=8` OI rise **and**
    `volMult>=OI_MIN_VOL_MULT(1.2)` **and** `|ret1|>=OI_MIN_RET(0.5)` (was a loose
    ≥3% any-volume gate that made it ~44% of all trades / the biggest drag) — cuts
    it ~59% (724→294 on the full universe). **Scorecard MFE/MAE:** `_resolve` now
    tracks max favorable / adverse excursion from entry over the hold (daily wicks in
    daily mode; `_reresolve_intrabar` overwrites with true intraday wicks in minute
    mode), surfaced as avg `avgMfePct`/`avgMaePct` columns. `medMinsToExit` is
    computed but NOT rendered in this table (multi-day holds make it wall-clock
    minutes ≈ holdDays×1440, redundant with Hold; keep it for the short-hold live
    sim).
    **Regime leaderboard + gating:** `_regime_map(hist)` builds a per-day market
    regime from an equal-weight proxy over the SAME fetched universe (`_median`
    1-day move + adv/dec breadth) classified by `_classify_regime` (identical
    thresholds to `strategies.detect_regime`, so labels match the live sim: Trend-Up
    / Recovery / Range / Pullback / Mixed / Trend-Down). Every trade gets
    `regimeAtEntry` = the label of its `openedDate`. `_regime_leaderboard(all_trades)`
    → regime × strategy matrix of expectancy R / win% / count with the best strategy
    per regime (≥3 samples) — pure attribution, NO look-ahead.
    **Volatility axis:** since there's no historical India-VIX feed here, `_regime_map`
    also computes a VIX-free proxy — `_stdev` of the median-move series over a
    10-session window (`_annotate_vol`), bucketed by its percentile within the tested
    window into `volState` Calm/Normal/Elevated (mirrors the live board's semantics).
    Each trade also carries `volAtEntry`, and `_vol_leaderboard` (built via the shared
    `_leaderboard(attr, field, order)` that also backs `_regime_leaderboard`) gives a
    vol × strategy matrix — result fields `volLeaderboard` / `volDist`. `_gated(by_strat)`
    applies an **a-priori** gate: keep only trades whose entry regime is in the
    strategy's `strategies.STRATEGY_MAP[sid]["regimeFit"]` (designed by trading
    logic, not fit to this window) and reports per-strategy all-vs-in-fit plus the
    combined gated portfolio, so Δ is an honest "does trading only your regime
    help?". `run()` adds `regimeLeaderboard` + `regimeDist` (days per regime in the
    window) + `gated`; the UI renders both (`renderDailyRegime`, reuses
    `REGIME_CLS`/`REGIME_ICON`/`heatColor`). Leaderboard cells colour by avgPnlPct
    but DISPLAY expectancy R (the backtest's headline metric). In minute mode the R
    is the intrabar-accurate one (regimeAtEntry is set pre-resolution, R updated
    after).
    **🎯 Strategy of the day** (`strategy_of_day()`, `/api/sim/strategy_of_day`):
    reads today's LIVE regime (`sim.current_regime()`) and returns the strategy
    with the best HISTORICAL expectancy R on that regime, from
    `cached_regime_leaderboard()` (a `run(days=60, universe=60)` memoised
    in-process for `_SOD_TTL_S=6h` behind `_sod_lock`; `min_closed=5` per cell).
    **Vol-conditioned:** the ranking is by `blendedR = _blend_r(regimeR, volR)`
    (`_VOL_BLEND_W=0.4`) — the regime-bucket edge blended with the *current* India-VIX
    bucket's edge (`_vol_cells` over the now-exposed `volLeaderboard`), so the pick
    reflects both direction and volatility; each candidate carries `volExpectancyR`/
    `blendedR` and the reason notes vol agree/disagree. The same blend feeds the live
    adaptive playbook (`_regime_playbook_pick(regime_label, vol_state)`). Walk-forward
    still gates the final choice; with no vol overlay `blendedR == regimeR`.
    Falls back to the a-priori `regimeFit` design when the regime is thin
    (`basis: history|fit|none`); `pick.fits` flags a pick winning OUTSIDE its
    designed regime. Pre-warmed in a daemon thread at startup (app.py `__main__`)
    so the first Sim-tab poll is instant — the cold ~30s compute happens in the
    background. UI: `renderStrategyOfDay` renders a `#sodCard` hero fetched
    SEPARATELY from the sim Promise.all (never stalls the tab), HTML cached in
    `window._sodHtml` and re-injected/re-bound each poll. The old live-ledger pick
    (`sim.strategy_of_the_day`) still shows, relabelled "⭐ Live forward-test
    leader" to disambiguate.
  - **🧪 Walk-forward out-of-sample validation** (`walkforward.py`,
    `/api/sim/walkforward`, "🧪 Walk-forward" button): the overfit guard on top of the
    daily backtest. `backtest_daily.run(..., _collect=True)` returns the flat `trades`
    list (each tagged `openedDate`/`regimeAtEntry`/`strategy`/`rMultiple`) + `dayRegime`;
    `walkforward.analyze()` is then **pure** (100 % covered, no network). Three views:
    (1) **holdout split** at `train_frac` (default 0.6) — earlier=train, later=OOS;
    per fixed strategy → in-sample vs OOS expectancy + `_verdict` (`robust` if OOS ≥
    60 % of IS, `decaying`, `overfit` = positive IS but negative OOS, `no-edge`,
    `improving`, `insufficient`). (2) **adaptive-selection test** — a fixed strategy
    has no fitted params, but the *which-strategy-per-regime* choice IS fit on train;
    so `_best_per_regime(train)` learns the playbook, `_apply_playbook(test, …)` follows
    it OOS, and `_adaptive_verdict` compares to the best single fixed strategy OOS +
    the a-priori `regimeFit` map (`adds-value` / `no-better-than-fixed`). (3) **anchored
    walk-forward folds** — `_split_folds` chunks the days, each fold re-learns on the
    expanding train and re-tests on the next fold, pooled, so the verdict isn't hostage
    to one arbitrary cut. UI `renderWalkforward` = adaptive-verdict banner + per-strategy
    IS→OOS table + fold table. This is the anti-curve-fit sanity check for the whole
    leaderboard — a strategy is only trustworthy if its OOS expectancy stays positive.
  - **Intrabar resolution** (`intrabar.py`): the sims used to decide target/stop
    against a single LTP per cycle (60s live, 5-min backtest), which misses wicks
    and detects exits late. `intrabar.resolve(trade, bars, risk, max_sessions)`
    walks minute candles: LONG STOP when a bar LOW <= stop, TARGET when HIGH >=
    target (mirror for SHORT); a bar that straddles both is assumed to hit the
    STOP first (conservative). Tracks true intrabar MFE/MAE and mins-to-exit;
    returns None when a symbol has no candles (renamed ticker / index) so callers
    fall back to LTP. The live sim runs a bounded catch-up sweep every ~180s to
    close trades whose stop/target was pierced between LTP samples — split into
    `_intrabar_fetch` (the 6-worker candle fan-out, run lock-free) +
    `_intrabar_apply` (in-memory resolve, under the sim lock) per AUDIT2 N1.
  - **EPOCH GOTCHA:** `charting.nseindia.com` bakes IST wall-clock into the epoch
    as if it were UTC — for BOTH returned candle `time` AND the fromDate/toDate
    query bounds. Build query epochs from the IST wall clock treated as UTC
    (`nse_quote._baked_now` / `_baked_epoch`), NOT the real unix timestamp (which
    is 5:30 behind and returns a clamped/wrong window). `intrabar.candle_dt` reads
    the ms back with `utcfromtimestamp` to recover true IST.
  - **Per-trade replay** (Sim tab ▶ button): opens the trade's 1-min candles for
    its holding window with entry/target/stop/exit lines overlaid + MFE/MAE and
    time-to-exit stats. On demand via `/api/ohlc/<sym>?from=&to=` (baked epochs);
    no storage — trades are within NSE's ~30-40 day 1-min retention.
  - Routes: `/api/sim/{strategies,summary[?strategy=],daily,leaderboard,performance,
    backtest[?resolve=intrabar|ltp],backtest_daily[?days=&universe=&maxHold=&refresh=&resolve=daily|intrabar&source=live|eod&minPrice=&minValueCr=],
    strategy_of_day[?source=eod],walkforward[?source=eod],
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
- Sparkline history + alert state persisted to localStorage (`nseHistory.v1`):
  a browser refresh no longer wipes the client-side sparklines. Intraday-only —
  reset on the **IST** day boundary (`todayStr`), debounced writes + a
  `beforeunload` flush, ≤120 points/symbol and ≤500 symbols (`pruneHistory`,
  most-recently-ticking kept) to stay well under the quota.
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

- Keep data-fetching logic in `nse_client.py` / `nse_quote.py` / the live-feed
  adapters (`angel_feed.py` / `dhan_feed.py`); keep `app.py` thin (routes only).
- Normalize NSE fields into stable keys (`symbol`, `ltp`, `pChange`, `volume`,
  ...) so the frontend/CLI don't depend on NSE's raw field names.
- No secrets in the repo. NSE needs no API key; the optional live feed reads creds
  from env or a **gitignored** config — `angel_config.json` (Angel One) or
  `dhan_config.json` (Dhan). `.gitignore` covers `.env`, `*.db`, state JSON, both
  config files, and `logs/`. Never commit a token/secret.
- Only commit/push when the user explicitly asks.
```
