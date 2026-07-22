"""
Auto EOD backfill after market close.

WHY THIS EXISTS
---------------
The EOD scanner, the conviction board, and the daily / portfolio backtests all
read the ingested bhavcopy universe (`db.eod_bars` / `eod_oi` + delivery + the
bulk/block deals CSV). Until now that only refreshed when the user clicked
**"Load EOD"**. This runs ONE paced, block-aware refresh shortly after the
15:30 IST close on trading days, so the whole EOD stack is current the next
morning — and (optionally) fires the off-screen conviction digest.

DESIGN
------
- The scheduling *decision* (`should_run`) is a **pure function** of
  ``(now, last_run_date, blocked)`` so it's unit-testable without sleeping or
  touching NSE.
- The loop wakes every few minutes, asks `should_run()`, and when true runs the
  job **once for the day** (backfill → refresh deals → optional digest), then
  records the IST date (persisted in `db.eod_meta`, so the dev auto-reloader's
  frequent restarts don't re-trigger it).
- **Block-aware:** never starts while `nse_client` is in a WAF cooldown; the
  backfill itself also paces between days and aborts early if a block appears
  mid-run (leaving the day un-recorded so it retries once the cooldown clears).
- **Gentle by design:** one small post-close pass (default the last 5 sessions,
  idempotent REPLACE) — the safe pattern. The Akamai WAF trips on *bursty
  repeated* backfills, not a single daily refresh. Disable with
  ``NSE_EOD_AUTO=0``.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("eod_scheduler")

IST = timezone(timedelta(hours=5, minutes=30))

# Durable "last run" marker lives in the eod_meta kv (survives restarts).
_META_SYMBOL = "__AUTOEOD__"
_META_KIND = "lastrun"


def _envint(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _envflag(name, default=True):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


# --- config (env-overridable; read as module attrs so tests can monkeypatch) --
ENABLED = _envflag("NSE_EOD_AUTO", True)      # opt-out; one gentle daily pass
RUN_HOUR = _envint("NSE_EOD_AUTO_HOUR", 16)   # 16:00 IST (~30 min after close)
RUN_MIN = _envint("NSE_EOD_AUTO_MIN", 0)
DAYS = max(1, _envint("NSE_EOD_AUTO_DAYS", 5))
DIGEST = _envflag("NSE_EOD_AUTO_DIGEST", True)  # self-noops if notify unconfigured
POLL_SEC = 300                                 # loop re-check cadence

_state = {"lastRunDate": None, "lastRunTs": None, "lastResult": None, "running": False}
_lock = threading.Lock()
_started = False


def _now_ist():
    return datetime.now(IST)


def _today(now=None):
    return (now or _now_ist()).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Pure scheduling decision (unit-tested)
# ---------------------------------------------------------------------------
def should_run(now, last_run_date, blocked, enabled=None):
    """Should the post-close job fire *right now*? Pure — no I/O.

    True iff enabled, not in a WAF cooldown, it's a weekday, we're at/after the
    configured HH:MM IST, and we haven't already run today. (Holidays aren't
    special-cased: the backfill just walks back to the prior session, so a
    holiday run is a cheap no-op rather than a correctness problem.)
    """
    enabled = ENABLED if enabled is None else enabled
    if not enabled or blocked:
        return False
    if now.weekday() >= 5:                      # Sat/Sun
        return False
    if last_run_date == now.strftime("%Y-%m-%d"):
        return False
    return (now.hour, now.minute) >= (RUN_HOUR, RUN_MIN)


# ---------------------------------------------------------------------------
# Persistence of the "already ran today" marker (durable across restarts)
# ---------------------------------------------------------------------------
def _load_last_run():
    try:
        from nse_pulse.core import db
        m = db.eod_meta_get(_META_SYMBOL, _META_KIND)
        return m.get("last_d") if m else None
    except Exception:
        log.debug("auto-EOD: could not load last-run marker", exc_info=True)
        return None


def _save_last_run(date_str, days):
    try:
        from nse_pulse.core import db
        db.eod_meta_set(_META_SYMBOL, _META_KIND, time.time(), date_str, int(days or 0))
    except Exception:
        log.warning("auto-EOD: could not persist last-run marker", exc_info=True)


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------
def run_job(days=None, digest=None):
    """Do the refresh ONCE: backfill bhavcopy (block-aware, paced) → refresh the
    bulk/block deals cache → optionally push the conviction digest. Returns a
    summary dict; safe to call manually (e.g. the 'run now' endpoint)."""
    from nse_pulse.eod import bhavcopy
    from nse_pulse.eod import deals as D
    days = DAYS if days is None else max(1, int(days))
    want_digest = DIGEST if digest is None else bool(digest)

    with _lock:
        _state["running"] = True
    out = {"startedAt": _now_ist().strftime("%Y-%m-%d %H:%M:%S"), "days": days}
    try:
        bf = bhavcopy.backfill(days=days)
        out["backfill"] = bf
        try:
            out["deals"] = {
                "bulk": len((D.latest("bulk", force=True) or {}).get("deals") or []),
                "block": len((D.latest("block", force=True) or {}).get("deals") or []),
            }
        except Exception:
            log.warning("auto-EOD: deals refresh failed", exc_info=True)
            out["deals"] = None
        # Only digest when a genuinely new session landed and we weren't blocked —
        # avoids re-sending yesterday's picks on a holiday / no-op pass.
        if want_digest and bf.get("days") and not bf.get("blocked"):
            try:
                from nse_pulse.web import notify
                out["digest"] = notify.send_digest()
            except Exception:
                log.warning("auto-EOD: digest failed", exc_info=True)
                out["digest"] = None
        return out
    finally:
        with _lock:
            _state.update(running=False, lastRunTs=time.time(), lastResult=out)


# ---------------------------------------------------------------------------
# Background loop + lifecycle
# ---------------------------------------------------------------------------
def _tick():
    """One loop iteration: fire the job if due, record the day on a clean run.
    Returns True if the job ran to completion (test hook)."""
    from nse_pulse.core import nse_client as nse
    now = _now_ist()
    if not should_run(now, _state["lastRunDate"], nse.blocked_for()):
        return False
    log.info("auto-EOD: post-close refresh starting (days=%d)", DAYS)
    res = run_job()
    if (res.get("backfill") or {}).get("blocked"):
        log.warning("auto-EOD: interrupted by a WAF block — will retry after cooldown")
        return False
    with _lock:
        _state["lastRunDate"] = now.strftime("%Y-%m-%d")
    _save_last_run(_state["lastRunDate"], (res.get("backfill") or {}).get("days"))
    log.info("auto-EOD: done — %s", res.get("backfill"))
    return True


def _loop():
    while True:
        try:
            _tick()
        except Exception:
            log.warning("auto-EOD loop error", exc_info=True)
        time.sleep(POLL_SEC)


def start():
    """Start the daemon loop once. No-op if disabled or already started."""
    global _started
    if _started or not ENABLED:
        return False
    _state["lastRunDate"] = _load_last_run()
    _started = True
    threading.Thread(target=_loop, daemon=True, name="eod-auto").start()
    log.info("auto-EOD scheduler ON: %02d:%02d IST, %d-day backfill%s (last run: %s)",
             RUN_HOUR, RUN_MIN, DAYS, ", digest" if DIGEST else "",
             _state["lastRunDate"] or "never")
    return True


def status():
    """UI/health-friendly scheduler state (no secrets)."""
    now = _now_ist()
    with _lock:
        st = dict(_state)
    st.update(
        enabled=ENABLED, started=_started, running=st.get("running", False),
        runAt="%02d:%02d IST" % (RUN_HOUR, RUN_MIN), days=DAYS, digest=DIGEST,
        nowIst=now.strftime("%Y-%m-%d %H:%M:%S"),
        dueToday=bool(should_run(now, st.get("lastRunDate"), False)),
    )
    return st
