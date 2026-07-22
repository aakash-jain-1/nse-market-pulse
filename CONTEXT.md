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
python -m nse_pulse.cli.db_inspect   # read-only SQLite peek (overview / <table> [N] / sql "...")
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
app.py               Root shim → nse_pulse.web.app:main (python app.py unchanged)
nse_demand.py        Root shim → nse_pulse.cli.nse_demand:main
pyproject.toml       Packaging + pytest config (pythonpath=["."], testpaths=["tests"])

nse_pulse/core/
  nse_client.py      NSE session mgmt + hot-list fetch/normalize (CORE) + _fetch micro-cache
  nse_quote.py       Per-stock quote/chart/DEPTH (NextApi) + OHLCV (charting) + get_book_stats
  db.py              SQLite store (snapshots/IV/context/sim_trades/ideas/alert_log/EOD/min_bars)
  intrabar.py        Minute-candle trade resolver (target/stop/MFE/MAE) + resolve_point
  snapshot_logger.py Background logger (snapshots+IV+context+sim+alerts) → SQLite
  paths.py           Repo-root-anchored paths — data/, *_config.json, state JSON, logs/ stay at root
nse_pulse/feeds/
  angel_feed.py      Live feed adapter — Angel One SmartAPI WebSocket (FREE default) + rest_quote/chart/ohlc
  dhan_feed.py       Live feed adapter — Dhan WebSocket (paid data plan)
nse_pulse/sim/
  sim.py             Multi-strategy forward-tester (per-strategy sims + daily rollup)
  strategies.py      Strategy library (17 generators) + market-regime detector
  paper.py           Paper-trading engine (equity + long/short options + long/short futures, margin-based)
  ideas_journal.py   Per-day idea entry/timestamp/live-move journal (Ideas tab)
nse_pulse/eod/
  bhavcopy.py        EOD UDiFF bhavcopy + sec_bhavdata_full delivery% — price/universe fallback + backfill(days)
  deals.py           Bulk/block deals (institutional footprint) from nsearchives CSV — parse/cache, off-hours
  eod_scanner.py     Full-market EOD/swing scanner over db.eod_bars — off-hours, pure math
  eod_conviction.py  EOD conviction board — fuses breakout+delivery+deals+OI+sector RS+chain+rollover; save→ideas
  eod_options.py     Resilient EOD option chain from FO bhavcopy (PCR/max-pain/OI walls); oi_map() analytics
  eod_scheduler.py   Auto post-close EOD refresh — pure should_run() + block-aware daemon, persists in eod_meta
  conviction_calibration.py  Does stacking pay? per-pillar lift + honest verdict; pillar_weights() feeds back (adaptive)
  rollover.py        Futures rollover tracker off the FO bhavcopy — roll%/cost/basis/net-OI, ranked
  sector_scan.py     Sector relative-strength (rotation) board over db.eod_bars — RS vs market median
  sectors.py         Curated NSE symbol→sector map (17 sectors, ~303 names) — static data
nse_pulse/backtest/
  backtest_daily.py  Daily-bar historical backtest — source="live" (curated NSE) OR "eod" (whole universe)
  backtest_strategies.py  Offline backtester: replays archived context, resolves on OHLCV
  walkforward.py     Walk-forward out-of-sample / overfit validation (pure over trades)
  portfolio_backtest.py  Portfolio-level backtest — replay through a real book → equity curve + CAGR/DD/Sharpe
nse_pulse/web/
  app.py             Flask routes (thin) + startup wiring (main()) + security guard/headers
  observability.py   Per-request access log (entry→exit/timing) + opt-in OpenTelemetry (OTLP)
  notify.py          Off-screen alerts (Telegram/webhook) — opt-in, rides snapshot logger
  templates/index.html   Entire dashboard UI (HTML+CSS+JS inline)
nse_pulse/cli/
  nse_demand.py      Standalone CLI scanner
  db_inspect.py      Read-only SQLite inspector CLI

tests/               Unit tests — 826 across 36 suites; import `from nse_pulse.<sub> import <mod>`
docs/                AUDIT.md (round 1) + AUDIT2.md (round 2)
data/market.db       (gitignored) SQLite; sim_state.json / paper_state.json / ideas_journal.json (gitignored, repo root)
*.example.json       Config templates (angel/dhan/notify) → copy to gitignored real files
```

## Architecture notes

- **NSE session (`nse_client.py`)**: NSE blocks plain HTTP. We keep a warmed
  `requests.Session` (browser UA + Referer + homepage/market cookies), reused,
  rebuilt on failure and after a TTL. Built **outside** the lock then swapped in
  (M3). `HTTPAdapter(pool_connections=16, pool_maxsize=32)` avoids pool-full warns.
  **`_fetch()`** has a path-keyed **15s TTL micro-cache** (shared read-only object;
  callers must not mutate) that cut duplicate hot-list GETs ~72%/cycle.
- **Global NSE request pacer (`nse_client._PacedSession`/`_pace`/`pacer_stats`)**: the
  15s cache + TTLs cut *duplicate* reads but nothing smoothed **bursts** — a cold
  `snapshot_logger`/`build_context()` cycle fans out across 6-8 worker pools and fires
  dozens of near-simultaneous NSE connections (the per-IP burst Akamai's rate detector
  flags; why the block builds over time and clears on a network switch). Because **every**
  NSE hit (live `_fetch`, per-stock `nse_quote._sget`/`_charting_get`, archive
  `bhavcopy._download`) shares the one warmed session, `_build_session()` returns a
  `_PacedSession(requests.Session)` whose `send()` throttles **all** of them at one choke
  point: at most **`_NSE_MAX_CONCURRENCY=4`** in flight, request STARTS **`_NSE_MIN_GAP=0.20s`
  (+jitter)** apart, and a **soft `_NSE_SOFT_RPM=120`/min** ceiling (sliding-window `deque`,
  same shape as `angel_feed._candle_throttle`). Callers need no changes. Turns the burst
  into a steady, browser-like stream; foreground UX barely changes (movers = ~7 endpoints,
  modal/Live are broker-first) since the heavy fan-outs are background.
- **Optional TLS-fingerprint impersonation + auto-failover (Phase 2, `curl_cffi`)**: the
  pacer + fuller headers smooth the *rate* and dress up the *headers*, but plain `requests`
  still presents a Python TLS/HTTP2 fingerprint (JA3/JA4) Akamai can flag as "not a browser"
  regardless of pacing. When the **optional** `curl_cffi` dep is installed, `_build_session()`
  can return a **`_PacedCffiSession(_cffi.Session)`** that presents a **real Chrome handshake**,
  paced through the **same** `_pace()`/`_NSE_GATE` gate (via `request()` instead of `send()`)
  so burst-smoothing still applies. **Policy** is `NSE_TLS_IMPERSONATE`:
  `off/none/0` = never; a literal profile (`chrome124`) = always; **`auto` (DEFAULT)** =
  **self-healing failover** — run the light pure-requests transport normally and only escalate
  to impersonation once the WAF ladder crosses `_AUTO_FAILOVER_AT` (env `NSE_TLS_AUTO_AT`,
  default **2**) consecutive blocks (`_auto_failover_armed()`), then **revert automatically**
  once the ladder goes cold (`_block_ladder_expired()`), no restart/manual toggle. Because
  `_impersonate_profile()` is read at each session build and sessions rebuild after the
  cooldown/TTL, the switch happens on its own. Fully transparent: `curl_cffi` responses expose
  the same `.get/.json/.status_code/.text/.raise_for_status`, so `_fetch`/`nse_quote`/
  `bhavcopy` are unchanged; a bad profile / build error falls back to `_PacedSession`. If the
  dep is absent it's a no-op. `pacer_stats()` exposes **`impersonate`** (profile in effect NOW,
  `null` until armed) and **`impersonateMode`** (the policy). Enable with `pip install curl_cffi`.
  **Live-verified** against NSE end-to-end (real Chrome handshake returned live lists).
- **Akamai/WAF block backoff (`nse_client.blocked_for`/`note_block`/`is_blocked_response`)**:
  NSE fronts everything with Akamai, which returns **HTTP 403 "Access Denied"
  (edgesuite.net, "Reference #…")** to EVERY request once our IP looks bot-like.
  Retrying — *especially* rebuilding the session, which itself GETs the homepage +
  market page — pours more requests into the block and lengthens it. So the **first
  403 starts a 10-min cooldown** (`_BLOCK_COOLDOWN=600`), and **consecutive blocks
  escalate** it — `note_block` doubles the pause each time (600 → 1200 → 2400 …, capped
  at `_BLOCK_MAX=3600`) via `_cooldown_for(_block_count)`, resetting the ladder only after
  a genuinely clean gap (backing off *harder* when the edge is still hot is what lets it
  cool down, vs re-poking it every 10 min). During a cooldown ALL NSE traffic
  short-circuits: `_fetch()` serves stale cache or fails fast (no NSE hit, no rebuild),
  `get_session()` reuses the stale session instead of warming up, `bhavcopy._download`
  returns `None` without retrying, and every per-stock call in `nse_quote` (via `_sget`)
  does the same. This is a **shared** cooldown — a block seen by the live API, the static
  archives, *or* the per-stock NextApi gateway pauses all of them. It can't un-block us (only time / a
  new IP does), but it stops us **re-earning or extending** the block. **Cause of a
  block:** bursty automated fetches — mainly repeated full-history **backfills** — plus
  live polling on the same IP. **Block-resilience UX:** `/api/health` reports
  `nse.blockedForSec` (the shared cooldown), the dashboard shows a **countdown banner**
  ("NSE has temporarily rate-limited this network… showing cached/EOD… auto-resuming in
  m:ss"), and **`/api/quote/<sym>` falls back to the EOD bhavcopy close** (`stale:true`,
  `source:"eod-bhavcopy"`) instead of erroring — so the stock modal still works during a
  block. All live scanner lists already serve their stale `_fetch` cache during a block.
  `/api/health.nse` = **`pacer_stats()`**: `blockedForSec`, `blockCount` (repeat blocks →
  the banner adds a "backing off longer" note), `cooldownSec`, `reqLastMin` (pacer window),
  `concurrency`/`minGap`/`softRpm`/`impersonate` (curl_cffi profile in effect, or null)/
  `impersonateMode` (the configured policy: auto/<profile>/off/null)/`endpoints` (per-endpoint
  request budget: hits per endpoint path over the last min/hour, ranked — shows which calls eat
  the most quota so trims are data-driven; `_record_endpoint` tags every hit in the pacer).
  **Header hardening:** `HEADERS` now sends modern-Chrome
  client hints (`sec-ch-ua*`, `Sec-Fetch-*`, `Accept-Encoding` — brotli only if decodable)
  matching the UA major, and the two cookie warm-ups send navigation-shaped `_NAV_HEADERS`
  so the handshake looks like a real browser landing rather than a bare script.
- **NextApi gateway (`nse_quote.py`)**: the old `/api/quote-equity` is 403 and
  `/api/chart-databyindex` is empty. The site's `/api/NextApi/apiClient/GetQuoteApi`
  (with a stock-specific Referer) unlocks per-stock quotes, **5-level depth**
  (`getSymbolData` → `orderBook`), and real intraday points. `_cache` is capped
  (`_CACHE_MAX=2000`). **All** NSE GETs here funnel through **`_sget()`**, which
  honours the shared WAF cooldown (§ Akamai block backoff) — during a block it
  short-circuits without hitting NSE and never does its force-rebuild retry; a 403
  records the block. Warm-up visits (`_warm`/`_oc_warm`/`_deriv_warm`) skip while blocked.
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
  daily-backtest universe to the whole market. **`backfill(days, pace=0.5)`** loops
  sessions with a **jittered pause per day** (`[pace, 2*pace)`) so a big history load
  doesn't burst the archive (the #1 way to trip the WAF), and **aborts early** with a
  `blocked` flag if `nse_client.blocked_for()` fires mid-run. Dependency-free
  (reimplements the slice of `jugaad-data` we need). The UDiFF CM file **omits delivery%**, so
  `ingest_db()` also pulls the **`sec_bhavdata_full`** plain CSV (`parse_sec_delivery`/
  `fetch_sec_delivery`) and merges per-symbol `delivPct`/`delivQty` **for the same
  session only** (never stamps a walked-back day's delivery onto today) — re-activating
  the delivery strategy and the scanner's accumulation view market-wide.
- **Deals (`deals.py`)**: NSE publishes the latest session's **bulk & block deals**
  (funds/HNIs/promoters — a legally-disclosed institutional footprint) as tiny plain
  CSVs on nsearchives (`/content/equities/bulk.csv`, `block.csv`; block ships a
  "NO RECORDS" sentinel on quiet days). `parse_deals()` is pure; fetch reuses
  `bhavcopy._download` + a 30-min lock-guarded cache. `by_symbol()` powers a cheap
  scanner cross-reference (🐋 badge + score bonus when `with_deals=1`); `recent()`/
  `status()` back `/api/eod/deals`. Off-hours friendly.
- **Conviction board (`eod_conviction.py`)**: FUSES the independent EOD signals —
  breakout of the N-day high, delivery% accumulation, bulk/block-deal footprint,
  F&O OI buildup, volume, trend — into ONE ranked "tomorrow's watchlist". The core
  idea is **confirmation stacking**: a pick is ranked by how many INDEPENDENT
  pillars agree first, then the blended score, so a 4-way-confirmed name beats a
  single strong signal. Pillar logic (`_pillars_long`/`_short`), OI classification
  (`_oi_state`: price×OI → long/short buildup / covering / unwinding) and the
  volatility-scaled 2R plan (`_plan`) are pure + tested. `board()` reuses
  `eod_scanner._features` + `db.eod_bars_all`/`eod_oi_all` + `deals.by_symbol`.
  `save()` writes picks into the `ideas` table (dated to the EOD session, reasons
  prefixed "🏆 EOD conviction") WITHOUT clobbering a live idea. `notify.send_digest()`
  pushes the top picks off-screen. `/api/eod/conviction[/save|/digest]`; 🏆 Conviction tab.
- **Conviction calibration (`conviction_calibration.py`)**: closes the loop — does
  the confirmation-stacking thesis actually hold on realized results? Reads back
  the saved conviction ideas (`db.ideas_all`, tag-filtered), scores each by its
  candle-accurate `TARGET`/`STOP` outcome, and buckets them: win rate by **pillar
  count** (do 4-signal picks beat 2-signal?), by rating/direction, the **per-pillar
  lift** (win rate WITH vs WITHOUT each of breakout/trend/delivery/volume/oi/deal/
  sector/option), and the **option-⚠️ warning impact** (does the soft-veto flag
  worse trades?). All maths pure + tested; `report()` = one DB read. Emits an honest
  one-line `verdict`. `/api/eod/conviction/calibration?days=N`; 📊 Calibration button
  (modal) on the 🏆 Conviction tab.
- **Adaptive weighting (calibration → scoring)**: closes the loop — the board can feed
  each pillar's measured edge BACK into its own scoring. `conviction_calibration.
  pillar_weights()` turns each pillar's realized win-rate lift into a clamped
  `[0.5,1.5]` scoring multiplier, **shrunk toward 1.0 by sample size** and neutral until
  a pillar has enough resolved history on both sides (`pillar_of()` is the one shared
  label→key map). `eod_conviction.board(adaptive=True)` scales pillar weights by it via
  `_apply_weights` — but the **confirmation COUNT (primary sort key) is untouched**, so
  weighting only re-orders WITHIN a tier, never overriding how many signals agree.
  Opt-in (`?adaptive=1`, ⚖️ Adaptive toggle, OFF by default); the board echoes the
  applied `adaptiveWeights` and the Calibration modal shows each pillar's earned "→ weight".
- **Futures rollover (`rollover.py`)**: near-vs-next month futures from the EOD FO
  bhavcopy. `bhavcopy.parse_fo_futures_all()` keeps ALL expiries per symbol (parse_fo
  keeps only the nearest); `rollover.board()` computes per name **rollover%** (nextOI /
  (near+next) — rising into expiry = positions CARRIED forward), **roll cost** (next−near
  spread; + contango / − backwardation) + annualized, near-month **basis** to spot, and a
  net-(near+next)-OI **state** (long/short buildup vs covering/unwinding). Each name gets
  a CROSS-SECTIONAL `rolloverRank` (percentile vs the market today — meaningful without a
  rollover history). Reuses `eod_options._fo_text()` so the FO file is fetched/cached ONCE
  for both views. Sharpest in the expiry week. `/api/eod/rollover`; 🔄 Rollover tab.
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
- **Snapshot logger (`snapshot_logger.py`)**: daemon loop every **90s during
  market hours** (Mon–Fri 09:15–15:30 IST; env `NSE_LOG_INTERVAL`, floor 30s — raised
  from 60 to trim the dominant per-minute NSE fan-out). Each cycle: demand+volgainers
  snapshot → SQLite; ATM IV every 5 min (`NSE_LOG_IV_INTERVAL`);
  `sim.build_ctx/update/take/daily_rollup`; context archive every 5 min
  (`NSE_LOG_CONTEXT_INTERVAL`); **`notify.tick(ctx)`**. Isolated sub-tasks + heartbeat +
  watchdog (`STALE_AFTER` scales with `INTERVAL`) + session-rebuild self-healing;
  `health()`. The heavy per-cycle cost is `build_context`'s per-symbol quote+candle
  fan-out, bounded to **`NSE_CTX_CANDIDATES=30`** liquid names (`strategies._CTX_CAND`,
  floor 10; was 45).

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
  minValueCr=&fno=1&deals=1`** (full-market swing scanner; `view=delivery` for
  accumulation, `deals=1` cross-references bulk/block deals),   **`/api/eod/deals?kind=
  bulk|block&limit=`** (+ `?status=1` for freshness), **`/api/eod/conviction?limit=&
  minPrice=&minValueCr=&minPillars=&fno=1&deals=0`** (stacked-conviction board) +
  **`/conviction/save`** (POST → persist to Ideas) + **`/conviction/digest`** (POST →
  off-screen digest) + **`/conviction/calibration?days=N`** (did the stacking pay? —
  realized win rate by pillar count + per-pillar lift + earned weights); the board
  itself takes **`?adaptive=1`** to apply those weights. **`/api/eod/rollover?minPrice=&
  minValueCr=&limit=&sort=rollover|rollcost|basis|dte`** (futures rollover% / roll-cost /
  basis / OI-state, cross-sectionally ranked). **`/api/eod/backfill`** (POST {days} starts a background
  history load — now also merges delivery%; GET polls), **`/api/eod/optionchain/
  <sym>[?expiry]`** + **`/summary`** (resilient EOD option chain from the FO bhavcopy
  — PCR/max-pain/OI walls, off-hours).
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

- `python -m pytest -q` — **826 tests** (grow it with every change; never shrink it).
  Suites: `test_intrabar.py`, `test_sim.py` + `test_sim_views.py` (DB-backed
  read/aggregation + settings), `test_take.py` (temp DB e2e), `test_backtest.py`,
  `test_backtest_daily.py` + `test_backtest_strategies.py` (signal/exit/regime
  math), `test_ideas.py` + `test_ideas_journal.py`, `test_fetch_cache.py`,
  `test_client.py` + `test_client_fetchers.py` (normalizers + raw-payload
  parsers),   `test_nse_client.py` (global request pacer: min-gap/soft-RPM/concurrency
  + escalating WAF cooldown + browser headers + `pacer_stats` + optional curl_cffi
  impersonation: env toggle/fallback/build-session transport pick + auto-failover
  arm/revert on the block ladder + per-endpoint request budget), `test_quote.py`
  + `test_quote_more.py`, `test_paper.py`,
  `test_strategies.py`, `test_bhavcopy.py` (EOD UDiFF + sec_bhavdata_full delivery
  parsers + fetch walk-back + price/lot fallback + delivery-merge wiring),
  `test_deals.py` (bulk/block parse incl. NO-RECORDS + cached fetch),
  `test_eod_scanner.py` (incl. delivery view + deals xref),
  `test_eod_conviction.py` (OI-state quadrants / pillars / 2R plan / stacked board /
  save-skip), `test_conviction_calibration.py` (pillar/confirmation parsing +
  bucket stats + per-pillar lift + verdict + report() on a temp DB), `test_app.py`
  (middleware) + `test_app_routes.py` (every endpoint via the Flask test client),
  `test_db.py`, `test_logger.py`, `test_feeds.py`, `test_book.py`, `test_notify.py`.
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
  `/summary`, plus `rollover.py` (near→next rollover% / roll cost / basis / OI-state).
- ✅ **Delivery% + bulk/block deals market-wide (`bhavcopy` delivery merge + `deals.py`)**
  — the UDiFF CM bhavcopy omits delivery%, so `ingest_db()` now also ingests
  **`sec_bhavdata_full`** and merges per-symbol `delivPct`/`delivQty` (same-session
  only) into `eod_bars`. This **re-activates the delivery strategy** in the EOD
  backtest (was 0 trades → now fires, regime-gated **+0.23R** on a real 23-session
  run) and adds an **Accumulation (high delivery%)** scanner view + a Deliv% column
  with a "+Npp vs avg" spike hint. `deals.py` fetches NSE **bulk/block deals** (the
  institutional footprint) and the scanner cross-references them (`?deals=1`) to flag
  🐋 rows a big player traded (+ score bonus). `/api/eod/deals`.
- ✅ **EOD conviction board (`eod_conviction.py`)** — fuses the independent EOD signals
  (breakout / delivery accumulation / bulk-block deal / OI buildup / volume / trend /
  leading-lagging sector / option chain / **futures rollover**) into ONE ranked
  "tomorrow's watchlist" via **confirmation stacking** (ranked by #signals that agree,
  then blended score). Each pick carries a volatility-scaled 2R plan. **Save→Ideas
  history** (durable watchlist, never clobbers a live idea) and an **off-screen digest**
  via `notify.send_digest()`. 🏆 Conviction tab; `?rollover=0`/`?options=0`/`?deals=0`
  disable a fuse, `?adaptive=1` weights pillars by realized edge. Verified e2e on ~3,300
  real names (HIRECT = breakout + 26.9× vol + 🐋 bulk deal, etc.).
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
  *Trade-offs:* minute re-resolution is forced off (needs per-symbol NSE fetches).
  *Still open:* a scheduled/auto backfill.
- ✅ **Portfolio-level backtest (`portfolio_backtest.py`)** — replays the daily-backtest
  trades through a REAL book (finite capital, concurrent-position cap, risk/equal
  sizing) → equity curve + CAGR / max-DD / Sharpe / profit-factor, overall + per
  strategy. Turns per-trade R into "could I actually have traded this?". Pure
  `simulate()`; `run()` sources trades from `bd.run(_collect=True)`; same-day signal
  contention is **conviction-ranked** (every `bd` trade carries an entry-time `score`);
  open positions are **marked to market** on daily closes (`bd` also returns traded
  symbols' `closes`) for true intra-trade drawdown. *Feature complete.*
- ✅ **Conviction calibration + adaptive weighting (`conviction_calibration.py`)** —
  measures whether the board's confirmation-stacking actually pays on realized outcomes:
  reads back the saved conviction ideas, scores their candle-accurate `TARGET`/`STOP`
  results, and reports win rate by **pillar count** (do 4-signal beat 2-signal?), by
  rating/direction, the **per-pillar lift** (WITH vs WITHOUT each of the 8 pillars), and
  the **option-⚠️ warning impact**, with an honest verdict. `/api/eod/conviction/calibration`;
  📊 Calibration modal on the 🏆 Conviction tab. Then **closes the loop**: `pillar_weights()`
  turns each measured lift into a clamped, sample-shrunk scoring multiplier that the board
  applies via `board(adaptive=True)` / **⚖️ Adaptive** toggle — re-ordering within a
  confirmation tier without ever touching the stacking count. *Feature complete.*

**Open (older roadmap, in AGENTS.md):**
- Route paper-trading fills / `get_price` through the broker feed; extend Live tab
  to index/F&O instruments (currently NSE cash equities only).
- Optional deploy (real WSGI server), server-side backtest logging growth.
- ✅ *(done)* Futures rollover tracker (`rollover.py`) — near→next OI shift + roll cost.

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

### 2026-07-22 — Fix: dashboard bind blocked ~85s by the live-feed scrip download (suite 826, unchanged)
- **Why:** `python app.py` took ~85s to start serving. `web/app.py:main()` called `live_feed.start()`
  synchronously before the banner + `app.run()`, and Angel's `start()` first runs `_load_scrip()` —
  `requests.get(SCRIP_URL, timeout=60)` for the **full instrument master** (a large JSON) — before it
  spawns its supervisor thread. So the socket bind waited on a network download every boot.
- **What:** start the feed on a daemon thread — `_th.Thread(target=live_feed.start, daemon=True,
  name="live-feed-start").start()`. Uses `_th` (not bare `threading`) because `main()` has a
  function-local `import threading` further down that would otherwise shadow the name (`UnboundLocalError`).
  The WS login/reconnect already ran inside `_supervise` (daemon); only the synchronous scrip fetch sat on
  the critical path. No-op for users without broker creds, so behaviour is otherwise unchanged.
  `snapshot_logger.start()` / `eod_scheduler.start()` were already non-blocking (they only spawn daemons).
- **Verified:** fresh timed boot **bound in ~1s** (was ~85s), `/api/health` 200 in ~14ms; the banner now
  prints with no preceding scrip/login lines. Full suite **826 green**.

### 2026-07-22 — Restructure: flat root → domain-grouped `nse_pulse/` package (suite 826, unchanged)
- **Why:** ~30 modules + templates all sat at the repo root; hard to navigate and to reason about
  boundaries. Standardised into a package **without changing behaviour or the run commands**.
- **What:** `git mv` the 30 modules into `nse_pulse/{core,feeds,sim,eod,backtest,web,cli}` (history
  preserved) + `templates/` → `nse_pulse/web/templates/`. A name-keyed codemod rewrote **322 import
  lines across 62 files** to `from nse_pulse.<sub> import <mod>` (plus one `from sim import …` →
  `from nse_pulse.sim.sim import …`). New `nse_pulse/core/paths.py` (`PROJECT_ROOT` / `DATA_DIR` /
  `root()`) so `data/market.db`, the `*_config.json`, `sim_state.json` / `paper_state.json` /
  `ideas_journal.json` and `logs/` still resolve to the **repo root** (repointed db / sim / paper /
  ideas / angel / dhan / notify / snapshot_logger + app logging off `os.path.dirname(__file__)`).
  `app.py`'s `__main__` block became `web/app.py:main()`; root `app.py` + `nse_demand.py` are now thin
  shims → package `main()`. `db_inspect` runs via `python -m nse_pulse.cli.db_inspect`. Tests moved to
  `tests/`, `AUDIT*.md` to `docs/`, added `pyproject.toml` (pytest `pythonpath=["."]`, `testpaths=["tests"]`).
- **Fix (tests):** `test_bhavcopy._patch_nse_module` swapped `sys.modules["nse_client"]`; since `_download`
  now binds `from nse_pulse.core import nse_client`, it patches the `nse_client` attribute on the
  `nse_pulse.core` package (+ `sys.modules["nse_pulse.core.nse_client"]`) instead.
- **Verified:** full suite **826 green**; smoke-ran `python app.py` (served `/api/health` 200, banner shows
  `Serving Flask app 'nse_pulse.web.app'`) and `python nse_demand.py gainers` (live table). Import graph +
  repo-root path resolution confirmed for all 31 modules.

### 2026-07-22 — Local OpenTelemetry backend (`docker-compose.otel.yml`, grafana/otel-lgtm)
- **Why:** the OTel export path (added earlier) had no backend to view it in; Docker is now available.
- **What:** committed `docker-compose.otel.yml` running `grafana/otel-lgtm:0.11.14` — ONE container with
  Grafana + Tempo (traces) + Prometheus/Mimir (metrics) + Loki (logs) + a built-in OTel Collector, ports
  3000 (Grafana UI) / 4317 (OTLP gRPC) / 4318 (OTLP HTTP). `docker compose -f docker-compose.otel.yml up
  -d`, then run the app with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`, explore at
  http://localhost:3000. Ephemeral by design (optional `./.otel-data` volume, gitignored).
- **Verified end-to-end (live):** launched an OTel-enabled instance, drove ~21 requests, and confirmed via
  Grafana's datasource proxy — **Tempo: 21 traces** (`GET /api/health`, `/api/recommendations`, …),
  **Prometheus: `http_server_duration_milliseconds_{bucket,count,sum}` + `http_server_active_requests`**,
  **Loki: logs** — all tagged `service.name=nse-market-pulse`. The access line's `trace=` now shows the
  real 32-hex id instead of `-`. No app code change; docs only (no test-count change).

### 2026-07-22 — /api/recommendations non-blocking + parallel scanner fan-out (suite 822 → 826)
- **Why (surfaced by the new access log):** `/api/recommendations` occasionally took **tens of
  seconds** (`GET /api/recommendations 200 43237ms` right after a reload). Two causes: (1) on a cold
  cache `get_scanner(250)` fetched **7 hot-lists sequentially** through the pacer, so under boot
  contention the paced round-trips stacked end-to-end; (2) the endpoint is polled constantly (Ideas tab
  + new-idea alert), yet **every** poll landing on an expired 12s cache **blocked** on the whole sweep.
- **Fix 1 — parallel fan-out (`_gather` in `nse_client.py`).** `get_scanner` now fetches its 7 hot lists
  CONCURRENTLY (the pacer still bounds the true rate; this just overlaps network latency). Benefits every
  caller (`/api/scanner`, `strategies.build_context`, CLI). Aggregation stays ordered/deterministic.
- **Fix 2 — stale-while-revalidate `get_recommendations`.** Split into `_reco_compute()` +
  `_maybe_refresh_reco()`: **cold** (no data) computes once (single-flight, dedupes concurrent
  first-calls); **stale** (have data, expired) serves the last set INSTANTLY and refreshes in a daemon
  thread. Journaling moves to the background pass. The endpoint never blocks in steady state.
- **Result (live-verified on :5056):** cold **43s → 2.5s**; the post-expiry (stale) poll that used to
  block now returns in **~2.5ms**; background refresh swaps in the fresh set. Tests **+4**
  (`_gather` collect/failure-isolation/concurrency + reco SWR serves-stale-then-refreshes). Suite **822 → 826**.

### 2026-07-22 — API request logging + OpenTelemetry (CNCF) instrumentation (suite 808 → 822)
- **Why:** wanted to see each API request in the terminal — entry/exit time, duration, status — and to
  follow a standard for metrics rather than a bespoke logger.
- **What (new `observability.py`, wired via one line in `app.py`):** two layers.
  1. **Terminal access log (always on, no deps).** One line per request on stdout (and into
     `logs/app.log`): `HH:MM:SS.mmm -> HH:MM:SS.mmm  METHOD  /path  status  Nms  ip=…  size  trace=…`.
     The `?token=` secret is redacted. Registered before the security guard so the timer is armed even
     for blocked (401/403) requests, and a raising view is still logged (as 500) via `teardown_request`.
  2. **OpenTelemetry (opt-in, the CNCF standard).** Auto-instruments Flask → server spans + the standard
     `http.server.*` RED metrics; optionally the `requests` lib and our logs. Exports over OTLP/HTTP when
     `OTEL_EXPORTER_OTLP_ENDPOINT` is set (e.g. Jaeger/Tempo/Grafana), or to the console with
     `OTEL_CONSOLE=1`. Imports OTel lazily and **no-ops if the packages are missing**, so the app always
     boots; when active, the trace id shows up in the access line for correlation.
- **Notes / safety:** `requests` instrumentation is **off by default** (`OTEL_INSTRUMENT_REQUESTS=1` to
  enable) because it injects W3C `traceparent` headers into every outbound call and NSE's Akamai WAF is
  header-sensitive — we don't touch NSE calls unless asked. Env: `OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_CONSOLE`, `OTEL_SERVICE_NAME`, `OTEL_SDK_DISABLED`, `OTEL_INSTRUMENT_REQUESTS`.
- **Result (live-verified):** terminal now prints e.g.
  `11:09:38.080 -> 11:09:38.130  GET  /api/sim/summary?book=fno&token=***  200  49.7ms  ip=127.0.0.1  8.5kB  trace=-`.
  Tests **+13** (`test_observability.py`: redact/human_size/clock/format, OTel gating idle+disabled,
  access-log hook status/timing/redaction, 500-via-teardown, idempotent init).
- **Gotcha fixed — "I don't see the APIs in the terminal."** Werkzeug binds with `SO_REUSEADDR`, so on
  Windows a second `python app.py` silently *shares* port 5055 instead of failing; requests then get
  routed nondeterministically to whichever instance, and the fresh terminal stays blank. Added a
  connect-probe **preflight** (`_port_in_use`) that, on the first (non-reloader) launch, fails fast with
  a clear "port already in use → stop it, or PORT=5056" message instead of a phantom second instance
  (+1 test). Suite **808 → 822**.

### 2026-07-22 — SIM summary made fully non-blocking (first F&O load is instant) (suite 804 → 808)
- **Why:** after the reprice throttle, repeated SIM polls were instant but the FIRST (cold)
  `summary?book=fno` still blocked 8–25s, and *every* poll landing after a >30s idle gap stalled 6–9s.
  Two residual synchronous NSE hops in `summary()`: (1) the reprice fan-out (cold hot-list map +
  charting tokens → big per-symbol quote fan-out), and (2) `current_regime()` → `get_index_snapshot()`,
  whose cache is only ~30s — so an expired index fetch queued behind the reprice on the global pacer.
- **What (`sim.py`):**
  - **Reprice is async.** `summary()` calls `_maybe_reprice_async()`, which kicks the fan-out on a
    daemon thread and returns immediately — the tab renders the last reprice from the DB at once and the
    fresh numbers land on the next poll. `_reprice_running` (under `_update_gate`) + `_UPDATE_TTL` ensure
    only one runs at a time; the SYNCHRONOUS `update()` (snapshot logger MTM) is unchanged. Shared body
    factored into `_reprice_open_trades()`.
  - **Regime is stale-while-revalidate.** `current_regime()` now serves the last snapshot INSTANTLY and
    recomputes in the background when stale (`_REGIME_TTL` 30s, `_regime_cache`/`_refresh_regime`); a
    cold start returns a cheap neutral regime and the real one lands on the next poll. This was the LAST
    synchronous NSE hop in the summary path.
- **Result (live-verified):** cold first `summary?book=fno` **0.04s** (was 22s); after a 35s idle gap
  **0.03s** (was 6–9s); `cash` 0.1s, `/regime` 0.001s. Regime badge fills with real data (Trend-Down,
  NIFTY/VIX) a beat after load. Tests **+4** (async kick non-blocking + reprices + skips within TTL;
  regime non-blocking on cold + serves-stale-then-refreshes). Suite **804 → 808**.

### 2026-07-22 — Fix: boot warm-up starved the app ("first call won't load") (suite 801 → 804)
- **Why:** a clean restart during market hours confirmed the acute boot hang — for ~5 min after
  startup even the local `/api/health` timed out. Cause: `_warm_sim` eagerly runs
  `cached_regime_leaderboard` + `cached_walkforward`, a **daily-bar backtest over a LIVE universe**
  (~60 paced NSE fetches + a heavy CPU pass). At boot, in market hours, that burst saturates the
  pacer and starves the dev server before the user does anything.
- **What (`app.py`):** extracted `_warm_sim_pass()` (module-level, testable) that (1) **skips during
  market hours** — the Sim tab computes the strategy-of-day card lazily on first visit (server-cached
  ~6h), idea generation falls back to the un-warmed board until then — and (2) bails on a WAF
  cooldown. Off-hours it still warms both caches (no contention, primes next session). The boot
  thread now also **defers** the pass by `SIM_WARM_DELAY_SEC` (default 60s) so the first
  page/poll is served first.
- **Result (live-verified):** after the fix a fresh boot no longer runs the live burst in market
  hours; steady-state settled at **reqLastMin ~55/120** (was pegged at 120), `symbolHistoricalData`
  27/min (was ~63), `symbolsDynamic` ~1/min, `/api/health` ~0.7s, and `summary?book=fno` 8.4s cold
  then **0.029s** throttled. Tests **+3** (`_warm_sim_pass` skips in-hours / warms off-hours / bails
  on block). Suite **801 → 804**.

### 2026-07-22 — Cut the per-symbol chart fan-out (biggest NSE consumer) (suite 799 → 801)
- **Why:** with SIM fixed, `/api/health` showed we were pegged at the pacer ceiling
  (`reqLastMin: 120`, **not** WAF-blocked) and the per-endpoint budget was dominated by
  charting: `symbolsDynamic` + `symbolHistoricalData` ≈ **63/120** of the minute. The bulk is
  `strategies.build_context`'s **5-min candle fan-out** over ~30 candidates — a *stable* cache
  key, so it re-hit `charting.nseindia.com` every `_OHLC_TTL` (30s), several sweeps/min.
- **What (`nse_quote.py`):** interval-aware cache TTL — `_ohlc_ttl(interval, chart_type)` caches
  COARSER bars much longer (5-min → 150s, 15-min → 300s, daily → 600s; 1-min stays 30s). A
  forming N-min bar barely moves in ~N min, so this is pure efficiency: build_context's 5-min
  refetch rate drops ~5× with no strategy-behavior change. The 1-min intrabar resolvers pass a
  moving `to_ts` (keys never repeat) so they're unaffected — for them we added **knobs**:
  `SIM_INTRABAR_SEC` / `IDEAS_INTRABAR_SEC` (default 180s) to lengthen those sweeps if the
  Akamai budget is tight.
- **Result:** the dominant chart consumer now serves mostly from cache; the minute budget frees
  up for the market-wide lists. Tests **+2** (`_ohlc_ttl` scaling; 5-min served from cache past
  the 1-min base TTL while 1-min refetches). Suite **799 → 801**.

### 2026-07-22 — Fix: SIM/F&O tabs stalled + piled up NSE calls (suite 797 → 799)
- **Why:** with the global NSE pacer live, the SIM (esp. **F&O**) tabs stopped loading and
  the network tab showed the same requests firing over and over (`summary?book=fno`,
  `recommendations`, even `health` all stuck *pending*). Root cause: `sim.summary()` calls
  `sim.update()` on **every** poll, which re-prices every OPEN trade — and F&O names are
  rarely in the hot-list map, so each fell through to a **per-symbol NSE quote**, run
  **sequentially** and funnelled through the pacer (min-gap + concurrency cap). So each poll
  did a slow N-symbol fan-out; meanwhile the frontend's auto-refresh + 20s idea-alert timer
  kept firing new polls without waiting, stacking requests until the browser's ~6-connection
  limit saturated and everything (incl. `/api/health`) queued.
- **What (`sim.py`):** throttle the reprice — `update()` now skips the NSE fan-out if it ran
  within `_UPDATE_TTL` (45s) and serves the last reprice from the DB (the snapshot logger still
  calls `update()` every cycle, so MTM stays fresh in market hours); added `force=True` to
  bypass. New `_resolve_prices()` warms the shared hot-list map **once** then fans the rest out
  in **parallel** (still pacer-bounded) instead of one blocking call at a time.
- **What (`templates/index.html`):** in-flight guards so a slow response can't stack — the
  auto-refresh tick skips while a `load()` is running (`_loadInFlight`), and `ideaAlertTick()`
  skips while its `/api/recommendations` poll is still resolving (`_ideaTickBusy`).
- **Result:** repeated SIM/F&O polls are now cheap DB reads; only one bounded reprice per 45s;
  no request pile-up. Tests **+2** (throttle honors TTL/force; parallel resolver maps all
  symbols). Suite **797 → 799**.

### 2026-07-20 — Endpoint budget in the UI (Log modal table) (suite 797, UI only)
- **Why:** the per-endpoint budget was only in `/api/health` JSON. Make "where our NSE quota
  goes" visible in the dashboard so trimming is self-service.
- **What (`templates/index.html`):** a **NSE request budget** table (`#nseBudget`) in the
  Log/diagnostics modal, rendered by `renderNseBudget()` from `/api/health.nse.endpoints` on
  open — endpoint path + hits `/min` and `/hour`, ranked. `test_index_renders` now asserts the
  element. No backend change; suite stays **797**. (Live-confirmed: top row is the history
  fetcher `generateSecurityWiseHistoricalData`, archives under the `nsearchives` host.)

### 2026-07-20 — Fix: startup banner crashed on non-UTF-8 stdout (suite 796 → 797)
- **Why:** the "dashboard is live" banner prints `⚠`/`…`/box-drawing glyphs. On a **cp1252**
  stdout (a plain Windows console, or output piped to a file) `print()` raised
  `UnicodeEncodeError` and **killed launch** — surfaced when restarting the app under a
  non-UTF-8 shell. A UTF-8 terminal (the usual case) never hit it, so it lurked.
- **What (`app.py`):** `_force_utf8_stdio()` reconfigures `sys.stdout`/`stderr` to
  `utf-8, errors="replace"` at the very top — BEFORE the deps that wrap stdout via colorama
  (smartapi→logzero) load — so the banner (and any glyph) is crash-proof on every console,
  keeping the emoji where it renders. Tests **+1** (idempotent/no-raise guard). Suite **796 → 797**.

### 2026-07-20 — Per-endpoint NSE request budget (data-driven trimming) (suite 791 → 796)
- **Why:** the pacer knows the *total* rate but not WHERE it goes. To target the next volume
  trim with evidence instead of guessing, tag each hit by endpoint and keep a 1h sliding log.
- **What (`nse_client.py`):** `_record_endpoint(url)` (called from both `_PacedSession.send`
  and `_PacedCffiSession.request`) logs `(ts, key)` into `_ep_calls`; `_endpoint_key` buckets
  by **path** (query dropped, non-www host prefixed) so the map stays ~15-20 stable endpoints
  (per-symbol quote/chart collapse into one bucket per TYPE; gainers+losers merge).
  `endpoint_budget()` returns per-endpoint `lastMin`/`lastHour` counts ranked by hourly volume,
  surfaced under `/api/health.nse.endpoints`. Own lock, so it never contends with pacer timing.
- **Tests +5** (`test_nse_client.py`, 25 → 30) + health-route assert: key bucketing, min/hour
  counts, >1h pruning, `send()` records, stats shape. Suite **791 → 796**, green, lint clean.

### 2026-07-20 — Header 🛡 Chrome-TLS badge (transport visible) (suite 791, UI only)
- **Why:** auto-failover flips the transport to a real Chrome handshake on repeat blocks, but
  that was invisible — only in `/api/health` JSON. Make the self-healing *show*.
- **What (`templates/index.html`):** a violet **`#nseTls` badge** next to the rate chip that
  appears ONLY when `nse.impersonate` is in effect ("🛡 Chrome TLS"), with a tooltip noting
  auto-failover vs always-on (from `nse.impersonateMode`); hidden on the normal requests path.
  The WAF-block banner also gains a line ("Now routing NSE through a real Chrome TLS handshake")
  when impersonation engages. Fed by the existing 45s `pollNseBlock()`; no backend change.
  Tests: `/api/health.nse` now asserts `impersonate`/`impersonateMode`, and `test_index_renders`
  asserts the badge markup. Suite stays **791**.

### 2026-07-20 — Auto-failover to impersonation + live-verified curl_cffi (suite 785 → 791)
- **Why:** Phase 2 landed impersonation but as a manual env toggle (default was always-on
  `chrome124`). Better: run the *light* pure-requests transport normally and only pay for the
  curl_cffi handshake **when the WAF actually starts blocking us repeatedly** — self-healing,
  not a switch to remember.
- **Live-verify (first):** installed `curl_cffi 0.15.0` and hit NSE end-to-end under a real
  Chrome handshake — `get_session()` built a `_PacedCffiSession`, warm-ups + one live list
  returned **20 real gainers** (not blocked), `pacer_stats().impersonate == chrome124`. The
  structural test now exercises the REAL override, and the full suite stays green with the dep
  installed.
- **What (`nse_client.py`):** `NSE_TLS_IMPERSONATE` gains an **`auto` mode, now the default**:
  `_impersonate_profile()` returns a profile only when `_auto_failover_armed()` — i.e.
  `_block_count >= _AUTO_FAILOVER_AT` (env `NSE_TLS_AUTO_AT`, default 2) and the ladder isn't
  cold (`_block_ladder_expired()`, factored out of `note_block`). It disarms itself once the
  ladder expires, so we drop back to plain requests without a restart. `off`/literal-profile
  policies are unchanged (never / always). `pacer_stats()` adds **`impersonateMode`** (policy)
  alongside `impersonate` (profile in effect now). Since the session rebuilds after the
  cooldown/TTL, the transport switch is automatic.
- **Tests +6** (`test_nse_client.py`, 19 → 25): failover arms past threshold / reverts after a
  clean window / stays off when disabled / literal profile ignores blocks / `_build_session`
  flips transport / `impersonateMode` in stats. Suite **785 → 791**, green, lint clean.

### 2026-07-20 — Phase 2: optional curl_cffi TLS-fingerprint impersonation (suite 778 → 785)
- **Why:** even with the pacer smoothing bursts and fuller Chrome headers, plain `requests`
  still hands Akamai a **Python TLS/HTTP2 fingerprint** (JA3/JA4) it can flag as non-browser
  regardless of rate — the one layer pacing + headers can't disguise. This is the deferred
  "Phase 2" the pacer plan called for, to use only if blocks persist.
- **What (`nse_client.py`):** an **optional** `curl_cffi` import (`_cffi`, `None` when absent).
  `_impersonate_profile()` returns the browser profile from `NSE_TLS_IMPERSONATE`
  (default `chrome124`; `off/none/0/false/no/""` disable it, `None` when the dep is missing).
  A new **`_PacedCffiSession(_cffi.Session)`** paces via `request()` through the SAME
  `_pace()`/`_NSE_GATE` gate. `_build_session()` prefers it when a profile is active
  (warming Referer + the two cookie GETs, letting curl keep its own header set/order),
  else falls back to the pure-requests `_PacedSession` — **fully transparent**: curl_cffi
  responses expose the same `.get/.json/.status_code/.text/.raise_for_status`, so `_fetch`,
  `nse_quote`, `bhavcopy` are untouched. `pacer_stats().impersonate` surfaces the active
  profile (or `null`) under `/api/health.nse`. Enable with `pip install curl_cffi`.
- **Tests +7** (`test_nse_client.py`): fallback when the dep is absent, env toggle
  (default/custom/off), `pacer_stats.impersonate`, `_build_session` picks the cffi transport
  when enabled and the requests one when disabled, plus a structural check of the real
  override when the dep is installed. Suite **778 → 785**, full suite green, lint clean.

### 2026-07-20 — Graceful shutdown: silence Ctrl+C daemon/server-thread noise (suite 776 → 778)
- **Why:** on Ctrl+C two benign tracebacks printed. (1) A daemon intrabar resolver
  (`ideas_journal.resolve_outcomes_intrabar`, `sim._intrabar_fetch`) could enter a
  `ThreadPoolExecutor` just as the interpreter began finalizing →
  `RuntimeError: cannot schedule new futures after interpreter shutdown`. (2) On Windows,
  `select()` on the just-closed dev-server socket raises `OSError(WinError 10038)`. Neither
  is a real failure (no data loss; idempotent, resumes next launch); daemon threads simply
  race the teardown.
- **What:** a `_STOPPING` `threading.Event` + `request_stop()` in `ideas_journal` and `sim` —
  `_intrabar_due()` / `_intrabar_fetch()` bail before spawning a pool, and the executor block
  is wrapped in `try/except RuntimeError`. `app.py` (serving process) registers an `atexit`
  hook that flips both flags + stops the snapshot logger, installs a `threading.excepthook`
  that drops ONLY those two shutdown exceptions (delegating everything else so real errors
  still surface), and wraps `app.run` to print a clean "Shutting down…" on KeyboardInterrupt.
- **Tests +2:** `request_stop()` gates `_intrabar_due` (ideas_journal) and halts
  `_intrabar_fetch` before the pool (sim). Suite **776 → 778**, full suite green, lint clean.

### 2026-07-20 — Header NSE-rate chip (pacer headroom visible) (suite 776, UI only)
- **Why:** the pacer/blocks were only observable via `/api/health` JSON. Surface the live
  request rate so you can *see* headroom before a block.
- **What (`templates/index.html`):** a small `#nsePulse` chip in the header, fed by the
  existing 45s `pollNseBlock()` from `/api/health.nse`. Shows `NSE <reqLastMin>/min` and
  colours by load — green (<60% of `softRpm`) / amber (60-90%) / red (≥90%), and
  `NSE blocked ×N` during an Akamai cooldown. Tooltip explains the soft ceiling +
  concurrency. No backend change (fields already shipped + tested); no new tests.

### 2026-07-20 — Trim NSE load at the source: slower logger cadence + smaller fan-out (suite 772 → 776)
- **Why:** complements the pacer — fewer *total* hits during market hours, not just smoother
  bursts. The dominant server-side NSE consumer is the snapshot logger's 60s loop, whose
  `sim.build_ctx()` → `strategies.build_context()` fans out per-symbol quotes + 5-min candles
  over 8-worker pools every cycle.
- **What (both env-tunable, with floors, so no code edit needed to dial):**
  - `snapshot_logger.INTERVAL` **60 → 90s** (`NSE_LOG_INTERVAL`, floor 30) — ~33% fewer cycles
    of everything; `IV_INTERVAL`/`CONTEXT_INTERVAL` also env-configurable; `STALE_AFTER` now
    `max(180, INTERVAL*2)` so a raised cadence isn't mis-flagged unhealthy. `_env_int()` helper
    parses overrides safely (garbage/blank → default, clamped to floor).
  - `strategies.build_context` candidate fan-out **45 → 30** (`NSE_CTX_CANDIDATES`,
    `_CTX_CAND`, floor 10); source slices derive from the cap (`n1,n2,n3 = cap, cap//2, cap//3`).
  - Net: ~33% fewer cycles × ~33% fewer per-cycle per-symbol calls ≈ **~55% less** per-symbol
    market-hours NSE volume. Trade-off: 90s snapshot granularity + 30 (vs 45) intraday
    candidates for the quote/candle strategies — both dial-able.
- **Tests +4:** `build_context` caps the fan-out at `_CTX_CAND` (and honors a patched cap);
  `_env_int` parsing/floor/garbage; trimmed default cadence + `STALE_AFTER` relationship. Suite
  **772 → 776**, full suite green, lint clean.

### 2026-07-20 — Global NSE request pacer + escalating cooldown + browser headers (suite 760 → 772)
- **Why:** user kept hitting the **NSE Akamai** block. The 10-min cooldown, 15s `_fetch`
  cache and per-endpoint TTLs cut *duplicate* reads but nothing smoothed **bursts** — a cold
  `snapshot_logger`/`build_context()` cycle fans out over 6-8 worker pools and fires dozens of
  near-simultaneous connections, the exact per-IP burst Akamai's rate detector flags (block
  builds up over time, clears on a network switch → rate/IP based). An audit confirmed every
  NSE hit funnels through the **one** warmed `requests.Session`, so a single choke point can
  pace all of it. Pure-Python; the stronger `curl_cffi` TLS-fingerprint swap is deferred to a
  Phase 2 only if blocks persist.
- **What (`nse_client`):**
  - **Global pacer** — `_build_session()` now returns a **`_PacedSession(requests.Session)`**
    whose `send()` gates every hit: a bounded semaphore (**`_NSE_MAX_CONCURRENCY=4`** in
    flight), a lock-serialized **min-gap** (`_NSE_MIN_GAP=0.20s` + up to `_NSE_JITTER=0.15s`)
    between STARTS, and a soft **`_NSE_SOFT_RPM=120`/min** sliding-window ceiling (`_pace()`).
    `nse_quote`/`bhavcopy` inherit it for free (no call-site changes).
  - **Escalating cooldown** — `note_block` now doubles the pause on consecutive fresh blocks
    (`_cooldown_for(_block_count)`: 600 → 1200 → 2400 …, capped `_BLOCK_MAX=3600`), resetting
    the ladder only after a clean gap; a straggler hit *during* a cooldown extends without
    climbing.
  - **Browser headers** — `HEADERS` gains modern-Chrome client hints (`sec-ch-ua*`,
    `Sec-Fetch-*`, `Connection`, `DNT`, `Accept-Encoding` — brotli only if decodable) matching
    the UA major; the two warm-up GETs send navigation-shaped `_NAV_HEADERS`.
  - **Observability** — `pacer_stats()` (blockedForSec/blockCount/cooldownSec/reqLastMin/
    concurrency/minGap/softRpm) is now the `/api/health.nse` payload; the dashboard banner
    adds a "repeat block #N — backing off longer" note when `blockCount > 1`.
- **Trade-off:** background sweeps get slower (steady ~4 concurrent); foreground UX barely
  changes (movers = ~7 endpoints; modal/Live are broker-first).
- **Tests +12** (`test_nse_client.py`): min-gap, soft-RPM wait/no-wait, concurrency cap
  (threaded), cooldown ladder + reset + straggler, header/nav-header shape, `pacer_stats`,
  `_build_session` paced + 2 warm-ups. `test_client._reset_block` + the `/api/health` route
  test now save/restore the escalation ladder. Suite **760 → 772**, full suite green, lint clean.

### 2026-07-20 — Short TTL cache for broker candles (suite 757 → 760)
- **Why:** builds on the rate-limit work. Re-opening the same stock/interval — or the
  modal's `rest_ohlc` + its `rest_chart` fallback + the Live seed all wanting the same
  series — was re-hitting Angel's (rate-limited) `getCandleData` each time. A short cache
  cuts those repeats: fewer Angel calls, snappier UI, more headroom under the 180/min cap.
- **What (`angel_feed`):** `_candle_cache` (dict) + `_candle_cache_get/put`, wired into
  `_get_candles`. Keyed by **(token, interval, from-DATE)** — different intervals/lookbacks
  don't collide; `todate` is excluded so the key is stable within the **30s TTL**
  (`_CANDLE_TTL`; the forming last candle is refined live by the WebSocket anyway). Bounded
  at 256 entries (drop-oldest-half). **Double-checked locking:** cache hits serve without the
  candle lock (fully concurrent); a re-check inside the lock stops two peers double-fetching
  the same key. Only successes are cached (incl. empty); failures aren't, so they retry.
- **Tests +3** (cache hit within TTL = one Angel call; TTL expiry → miss; failures never
  cached). Also reset the cache in the `_angel_rest` fixture so it can't leak between tests.
  Suite **757 → 760**.

### 2026-07-20 — Live-verified the Angel REST path + hardened getCandleData rate limits (suite 753 → 757)
- **Live check (real creds, read-only, no orders):** logged into Angel with the configured
  `angel_config.json`, exercised `rest_quote` / `rest_chart` / `rest_ohlc` on RELIANCE.
  **All work** and return real data with correct **IST-baked timestamps**: quote =
  LTP+OHLC+5-level depth; candles at **1m / 5m / 15m / 1D** (e.g. 1m from 09:15→now, daily
  = 20 sessions). So the whole broker-first migration is real, not just fake-tested.
- **The one real gap the fakes couldn't catch:** Angel's **historical (getCandleData) API is
  rate-limited on three sliding windows — 3/s, 180/min, 5000/hr** (per Angel's docs) — and
  returns a plain-text *"Access denied because of exceeding access rate"* (SDK → `DataException`)
  when bursted (clicking through 1m/5m/15m/D, flicking between stocks). The nasty one is the
  **sliding per-minute window**: 180 calls in the first 10s blocks you for the rest of the
  minute even at zero req/s after. Isolated calls always succeed; only bursts trip it. Left
  unhandled it silently falls back to NSE, defeating broker-first.
- **Fix (`angel_feed._get_candles` + `_candle_throttle`):** a **serialized, rate-limit-aware**
  wrapper both `rest_chart` and `rest_ohlc` now use — proactively honors the **3/s min gap
  (~0.4s)** AND the **180/min sliding cap** (deque of recent call times, with headroom), and on
  an actual trip backs off **exponentially (1s→2s→4s)**, Angel's own recommendation; other
  errors fail fast. Bursts degrade to a small delay instead of an NSE hit; None→NSE stays the
  final safety net. (Live prices already stream over the WebSocket, not REST — so only the
  historical path needs this.)
- **Tests +4** (`_get_candles`: retries-then-succeeds, gives-up→None, no-retry-on-other-error;
  `_candle_throttle` waits on a full minute-window). Suite **753 → 757**.

### 2026-07-20 — Data-source provenance chip: see which feed served each number (suite 753)
- **Why:** after the broker-first migration + adaptive refresh, a given number in the
  detail modal / Live tab could come from **Angel (broker)**, **NSE**, or the **EOD
  bhavcopy** fallback — but the UI never said which. Made provenance visible so you can
  confirm the broker-first/fallback chain is actually working.
- **Backend:** `nse_quote.get_quote/get_chart/get_ohlc` now stamp `source:"nse"` (Angel
  already stamps `source:"angel"`; the EOD fallback already stamped `source:"eod-bhavcopy"`),
  so every quote/chart/candle payload self-identifies.
- **Frontend (index.html):** a small colored `.src-chip` helper (`srcInfo`/`srcChipHtml`) —
  **Angel/Dhan** (broker, no NSE hit) / **NSE** / **EOD** (block/off-hours). Shows next to
  the symbol in the **detail modal** header (from the quote's `source`), inside the chart
  note (OHLCV/intraday), in the **Live-tab seed note** ("…candles from Angel/NSE"), and the
  Live-tab **NSE-poll** path now labels itself honestly (it's broker-first, so a WS-down /
  REST-up poll reads "Angel REST · polled ~12s", not "NSE").
- **Tests:** frontend + self-describing keys, so no new test *function*; locked
  `get_quote`/`get_chart` now return `source:"nse"`. Suite stays **753**; JS `node --check` clean.

### 2026-07-20 — Adaptive auto-refresh: throttle/pause the last foreground NSE hit (frontend; suite 753)
- **Why:** after the broker-first migration, the 30s movers auto-refresh is the ONE
  remaining foreground NSE hit (no broker offers market-wide movers/OI — can't move it
  off NSE), so the win is to stop polling it *needlessly*. It used to fire a blind
  `setInterval(load, 30s)` regardless of whether anyone was looking or NSE was even up.
- **What (index.html only):** replaced the fixed interval with a self-scheduling
  `setTimeout` loop (`scheduleRefresh`/`refreshTick`) that re-plans each cycle:
  - **tab backgrounded** (Page Visibility) → pause entirely; resume + immediate refresh
    on return (`visibilitychange`).
  - **NSE WAF-blocked** (`_nseBlockUntil` from `/api/health`) → pause and wake ~1.5s
    after the cooldown clears (polling NSE mid-block is pointless — server serves cached).
  - **market closed** (`_marketOpen` from `/api/health` → `logger.marketHours`) → stretch
    to ≥5 min (`MKT_CLOSED_MIN_SEC`); lists are static off-hours. Shows
    "· market closed (slow refresh)" on the Updated line.
  - `pollNseBlock()` now also reads `logger.marketHours` and re-plans the loop whenever
    block/market state changes; the "Off" dropdown option still fully stops it.
- **Tests:** frontend-only, so no new test *function*, but locked the contract the loop
  depends on — `test_health_reports_nse_block` now asserts `/api/health` exposes
  `logger.marketHours`. Suite stays **753**. JS `node --check` clean.

### 2026-07-20 — Live-tab chart seed + /api/ohlc served from the broker too (suite 750 → 753)
- **Why:** finishing the broker-first migration. The detail modal already went broker-first
  (previous entry), but the **Live tab** still seeded its candlestick chart from NSE
  (`/api/live/seed`) and the 12s NSE poll fallback used `/api/ohlc`. So opening the Live
  tab still hit NSE even with Angel connected.
- **What:** `angel_feed.rest_ohlc(symbol, interval, chart_type, days)` — OHLCV candles via
  SmartConnect `getCandleData`, mapped to the exact `nse_quote.get_ohlc` shape
  (`points:[{t,o,h,l,c,v}]`), interval keyworded (1→ONE_MINUTE … D→ONE_DAY). `app.py`
  `/api/live/seed` and `/api/ohlc` are now **broker-first when connected → NSE**, but an
  explicit `from/to` window (the backtester's exact holding period) always stays on NSE.
- **Timestamp fix:** candle `t` is now **IST-wall-clock baked as UTC** (`_baked_iso_to_ms`,
  renamed from `_iso_to_ms`), matching `get_ohlc`'s `t` and the live forming bar's
  `_baked_ms` — so seeded history and live ticks land on the same axis (the old
  true-UTC convert would have shifted the seed −5:30h once Angel went live). `rest_chart`
  now uses the baked converter too. `dhan_feed` gets a `rest_ohlc` no-op stub.
- **Tests +3** (candle→ohlc map incl. daily, baked-iso, `/api/ohlc` broker-first but
  window→NSE, `/api/live/seed` broker-first then fallback). Suite **750 → 753**.

### 2026-07-20 — Stock-detail modal served from the broker (Angel), not NSE (suite 740 → 750)
- **Why (the "aren't we using Angel?" question):** the app is a deliberate hybrid —
  NSE for market-wide *discovery* (movers / OI / scanner / option chain / EOD bhavcopy —
  no broker offers those), Angel/Dhan for *live ticks* on symbols you drill into. But the
  stock-detail modal was still calling NSE per row-click (`/api/quote` + `/api/chart`),
  a big chunk of avoidable Akamai load. Audit of NSE call paths: foreground = the 30s
  auto-refresh of the movers views (irreplaceable) + the detail modal (replaceable);
  background = snapshot_logger's 60s market-hours loop + the once-a-day EOD scheduler.
  `_fetch` already de-dupes NSE JSON for 15s.
- **What:** `angel_feed.rest_quote()` / `rest_chart()` — on-demand REST for ARBITRARY
  symbols (not just the streamed watch set) via SmartConnect `getMarketData` FULL (LTP +
  OHLC + 5-level depth; falls back to `ltpData`) and `getCandleData` (5-min points),
  mapped to the exact `nse_quote.get_quote/get_chart` shapes. `app.py` `/api/quote` +
  `/api/chart` are now **broker-first when connected** → NSE NextApi → EOD close, each
  guarded so any miss cleanly falls back (so it's safe even before Angel is live-verified).
  Broker REST isn't behind NSE's Akamai, so this dodges the block entirely. `dhan_feed`
  gets safe `rest_*` no-op stubs (paid data plan not wired) for interface parity.
- **Tests +10** (Angel FULL→quote+depth map, ltpData fallback, candle→points, guards/raise
  →None, iso→ms; Dhan stubs; route broker-first / falls back on miss / skipped when
  disconnected / chart empty→NSE). Suite **740 → 750**.

### 2026-07-20 — Rollover surfaced in the EOD Scan tab (suite 735 → 740)
- **Why:** rollover was only actionable on the Conviction board. This puts the same
  "carrying into next month" read on the market-wide scanner so it shows up everywhere.
- **What:** `eod_scanner._rollover_map()` (reuses `rollover.rank_map()` — the cached FO
  text, so usually free) + `_attach_rollover()` tags each F&O row with `carrying / shedding
  / rolloverPct / rollBullish / rollOiState`. `_tags()` adds a **🔄 carrying N%** badge;
  `_score()` gives **+6** when a name is carrying AND net-bullish (aligned with the bullish
  setup score; no penalty otherwise). `scan(with_rollover=…)`; the `/api/eod/scan` route +
  a UI checkbox default it **on** (only F&O names are affected; cash-only names untouched).
- **Tests +5** (score bonus gated on direction; 🔄 tag; attach only touches F&O names;
  board annotates + boosts; off-by-default doesn't fetch). Flask-client smoke: `?rollover=0`
  strips the flag. Suite **735 → 740**.

### 2026-07-20 — Digest trust footer (calibration → off-screen alerts, suite 730 → 735)
- **Why:** the EOD Telegram/webhook digest listed picks but gave no reason to trust them.
  We already score whether confirmation-stacking pays (`conviction_calibration`) — this
  surfaces that realized track record right in the alert you actually see.
- **What:** `notify._fmt_trackrecord(rep)` (pure) turns a calibration report into a compact
  footer — overall win rate + per-confirmation-tier win rate over RESOLVED ideas, e.g.
  `📊 Track record (30d, 42 resolved): 2✓ 44% · 3✓ 58% · 4✓ 71% · overall 57%`. It's
  **gated**: hidden entirely until ≥8 resolved ideas, and a tier is listed only with ≥3
  resolved (so a thin sample can't mislead). `send_digest()` computes it best-effort
  (`report(days=30)`) and appends it before the disclaimer; a calibration hiccup never
  blocks the digest.
- **Tests +5** (footer tiers/gate/overall; thin/empty → ""; digest appends before
  disclaimer; `send_digest` includes it; survives a calibration error). Suite **730 → 735**.

### 2026-07-20 — Rollover fused into the Conviction board (pillar, suite 723 → 730)
- **Why:** the rollover tracker (below) was a standalone tab; this makes it ACTIONABLE
  everywhere — a breakout on a name whose positions are being CARRIED into next month is
  higher-conviction than one on shrinking OI. Mirrors how sector RS + the option chain were
  folded into the board.
- **What:** `rollover.rank_map()` — the market-wide `{SYMBOL: metrics + cross-sectional
  rolloverRank/carrying/shedding}` (ranked over the WHOLE futures universe, no price/value
  filter, so any pick can look up its standing), cached 15-min, reusing the same FO text.
  `eod_conviction._roll_pillar()` fires a pillar only when a name is CARRYING (rollover% in
  the top fifth today) AND its net near+next OI direction matches the trade side (longs
  carrying → long pillar; shorts carrying → short pillar). Threaded through `_pick` →
  `board(with_rollover=True)`; the board echoes `withRollover`.
- **Discipline preserved:** rollover is just one more independent pillar — it lifts a name's
  confirmation COUNT + score, never overrides the stacking sort; adaptive weighting recognizes
  it (new `rollover` key in `conviction_calibration._PILLARS`, so calibration tracks its lift
  and the ⚖️ toggle can weight it).
- **API/UI:** `?rollover=0` disables the fuse on `/api/eod/conviction[/save]` (on by default);
  the board legend gains 🔄, the Calibration modal a "🔄 Rollover carry" row, the tab desc lists it.
- **Tests +7** (rank_map keys/empty; `_roll_pillar` gating; long-pillar add; `_pick` add;
  board fuse on + `with_rollover=False` skips the fetch; calibration label→key). Suite
  **723 → 730**; verified e2e through the route (STACKED gains 🔄, `rollover=0` drops it).

### 2026-07-18 — Futures rollover tracker (`rollover.py`, suite 709 → 723)
- **Why:** a genuinely new F&O signal we hadn't surfaced. Near expiry, traders roll
  positions from the near to the next month; HOW MUCH rolls (conviction to carry a view)
  and at WHAT spread (contango/backwardation) is a real read the FO bhavcopy already
  carries — every futures contract's EOD OI/close/settle/spot for near, next AND far.
- **What:** `bhavcopy.parse_fo_futures_all()` — pure parser keeping ALL STF/IDF expiries
  per symbol (`parse_fo` keeps only the nearest). `rollover.py` = analytics layer:
  `_metrics()` → **rollover%** (nextOI/(near+next)), **roll cost** (next−near spread) +
  annualized, near-month **basis** to spot, net-(near+next)-OI **state** (buildup/covering/
  unwinding via the price×OI quadrant). `board()` ranks the F&O universe with a
  CROSS-SECTIONAL `rolloverRank` (percentile vs the market median today — meaningful with
  no rollover history), filters by price/turnover, and `sort` ∈ rollover/rollcost/basis/dte.
- **Resilience:** reuses `eod_options._fo_text()` (the SAME cached FO text the option views
  use) so the big file is fetched/parsed once for both; works off-hours / when live is blocked.
- **API/UI:** `/api/eod/rollover`; a **🔄 Rollover** tab (sort + price/value filters) with a
  table — rollover% + a vs-median bar, roll cost (+/− coloured), annualized, basis, OI-state
  chip, and 🟢 carrying / 🔴 shedding badges. Sharpest in the expiry week (a note flags when
  the near expiry is >12 days out).
- **Tests +14** (`test_rollover.py` 12: days/oi-state/metrics/percentile/median/board
  rank+filter+sort+far-expiry-note+empty; +1 `parse_fo_futures_all` in `test_bhavcopy.py`;
  +1 route arg). Suite **709 → 723**, all green; lint clean.

### 2026-07-18 — Adaptive pillar weighting: calibration → scoring (suite 698 → 709)
- **Why:** the calibration report *measures* each pillar's edge but was read-only. The
  obvious close-the-loop step: feed that measured edge back into the board's scoring so
  pillars that have actually worked count for more — the board grades its own homework.
- **What:** `conviction_calibration.pillar_weights()` maps each pillar's realized
  win-rate lift → a scoring multiplier, **clamped `[0.5,1.5]`, shrunk toward 1.0 by the
  thinner side's sample size, and neutral until ≥5 resolved on BOTH sides** (`_mult_from_lift`,
  pure). `pillar_of()` is now the ONE shared label→key classifier (calibration's
  `_pillars_in` refactored onto it, so the parser and the weighter can't drift).
  `report()` attaches each pillar's earned `weight` + a top-level `adaptiveWeights` map.
- **Board:** `eod_conviction.board(adaptive=True)` resolves the weights once and scales
  pillar weights via `_apply_weights` (the option-pillar bonus too) — crucially the
  **confirmation COUNT is left untouched**, so adaptive weighting only re-orders WITHIN a
  confirmation tier and can never let one weighted signal jump the stacking discipline.
- **API/UI:** `?adaptive=1` on the board + save routes (OFF by default). A **⚖️ Adaptive**
  toggle on the Conviction tab; when on, the board shows the applied non-neutral weights
  ("sector ×1.3 · breakout ×0.7") and the 📊 Calibration modal gains a "→ weight" column.
- **Tests +11** (`test_conviction_calibration.py`: `pillar_of`, gate/clamp/shrink/sign of
  `_mult_from_lift`, `pillar_weights`, report attaches weights; `test_eod_conviction.py`:
  `_apply_weights`, weighted `_pick` re-orders within tier / scales option pillar, board
  adaptive returns weights + neutral-history no-op; +1 route arg). Suite **698 → 709**.

### 2026-07-18 — Conviction calibration / hit-rate report (suite 678 → 698)
- **Why:** the whole conviction thesis is "agreement across INDEPENDENT evidence raises
  the odds." We stamp every saved board into `ideas` and resolve candle-accurate
  `TARGET`/`STOP` outcomes — so we can finally *test* the claim instead of asserting it:
  do 4-pillar picks really beat 2-pillar ones, and does each pillar add or subtract edge?
- **What:** `conviction_calibration.py` — pure parsers over the saved idea dicts
  (`is_conviction` tag-filter, `_confirmations_of` reads "(N signals)" with a non-warning
  fallback, `_pillars_in` maps reason labels → the 8 pillar keys, `has_warning` spots the
  option ⚠️ soft-veto), plus `_bucket_stats` (win rate over RESOLVED, MFE/MAE over ALL),
  `_lift` (WITH vs WITHOUT a pillar) and an honest `_verdict`. `report(days, limit)` = the
  only impure bit: one `db.ideas_all()` read (new — newest-day-first, optional `since`
  floor), bucketed by pillar count / rating / direction / per-pillar / warning.
- **API/UI:** `/api/eod/conviction/calibration?days=N`; a **📊 Calibration** button on the
  🏆 Conviction tab opens a modal — headline verdict + totals, "win rate by pillar count",
  by rating/direction, per-pillar win/move lift, and the option-⚠️ impact table.
- **Tests +20** (`test_conviction_calibration.py` 19: parsing / bucket math / lift /
  verdict / `report()` on a temp DB incl. live-idea exclusion + warning impact; +1 route
  arg test in `test_app_routes.py`). Suite **678 → 698**, all green.

### 2026-07-17 — Option chain fused into the Conviction board (suite 667 → 678)
- **Why:** we already assemble max-pain / PCR / OI walls off the FO bhavcopy, but only
  on the option tab. Those levels are exactly what should confirm or *veto* a directional
  swing pick — a long into a fat call OI wall, or pinned above max-pain into expiry, is a
  worse bet than the same breakout with clear air above.
- **What:** `bhavcopy.parse_fo_options_all(text)` — ONE pass over the FO file grouping by
  `(symbol, expiry)` (the existing single-symbol parser merges strikes across symbols when
  unfiltered, so it can't feed per-name analytics). `eod_options.oi_map()` — cached (15-min)
  `{SYMBOL: {expiry, underlying, pcr, maxPain, atmStrike, resistance, support, …}}` for the
  **nearest** expiry of every F&O underlying, so the board parses the big file **once** and
  reuses `nse_quote._max_pain` / `_walls` (one implementation).
- **Fuse** (`eod_conviction`): `_option_overlay(direction, entry, target, opt)` →
  `{maxPain, pcr, wall, confirms[], warns[]}`.
  * max-pain: long UNDER it (short OVER it) = tail-wind → confirm; the wrong side by
    ≥`_PIN_TOL` (3%) = head-wind → warn.
  * OI wall: nearest call (long) / put (short) OI strike between entry and target — target
    BEYOND it must punch through heavy interest → warn; a wall past the target = room → confirm.
  * PCR: put-heavy supports longs, call-heavy supports shorts (weak, labelled).
  A non-empty `confirms` adds ONE **🎯 pillar** (`_OPT_W = 12`, lifts confirmation count +
  conviction); each warn shaves `_OPT_WARN = 8` (a transparent **soft veto** — the name stays
  on the board with a ⚠️, never silently dropped). `board(with_options=True)` builds the map
  once and threads `opt=omap.get(sym)` into `_pick`; picks gain `options` + `warnings`, and
  saved ideas carry the ⚠️ lines.
- **Perf/resilience:** one FO fetch per board call (15-min cached); best-effort — if the FO
  text is unavailable / NSE blocked, `omap = {}` and the board is unchanged.
- **UI:** 🎯 max-pain/PCR chip on each conviction card + a red ⚠️ warnings block; tab/legend
  copy updated.
- **Smoke:** ACME nearest-expiry maxPain 100 / PCR 1.02 / call wall 110 / put wall 90; a long
  below max-pain with room + high PCR picks up the 🎯 pillar, one above max-pain into a wall
  gets two ⚠️ and lower conviction.
- **Tests:** +11 (**667 → 678**): `parse_fo_options_all` (per-symbol grouping, no strike
  collision), `oi_map` (all underlyings one parse + cache + empty), `_nearest_wall`,
  `_option_overlay` (long/short confirm + warn + none), `_pick` (confirm adds a pillar / warn
  shaves conviction), seeded `board()` fusion. Lint clean.

### 2026-07-17 — Sector RS wired into Conviction + EOD scanner (suite 655 → 667)
- **Why:** we built a sector RS board but it sat on its own tab. A breakout **in a
  leading sector** should outrank the same breakout in a laggard — so sector strength
  belongs as a confirmation pillar inside the boards that actually rank names.
- **What:** `sector_scan.py` refactored — record-building extracted into pure
  `_collect(grouped,…)` + `_rank_records()`, reused by both `scan()` and two new
  reusable helpers: **`strength_map(grouped,…)`** → `{sector: {rank, rs, strength,
  count, total}}`, and **`context(smap, symbol)`** → per-name `{sector, rank, rs,
  strength, total, leading, lagging}` (leading ≥67th pct, lagging ≤33rd). Both compute
  off the **already-loaded** bars — no second DB pass.
- **Conviction** (`eod_conviction.board`): computes the strength map once, threads a
  per-name `context` into `_pick`. `_pillars_long` gains a **🧭 leading-sector** pillar,
  `_pillars_short` a **🧭 lagging-sector** pillar (weight `_SECTOR_W = 14`) — so it's a
  real, independent confirmation that lifts confirmation count + conviction. Each pick
  now carries `pick["sector"]`.
- **EOD scanner** (`eod_scanner.scan`): attaches sector context to each row; `_score`
  adds **+8** for a leading sector / **−6** for a lagging one, and `_tags` adds a
  `🧭 <sector> #<rank>` badge. Lazy `import sector_scan` inside the functions breaks the
  `sector_scan → eod_scanner` import cycle.
- **UI:** coloured 🧭 sector chip on each conviction card (green leading / red lagging),
  the badge on scanner rows, and updated tab/tooltip copy.
- **Smoke run:** IT ramped up + Banks down → IT strength 100 (leading); TCS long picks up
  the `🧭 IT is a leading sector (#1/2, RS +35)` pillar, scanner tags it `🧭 IT #1`.
- **Tests:** +12 (**655 → 667**): `strength_map`/`context` leading/lagging thresholds +
  empty/unclassified guards; conviction sector pillar (long-leading / short-lagging,
  none-when-mid, `_pick` carries sector + extra confirmation, seeded `board()`); scanner
  `_score` bonus/penalty + `_tags` badge + seeded `scan()`. Lint clean.

### 2026-07-17 — Sector relative-strength (rotation) board (suite 631 → 655)
- **Why:** individual breakouts work better when the whole SECTOR is bid — money
  rotates between sectors over weeks and riding the leading one is a durable swing edge.
  We had zero sector awareness.
- **What:** `sectors.py` — a curated, dependency-free NSE symbol→sector map (**17 sectors,
  ~303 names** covering F&O + the liquid cash universe; unrecognised symbols are simply
  left unclassified). `sector_scan.py` — mines `db.eod_bars` for **cross-sectional**
  relative strength: each name's blended (20/60-day) return minus the **market median**
  (we have no index history in the bhavcopy, so the market IS the universe). A sector's
  strength = the median RS of its present constituents; sectors are ranked, and the top
  names inside the strongest `leadSectors` become the **leader board** (downtrends
  excluded); the weakest sector's names are the **laggards**. All the maths (`_ret`,
  `_blended`, `_median`, `_percentiles`, `_aggregate`) is pure; `scan()` is one
  `eod_bars_all` query reusing `eod_scanner._features`. Works off-hours, no network.
- **Endpoint/UI:** `GET /api/eod/sectors?minPrice=&minValueCr=&namesPerSector=&leadSectors=`
  + a **🧭 Sectors** tab (ranked sector table with a centre-zero RS bar + breadth, and
  Leaders/Laggards name tables; rows click through to the stock modal).
- **First real run:** Realty strongest (RS +16.5), across 303 classified names / 17 sectors.
- **Note:** RS improves with backfill depth (best with ~60+ sessions); with only a few days
  it degrades to a short-horizon RS. It's a market-wide *board* (like Conviction), not a
  per-symbol backtest strategy — sector strength is cross-sectional.
- **Tests:** +24 (**631 → 655**): map integrity/canonicalisation, RS math + percentiles +
  aggregation ranking/breadth, seeded `scan()` (IT leaders vs Banks laggards, filters,
  empty-db note, clamps), and the route arg-parsing. Lint clean.

### 2026-07-17 — Auto EOD backfill after close (suite 618 → 631)
- **Why:** the EOD scanner, conviction board, and daily/portfolio backtests all read the
  ingested bhavcopy universe (`eod_bars`/`eod_oi` + delivery + deals), which only refreshed
  when the user clicked **"Load EOD"**. So the "tomorrow's watchlist" was stale unless you
  remembered to load it.
- **What:** `eod_scheduler.py` — a daemon that runs **one paced, block-aware refresh**
  (`bhavcopy.backfill` → refresh `deals` → optional `notify.send_digest`) shortly after the
  15:30 close on trading days. The decision `should_run(now, last_run_date, blocked)` is a
  **pure function** (weekday + at/after 16:00 IST + not already run today + not in a WAF
  cooldown), so it's fully unit-testable without sleeping/NSE. The last-run date is persisted
  in `db.eod_meta` (`__AUTOEOD__`/`lastrun`) so the dev auto-reloader's frequent restarts
  don't re-trigger it, and a block mid-run leaves the day **un-recorded** so it retries once
  the cooldown clears. Digest only fires when a genuinely new session landed (`backfill.days>0`)
  and we weren't blocked — no re-sending yesterday's picks on a holiday.
- **Config (env):** `NSE_EOD_AUTO` (default **on**; `=0` to disable), `NSE_EOD_AUTO_HOUR`/`MIN`
  (default 16:00), `NSE_EOD_AUTO_DAYS` (default 5 — small since it runs daily + is idempotent),
  `NSE_EOD_AUTO_DIGEST` (default on; self-noops if notify unconfigured).
- **Endpoints:** `GET /api/eod/scheduler` (state: enabled/runAt/days/digest/dueToday/lastRun),
  `POST /api/eod/scheduler/run?days=N` (trigger now, off-thread). `/api/health` gains an
  `autoEod` summary. Safe by design — one gentle daily pass is the pattern the WAF *doesn't*
  trip on (bursty repeated backfills are).
- **Tests:** +13 (**618 → 631**): the pure decision (time/weekend/blocked/done/boundary), job
  orchestration (backfill→deals→digest, digest skipped on block/no-op/flag), `_tick` records
  the day only on a clean run, and the two routes. Lint clean.

### 2026-07-17 — Block-resilience UX (suite 616 → 618)
- **Why:** closes the loop on the Akamai incident. The backoff already *stopped us
  re-earning* a block, but the UI still silently showed stale numbers and the stock
  modal 403'd during a cooldown — the user had no idea NSE was paused.
- **What:** (1) `/api/health` now reports `nse.blockedForSec` (the shared cooldown).
  (2) A dashboard **banner** (top of `<body>`) polls health every 45s and shows a live
  m:ss **countdown** — "NSE has temporarily rate-limited this network… showing cached/EOD…
  auto-resuming in …" — auto-hiding when it clears. (3) **`/api/quote/<sym>` falls back
  to the EOD bhavcopy close** while blocked (or if the live call throws): `ltp`/`change`/
  `pChange` from the last close, tagged `stale:true` + `source:"eod-bhavcopy"` +
  `blockedForSec`, and it **never touches NSE** during the block. Scanner lists already
  serve their stale `_fetch` cache, so the whole app stays useful mid-block.
- **Tests:** +2 (**616 → 618**): `/api/health` surfaces the cooldown; `/api/quote` degrades
  to EOD (and does *not* call the live path) while blocked. Full suite green, lint clean.

### 2026-07-17 — Portfolio mark-to-market (suite 615 → 616)
- **Why:** open positions were held at **cost**, so equity only stepped on exits and the
  curve hid all intra-trade heat (drawdown looked artificially small).
- **What:** `bd.run(_collect=True)` now also returns `closes` = traded symbols' daily
  closes. `simulate(closes=…)` marks each open position to market every day (contribution
  = reserve + unrealized P&L; LONG = qty×close, SHORT = margin + qty×(entry−close)),
  carrying the last close forward across gap days. The date axis is expanded to the full
  trading calendar (not just open/close days) so the curve is daily. Sizing uses the
  marked equity. `closes=None` → unchanged cost-basis behaviour (keeps pure tests simple).
- **Result (EOD, same run):** max-DD **4.6% → 5.5%** (the honest intra-trade number),
  Sharpe 0.76 → 0.60, curve now daily. Realized end-capital unchanged — only the *path*.
- **Tests:** +1 (**615 → 616**): a long that dips to −8% mid-hold then exits a winner —
  MTM shows the 0.8% drawdown + daily curve; cost-basis shows 0. Portfolio engine now
  feature-complete. Lint clean.

### 2026-07-17 — Conviction-ranked portfolio selection (suite 612 → 615)
- **Why:** the fresh portfolio backtest exposed the real problem — with 5 slots the book
  took an **arbitrary 74 of 5,712** signals (neutral strategy/symbol order), and lost
  (−2.5%, Sharpe −0.98). Which signals you pick matters more than the raw per-signal edge.
- **What:** every `backtest_daily` trade now carries an entry-time **conviction `score`
  (0-100)** scaled from its *own* trigger magnitude (momentum: move × volume; meanrev:
  size of the extreme; delivery: delivery% + move; high52w: distance into the top band;
  vol_breakout: volume × breakout distance; oi_smart: OI% × volume; gap: gap size;
  squeeze: break beyond the NR7 range; rel_strength: RS vs market). All **entry-time only
  — no look-ahead**. New `_conv(x, lo, hi)` clamps a raw magnitude to 0-100 (None →
  neutral 50). `_signals` now returns `(id, dir, score)` triples; `_trade` stores `score`.
  `portfolio_backtest.run()` passes `rank_key="score"`, so same-day contention takes the
  **strongest** signals.
- **Result (same EOD universe, 5 slots):** flips from **−2.5% → +2.2%**, CAGR −9.9% →
  **+9.1%**, Sharpe −0.98 → **+0.76**, max-DD 7.2% → **4.6%**, PF 0.87 → **1.08**;
  `oi_smart` surfaces as the standout (+18.7%). Same slots, same signals — just picking
  the best ones. Proves the feature's thesis.
- **Tests:** +3 (suite **612 → 615**): `_conv` scale/clamp/abs, `_trade` carries score
  (+ optional), and a portfolio `run()` test that the book takes the higher-conviction of
  two contending same-day signals. Lint clean.

### 2026-07-17 — Portfolio-level backtest (`portfolio_backtest.py`, suite 595 → 612)
- **Why:** `backtest_daily` reports per-trade **expectancy in R** — great for "does this
  signal have an edge?", useless for "could I have traded it?". It implicitly assumes
  infinite capital and that every signal is taken. Real trading has a **concurrent-
  position cap** and **finite capital tied up** in open positions.
- **What:** `simulate(trades, …)` (PURE) replays the exact `bd.run(_collect=True)` trades
  through a book: walks date-by-date, closes exits first (frees capital), then opens the
  day's signals in a look-ahead-free order while **slots + cash** allow. Sizing: `risk`
  (lose ~`riskPct`% of equity at the stop) or `equal` (equity / maxPositions), capped by
  `maxAllocPct` + available cash. Opening reserves `qty×entry`; closing returns
  `reserve + pnl` (shorts model margin as full notional). Open positions marked **at
  cost** (curve steps on exits). Metrics: end capital, total return, **CAGR**, **max
  drawdown**, **Sharpe** (daily rets ×√252), win%, profit-factor, exposure, max
  concurrent, trades taken vs **skipped (slot/capital)**.
- **`run()`** (impure): pulls trades from `bd.run` (live or full EOD universe), simulates
  overall + **per strategy** (ranked by total return → which one actually compounds).
- **API/UI:** `/api/sim/portfolio` (`capital`/`maxPositions`/`riskPct`/`sizing`/`source`/
  `days`/`universe`/`minPrice`/`minValueCr`) + a **📈 Portfolio backtest** button with an
  SVG equity curve, a metric grid and a per-strategy table in the Sim tab.
- **Finding (EOD, 209 names, 90 sessions):** 5,712 raw signals but only **74 taken** with
  5 slots (5,637 slot-skipped) → −2.5%, CAGR −9.9%, Sharpe −0.98; `squeeze` the only
  positive strategy (+2.4%). Exactly the reality the per-trade R view hides — and strong
  motivation for conviction-ranked selection next.
- **Gotcha fixed:** never emit `float('inf')` for profit-factor (Flask would serialise the
  invalid `Infinity` JSON token) — return `None` when there are no losing trades; UI shows
  ∞ when win-rate is 100%.
- **Tests:** +17 (suite **595 → 612**): `test_portfolio_backtest.py` (16, pure — usable
  filter, direction-aware pnl/move, drawdown/Sharpe, risk/equal sizing + caps, single
  winner/loser compounding, slot + capital gating, shorts, capital-frees-for-reuse,
  rank_key, `run()` wiring + no-trades) + 1 route arg-parsing test. Lint clean.

### 2026-07-17 — Akamai/WAF block backoff + gentle backfill pacing (suite 578 → 595)
- **Why:** the user hit **"Access Denied … edgesuite.net Reference #…"** in Chrome —
  NSE's Akamai edge had temporarily **blocked their IP**. Root cause was our own
  bursty automated traffic: repeated full-history **backfills** (dozens of archive
  fetches back-to-back) + live polling on the same IP. Worse, our failure path made
  it *self-perpetuating* — every `_fetch` 403 triggered a `get_session(force=True)`
  **rebuild**, which itself GETs the homepage + market page, i.e. **3 more requests
  into an active block** per call, several times a minute.
- **How:** a **shared cooldown** in `nse_client` (`blocked_for()`/`note_block()`/
  `is_blocked_response()`, `_BLOCK_COOLDOWN=600s`). The first 403 (or a WAF body
  marker) starts it; while active, `_fetch()` serves stale cache or fails fast **without
  hitting NSE or rebuilding**, and `get_session()` reuses the stale session instead of
  warming up. `bhavcopy._download` honours + reports the same cooldown (no retry into a
  block). `backfill(pace=0.5)` now **spaces days with a jittered pause** and **aborts
  early** (`blocked` flag) if the WAF fires mid-run. `deals.latest` no longer caches an
  *empty* result during a block (keeps prior data, doesn't advance TTL); `deals.status`
  surfaces `blockedForSec`. The snapshot logger's forced-rebuild self-heal is
  automatically neutered by the `get_session` guard.
- **Follow-up (same session):** the user's log showed the per-stock path still 403-ing
  (`/api/quote/AIIL`) — `nse_quote.py` wasn't covered. Routed **all** its NSE GETs
  (quote/depth/chart/futures/expiries/option-chain) through a new block-aware **`_sget()`**
  helper (short-circuit while blocked, `note_block` on a 403, no retry into a block) and
  gated the warm-up visits. Now the live API, static archives AND per-stock gateway all
  share one cooldown.
- **Note:** this can't *un-block* an IP (only time / a new network does) — it stops us
  **re-earning or extending** the block. Recovery for the user: switch network (mobile
  hotspot), clear NSE cookies + Incognito, or just wait it out.
- **Tests:** +17 (suite **578 → 595**, all green): `test_client.py` block helpers +
  `_fetch`/`get_session` short-circuit (no rebuild into a block, serves stale);
  `test_bhavcopy.py` 403-marks-block/no-retry, short-circuit-while-blocked, backfill
  abort-on-block + per-day pacing; `test_deals.py` keep-cache-during-block + status
  field; `test_quote.py` `_sget` short-circuit/mark-block, `_call` no-retry-into-block,
  warm skipped while blocked. Lint clean.

### 2026-07-17 — EOD conviction board — "tomorrow's watchlist" (`eod_conviction.py`, suite 555 → 578)
- **Why:** we now compute lots of INDEPENDENT market-wide EOD signals (breakout,
  delivery% accumulation, bulk/block deals, F&O OI buildup, volume, trend) but they
  lived in separate views. A trader still had to eyeball several tabs to find the
  names where evidence *agrees*. Agreement across independent signals is exactly what
  raises the odds — so this fuses them into one ranked board.
- **How:** `eod_conviction.board()` reuses `eod_scanner._features` over the whole
  ingested universe (`db.eod_bars_all`), pairs it with the near-month OI series
  (`db.eod_oi_all` → `_oi_state` classifies price×OI into long/short buildup /
  covering / unwinding) and the latest bulk/block deals (`deals.by_symbol`). Per name
  it fires independent LONG/SHORT **pillars** (`_pillars_long`/`_short`), picks the
  stronger side, and ranks by **confirmations first, then blended conviction** —
  confirmation stacking, so a 4-way-confirmed name beats a lone strong signal. Each
  pick gets a volatility-scaled **2R plan** (`_plan`: stop ≈ 1.3× recent daily range,
  floored 3% / capped 9%; 2:1 target).
- **Persist + push:** `save()` writes picks into the `ideas` table dated to the EOD
  session (reasons prefixed "🏆 EOD conviction"), and **skips any existing
  (day,symbol,direction)** so it never clobbers a tracked live idea — they then show
  up in the Ideas history as a durable watchlist. `notify.send_digest()` +
  `_fmt_digest()` push the top longs/shorts off-screen (Telegram/webhook).
- **API/UI:** `/api/eod/conviction` (+ `/save`, `/digest` POST); a new **🏆 Conviction**
  tab with a min-signals selector, price/value/F&O filters, card layout (confirmation
  badge + stacked reasons + plan), and Save / Send-digest buttons.
- **Real e2e:** a 28-session backfill (88,171 bars, delivery on 100%, 6,036 OI rows)
  → board scanned 3,288 names → 12 longs + 12 shorts; e.g. HIRECT (breakout + 26.9×
  vol + 🐋 bulk deal), PRIMECAB/IPCALAB (breakout + delivery + volume). Save persisted
  24 picks; digest formatted cleanly.
- **Tests:** +23 (suite **555 → 578**, all green): OI-state quadrants, deal netting,
  pillar firing (long/short), avg-range/2R plan (+ clamps), pick side-selection,
  board ranking/filters/empty-note, save persist + skip-existing; notify `_fmt_digest`
  (shape/escaping/empty) + `send_digest` (no-channel / supplied-board); conviction
  route arg-parsing + save/digest routes. Lint + py_compile + JS syntax clean.

### 2026-07-17 — Delivery% + bulk/block deals market-wide (`bhavcopy` delivery merge + `deals.py`, suite 530 → 555)
- **Why:** the previous full-universe EOD backtest found the **Delivery% strategy had
  gone quiet (0 trades)** — because the UDiFF CM bhavcopy we ingest **omits the
  delivery column** entirely, so `delivPct` was always null and the strategy never
  fired. Delivery% (shares actually delivered vs traded) is the single best "real
  accumulation vs intraday churn" tell, so this was a real gap, not a dead strategy.
- **How (delivery):** NSE publishes a separate **`sec_bhavdata_full_DDMMYYYY.csv`**
  (security-wise delivery position) as a plain CSV on nsearchives. Added pure
  `parse_sec_delivery()` (handles the file's **leading-space headers** ` SERIES`/
  ` DELIV_PER`, the `-` sentinel for series NSE doesn't compute delivery on, and
  EQ-wins dedup) + `fetch_sec_delivery()` (walk-back over holidays). `ingest_db()`
  now pulls it **for the CM session only** and merges `delivPct`/`delivQty` into the
  ~3100 CM bars **before** the bulk write — and crucially **guards against stamping a
  walked-back day's delivery onto a different session** (`dd == cm_date`). `eod_bars`
  already had the columns, so no schema change. **Real e2e:** a 23-session backfill
  merged delivery on **72,549/72,549 bars (100%)**; the delivery strategy now fires
  **44 trades** (regime-gated **+0.23R**, was 0).
- **How (deals):** new `deals.py` fetches NSE **bulk & block deals** (funds/HNIs/
  promoters — a legally-disclosed institutional footprint) from the tiny nsearchives
  CSVs. `parse_deals()` is pure (handles the block file's **"NO RECORDS"** sentinel);
  fetch reuses `bhavcopy._download` + a 30-min cache. **Real feed:** 102 bulk deals
  pulled live. The scanner cross-references them (`?deals=1` → `with_deals`) to flag
  🐋 rows a big player traded (+8 score bonus on a bulk BUY).
- **Scanner:** new **`delivery`** view (high delivery% on an up day = accumulation),
  `avgDelivPct`/`delivVsAvg` features (delivery-spike-vs-own-average), 🚚 deliv / +Npp
  / 🐋 bulk BUY|SELL tags, and a **Deliv%** column in the UI (green when hot, "+Npp"
  spike hint). E2e delivery view surfaced BALAJIPHOS 100%/8.4× vol and SINTERCOM 98%
  with a 🐋 bulk-SELL flag.
- **API/UI:** `/api/eod/deals?kind=bulk|block&limit=` (+ `?status=1`); `/api/eod/scan`
  gains `?deals=1`; backfill result now reports `deliv`. EOD-scan tab gets a
  **Accumulation (high delivery%)** setup + a **🐋 deals** checkbox.
- **Tests:** +25 (suite **530 → 555**, all green): `parse_sec_delivery` (series
  filter / dash / EQ-wins / empty), `fetch_sec_delivery` walk-back, `ingest_db`
  delivery-merge **and** different-day guard, backfill `deliv` aggregation; new
  `test_deals.py` (parse incl. NO-RECORDS + bad numbers, cache TTL/force, recent/
  by_symbol/status); scanner delivery feature/view/predicate + deals annotation +
  score bonus; `/api/eod/deals` + scan `deals=1` route parsing. Lint + JS clean.

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
  *(Update 2026-07-17: delivery% is no longer quiet — see the delivery/deals entry.)*
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
