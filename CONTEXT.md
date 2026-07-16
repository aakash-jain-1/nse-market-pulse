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
angel_feed.py        Live feed adapter — Angel One SmartAPI WebSocket (FREE default)
dhan_feed.py         Live feed adapter — Dhan WebSocket (paid data plan)
notify.py            Off-screen alerts (Telegram/webhook) — opt-in, rides snapshot logger
paper.py             Paper-trading engine (equity/options/futures, JSON-persisted)
strategies.py        Strategy library (10 generators) + market-regime detector
sim.py               Multi-strategy forward-tester (per-strategy sims + daily rollup)
intrabar.py          Minute-candle trade resolver (target/stop/MFE/MAE) + resolve_point
backtest_strategies.py  Offline backtester: replays archived context, resolves on OHLCV
backtest_daily.py    Daily-bar historical backtest over real NSE EOD data
db.py                SQLite store (snapshots/IV/context/sim_trades/ideas/alert_log/EOD/min_bars)
snapshot_logger.py   Background logger (snapshots+IV+context+sim+alerts) → SQLite
db_inspect.py        Read-only SQLite inspector CLI
nse_demand.py        Standalone CLI scanner
templates/index.html Entire dashboard UI (HTML+CSS+JS inline)
test_*.py            Unit tests — 340 across 21 suites (client/quote/paper/strategies/sim/backtests/db/app/feeds/notify/…)
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
- Live: `/api/live/config`, `/api/live/watch` (POST), `/api/live/seed/<sym>`, SSE stream.
- Alerts: **`/api/alerts/status`** (no secrets), **`/api/alerts/test`** (POST).
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

- `python -m pytest -q` — **340 tests** (grow it with every change; never shrink it).
  Suites: `test_intrabar.py`, `test_sim.py` + `test_sim_views.py` (DB-backed
  read/aggregation + settings), `test_take.py` (temp DB e2e), `test_backtest.py`,
  `test_backtest_daily.py` + `test_backtest_strategies.py` (signal/exit/regime
  math), `test_ideas.py` + `test_ideas_journal.py`, `test_fetch_cache.py`,
  `test_client.py` + `test_client_fetchers.py` (normalizers + raw-payload
  parsers), `test_quote.py` + `test_quote_more.py`, `test_paper.py`,
  `test_strategies.py`, `test_app.py`, `test_db.py`, `test_logger.py`,
  `test_feeds.py`, `test_book.py`, `test_notify.py`.
- Coverage: `python -m coverage run -m pytest && coverage report -m --omit="test_*.py"`
  → **~69 % of source** (100 % pure math; the rest is network/thread/websocket
  glue tested via stubs). `.coverage`/`htmlcov/` are gitignored.
- Also: `py_compile` for Python, `node --check` on the extracted inline `<script>`,
  and `curl` smoke tests for endpoints.

## Working vs blocked NSE endpoints

- **Working**: live-analysis variations (gainers/`loosers`[sic]), most-active
  volume/value, volume-gainers, OI-spurts underlyings, `liveEquity-derivatives`
  (stock_fut), NextApi `getSymbolData` (quote+depth), `getSymbolChartData`,
  `getSymbolDerivativesData`, charting.nseindia.com OHLCV, option chain.
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
- ⏭ **#3 Deepen the strategy engine** — leaning **walk-forward out-of-sample
  validation** (rolling train/test, per-strategy/per-regime OOS expectancy +
  overfit verdict); optionally new researched edges + sharper regime board.
- ⏭ **#4 Data resilience** — `jugaad-data`/`nsefeed` fallback for flaky endpoints
  + broaden the tradable universe beyond hot lists.

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
- Only hot-list symbols (~100–150) have a live price ⇒ only those are paper-tradable.
- Live tab needs the user's own broker creds (Angel free / Dhan paid); NSE cash only.

---

## Findings & change log (newest first, IST)

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
