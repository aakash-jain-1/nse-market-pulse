# NSE Market Pulse — Deep Code Audit

> **Audit date:** 2026-07-16
> **Auditor:** AI agent (inline, whole-repo read)
> **Commit basis:** working tree at audit time (branch `main`)
> **Scope:** every tracked Python module, the frontend template, config &
> dependency manifests. Static read-through + data-flow/threading/финancial-logic
> reasoning. No code was changed by this audit.

---

## 0. How to read this document

Findings are graded for **this project's actual deployment model**: a
single-user, local, educational dashboard that talks to NSE's *unofficial*
endpoints and (optionally) a broker WebSocket. It is **not** investment advice
and holds **no** real money.

Because of that, "security" severity is stated **in context**:

- **Loopback-only (`127.0.0.1`)** — most issues below are Low/informational.
- **LAN-exposed (`0.0.0.0`, the current default)** — several jump to **High**,
  because *any device on your Wi-Fi* can reach every endpoint with no auth.

| Severity | Meaning |
|----------|---------|
| **High** | Real risk of RCE, data loss, or silent wrong financial numbers in a realistic use of the app as shipped. Fix soon. |
| **Medium** | Correctness/robustness/DoS/maintainability issue that will bite under normal growth or a specific action. Plan a fix. |
| **Low** | Hygiene, latent bug, future-proofing, or minor inefficiency. Fix opportunistically. |

Nothing here is an emergency for a laptop-only, loopback user. The **High**
items matter the moment you (a) show an error page or (b) bind to the LAN — both
of which the app does today.

---

## 1. Executive summary

The codebase is **surprisingly mature for a solo research tool**: WAL-mode
SQLite with indexes, atomic JSON writes, look-ahead-safe backtests with
conservative stop-first tie-breaking, risk-normalised R-expectancy, a
self-healing capture loop with a watchdog, provider-agnostic broker feeds with
market-hours gating + exponential backoff, and genuinely careful "this is
educational, not a portfolio you'd hold" framing in the UI. Secrets are
gitignored and kept out of status payloads. This is good engineering.

The weak spots cluster in four places:

1. **Deployment posture** — `debug=True` **and** `host=0.0.0.0` **and** no auth.
   The Werkzeug interactive debugger + full tracebacks are reachable by anyone on
   the LAN, and every state-changing endpoint (reset ledgers, place paper trades,
   trigger a full-universe backtest) is unauthenticated and CSRF-open. This is the
   single most important cluster to address. *(H1, H2)*

2. **Observability** — near-universal `except Exception: pass`. When NSE blocks
   you or a symbol delists, the app degrades to empty/stale data **silently**.
   There is no `logging`. Diagnosing "why is it blank" is guesswork. *(M5)*

3. **Concurrency & load** — the session-rebuild lock is held across network I/O
   (stalls all request threads), and the expensive backtests/full-universe sweeps
   have no admission control, so concurrent cold calls stampede NSE. *(M2, M3)*

4. **Correctness drift** — three separate trade-resolution engines with subtly
   different exit rules; live coarse path checks target-before-stop (optimistic);
   trades on symbols that fall out of the hot list can hang `OPEN`. The numbers
   are directionally sound but not reproducible across engines. *(M4, M9)*

Plus one **concrete, currently-broken button** (the offline "⏮ Backtest history"
throws `ReferenceError: host is not defined` before it ever fetches). *(M1)*

---

## 1a. Remediation status (2026-07-16)

All P0/P1/P2 items from §5 were implemented (verified: every module imports,
**48/48 unit tests pass** — `test_intrabar.py` + `test_sim.py` + `test_backtest.py`
+ `test_ideas.py` — and a Flask test-client run confirms CSRF/CSP/health). Summary:

| ID | Status | What changed |
|----|--------|--------------|
| H1 | ✅ Fixed | `DEBUG`/interactive debugger now **off by default** (opt-in `FLASK_DEBUG=1`); reloader kept independently so `.py` edits still auto-restart; `TEMPLATES_AUTO_RELOAD` keeps UI edits live; error handler logs server-side and returns a generic message (no `str(e)`). |
| H2 | ✅ Fixed | `before_request` **same-origin check** on all writes (blocks CSRF); optional **`NSE_TOKEN`** gate (header/cookie/`?token=`); startup **LAN warning** when bound to `0.0.0.0` without a token. LAN default preserved so mobile access still works. |
| M1 | ✅ Fixed | Deleted the dead `host.innerHTML` line — the "⏮ Backtest history" button works again. |
| M4 | ✅ Fixed | `sim._refresh_trade` now enforces the max-hold horizon even when no live price is available, closing at last mark — no more immortal `OPEN` trades. |
| M5 | ✅ Fixed | `logging` with a rotating file (`logs/app.log`) + console; strategic logs at session-rebuild/enrich failures; new consolidated **`/api/health`** (loop + feed + DB + posture). |
| M3 | ✅ Fixed | `nse_client.get_session` builds the new session **outside** the lock and swaps under it (with a just-rebuilt guard) — no more whole-app stall during a rebuild. |
| M2 | ✅ Fixed | `backtest_daily.run` serialised via `_run_lock`; `get_all_futures` given a single-flight lock — concurrent cold callers coalesce instead of stampeding NSE. |
| M9 | ✅ Fixed | Added `intrabar.resolve_point()` (documented stop-first rule); both coarse paths (live sim + context backtest) now route through it; daily engine cross-referenced. |
| M6 | ✅ Fixed | `db.retention()` prunes reproducible logs (snapshots 90d / iv 120d / context 60d / min_bars 45d); durable ledgers + EOD cache kept; runs in a daemon thread at startup. |
| L4 | ✅ Fixed | All DB writers now hold `_write_lock` (snapshots/iv/context/sim), matching the EOD/min/ideas writers. |
| M7 | ✅ Fixed | Feed `error` is mapped to coarse categories (`auth_failed`/`rate_limited`/`network`/`data_plan`/`error`) before it hits the open `/api/live/config`. |
| M8 | ◑ Mitigated | Added `escapeHtml()`, sanitised + escaped the user-typed sinks (live watch, deep-dive, option-chain inputs), and set a CSP + `X-Frame/Referrer/nosniff` headers. Broad NSE-data interpolation remains (self-XSS class) with CSP as defense-in-depth. |
| L1 | ✅ Fixed | `get_recommendations(limit=None)` — honest: `None` returns all (today's behaviour), a positive value slices the view. |
| L2 | ◑ Mitigated | `nse_quote._cache` now size-capped w/ eviction (the window-keyed one that grew during backtests). `_hist_/_focpv_` are universe-bounded (~215) and left as-is. |
| L3 | ✅ Fixed | `intrabar.candle_dt` uses tz-aware UTC (no more `utcfromtimestamp` deprecation). |
| L5 | ✅ Fixed | `_load_config()` in both feeds caches by file mtime — no per-second disk read from the SSE status poll. |
| L6 | ✅ Fixed | `sim._sessions_elapsed` counts business days (not just logged days), so trades expire on schedule even if the app was offline. |
| L7 | ✅ Fixed | Idea verdicts now resolve against real 1-min candles (`ideas_journal.resolve_outcomes_intrabar`, STOP-first) — done load-consciously: throttled (~3 min), market-hours gated, token-gated + 30s-cached, batched (6 workers), fired off the poll thread, coarse LTP kept as fallback. |
| L8 | ✅ Fixed | Test suite added: `test_sim.py` (sizing, %-move, business-day horizon, coarse exit + M4 expiry, scorecard R/expectancy), `test_backtest.py` (daily stop-first tie-break + MFE/MAE, coarse resolve, `_scorecard`/`_median`/`_equity`) and `test_ideas.py` (intrabar verdict/tie/fallback/throttle). 48 tests total. |
| L9 | ✅ Noted | `requirements.txt` documents the no-hash/no-lock choice + how to harden for deployment. |

Legend: ✅ fixed · ◑ partially addressed / mitigated · ⚠ deliberately deferred (with reason).

---

## 2. Findings at a glance

| ID | Sev | Area | One-liner |
|----|-----|------|-----------|
| H1 | **High** | Deploy | `debug=True` + LAN bind → Werkzeug debugger/RCE + source disclosure to the whole network |
| H2 | **High** | Deploy | No auth + `0.0.0.0` + no CSRF → any LAN device / any website can reset ledgers, place paper trades, trigger heavy backtests |
| M1 | Med | Frontend | "⏮ Backtest history" button is dead — `host is not defined` throws before fetch (`index.html:4053`) |
| M2 | Med | Load | Full-universe backtests / futures sweeps have no admission control → concurrent cold calls stampede NSE (bot-block/DoS) |
| M3 | Med | Concurrency | `get_session()` holds its lock across 2 network GETs → all request threads serialize for up to ~30 s on rebuild |
| M4 | Med | Sim logic | Live trades on symbols that leave the hot list never MTM/expire via the fast path → can hang `OPEN` |
| M5 | Med | Observability | Pervasive `except Exception: pass`; no `logging` → silent failure, blank data, hard to debug |
| M6 | Med | DB | No retention/VACUUM — `snapshots`, `context_log`, `min_bars` grow unbounded (min_bars already ~1.7M rows) |
| M7 | Med | Feeds | `/api/live/config` (no auth) can echo raw feed exception strings → possible token/JWT fragment disclosure on LAN |
| M8 | Med | Frontend | Many `innerHTML` sinks interpolate data with **no HTML escaping**; no CSP. Self-XSS today, latent injection surface |
| M9 | Med | Sim logic | Three divergent exit engines; live coarse path checks TARGET before STOP (optimistic) vs conservative intrabar |
| L1 | Low | API | `get_recommendations(limit=…)` never slices → `/api/recommendations?limit=` is a no-op |
| L2 | Low | Memory | Unbounded module caches (`nse_quote._cache/_hist_cache/_focpv_cache/_token_cache`) creep during wide backtests |
| L3 | Low | Compat | `datetime.utcfromtimestamp` (deprecated 3.12+) in `intrabar.py` / `db_inspect.py` |
| L4 | Low | DB | Inconsistent write-locking: `_write_lock` wraps only EOD/min/ideas writes; sim/snapshot/iv/context rely on busy-timeout |
| L5 | Low | Perf | `is_configured()` hits disk every call; SSE calls `public_status()` every second per client → per-second disk I/O |
| L6 | Low | Sim logic | Multi-day horizon counts only sessions the app logged; offline days under-count → late expiries |
| L7 | ✅ Low | Sim logic | ~~Idea outcomes use coarse hot-list LTP~~ → intrabar-accurate first-touch via `resolve_outcomes_intrabar` (throttled, gated, batched); coarse LTP kept as fallback |
| L8 | ✅ Low | Tests | ~~Only `test_intrabar.py` exists~~ → added `test_sim.py` + `test_backtest.py` + `test_ideas.py`; 48 tests cover sizing/exit/scorecard/idea-verdict math |
| L9 | Low | Supply chain | `requirements.txt` has no hashes / no lock file |

---

## 3. Detailed findings

### Security & deployment

#### H1 — Debug server exposed on the LAN
**Where:** `app.py:514` (`DEBUG = True`), `app.py:518` (`HOST = ...0.0.0.0`),
`app.py:569` (`app.run(debug=DEBUG, host=HOST, ...)`), `app.py:504-511`
(`@app.errorhandler(Exception)` returning `str(e)`).

**What:** Flask runs with the Werkzeug reloader **and interactive debugger**
enabled, bound to all interfaces. Any unhandled exception on a browser-rendered
route serves the traceback page with a **PIN-gated Python console** (arbitrary
code execution on your machine). The JSON error handler additionally returns raw
`str(e)` to callers. The reloader also means a stray write to any watched `.py`
restarts the server (the "server is stuck/restarting" you saw earlier).

**Why it matters:** On shared Wi-Fi this is a remote code-execution surface, and
even without cracking the PIN it leaks source paths, local variables, and
environment. This is the highest-value fix.

**Fix:**
- Default `DEBUG = os.environ.get("FLASK_DEBUG") == "1"` (off by default).
- When you do need reload, keep `use_debugger=False`.
- Default `HOST=127.0.0.1`; require an explicit opt-in env var for `0.0.0.0`.
- Make the exception handler return a generic message; log the detail server-side.

#### H2 — No authentication + CSRF-open state changes
**Where:** all `@app.route` POSTs, e.g. `/api/paper/order`, `/api/paper/reset`,
`/api/sim/reset`, `/api/sim/take`, `/api/sim/mode`, `/api/fnosim/*`, plus heavy
GETs `/api/sim/backtest_daily`, `/api/futures/all`.

**What:** Every endpoint is unauthenticated. Combined with `0.0.0.0` (H1), any
device on the network can wipe your paper/sim ledgers or place trades. Even on
loopback, there is **no CSRF protection or `Origin`/`Referer` check**, so a
malicious web page you visit in the same browser can `fetch()`/form-POST to
`http://127.0.0.1:5055/api/sim/reset` and destroy state. Heavy GETs
(`backtest_daily` full-universe) are a trivial DoS/NSE-bot-block trigger.

**Fix (pick per exposure):**
- Loopback default (removes the LAN vector) — biggest win, smallest change.
- For genuine LAN use: a shared-secret header or token check in a
  `before_request`, plus a same-origin/`Origin` check on POSTs.
- Rate-limit / single-flight the expensive backtest routes (see M2).

#### M7 — Feed status can echo raw exception strings
**Where:** `angel_feed.public_status()` / `dhan_feed.public_status()` `error`
field → surfaced by `/api/live/config` and `/api/live/stream` (no auth).

**What:** The status payload deliberately omits secrets (good), but the `error`
string is passed through verbatim. Broker SDK/HTTP errors sometimes embed the
request URL or auth material. On a LAN-exposed, unauthenticated endpoint that's
an information-disclosure path.

**Fix:** Whitelist/normalise `error` to a small set of coarse states
(`auth_failed`, `ratelimited`, `network`, `disconnected`) before exposing it.

#### M8 — Unescaped `innerHTML` sinks + no CSP
**Where:** `templates/index.html` — deep-dive, option-chain, live-watch, and
table renderers build markup with template literals and assign to `innerHTML`;
the only `esc()` helper (`index.html:3380`) is a **CSV** escaper, not HTML. No
`Content-Security-Policy` header is set anywhere.

**What:** Symbols/labels flow from NSE (and, for the live watch, from a user
text box) straight into `innerHTML` without escaping, and `onclick` handlers are
built by string interpolation (`onclick="openDeepDive('${sym}')"`). Today this is
**self-XSS** (the data source is NSE and the "attacker" is you), so real-world
risk is low — but it's a fragile, latent injection surface and poor hygiene.

**Fix:** Add an `escapeHtml()` helper and use it for all interpolated text;
prefer `textContent` / `dataset` + `addEventListener` over inline `onclick`
string building; add a restrictive CSP (`default-src 'self'` + the CDN you use
for Lightweight Charts, or self-host it).

---

### Backend / data fetching

#### M3 — Session-rebuild lock held across network I/O
**Where:** `nse_client.get_session()` (~`nse_client.py:50-76`).

**What:** On TTL expiry or after a failure, the rebuild happens **inside** the
`_session_lock`, and the warm-up does two blocking `GET`s to nseindia.com. Every
other request thread that needs the session blocks for the whole rebuild (up to
the 30 s timeout). Under the reloader's threaded server this turns one slow NSE
call into a whole-app stall.

**Fix:** Build the new session **outside** the lock, then swap the reference
under the lock (double-checked). Or use a short lock only around the
pointer swap and let one thread win the rebuild while others serve stale.

#### M2 — No admission control on expensive sweeps
**Where:** `backtest_daily.run()` (`backtest_daily.py:625+`, only
`cached_regime_leaderboard` is `_sod_lock`-guarded at `:744`),
`nse_client.get_all_futures()` / `strategies.build_context()`.

**What:** A cold `backtest_daily` (universe=all) fans out ~200+ symbols ×
(EOD + OI + maybe minute) over a 6-worker pool — hundreds of NSE requests. There
is no single-flight guard on the raw `run()` path, so two browser clicks (or the
SoD pre-warm racing a manual click) launch **parallel** stampedes. NSE will
bot-block you, and every other feature sharing the session degrades.

**Fix:** Wrap `run()` in a per-arg single-flight lock (coalesce concurrent
identical requests), add a global semaphore capping concurrent heavy jobs to 1,
and return a "busy, try again" for extra clicks. The EOD cache already helps once
warm; this protects the cold path.

#### M5 — Silent failure everywhere / no logging
**Where:** pervasive `except Exception: pass` / `except: return []` across
`nse_client.py`, `nse_quote.py`, `strategies.py`, feeds, `sim.py`.

**What:** Errors are swallowed and surfaced as empty lists / `None` / stale data.
There is no `logging` module usage, so when the dashboard goes blank you cannot
tell NSE-block from parse-error from delisted-symbol. This is the biggest drag on
maintainability and on trusting the numbers.

**Fix:** Introduce `logging` with a rotating file handler; replace bare
`except: pass` with `except Exception: log.warning(..., exc_info=True)` at least
at module boundaries. Keep graceful degradation, but **record** it. Add a
lightweight `/api/health` that reports last-success timestamps per data source
(the logger already tracks heartbeats — expose them).

#### L1 — `limit` parameter is dead
**Where:** `nse_client.get_recommendations(..., limit=...)` (~`:699`) never
slices its result; `/api/recommendations?limit=` has no effect.

**Fix:** Either honour `limit` (slice before return) or drop the parameter to
avoid a misleading API contract.

#### L2 — Unbounded module-level caches
**Where:** `nse_quote.py` `_cache`, `_token_cache`, `_warmed`; `nse_client.py`
`_hist_cache`, `_focpv_cache`, `_price_cache`, etc.

**What:** Keyed by symbol/expiry/window with no eviction. Normal dashboard use is
bounded (~150 hot symbols), but a wide/repeated backtest pulls many OHLC windows
and grows these dicts for the process lifetime.

**Fix:** Cap with a small LRU (`functools.lru_cache` or an OrderedDict with a max
size) and/or a TTL sweep.

---

### Financial logic (sims, strategies, backtests)

#### M4 — Trades can hang `OPEN` when a symbol leaves the hot list
**Where:** `sim._refresh_trade()` (`sim.py:259-261`): `px = _price(sym)`; if
`px is None` it returns **without** MTM or expiry.

**What:** `_price()` resolves from the merged hot-list map (~150 names). A live
trade whose symbol cools off the lists gets no repricing and, crucially, **skips
the expiry check**, so it can sit `OPEN` past its horizon, distorting open-book
MTM and horizon stats. The intrabar catch-up rescues it only if the symbol has a
charting token.

**Fix:** In the `px is None` branch, still evaluate time-expiry against
`_sessions_elapsed`, closing at last-known LTP (or fetch a single quote via
`nse_quote` as a fallback price source) so no trade is immortal.

#### M9 — Three divergent resolution engines; live coarse path is optimistic
**Where:** live `sim._refresh_trade` (`sim.py:268-278`, checks **TARGET before
STOP**) + `_intrabar_catchup`; offline `backtest_strategies.py`; daily
`backtest_daily.py` (conservative "bar pierces both ⇒ stop").

**What:** Same trade, three code paths, subtly different exit rules. The live
coarse tick checks target first, so a poll where price swung through both levels
records the optimistic outcome; the intrabar and daily engines are (correctly)
conservative. Results are directionally consistent but not identical or
reproducible across engines — a maintenance and trust hazard.

**Fix:** Extract one `resolve_exit(direction, entry, target, stop, path)` helper
with a single documented tie-break rule (stop-first) and call it from all three
engines. This is the highest-leverage correctness refactor.

#### L6 — Multi-day horizon under-counts offline days
**Where:** `sim._sessions_elapsed()` (`sim.py:249-252`) counts sessions from the
daily-rollup history, which only advances on days the app actually ran.

**What:** If the app is off for a session, that day isn't counted, so a
"≤3 session" hold can span more calendar days than intended and expire late.

**Fix:** Count elapsed **trading days** from a market calendar (or from
`openedDate` vs today's IST date minus weekends/holidays) rather than logged
sessions.

#### L7 — Idea verdicts use coarse LTP — ✅ RESOLVED (2026-07-16)
**Was:** `ideas_journal` TARGET/STOP verdicts came from the hot-list LTP at poll
time, so they could miss an intrabar wick between polls or record a poll-timing
level rather than the exact touch.

**Done:** added `ideas_journal.resolve_outcomes_intrabar()` — for today's *unresolved*
ideas it pulls real 1-min candles from each idea's `firstSeenAt` and resolves the
first touch through the canonical `intrabar.resolve` (STOP-first tie-break, M9), so
verdicts now match the daily/strategy backtesters. It's deliberately light on NSE:
throttled to `INTRABAR_INTERVAL` (~3 min) under a race-safe lock, **market-hours
gated** (`_market_ish`, weekday 09:15–15:45), one **batched** fetch per symbol
(6-worker pool, deduped, **token-gated** + 30 s-cached in `nse_quote`), and fired on
a **background daemon thread** so it never blocks the `/api/recommendations` poll.
The DB write re-reads under the lock and sets **only** the outcome fields, so it
can't clobber a concurrent poll's live `ltp`/`movePct`. Symbols with no charting
token keep the coarse `_resolve_outcome` verdict as a labelled fallback. Covered by
`test_ideas.py` (target, STOP-first tie, no-token fallback, already-resolved skip,
throttle no-op).

#### L8 — Financial aggregation is untested — ✅ RESOLVED (2026-07-16)
**Was:** only `test_intrabar.py` existed; scorecard math, R-multiple/expectancy,
sizing and the exit rules had no tests.

**Done:** added `test_sim.py` (14+ cases: `size_position` incl. the notional cap
and no-stop fallback, `_move_pct`, the business-day `_sessions_elapsed`, the
coarse `_refresh_trade` exit path **including the M4 no-price expiry**, and
`_scorecard` R/expectancy/win-rate) and `test_backtest.py` (`backtest_daily._resolve`
stop-first tie-break + MFE/MAE + expiry for LONG/SHORT, `backtest_strategies._resolve`,
and `_scorecard`/`_median`/`_equity`), plus `test_ideas.py` for the L7 intrabar
verdicts. **48 tests pass** (`python -m pytest -q`). Remaining follow-up: end-to-end
tests for `take()` dedupe + regime tagging (need a temp DB fixture); the pure
exit/aggregation/verdict math — the part most likely to silently corrupt a
scorecard — is now covered.

---

### Persistence / DB / threading

#### M6 — No retention or compaction
**Where:** `db.py` tables `snapshots` (~16k rows/day), `context_log`
(~6 KB gzipped/cycle), `min_bars` (already ~1.7M rows per your last backtest),
`iv_log`, `eod_*`.

**What:** Everything is append-only with no TTL/rollup/`VACUUM`. `market.db` will
grow without bound over months; `min_bars` dominates. Nothing breaks soon, but
disk and query latency creep.

**Fix:** Add a retention job (e.g. keep `snapshots` 90 d, `context_log` 30 d,
`min_bars` 45 d — matching NSE's own minute retention), and an occasional
`PRAGMA incremental_vacuum`/`VACUUM`. Expose counts via `db_inspect.py`
(already partly there).

#### L4 — Inconsistent write-locking
**Where:** `db.py` `_write_lock` wraps EOD/min/ideas writes (`:451, 470, 486,
539, 585`) but **not** `insert_snapshots`, `insert_iv`, `insert_context`, or
`sim_insert_trades`.

**What:** Not a deadlock risk (WAL + `timeout=30` in `_conn()` retries), but the
inconsistency is confusing and leaves the unlocked writers relying purely on
SQLite's busy-timeout under contention.

**Fix:** Either wrap all writers in `_write_lock` for uniformity, or document
explicitly that WAL + busy-timeout is the chosen strategy and drop the partial
lock to avoid implying more protection than exists.

#### L5 — Per-second disk I/O per SSE client
**Where:** `is_configured()` reads the config file on every call; the SSE loop
(`app.py:233+`) calls `public_status()` (→ `is_configured()`) every ~1 s per
connected client.

**Fix:** Cache the parsed config in memory (invalidate on a mtime check or on an
explicit reload), so idle SSE clients don't hit disk every second.

---

### Feeds / secrets / dependencies

- **Secrets hygiene — good.** `angel_config.json`, `dhan_config.json`, `.env`,
  `*.db`, state JSONs, and `logs/` are all gitignored; `*.example.json` templates
  are committed instead. `public_status()` omits tokens (except the M7 `error`
  passthrough). Keep it this way.
- **Feed robustness — good.** Both feeds gate on market hours and back off
  exponentially on reconnect, which is what stopped the earlier `HTTP 429`
  storms.
- **L3 — deprecation.** `datetime.utcfromtimestamp()` in `intrabar.py` /
  `db_inspect.py` is deprecated in Python 3.12+. Switch to
  `datetime.fromtimestamp(ts, tz=timezone.utc)`.
- **L9 — supply chain.** `requirements.txt` pins versions (good) but has no
  hashes / lock file. For a research tool this is acceptable; if you ever deploy,
  add `pip-tools`/hashes. Note the broker SDKs (`smartapi-python`, `dhanhq`) pull
  transitive deps not individually pinned.
- **`db_inspect.py` — safe by construction.** Opens the DB `mode=ro`; `dump`
  whitelists table names; `run_sql` restricts to read-only statement prefixes.
  Fine for a local operator tool.

---

### Frontend

#### M1 — "⏮ Backtest history" button is broken (concrete bug)
**Where:** `templates/index.html:4053` — inside the `simBacktest` onclick,
`host.innerHTML = ...` references an **undefined** `host`. It throws
`ReferenceError: host is not defined` **before** the `try` block (`:4055`) and
before the fetch (`:4057`), so the handler aborts with the button stuck disabled
at "⏳ Replaying…". The very next line (`:4054`) already does the correct
`setBtHtml("btResult", ...)`, so line 4053 is dead/wrong.

**Fix:** Delete line 4053 (the `setBtHtml` on 4054 already renders the loading
state). One-line fix; restores the offline backtest button.

- **UI re-render churn.** Several tabs rebuild whole HTML sections each poll
  (flicker, scroll-jump, lost hover). Not a bug, but a polish/perf item — diff or
  update-in-place for the Sim/Ideas tables. *(informational)*
- **Time handling — good.** The `istTime` helper correctly un-bakes NSE's
  "IST-as-UTC" timestamps; keep using it consistently for any new timestamped UI.

---

## 4. What's done well (keep doing)

- **Look-ahead-safe backtests** with an explicit, conservative "bar pierces both
  ⇒ stop" rule, and honest caveats printed in the UI.
- **Risk-normalised evaluation** (R-multiples / ExpectancyR) instead of naive
  rupee totals, with an explicit "don't read the ₹ sum as a portfolio" note.
- **Durable, indexed SQLite** (WAL, busy-timeout, gzip'd context) + a persistent
  EOD/minute cache that makes wider backtests cheap after the first pull.
- **Self-healing capture loop** (per-task isolation, watchdog, heartbeats) so
  sessions record without babysitting.
- **Provider-agnostic feeds** with market-hours gating and backoff; clean
  Angel/Dhan selection.
- **Secret hygiene** (gitignored configs + committed examples).
- **Atomic JSON persistence** (temp-file + replace) for paper/sim state.
- **Intellectually honest UX** — Tgt% vs Profit%, regime caveats, "educational,
  not advice" throughout.

---

## 5. Prioritised remediation roadmap

**P0 — do first (small, high impact)**
1. **H1/H2 posture:** default `DEBUG=off`, `HOST=127.0.0.1`, opt-in env for LAN;
   generic error handler. (~15 lines in `app.py`.)
2. **M1:** delete the dead `host.innerHTML` line — restores the backtest button.
3. **M4:** evaluate expiry in the `px is None` branch so no trade is immortal.

**P1 — robustness (medium)**
4. **M5:** add `logging` + a rotating file + `/api/health` from existing
   heartbeats.
5. **M3:** rebuild the NSE session outside the lock (swap under lock).
6. **M2:** single-flight + semaphore on `backtest_daily.run()` and futures sweep.
7. **M9:** extract one `resolve_exit()` used by all three engines.

**P2 — hygiene & scale (low)**
8. **M6/L4:** retention + VACUUM job; make DB write-locking consistent (or
   document WAL-only).
9. **M8:** `escapeHtml()` + CSP; drop inline-`onclick` string building.
10. **M7:** normalise feed `error` before exposing.
11. **L1/L2/L3/L5/L6/L7/L8/L9:** dead `limit`, cache caps, timezone-aware UTC,
    config caching, trading-day horizon, intrabar idea verdicts, aggregation
    tests, dependency hashes.

**Guiding principle:** the app is a *good research instrument*; the fixes above
are about making it **trustworthy under growth and safe to leave running**, not
about changing what it does.

---

## 6. Appendix — module inventory

| Module | Role |
|--------|------|
| `app.py` | Flask routes + SSE + feed selector + startup |
| `nse_client.py` | NSE session mgmt, list endpoints, recommendations, caching |
| `nse_quote.py` | NextApi gateway: quotes, charts, option chain, metadata |
| `strategies.py` | 10 trade-idea generators + market-regime detector |
| `sim.py` | Multi-strategy live paper-sim ledger (cash + F&O books) |
| `intrabar.py` | Minute-candle-accurate exit resolution |
| `backtest_strategies.py` | Offline replay over archived `context_log` |
| `backtest_daily.py` | Daily-bar (+ optional intrabar) historical backtest |
| `db.py` | SQLite: snapshots, iv_log, context_log, sim_trades, ideas, eod_*, min_bars |
| `snapshot_logger.py` | Self-healing capture loop + watchdog + health |
| `ideas_journal.py` | Durable per-day idea journal + outcomes + history |
| `paper.py` | Manual virtual-portfolio engine (equity + options + futures) |
| `angel_feed.py` | Angel One SmartAPI WebSocket feed (free) |
| `dhan_feed.py` | Dhan WebSocket feed (paid data API) |
| `db_inspect.py` | Read-only SQLite inspector CLI |
| `nse_demand.py` | Original standalone CLI scanner |
| `templates/index.html` | Entire dashboard UI (HTML+CSS+JS inline) |

*End of audit. No source files were modified in producing this report.*
