# Deep Audit — Round 2 (2026-07-16)

> Second full re-sweep after Round 1 (`AUDIT.md`) landed and all its P0/P1/P2
> items were implemented. Round 1's focus was security / deployment / concurrency
> / perf. This round re-checks those **and** goes deeper on the thing that most
> silently corrupts a trading-research tool: **financial / statistical
> correctness** (fills, exits, attribution, look-ahead bias). Educational tool —
> not investment advice.

## TL;DR

**Healthy. No High/critical findings.** The core money + backtest math — the
part that would quietly make every conclusion wrong — was read line-by-line and
is **sound**. Round 1's security posture is intact. This round found **1 Medium**
(a lock held across network I/O, same anti-pattern as R1's M3) and a handful of
**Low** hygiene/robustness/consistency items plus one product caveat. The Medium
(N1) and the N4/N5/N6 Low bundle are now **fixed** (see §4); N2/N3/N7 deferred.

---

## 1. Verified correct (actually traced, not rubber-stamped)

| Area | What was checked | Verdict |
|------|------------------|---------|
| **Paper money math** (`paper.py`) | Cash/option/futures orders: cash debit/credit, **futures margin post/release**, P&L realization on close **and flip-through-zero**, weighted-avg on adds, pure-reduction margin release, `portfolio()` MTM (`equity = cash + Σ(margin + unrealised)`). | ✅ correct |
| **Sim sizing/exit** (`sim.py`) | Risk-based `size_position` (+ `MAX_NOTIONAL` cap, no-stop flat fallback), stop-first coarse exit via `intrabar.resolve_point`, business-day `_sessions_elapsed`, M4 no-price expiry, MFE/MAE. | ✅ correct |
| **Backtest look-ahead** (`backtest_daily.py`) | **Signal-at-close → enter-at-close → resolve strictly from the *next* bar** (`_resolve` loops `i+1…`); 20-day features use `[i-20:i]` (exclude the signal day); running hi/lo only through day `i`; stop-first straddle tie-break; time-expire at horizon close; `busy` map prevents overlaps. | ✅ no look-ahead |
| **Intrabar engine** (`intrabar.py`) | Session-bounded horizon, stop-first straddle rule, MFE/MAE from true wicks, OPEN-vs-EXPIRED decision, tz-safe candle time. Shared by live sim + both backtesters. | ✅ correct |
| **Regime mapping** (`strategies.py`) | `detect_regime` emits exactly `Trend-Up/Trend-Down/Mixed/Recovery/Range/Pullback/Unknown` — the **same strings** used in every strategy's `regimeFit`, so leaderboard / strategy-of-the-day matching can't silently miss. | ✅ consistent |
| **Security** (`app.py`) | Same-origin CSRF on writes, constant-time (`hmac.compare_digest`) token gate on **all** methods when `NSE_TOKEN` is set, CSP + `X-Frame`/`nosniff`/`Referrer`, generic error handler that preserves real HTTP codes and never leaks `str(e)`. | ✅ intact (R1) |
| **DB writes** (`db.py`) | Every writer (`insert_snapshots/iv/context`, `sim_insert_trades/clear`, eod, min_bars, ideas) funnels through one `_write_lock`; WAL; typed columns. | ✅ consistent |
| **Frontend lifecycle** (`index.html`) | Live SSE `EventSource` + poll `setInterval`s are torn down on tab switch (`stopLive()`, and `current !== "live"` guard) and closed before re-open — no accumulating connections/timers. | ✅ no leak |
| **New `_fetch` cache** (`nse_client.py`) | Dedupe / TTL expiry / `ttl=0` bypass / errors-not-cached / size cap — covered by `test_fetch_cache.py`. | ✅ correct |

---

## 2. Findings

### N1 — `sim.update()` holds the sim lock across network I/O · **Medium** (concurrency/latency) · ✅ FIXED
**Where:** `sim.update()` (`sim.py:399`) runs entirely inside `with _lock:` — and
that body calls `_refresh_trade` → `_price()` (network on a cache miss) for every
open trade, plus, every 3 min, `_intrabar_catchup()` which fans out a **6-worker
`ThreadPoolExecutor` fetching minute candles for every open symbol**.

**Impact:** `summary()` (`sim.py:972`, hit on *every* Sim-tab poll and after
take/reset) and the 60 s snapshot-logger both call `update()`. Because the lock
wraps the slow candle sweep, concurrent sim calls **serialize behind it and can
stall a few seconds every ~3 min**. Single-user, low-traffic, so mild — but it's
exactly the "never hold a lock across network" anti-pattern Round 1 fixed for the
NSE session (M3).

**Fix:** mirror M3 — do the price/candle fetches **lock-free** (gather results
into locals), then acquire `_lock` only to apply the resolved exits + persist.

**Done:** `update()` now resolves every open symbol's price into a `{symbol:
price}` map and calls the new `_intrabar_fetch()` (the 6-worker candle fan-out,
still throttled to 3 min) **before** taking the lock. `_intrabar_catchup` was
split into `_intrabar_fetch` (network, lock-free) + `_intrabar_apply` (pure
in-memory resolve, under the lock). `_refresh_trade` grew an optional pre-fetched
`px` arg (UNSET → fetch inline for tests/direct callers; explicit `None` → no
network). The critical section is now pure in-memory reprice + DB write, so a
slow NSE call can no longer serialize concurrent sim readers/writers. Under the
lock it re-reads open trades fresh so a trade another thread just closed is never
resurrected.

### N2 — Idea resolver spawns a thread on every `enrich()` · **Low** (hygiene)
**Where:** `ideas_journal.enrich()` (`ideas_journal.py:236`) does
`threading.Thread(target=resolve_outcomes_intrabar, daemon=True).start()` on
**every** call (~every 12 s while Ideas/alerts poll), even though the resolver
usually returns immediately (throttled / off-hours). Thread creation is cheap but
this is needless churn.

**Fix:** do the cheap `_market_ish()` + interval due-check *inline* before
spawning (only spawn when a pass is actually due), or run one long-lived ticker
thread.

### N3 — No transaction costs / slippage / gap fills · **Low** (correctness caveat, by design)
**Where:** every engine (`paper.py`, `sim._refresh_trade`, `backtest_daily`,
`backtest_strategies`) fills at the exact close/stop/target. No brokerage, STT,
slippage, or gap-through (a bar that gaps past the stop still exits *at* the stop).

**Impact:** **absolute** P&L / win-rate is optimistic. Relative strategy ranking
(the tool's actual purpose) stays valid since the bias hits all strategies alike.

**Fix (optional/product):** a configurable per-trade cost + slippage bps knob;
fill gap-through exits at the bar open, not the level.

### N4 — `place_futures_order` reads `pos["margin"]` directly · **Low** (robustness) · ✅ FIXED
**Where:** `paper.py:276` (`margin_freed = pos["margin"] * …`). A futures position
persisted by an older build (or hand-edited `paper_state.json`) without a
`margin` key would raise `KeyError`. Other sites already use `pos.get("margin", 0.0)`.

**Fix:** `pos.get("margin", 0.0)` for the one direct access.

**Done:** the one direct read now uses `pos.get("margin", 0.0)`, matching the
other sites.

### N5 — `paper._now()` uses naive local time · **Low** (consistency) · ✅ FIXED
**Where:** `paper.py:29` — `datetime.now()` (local tz), whereas `sim`, `db`,
`ideas_journal`, feeds all stamp **IST**. On a non-IST host, paper order
timestamps drift vs the rest of the app.

**Fix:** stamp IST like everywhere else.

**Done:** `paper.py` defines `IST = timezone(timedelta(hours=5, minutes=30))` and
`_now()` uses `datetime.now(IST)`, consistent with `sim`/`db`/`ideas_journal`/feeds.

### N6 — Coarse sim closes don't set `closedDay` · **Low** (consistency) · ✅ FIXED
**Where:** `sim._refresh_trade` (`sim.py:288-294`, `309-319`) sets `closedAt` but
not `closedDay`; `_intrabar_catchup` (`sim.py:396`) does. **Harmless** — all
consumers use a `closedDay → closedAt[:10] → openedDate` fallback
(`sim.py:559,611,814`) — but inconsistent.

**Fix:** set `closedDay = closedAt[:10]` in the coarse paths too.

**Done:** both coarse close paths in `_refresh_trade` (the M4 no-price expiry and
the target/stop/expiry hit) now set `t["closedDay"] = t["closedAt"][:10]`, so the
column is always populated — no more relying on the consumer-side fallback.

### N7 — `_fetch` cache returns shared objects / fill staleness · **Info**
The 15 s `_fetch` cache hands the **same dict** to concurrent callers — safe today
because every getter treats the response as read-only, but a future mutating
caller would corrupt the cache (worth a one-line contract note). It also lifts
worst-case paper-fill price staleness to ~20–35 s (its own 20 s `_price_cache`
plus up to 15 s underneath) — within the pre-existing tolerance.

---

## 3. Recommendation

- **Fix proactively:** **N1** (lock-free fetch, apply under lock) — the only
  finding with a real runtime effect. ✅ done
- **Quick hygiene (bundle):** **N4, N5, N6** — a few lines each, zero risk. ✅ done
- **Consider (product):** **N3** cost/slippage model if you ever want the
  *absolute* P&L to be trustworthy, not just the ranking.
- **N2, N7:** nice-to-have / note-only.

Nothing here blocks anything. The instrument is trustworthy for its stated job
(ranking strategies by regime); the caveats are about *absolute* realism and a
periodic few-second latency blip, not about silently wrong results.

---

## 4. Remediation status (2026-07-16)

| ID | Sev | Status | Notes |
|----|-----|--------|-------|
| N1 | Medium | ✅ Fixed | `update()` fetches prices + candles lock-free; `_intrabar_catchup` split into `_intrabar_fetch`/`_intrabar_apply`; lock now guards only in-memory reprice + DB write. |
| N2 | Low | ⏳ Open | Idea resolver still spawns a thread per `enrich()` — cheap churn, deferred. |
| N3 | Low | ⏳ Open (by design) | No transaction cost / slippage model; relative ranking unaffected. |
| N4 | Low | ✅ Fixed | `pos.get("margin", 0.0)` for the one direct access in `place_futures_order`. |
| N5 | Low | ✅ Fixed | `paper._now()` stamps IST. |
| N6 | Low | ✅ Fixed | Coarse `_refresh_trade` closes now set `closedDay`. |
| N7 | Info | ⏳ Note | `_fetch` cache shares read-only dicts (contract note); fill staleness within tolerance. |

Full suite green after the changes: **62 passed**. N1's new split is exercised
live (`update()` resolved 533 open trades' prices + candles outside the lock).
Remaining open items (N2, N3, N7) are deferred hygiene / product caveats with no
correctness impact.
