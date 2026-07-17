# CONTEXT — NSE Market Pulse (living memory)

> **This file is the single living context/memory for the project.** AI agents
> MUST read it at the start of every task and keep it updated with every new
> finding, decision, or observed behavior (see `.cursor/rules/`). Newest changes
> are logged at the bottom (**Findings & change log**, newest first).

## How the docs fit together

| Doc | Role |
|-----|------|
| **CONTEXT.md** (this) | Living memory: current state + running findings/change log. Always read + update. |
| `AGENTS.md` | Project guide: conventions, file tree, "Done recently", roadmap. |
| `README.md` | User-facing overview / run / features. |
| `AUDIT.md` | Deep code audit round 1 — findings, severities, remediation status. |
| `AUDIT2.md` | Deep audit round 2 — financial-correctness + concurrency deep-dive. |

## Critical agent rules (enforced via `.cursor/rules/`)

1. **Testing is the top priority** — extensive + in-depth, before anything is
   "done". Run `python -m pytest -q`; add tests for every new behavior/bugfix.
2. **Never spawn subagents / use the Task tool** — Max Mode is admin-disabled, so
   subagents fall back to Composer 2.5 Fast (can't inherit Opus). Work inline,
   sequentially, one module at a time. Search with Grep/Glob/Read.
3. **Always read + update CONTEXT.md.**
4. **Always keep README + related docs (AGENTS/AUDIT/roadmap) in sync.**
5. **Only commit/push when the user explicitly asks.** Never commit secrets.

---

## What this project is

**NSE Market Pulse** — a live Flask dashboard + CLI that surfaces which NSE
(India) stocks are "in demand" right now (intraday momentum, unusual activity,
F&O signals), plus a multi-strategy forward-tester/backtester, paper trading, an
optional live broker feed, and off-screen alerts. Data from NSE India's public
(unofficial) JSON APIs. **Educational/research only — NOT investment advice.**

- GitHub: `git@github.com:aakash-jain-1/nse-market-pulse.git` (branch `main`).
- Owner: aakash-jain-1. Single-user local tool on Windows.

## Tech stack

- **Python 3.13** (Windows), **Flask 3.1.x** (server + JSON API, port **5055**).
- **requests** (NSE HTTP with cookie warm-up), **sqlite3** (stdlib, WAL),
  **tabulate** (CLI). Frontend: vanilla HTML/CSS/JS in one template (no build).
- Optional live feed: **Angel One SmartAPI** (free, default) via `smartapi`/
  `logzero`/`websocket-client`, or **Dhan** (paid data plan). Charts: TradingView
  **Lightweight Charts** (CDN, or self-hosted in `static/vendor/`).

## How to run

```bash
python app.py            # dashboard at http://127.0.0.1:5055 (binds 0.0.0.0 for LAN)
python nse_demand.py     # CLI scanner (gainers/losers/volume/value/volgainers)
python db_inspect.py     # read-only SQLite peek (overview / <table> [N] / sql "...")
python -m pytest -q      # full unit-test suite
```

- App **auto-reloads** on `.py` changes; re-reads `templates/index.html` per
  request (no restart for UI edits). Changing `HOST`/`PORT` needs a full restart.
- Env knobs: `FLASK_DEBUG=1` (debugger, OFF by default — RCE surface),
  `FLASK_RELOAD=0`, `HOST=127.0.0.1` (loopback), `PORT=xxxx`, `NSE_TOKEN=<secret>`
  (require token; open once with `?token=<secret>`). Health: `GET /api/health`.
- Alerts env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALERT_WEBHOOK_URL`.

### Environment gotchas (IMPORTANT)

- Bare `python` sometimes hits the **Microsoft Store shim** ("Python was not
  found"). Full path: `C:/Users/aakas/AppData/Local/Programs/Python/Python313/python.exe`.
- Scripts that print emojis/₹ on Windows need `PYTHONIOENCODING=utf-8` (or
  `sys.stdout.reconfigure(encoding="utf-8")`) to avoid `UnicodeEncodeError`.
- **Port 5000 is contaminated** by a cached service worker from a different PWA →
  we use **5055**.
- Protected `main`: pushes require explicit user approval.

## File map

```
app.py               Flask routes (thin) + startup wiring + security guard/headers
nse_client.py        NSE session mgmt + hot-list fetch/normalize (CORE) + _fetch micro-cache
nse_quote.py         Per-stock quote/chart/DEPTH (NextApi) + OHLCV (charting) + get_book_stats
bhavcopy.py          EOD UDiFF bhavcopy ingest (static archive) — resilient price/universe fallback + backfill(days)
eod_scanner.py       Full-market EOD/swing scanner over db.eod_bars (breakouts/gaps/vol/MA/NR7) — off-hours, pure math
eod_options.py       Resilient EOD option chain from FO bhavcopy (PCR/max-pain/OI walls) — matches live shape, off-hours
angel_feed.py        Live feed adapter — Angel One SmartAPI WebSocket (FREE default)
dhan_feed.py         Live feed adapter — Dhan WebSocket (paid data plan)
notify.py            Off-screen alerts (Telegram/webhook) — opt-in, rides snapshot logger
paper.py             Paper-trading engine (equity + long/short options + long/short futures, margin-based; JSON-persisted)
strategies.py        Strategy library (17 generators) + market-regime detector
sim.py               Multi-strategy forward-tester (per-strategy sims + daily rollup)
intrabar.py          Minute-candle trade resolver (target/stop/MFE/MAE) + resolve_point
backtest_strategies.py  Offline backtester: replays archived context, resolves on OHLCV
backtest_daily.py    Daily-bar historical backtest — source="live" (curated NSE) OR "eod" (whole bhavcopy universe from SQLite, off-hours)
walkforward.py       Walk-forward out-of-sample / overfit validation (pure over trades)
db.py                SQLite store (snapshots/IV/context/sim_trades/ideas/alert_log/EOD/min_bars)
snapshot_logger.py   Background logger (snapshots+IV+context+sim+alerts) → SQLite
db_inspect.py        Read-only SQLite inspector CLI
nse_demand.py        Standalone CLI scanner
templates/index.html Entire dashboard UI (HTML+CSS+JS inline)
test_*.py            Unit tests — 530 across 26 suites (client/quote/paper/strategies/sim/backtests/walkforward/bhavcopy/eodscanner/eodoptions/db/app+routes/feeds/notify/…)
*.example.json       Config templates (angel/dhan/notify) → copy to gitignored real files
data/market.db       (gitignored) SQLite; sim_state.json / paper_state.json (gitignored)
```

## Architecture notes

- **NSE session (`nse_client.py`)**: NSE blocks plain HTTP. We keep a warmed
  `requests.Session` (browser UA + Referer + homepage/market cookies), reused,
  rebuilt on failure and after a TTL. Built **outside** the lock then swapped in
  (M3). `HTTPAdapter(pool_connections=16, pool_maxsize=32)` avoids pool-full warns.
  **`_fetch()`** has a path-keyed **15s TTL micro-cache** (shared read-only object;
  callers must not mutate) that cut duplicate hot-list GETs ~72%/cycle.
- **NextApi gateway (`nse_quote.py`)**: the old `/api/quote-equity` is 403 and
  `/api/chart-databyindex` is empty. The site's `/api/NextApi/apiClient/GetQuoteApi`
  (with a stock-specific Referer) unlocks per-stock quotes, **5-level depth**
  (`getSymbolData` → `orderBook`), and real intraday points. `_cache` is capped
  (`_CACHE_MAX=2000`).
- **EOD bhavcopy (`bhavcopy.py`)**: NSE's live JSON is anti-bot/flaky and only
  the ~100-150 hot-list names have a price. NSE ALSO publishes the daily "UDiFF"
  **Common Bhavcopy** as STATIC ZIP/CSV on `nsearchives.nseindia.com` (no anti-bot
  gate) — one CM (cash, ~3100 equities) + one FO (derivatives, ~215 futures + lots)
  file per day, `TradDt` already `YYYY-MM-DD`. Parsing is pure (`parse_cm`/
  `parse_fo`); downloads walk back over weekends/holidays (404 → prior session)
  and cache 30 min (`latest()`, lock-guarded). Wired as the **last-resort price**
  in `nse_client.get_price()` (→ any listed symbol is priceable, off-hours + when
  the live API is down) and a **lot-size fallback** in `get_lot_sizes()`.
  `ingest_db()` bulk-loads CM bars + FO OI into `eod_bars`/`eod_oi` to widen the
  daily-backtest universe to the whole market. Dependency-free (reimplements the
  slice of `jugaad-data` we need).
- **Data flow**: `nse_client`/`nse_quote` normalize NSE fields into stable keys
  (`symbol`, `ltp`, `pChange`, `volume`, ...). `app.py` (JSON API) + `nse_demand.py`
  (CLI) consume them; the frontend polls `/api/<view>` and renders client-side.
- **Live feed (optional)**: provider-agnostic; `app.py` picks Angel or Dhan at
  import. Supervisor thread holds the WebSocket during a market-hours window with
  exponential backoff; pushes ticks to the frontend via **SSE**. Falls back to
  **NSE-polled** depth/quotes (~12s) when no broker is connected. Errors are
  normalized to coarse categories (no secret leakage).
- **Sim (`sim.py` + `strategies.py`)**: `build_ctx()` fetches the shared context
  once/cycle + attaches regime; `update()` marks trades to market (I/O off the
  lock); `take()` opens new ideas (deduped per symbol/dir/strategy/day/book;
  `cash` + `fno` books). Risk-based sizing (₹2,000 risk/trade), ≤3 business-day
  hold, expectancy in R. Coarse exits go through `intrabar.resolve_point()`
  (stop-first tie-break).
- **Snapshot logger (`snapshot_logger.py`)**: daemon loop every **60s during
  market hours** (Mon–Fri 09:15–15:30 IST). Each cycle: demand+volgainers
  snapshot → SQLite; ATM IV every 5 min; `sim.build_ctx/update/take/daily_rollup`;
  context archive every 5 min; **`notify.tick(ctx)`**. Isolated sub-tasks +
  heartbeat + watchdog + session-rebuild self-healing; `health()`.

## Key API endpoints (non-exhaustive)

- Views: `/api/scanner`, `/api/demand`, `/api/gainers|losers`, `/api/volume|value`,
  `/api/volgainers`, `/api/oi`, `/api/futures` (+`/all`, `/<sym>`),
  `/api/recommendations?fno=1`.
- Per-stock: `/api/quote/<sym>` (incl. 5-level depth), `/api/chart/<sym>`,
  `/api/ohlc/<sym>`, `/api/deepdive/<sym>`, `/api/optionchain/<sym>[/summary]`.
- **`/api/depth?symbols=A,B,C`** — batch order-book imbalance (capped 30, pooled).
- **EOD**: `/api/eod/status[?refresh=1]` (bhavcopy freshness/coverage, no secrets),
  `/api/eod/price/<sym>`, `/api/eod/quote/<sym>`, `/api/eod/refresh` (POST → ingest
  the whole market into the EOD cache), **`/api/eod/scan?view=&limit=&minPrice=&
  minValueCr=&fno=1`** (full-market swing scanner), **`/api/eod/backfill`** (POST
  {days} starts a background history load; GET polls progress),
  **`/api/eod/optionchain/<sym>[?expiry]`** + **`/summary`** (resilient EOD option
  chain from the FO bhavcopy — PCR/max-pain/OI walls, off-hours).
- Live: `/api/live/config`, `/api/live/watch` (POST), `/api/live/seed/<sym>`, SSE stream.
- Alerts: **`/api/alerts/status`** (no secrets), **`/api/alerts/test`** (POST).
- Sim/research: `/api/sim/summary|daily|leaderboard|performance|analytics|regime`,
  `/api/sim/backtest[_daily]`, `/api/sim/strategy_of_day`,
  **`/api/sim/walkforward?days=120&universe=60&folds=4`** (out-of-sample validation).
  `backtest_daily`, `strategy_of_day` and `walkforward` all take **`?source=eod`**
  (+ `minPrice`/`minValueCr`) to run over the WHOLE ingested bhavcopy universe from
  SQLite instead of a curated NSE pull — off-hours, thousands of trades.
- Ops: `/api/health`, `/api/log/status|health|snapshot`, sim + ideas + paper routes.

## Data storage

- **SQLite** (`db.py`, `data/market.db`, WAL, all writes under `_write_lock`):
  `snapshots`, `iv_log`, `context_log` (gzipped ctx/cycle), `sim_trades` (durable
  ledger), `ideas` (PK day/symbol/direction), `alert_log` (PK key; alert dedupe),
  `eod_bars`/`eod_oi`/`eod_meta` (immutable EOD cache), `min_bars` (1-min OHLCV).
  `retention()` prunes reproducible logs at startup (snapshots 90d, iv 120d,
  context 60d, min_bars 45d, alert_log 14d).
- **JSON state** (gitignored, atomic): `sim_state.json` (sim settings + rollup
  only — trades live in SQLite), `paper_state.json` (virtual portfolio).

## Security posture (post-audit)

Debugger OFF by default; generic error handler (no tracebacks to clients);
**CSRF same-origin check on all writes**; optional `NSE_TOKEN` gate; CSP +
security headers; LAN-exposure warning at startup; `escapeHtml()` + input
sanitization on user-typed sinks. See `AUDIT.md` for the full posture + status.

## Testing

- `python -m pytest -q` — **530 tests** (grow it with every change; never shrink it).
  Suites: `test_intrabar.py`, `test_sim.py` + `test_sim_views.py` (DB-backed
  read/aggregation + settings), `test_take.py` (temp DB e2e), `test_backtest.py`,
  `test_backtest_daily.py` + `test_backtest_strategies.py` (signal/exit/regime
  math), `test_ideas.py` + `test_ideas_journal.py`, `test_fetch_cache.py`,
  `test_client.py` + `test_client_fetchers.py` (normalizers + raw-payload
  parsers), `test_quote.py` + `test_quote_more.py`, `test_paper.py`,
  `test_strategies.py`, `test_bhavcopy.py` (EOD UDiFF parsers + fetch walk-back +
  price/lot fallback wiring), `test_app.py` (middleware) + `test_app_routes.py`
  (every endpoint via the Flask test client), `test_db.py`, `test_logger.py`,
  `test_feeds.py`, `test_book.py`, `test_notify.py`.
- Coverage: `python -m coverage run -m pytest && coverage report -m --omit="test_*.py"`
  → **~73 % of source** (100 % pure math, `app.py` routes 86 %; the rest is
  startup/thread/websocket/SSE glue tested via stubs or left to integration).
  `.coverage`/`htmlcov/` are gitignored.
- Also: `py_compile` for Python, `node --check` on the extracted inline `<script>`,
  and `curl` smoke tests for endpoints.

## Working vs blocked NSE endpoints

- **Working**: live-analysis variations (gainers/`loosers`[sic]), most-active
  volume/value, volume-gainers, OI-spurts underlyings, `liveEquity-derivatives`
  (stock_fut), NextApi `getSymbolData` (quote+depth), `getSymbolChartData`,
  `getSymbolDerivativesData`, charting.nseindia.com OHLCV, option chain,
  **nsearchives UDiFF bhavcopy** (CM+FO daily ZIP — static, no anti-bot gate).
- **Blocked/unreliable**: `/api/quote-equity` (403), `/api/chart-databyindex`
  (empty grapthData), snapshot-derivatives pre-market ("No Data"). Depth is
  all-zeros outside market hours.

## Roadmap

**Done recently (this session):**
- ✅ **#1 Order-book intelligence** — buy/sell imbalance (ΣBid vs ΣAsk) + spread
  from 5-level depth on Live depth panel, watchlist row stripes, detail modal; +
  Scanner **⚖ Order-book scan** button/column via `/api/depth` (capped, pooled).
- ✅ **#2 Off-screen alerts (`notify.py`)** — server-side Telegram/webhook alerts
  on fresh high-conviction ideas + volume spikes; opt-in, deduped (`alert_log`),
  rides the snapshot logger; header **🔔 Push** pill + status/test endpoints.

**Selected next (user picked, sequenced):**
- ✅ **#3 Walk-forward out-of-sample validation (`walkforward.py`)** — holdout
  train/test split + anchored folds over the daily backtest's trades; per-strategy
  in-sample vs OOS expectancy with an overfit verdict (robust / decaying / overfit /
  no-edge / improving), plus the headline **adaptive-selection test** (learn the
  best-per-regime playbook on train, follow it on test, compare to best fixed +
  a-priori design). `/api/sim/walkforward` + Sim-tab 🧪 card. Pure → 100 % covered.
- ✅ **Engine sharpening — volatility-aware regime board** — added an India-VIX
  volatility axis (`volState` Calm/Normal/Elevated + 52-wk percentile) orthogonal
  to the 6 directional labels, mirrored by a realized-vol proxy in the backtest;
  every sim + backtest trade now tagged `volAtEntry`, plus a vol × strategy
  leaderboard.
- ✅ **Engine sharpening — vol-conditioned selection** — `strategy_of_day` + the
  live adaptive playbook now pick using a **blend of the regime and vol marginal
  expectancies** (`blendedR`, vol weight 0.4), walk-forward-gated. *Still open:*
  more researched edges; a joint regime×vol view once samples are deep enough.
- ✅ **#4 Data resilience + broaden universe (`bhavcopy.py`)** — native EOD UDiFF
  bhavcopy ingest from NSE's static archive (no anti-bot gate). Prices ANY listed
  symbol (last-resort in `get_price`, works off-hours + when the live API is down),
  gives a lot-size fallback, and `ingest_db()` bulk-loads the whole market into the
  EOD cache to widen the daily-backtest universe. `/api/eod/*` + Sim-tab "⬇ Load
  EOD" button. Dependency-free (the `jugaad-data` slice we needed, in-house).
- ✅ **Full-market EOD / swing scanner (`eod_scanner.py`)** — cashes in the
  bhavcopy universe: a whole-market board (up to ~2400 cash names + the F&O set,
  not just the ~100–150 live hot lists) ranked by end-of-day setups — breakouts/
  breakdowns of the recent N-day high/low, gaps, unusual volume vs the trailing
  average, trend vs the 20/50-day MAs, and NR7 squeezes. Pure feature math over
  `db.eod_bars` → **works off-hours & weekends** (no live API). New **🌐 EOD Scan**
  tab (view selector + filters + ⬇ Backfill), `/api/eod/scan`, and a background
  `/api/eod/backfill` (POST starts, GET polls) built on `bhavcopy.backfill(days)`.
- ✅ **EOD option chain (`eod_options.py`)** — resilient option chain from the FO
  bhavcopy option rows (STO/IDO): PCR, max-pain, ATM, OI walls (support/resistance),
  per-expiry summary — **off-hours & when the live NextApi is blocked**. Returns the
  **same shape** as `nse_quote.get_option_chain`, so the existing ⛓ Option-Chain UI
  renders it unchanged; the loader now **auto-falls-back** to EOD when the live chain
  is empty/blocked, with a 🌐 EOD badge. `/api/eod/optionchain/<sym>[?expiry]` +
  `/summary`. *Still open:* delivery% market-wide (UDiFF CM has no delivery column);
  a futures rollover tracker.
- ✅ **Full-universe EOD backtest (`backtest_daily.py source="eod"`)** — runs the 9
  EOD-computable strategies over the WHOLE ingested bhavcopy universe read straight
  from SQLite (`db.eod_bars`/`db.eod_oi`) instead of a curated ~40–260-name NSE pull.
  No network, works off-hours, and produces **thousands of trades** (~1500 names →
  ~5k trades in <1s) so the regime/vol leaderboards, `strategy_of_day` and the
  walk-forward validator become statistically trustworthy (the curated run flatters
  the strategies; the whole market is the honest test). New loaders `_load_live` /
  `_load_eod` share the whole analysis pipeline; `?source=eod` (+ `minPrice`/
  `minValueCr` liquidity floors) on `/api/sim/backtest_daily|strategy_of_day|
  walkforward`; Sim-tab **Backtest source** selector (Live NSE ↔ Full-market EOD).
  *Trade-offs:* Delivery% goes quiet (bhavcopy omits it) and minute re-resolution is
  forced off (needs per-symbol NSE fetches). *Still open:* a scheduled/auto backfill.

**Open (older roadmap, in AGENTS.md):**
- Route paper-trading fills / `get_price` through the broker feed; extend Live tab
  to index/F&O instruments (currently NSE cash equities only).
- Futures rollover tracker (OI shift current→next month near expiry).
- Optional deploy (real WSGI server), server-side backtest logging growth.

**Explicitly NOT doing:** transaction cost/slippage model (AUDIT2 N3 — accepted as
a documented caveat).

## Known limitations

- Real intraday charts + depth are per-symbol NextApi (need stock Referer); depth
  empty outside market hours. OI price-direction coverage partial pre-market.
- All endpoints unofficial; data meaningful only during market hours.
- Only hot-list symbols (~100–150) have a *live intraday* price; any other listed
  name now falls back to its **EOD bhavcopy close** (`bhavcopy.py`) — so paper
  trading/pricing works market-wide, just at last close when it's not live.
- Live tab needs the user's own broker creds (Angel free / Dhan paid); NSE cash only.

---

## Findings & change log (newest first, IST)

### 2026-07-17 — Full-universe EOD backtest (`backtest_daily.py source="eod"`, suite 523 → 530)
- **Why:** the daily backtest (and everything downstream — regime/vol leaderboards,
  `strategy_of_day`, walk-forward) ran over a curated ~40–260-name universe pulled
  one symbol at a time from NSE. That's slow, network-bound, and — worse — a
  **flattering** sample: those are liquid momentum favourites. Meanwhile we already
  ingest the WHOLE market (~2400 cash + ~210 F&O OI) into `db.eod_bars`/`db.eod_oi`
  via `bhavcopy.backfill`. Reading THAT makes the stats statistically trustworthy.
- **How:** split the data layer into `_load_live` (the old per-symbol NSE pull) and
  `_load_eod` (a bulk SQLite read of the ingested universe), both returning
  `(hist, ois, meta)` so the entire analysis pipeline (`_regime_map` /
  `_backtest_symbol` / leaderboards / scorecards / gating) is shared unchanged.
  `_load_eod` applies a liquidity floor (recent price ≥ `minPrice`, turnover ≥
  `minValueCr`), keeps the top-N by turnover, and builds a **continuous near-month
  OI% series** from `db.eod_oi_all()` (new — groups OI rows per symbol across
  expiries/rollovers). `run(..., source="eod")` forces `resolve="daily"` (minute
  re-resolution needs per-symbol NSE fetches → defeats the off-hours premise) and
  returns a helpful "load the bhavcopy first" message when the store is empty.
- **Wiring:** `source` threads through `cached_regime_leaderboard`,
  `cached_walkforward` (both keyed by source so live/EOD boards coexist),
  `strategy_of_day`, and `walkforward.run`. API: `?source=eod` (+ `minPrice`/
  `minValueCr`) on `/api/sim/backtest_daily|strategy_of_day|walkforward`, defaulting
  the EOD universe to the whole market (2500). UI: a **Backtest source** selector on
  the Sim tab (Live NSE ↔ Full-market EOD); the curated-universe / refresh /
  minute-accurate controls grey out for EOD; the result shows a source badge, store
  coverage, and a "thin history — load more sessions" hint.
- **Trade-offs (documented in UI + docstring):** Delivery% goes quiet (the UDiFF CM
  bhavcopy has no delivery column) and exits are daily-only.
- **Verified end-to-end on the live archive:** backfilled 12 real sessions (~3300
  names, ~35k bars), then `source="eod"` scanned **1561 liquid names → 5144 trades in
  0.3s** (vs 156 on the curated 40) — and honestly, the whole-market expectancy sits
  near breakeven where the curated run showed a rosy edge. That gap IS the point.
- **Tests +7** (`db.eod_oi_all`; `_load_eod` filter/rank + OI series; `run(source=eod)`
  end-to-end / empty-store message / forced-daily; walkforward source passthrough;
  app-route source parsing). Suite **523 → 530**, green; lint + JS syntax clean.

### 2026-07-17 — EOD option chain from the FO bhavcopy (`eod_options.py`, suite 507 → 523)
- **Why:** the live option chain rides NSE's anti-bot NextApi — it 403s
  intermittently and reads empty/stale off-hours. But the FO bhavcopy carries every
  contract's EOD OI/close/volume in a plain static ZIP (no anti-bot gate), so we can
  rebuild the chain + analytics resiliently, off-hours and when the live feed is down.
- **How:** `bhavcopy.parse_fo_options(text, underlying)` (PURE) extracts the option
  rows (STO stock / IDO index) that `parse_fo` drops, into a per-expiry chain; new
  `bhavcopy.fetch_fo_text()` gets the raw FO CSV (same walk-back as `fetch_fo`).
  `eod_options.chain()/summary()` assemble it into the **exact shape** of
  `nse_quote.get_option_chain`/`get_option_summary` (rows[{strike,ce,pe}], pcr,
  maxPain, atmStrike, support/resistance walls) + `{eod:true, date}`. **Max-pain is
  delegated to `nse_quote._max_pain`** (one implementation, same rows shape). The
  bhavcopy has no IV/bid-ask/Greeks → those legs come back None (UI shows "—").
- **Caching:** FO text cached module-side (30-min TTL, lock-guarded so cold callers
  don't each re-download the ~MBs file); per-(symbol,expiry) chains memoized 15 min
  (cap 128). Verified end-to-end on the live archive (RELIANCE: 3 expiries, spot
  1296, 45 strikes, PCR 0.59, max-pain 1320, ATM 1300).
- **UI:** the ⛓ Option-Chain loader now **auto-falls-back** to `/api/eod/optionchain`
  when the live chain is empty/blocked (off-hours / NextApi 403), rendering with the
  SAME renderer + a 🌐 EOD badge; the expiry dropdown and all-expiry summary stay in
  EOD mode; IV-rank is skipped (no EOD IV).
- **API:** `/api/eod/optionchain/<sym>[?expiry]` + `/api/eod/optionchain/<sym>/summary`.
- **Tests +16** (`test_eod_options.py` 12 — helpers/_assemble/chain/summary/caching;
  +3 bhavcopy parse_fo_options/fetch_fo_text; +1 app route). Suite **507 → 523**,
  green; lint + JS syntax clean.

### 2026-07-17 — Full-market EOD / swing scanner (`eod_scanner.py`, suite 475 → 507)
- **Why:** the live scanner only sees NSE's ~100–150 intraday hot lists and reads
  all-zeros off-hours, yet we already persist whole-market daily bars in
  `db.eod_bars` (from bhavcopy). This mines that history for swing setups so the
  app has a **market-wide** board that also works nights/weekends — the payoff
  from the bhavcopy data-resilience work.
- **What it computes (per name, from its own daily bars):** proximity to / break
  of the recent **N-day high/low** (breakout/breakdown), **gap** vs prior close,
  **unusual volume** vs the trailing 20-day average, **trend** vs the 20/50-day
  MAs, and an **NR7 squeeze** (today's range a *genuine* contraction — strictly
  narrower than each prior session in the window; a flat series is NOT a squeeze).
  Plus money flow (turnover) and delivery% when present.
- **Design:** all feature math (`_features`/`_tags`/`_score` + per-view predicate &
  sort key) is **pure** → fully unit-tested over hand-built bars. `scan(view,…)`
  is the only impure bit: one `db.eod_bars_all(since=…)` read (grouped by symbol),
  run the pipeline over every name, filter (min price / min turnover / F&O-only),
  rank by view, return top N + coverage. Signals **degrade gracefully** with depth
  (2 bars → %chg/gaps; ~20 → MAs / avg-vol / a real N-day high); missing → None.
- **Views:** setups (bullish composite, default) · breakout · breakdown · gainers ·
  losers · unusual · squeeze · value.
- **Backfill:** `bhavcopy.backfill(days)` ingests the last N sessions' bhavcopies
  into `eod_bars` (lock-guarded, idempotent, dedups holiday walk-backs) to give the
  scanner market-wide *history* (MAs/N-day-high need depth). Runs off a background
  thread via `POST /api/eod/backfill`; the UI polls `GET /api/eod/backfill`.
- **DB:** new `eod_bars_all(since)` (one grouped read for ~2400 names, avoids a
  per-symbol query), `eod_latest_date()`, `eod_oi_symbols()` (local F&O universe).
- **API/UI:** `/api/eod/scan?view=&limit=&minPrice=&minValueCr=&fno=1`; new
  **🌐 EOD Scan** tab with a setup selector, price/value/limit/F&O filters, a
  ⬇ Backfill control (days + live progress), and a coverage line. Prices shown are
  the last EOD **close** (labelled — not live).
- **Verified end-to-end:** against the real DB it already scans the 210 F&O names
  cached by the daily backtest (34.7k bars back to 2025-11), flagging e.g. a 66-day
  high with 2.1× volume; a backfill widens it to the whole cash market.
- **Tests +32** (`test_eod_scanner.py` 25 — helpers/features/tags/score/views/scan/
  status; +3 bhavcopy backfill; +2 db bulk readers; +2 app routes). Suite
  **475 → 507**, green; lint + JS syntax clean.

### 2026-07-17 — Vol-conditioned strategy selection (suite 470 → 475)
- **Why:** the volatility axis was surfaced/attributed but selection still keyed
  only on the directional regime. This closes the loop — the pick now uses **both**
  axes, data-driven from the vol leaderboard we already build.
- **How (marginal-blend, never a joint key):** for the current regime *and* vol
  bucket, blend each strategy's two **marginal** expectancies into one score —
  `blendedR = (1−w)·regimeR + w·volR` with `w=_VOL_BLEND_W=0.4` (regime primary,
  vol a weighted second opinion; falls back to whichever axis exists). We do NOT
  key on `(regime,vol)` jointly — that would starve samples; blending marginals
  keeps both buckets well-populated.
  - `backtest_daily`: new `_blend_r` + `_vol_cells`; `cached_regime_leaderboard`
    now also exposes `volLeaderboard`/`volDist`; `strategy_of_day` ranks by
    `blendedR`, annotates each candidate with `volExpectancyR`/`volClosed`/
    `blendedR`, and the pick reason notes whether the current vol "agrees"/
    "disagrees". Walk-forward `_prefer_robust` still gates the final choice.
  - `strategies._regime_playbook_pick(regime_label, vol_state=None)` blends the
    vol bucket into the LIVE adaptive pick (non-blocking peek); `gen_adaptive`
    passes today's `volState` and mentions it in the reasoning.
  - UI: Strategy-of-the-Day card shows a 🌊 line — "Elevated vol agrees/disagrees:
    +x.xxR → blended +y.yyR (picked on regime+vol)".
- **Backward compatible:** with no vol overlay (thin/absent), `blendedR == regimeR`
  and the pick is unchanged (existing tests untouched).
- **Tests +5** (`_blend_r`, `_vol_cells`, SoD vol flip, SoD no-overlay control,
  playbook vol pick). Suite **470 → 475**, green.

### 2026-07-17 — Volatility-aware regime board (India VIX axis, suite 456 → 470)
- **Why:** the regime engine was **momentum-only** — NIFTY %, breadth, prior-day
  move — with no volatility dimension (VIX was never fetched; PCR captured but
  unused). A Trend-Up on a sleepy 11-VIX tape ≠ a Trend-Up on a 22-VIX tape.
- **What:** added an orthogonal **volatility axis** kept *separate* from the 6
  directional labels (so per-regime sample sizes / leaderboard / walk-forward keys
  stay stable — `volState` is a tint, not a new label).
  - `nse_client.get_index_snapshot` now also pulls **INDIA VIX** from
    `/api/allIndices` (+ `yearHigh`/`yearLow` on every index for a 52-wk percentile).
  - `strategies.detect_regime` → new `vix`, `vixPctile`, `volState`
    (**Calm** <13 / **Normal** 13–18 / **Elevated** ≥18) + richer note. Helpers
    `_vol_state`, `_vix_pctile`. Directional label logic **unchanged**.
  - `backtest_daily`: a VIX-free realized-vol proxy (`_stdev` → 10-session rolling
    stdev of the median move) bucketed by within-window percentile
    (`_vol_state_pct`/`_annotate_vol`) so `_regime_map` days now carry
    `realVol`/`volState`. Every backtest trade is tagged `volAtEntry`. New
    `_vol_leaderboard` (vol × strategy expectancy) via a refactored shared
    `_leaderboard(attr, field, order)`; result gains `volLeaderboard`/`volDist`.
  - `sim.take` tags each live trade's `volAtEntry` (new **DB column** on
    `sim_trades`, additive migration; NULL for legacy rows). `current_regime`
    surfaces the vol axis for free.
  - UI: 🌊 VIX badge on the Sim regime banner + Strategy-of-the-Day card, and a
    **Volatility leaderboard** heat matrix under the regime leaderboard.
- **Instrumentation, not yet selection:** `volAtEntry` is now recorded on every
  sim + backtest trade so vol-*conditioned* strategy selection can later be
  **data-driven**. Today the axis is surfaced/attributed; selection still keys on
  the directional label. Next: prefer vol-appropriate families once samples build.
- **Tests +14** (`test_strategies` +4, `test_client` +1, `test_backtest_daily` +5,
  `test_take` +2, `test_sim_views` +1, `test_db` +1). Suite **456 → 470**, green.

### 2026-07-17 — Paper: option WRITING / short-selling (`paper.py`, suite 452 → 456)
- **Report:** "Cannot sell 1 lot of HCLTECH 1220CE… you hold 0 lot(s). Why? I can sell
  even if I don't hold a long." Correct — `place_option_order` only did buy-to-open /
  sell-to-**close** (no writing), while futures already did both sides.
- **Fix:** options now use **signed qty** (long +, short −) like futures. `BUY` =
  buy-to-open long / buy-to-cover short; `SELL` = sell-to-close long / **sell-to-open
  short (writing)** — no long needed. Cash/margin mirror real F&O: **long** pays the
  premium up front (no margin, max loss = premium); **short (written)** RECEIVES the
  premium but POSTS margin (`OPT_SHORT_MARGIN_RATE=0.15` × underlying-spot notional,
  spot via `nse.get_price` → EOD fallback, else strike). Covering frees margin
  proportionally + realizes P&L; supports adds (weighted-avg premium) and
  flip-through-zero. `portfolio()`: written options are margin-based — MTM as
  `ltp*qty (signed) + margin` so the received premium isn't double-counted (equity is
  correct at entry; short profits as premium decays). Position row shows SHORT/LONG +
  margin; ticket button relabeled **Sell / Write**.
- **Tests:** replaced the obsolete "oversell rejected" with write/cover/MTM/flip/
  insufficient-margin cases (+5, −1). **This is paper money only** (₹10L virtual,
  `paper_state.json`) — no broker, no real orders.

### 2026-07-17 — Data resilience + broaden universe: EOD bhavcopy (`bhavcopy.py`, suite 410 → 452)
- **Problem:** the live NSE JSON is anti-bot/flaky and only ~100-150 hot-list
  names get a price → capped pricing/paper-trading/scanning, and nothing off-hours.
- **Fix:** NSE publishes the daily **UDiFF Common Bhavcopy** as STATIC ZIP/CSV on
  `nsearchives.nseindia.com` (no anti-bot gate). New `bhavcopy.py`:
  - `parse_cm` (cash → {SYMBOL: bar}, equity series EQ/BE/BZ/SM/ST, EQ wins on dup)
    and `parse_fo` (derivatives → near-month futures + `lots` + `underlying`). Both
    PURE; `TradDt` is already `YYYY-MM-DD`. Verified live: 3166 equities, 215 futs.
  - `_download` (404 → None; one force-session retry on other errors),
    `_recent_trading_days` weekend/holiday **walk-back**, `latest()` 30-min cache
    (lock-guarded, no stampede). `eod_price_map`/`eod_close`/`eod_quote`/`lot_sizes`/
    `status`/`ingest_db`.
- **Wiring:** `nse_client.get_price()` now falls back hot-list → NextApi live →
  **EOD close** (any listed symbol is priceable; e.g. `get_price('NELCO')`→848.65).
  `get_lot_sizes()` falls back to the FO bhavcopy lot column. `db.eod_bars_put_bulk`
  bulk-loads ~2400 CM bars in one txn; `ingest_db()` widens the daily-backtest
  universe to the whole market. `app.py`: `/api/eod/status|price|quote|refresh` +
  a startup pre-warm (`_warm_eod`). UI: Sim-tab **⬇ Load EOD (whole market)** button
  + a freshness pill.
- **Tests (+42):** `test_bhavcopy.py` (39 — pure parsers on hand-built UDiFF CSV,
  fetch walk-back/corrupt-zip, `_download` 404/retry, latest-cache, price/lot/quote,
  `ingest_db`, `get_price`/`get_lot_sizes` fallback wiring; module **99%** covered),
  `db.eod_bars_put_bulk` (test_db), 2 EOD route tests (test_app_routes).
- Deliberately dependency-free — reimplements only the bhavcopy slice of
  `jugaad-data` we need, with full control of the format.

### 2026-07-17 — Walk-forward robustness overlay on strategy-of-the-day (suite 405 → 410)
- The regime leaderboard / strategy-of-the-day picked the best **in-sample** edge,
  which can be curve-fit. Now the pick PREFERS a walk-forward-**robust** strategy and
  skips one flagged **overfit** out-of-sample.
- `backtest_daily`: added `cached_walkforward()` (memoised ≤1/6h, lazy-imports
  `walkforward` to dodge the cycle, serialised on the shared run lock),
  `peek_walkforward()` (non-blocking — for the per-minute hot path), `robustness_map()`
  ({strategy_id: verdict} from the holdout `perStrategy`), and `_prefer_robust()`
  (from candidates sorted by in-sample expectancy, take the first whose verdict isn't
  `overfit`/`no-edge`; fall back to the raw top if none pass or no walk-forward yet).
  `UNTRUSTED_VERDICTS = {overfit, no-edge}`.
- `strategy_of_day()`: overlays a robustness verdict on every ranked candidate, uses
  `_prefer_robust` for the pick, and returns new fields — `pick.robustness`,
  `ranked[].robustness`, `walkForward` (ok/trainCut/testN), `skippedOverfit`
  ({id,name,expectancyR,robustness}) when a higher in-sample pick was passed over.
- `strategies._regime_playbook_pick()` (live `gen_adaptive`): same robust-preference
  via the **non-blocking** `peek_walkforward()` (so it never blocks the snapshot loop);
  `gen_adaptive` appends the delegated strategy's walk-forward verdict to its reasons.
- **UI:** strategy-of-the-day card shows a colour-coded `WF: <verdict>` badge + a
  "↩ Skipped X (overfit)" note (`_wfBadge` in `index.html`).
- Cost note: `strategy_of_day` now also triggers a cached (6h) walk-forward backtest
  (120d/60u) on cold poll — same synchronous-on-first-poll pattern as the leaderboard;
  shares the EOD SQLite cache. Live idea generation stays non-blocking (peek only).
- Tests: +5 in `test_backtest_daily.py` (`robustness_map`, `_prefer_robust` ×3,
  strategy-of-day prefers-robust integration). Suite 405 → 410.

### 2026-07-17 — Seven new strategies (library 10 → 17; suite 377 → 405)
- Added seven researched edges to `strategies.py`, each a standard `gen_*` returning
  `_mk_idea` shapes + a `regimeFit`, so they run in the parallel sim, get tracked
  per-regime, and (for the EOD-computable ones) are backtested + walk-forward-vetted:
  - **`fut_basis`** — Futures Basis / Cost-of-Carry: rich premium + rising OI = LONG,
    discount/backwardation + rising OI = SHORT (reads the spot↔future *price* gap, vs
    OI Smart-Money's OI *direction*). Uses `ctx["futures"]` — zero extra fetch.
  - **`rel_strength`** — Relative Strength vs NIFTY: buy leaders / short laggards vs
    the index (live: today's move vs NIFTY; backtest: 5-day stock vs market proxy).
  - **`squeeze`** — Volatility Squeeze (NR7): tightest daily range in 7 then a break.
  - **`gap`** — Gap-and-Go / Fade: regime-tilted opening-gap play (go on trend, fade
    on range), open vs prevClose.
  - **`pcr_extreme`** — PCR Contrarian (per-stock option chain; live-only).
  - **`max_pain`** — Max-Pain Expiry Pin (option chain + expiry-gated; live-only).
  - **`pdhl`** — Prior-Day High/Low Breakout (live-only).
- `build_context()` gained two bounded, cached loaders: **`ctx["daily"]`** (recent
  daily bars, session-cached — immutable intraday; powers squeeze + pdhl) and
  **`ctx["chains"]`** (per-stock PCR/max-pain for a small F&O subset, 5-min TTL;
  powers pcr_extreme + max_pain). Both best-effort so they never stall the per-minute
  snapshot loop; auto-dropped by `_trim_context` (no context_log bloat).
- **`backtest_daily`** now reconstructs `rel_strength` / `gap` / `squeeze` from daily
  bars (STRATS 6 → 9); `_backtest_symbol` takes `day_regime` for the market-relative
  signals. `fut_basis`/`pcr_extreme`/`max_pain`/`pdhl` are in `NOT_COVERED` (live-only).
  Walk-forward picks up the 3 new EOD strategies automatically (reads `bd.STRATS`).
- Tests: +23 in `test_strategies.py` (generators + guard branches + `_dte`), +5 in
  `test_backtest_daily.py` (gap/squeeze/rel_strength signals). Suite 377 → 405.

### 2026-07-16 — Walk-forward out-of-sample validation (`walkforward.py`; suite 363 → 377)
- New **`walkforward.py`** — the credibility check the Sim leaderboard was missing.
  It answers "does the edge survive out-of-sample, or is it curve-fit?" as a **pure**
  function over the daily backtest's trade list (100 % covered):
  - **Holdout split** (`train_frac`, default 0.6): earlier = train, later = OOS. Per
    fixed strategy → in-sample vs OOS expectancy + verdict: `robust` (OOS ≥ 60 % of
    IS), `decaying`, `overfit` (positive IS, negative OOS), `no-edge`, `improving`,
    `insufficient`.
  - **Adaptive-selection test** (the headline): a fixed strategy has no fitted params,
    but the *which-strategy-per-regime* choice is fit on train. So we learn the
    best-per-regime playbook on train, **follow it on test**, and compare to the best
    single fixed strategy OOS + the a-priori regimeFit design. Verdict `adds-value` /
    `no-better-than-fixed` — if switching doesn't beat a fixed strategy OOS, it was
    curve-fit.
  - **Anchored walk-forward folds**: re-learn on expanding train → re-test on the next
    fold, pooled, so the verdict isn't hostage to one arbitrary cut.
- `backtest_daily.run(..., _collect=True)` now optionally returns the flat `trades`
  list + `dayRegime` map (omitted from the normal API payload to keep it lean).
- **`/api/sim/walkforward`** route + Sim-tab **🧪 Walk-forward (out-of-sample)** button
  → `renderWalkforward()` card (adaptive verdict banner + per-strategy IS→OOS table +
  fold table). Tests: `test_walkforward.py` (13, pure) + 1 route test.

### 2026-07-16 — Route/endpoint tests (suite 340 → 363; `app.py` 51 % → 86 %)
- Added `test_app_routes.py` (23): drives **every JSON endpoint** through the
  Flask test client with backends stubbed — boards, per-symbol quote/chart/
  futures/deepdive/option-chain, `/api/ohlc` + `/api/depth` arg parsing, ideas
  journal, alerts, live feed (config/watch/snapshot/seed), paper orders
  (equity/option/futures), the full sim read+write surface (+ `book=` arg),
  backtest arg normalization, logger endpoints + CSV download (404 + send_file),
  and the pure helpers (`_select_live_feed`, `_lan_ip`, `_envflag`).
- `test_app.py` stays focused on middleware (CSRF/token/headers/error contract);
  `test_app_routes.py` owns the route table. Modules imported *inside* handlers
  (`sim`, `ideas_journal`, `notify`, backtests) are stubbed by patching the cached
  module's attributes. Source total ~69 % → **~73 %**.

### 2026-07-16 — Full test-coverage sweep (suite 98 → 340, source ~54 % → ~69 %)
- New suites for the previously thin modules:
  - `test_sim_views.py` (12) — `performance`/`daily_performance`/`day_trades`/
    `analytics`/`_by_regime_r`/`regime_leaderboard`/`strategy_of_the_day`/
    `equity_curves`/settings/`reset` on a temp DB + temp `sim_state.json`.
  - `test_backtest_daily.py` (17) — date parsers, `_features`, `_signals`,
    stop-first `_resolve` (incl. straddle/expiry), `_trade`, `_backtest_symbol`,
    `_classify_regime`/`_regime_map`, regime leaderboard, `_gated`, `_scorecard`,
    `strategy_of_day` (regime + leaderboard stubbed).
  - `test_backtest_strategies.py` (12) — `_epoch_s` (baked-UTC), `_price_map`,
    `_resolve`, `_median`, `_scorecard`, `_equity`, `_leaderboard`,
    `_resolve_ltp`, `_take_entries` (dedup) with `strat.generate` stubbed.
  - `test_client_fetchers.py` (8) — `get_stock_history`/`get_futures_oi_history`
    (raw NSE JSON → clean bars), `get_fno_universe`, `get_lot_sizes` (CSV),
    `get_recommendations` (split/filter/limit), `_underlying_price_map`,
    `_oi_change_map`, `_mean`/`_pct`; all via a fake `requests.Session`/`_fetch`.
  - `test_quote_more.py` (8) — `_leg`, `get_ltp`, `get_token` (exact-EQ vs prefix
    + cache), `get_ohlc` parse + token-not-found, `get_option_expiries`/
    `get_option_summary`, IST-as-UTC clock helpers.
  - `test_ideas_journal.py` (11) — `_move_pct`/`_key`/`_age_min`, sticky
    `_resolve_outcome`, `enrich()` freeze/track/resolve/sort + history views.
  - `+2` to `test_logger.py` — `capture_context` (trimmed gzip cycle) + `_note_error`.
- Result: `nse_client` 48→66 %, `sim` 59→70 %, `nse_quote` 68→82 %,
  `backtest_daily` 15→56 %, `backtest_strategies` 30→71 %, `ideas_journal` →82 %.
  Remaining misses are session/HTTP/websocket/route/thread glue (integration, not
  unit). Installed `coverage.py` to target gaps; `.coverage`/`htmlcov/` gitignored.

### 2026-07-16 — Extensive tests for the new features (suite 62 → 98)
- Added `test_book.py` (11) + `test_notify.py` (25): imbalance/spread math,
  symbol sanitisation/dedupe/cap, per-symbol error isolation; alert config
  precedence (defaults < json < env), `public_status` leaks no secrets, HTML-safe
  formatting, transport fan-out (true-if-any), and full idea/volume detection +
  dedupe + `tick()` gating against a temp DB. `python -m pytest -q` → **98 passed**.
- Pattern for stateful tests: repoint `db.DATA_DIR/DB_FILE`, `db.init()`, restore +
  `gc.collect()` + `rmtree` (Windows file-lock). Monkeypatch transports/`get_quote`/
  `get_recommendations` — never hit the network in tests.

### 2026-07-16 — Process rules + this context file
- Added `.cursor/rules/`: `00-testing` (extensive testing first), `10-no-subagents`
  (never use Task tool — Max Mode admin-disabled ⇒ subagents fall back to Composer
  2.5 Fast), `20-context-file` (read+update this file), `30-documentation` (keep
  README + AGENTS + AUDIT + roadmap in sync). Created this `CONTEXT.md`.
- **Behavior note:** the dev server runs with the reloader ON ("Debug mode: off" +
  "Restarting with stat"), so `.py` edits hot-reload and `templates/index.html`
  re-reads per request. A prior run hit a benign Werkzeug `WinError 10038` on
  socket teardown during reload; it self-recovered.

### 2026-07-16 — Features #1 (order-book) + #2 (alerts) shipped
- Committed `f9af02d`, pushed to `main`. Verified: `/api/depth` 200 (empty after
  hours — no live book, correct), `/api/alerts/status|test` 200, notify formatting +
  `db.alert_seen/alert_mark` dedupe, inline JS `node --check` clean, page renders.
- `nse_quote.get_book_stats(symbols, limit=30)` fans out `get_quote` over ≤6
  workers, reuses the 12s quote cache, omits symbols with no live book.
- `notify.tick(ctx)` is a **fast no-op unless a channel is configured** — zero cost
  for users who haven't opted in. Idea alerts use `get_recommendations()`
  (conviction floor by `min_rating`), volume alerts use `ctx` volgainers/scanner.
