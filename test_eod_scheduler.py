"""
Unit tests for eod_scheduler.py — the auto post-close EOD refresh.

Everything here is time-injected / stubbed: the scheduling *decision*
(`should_run`) is pure, and the job/tick tests patch bhavcopy/deals/notify so
nothing sleeps or touches NSE.
"""

import contextlib
from datetime import datetime

import eod_scheduler as S

# 2026-07-16 is a Thursday; 07-18 Saturday, 07-19 Sunday (matches test_logger).
THU = datetime(2026, 7, 16)
SAT = datetime(2026, 7, 18)


def _at(base, h, m):
    return base.replace(hour=h, minute=m)


@contextlib.contextmanager
def _attrs(**kw):
    saved = {k: getattr(S, k) for k in kw}
    for k, v in kw.items():
        setattr(S, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(S, k, v)


@contextlib.contextmanager
def _patch(obj, name, val):
    saved = getattr(obj, name)
    setattr(obj, name, val)
    try:
        yield
    finally:
        setattr(obj, name, saved)


@contextlib.contextmanager
def _reset_state():
    saved = dict(S._state)
    S._state.update(lastRunDate=None, lastRunTs=None, lastResult=None, running=False)
    try:
        yield
    finally:
        S._state.clear()
        S._state.update(saved)


@contextlib.contextmanager
def _no_block():
    import nse_client as nse
    saved = nse._blocked_until
    nse._blocked_until = 0.0
    try:
        yield nse
    finally:
        nse._blocked_until = saved


# ---------------------------------------------------------------------------
# should_run — the pure decision
# ---------------------------------------------------------------------------
def test_should_run_fires_after_close_on_a_weekday():
    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0):
        assert S.should_run(_at(THU, 16, 0), None, 0) is True
        assert S.should_run(_at(THU, 17, 30), None, 0) is True


def test_should_run_blocked_or_disabled_or_done():
    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0):
        now = _at(THU, 16, 30)
        assert S.should_run(now, None, 120) is False           # WAF cooldown
        assert S.should_run(now, "2026-07-16", 0) is False     # already ran today
        assert S.should_run(now, None, 0, enabled=False) is False
    with _attrs(ENABLED=False):
        assert S.should_run(_at(THU, 16, 30), None, 0) is False


def test_should_run_before_time_and_on_weekend():
    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0):
        assert S.should_run(_at(THU, 15, 59), None, 0) is False   # before HH:MM
        assert S.should_run(_at(SAT, 18, 0), None, 0) is False     # Saturday
        assert S.should_run(_at(SAT.replace(day=19), 18, 0), None, 0) is False  # Sunday


def test_should_run_ran_yesterday_still_fires_today():
    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0):
        assert S.should_run(_at(THU, 16, 5), "2026-07-15", 0) is True


# ---------------------------------------------------------------------------
# run_job — orchestration
# ---------------------------------------------------------------------------
def test_run_job_orchestrates_backfill_deals_digest():
    import bhavcopy
    import deals
    import notify
    seen = {}

    def fake_bf(days=5):
        seen["days"] = days
        return {"days": 2, "bars": 10, "oi": 3, "deliv": 5}

    def fake_latest(kind, force=False):
        seen.setdefault("forced", force)
        return {"deals": [1, 2] if kind == "bulk" else [3]}

    def fake_dg():
        seen["digest"] = True
        return {"ok": True, "channels": ["telegram"]}

    with _attrs(DAYS=5, DIGEST=True), _reset_state(), \
            _patch(bhavcopy, "backfill", fake_bf), \
            _patch(deals, "latest", fake_latest), \
            _patch(notify, "send_digest", fake_dg):
        out = S.run_job()
        # assert inside the context — _reset_state restores _state on exit
        assert seen["days"] == 5 and seen["forced"] is True
        assert out["backfill"] == {"days": 2, "bars": 10, "oi": 3, "deliv": 5}
        assert out["deals"] == {"bulk": 2, "block": 1}
        assert out["digest"] == {"ok": True, "channels": ["telegram"]} and seen["digest"]
        assert S._state["running"] is False and S._state["lastResult"] is out


def test_run_job_skips_digest_when_blocked_midrun():
    import bhavcopy
    import deals
    import notify

    def boom():
        raise AssertionError("digest must not fire after a block")

    with _attrs(DAYS=5, DIGEST=True), _reset_state(), \
            _patch(bhavcopy, "backfill", lambda days=5: {"days": 1, "blocked": True}), \
            _patch(deals, "latest", lambda kind, force=False: {"deals": []}), \
            _patch(notify, "send_digest", boom):
        out = S.run_job()
    assert out["backfill"]["blocked"] is True
    assert "digest" not in out


def test_run_job_skips_digest_on_noop_day():
    import bhavcopy
    import deals
    import notify

    def boom():
        raise AssertionError("digest must not fire when nothing new landed")

    with _attrs(DAYS=5, DIGEST=True), _reset_state(), \
            _patch(bhavcopy, "backfill", lambda days=5: {"days": 0}), \
            _patch(deals, "latest", lambda kind, force=False: {"deals": []}), \
            _patch(notify, "send_digest", boom):
        out = S.run_job()
    assert "digest" not in out


def test_run_job_digest_off_by_flag():
    import bhavcopy
    import deals
    import notify

    def boom():
        raise AssertionError("digest disabled")

    with _attrs(DAYS=3, DIGEST=False), _reset_state(), \
            _patch(bhavcopy, "backfill", lambda days=3: {"days": 2}), \
            _patch(deals, "latest", lambda kind, force=False: {"deals": []}), \
            _patch(notify, "send_digest", boom):
        out = S.run_job()
    assert "digest" not in out


# ---------------------------------------------------------------------------
# _tick — records the day only on a clean run
# ---------------------------------------------------------------------------
def test_tick_records_date_on_clean_run():
    now = _at(THU, 16, 5)
    saved = {}
    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0), _reset_state(), _no_block(), \
            _patch(S, "_now_ist", lambda: now), \
            _patch(S, "run_job", lambda: {"backfill": {"days": 1}}), \
            _patch(S, "_save_last_run", lambda d, n: saved.update(d=d, n=n)):
        ran = S._tick()
        assert ran is True
        assert S._state["lastRunDate"] == "2026-07-16"
    assert saved == {"d": "2026-07-16", "n": 1}


def test_tick_does_not_record_when_blocked_midrun():
    now = _at(THU, 16, 5)
    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0), _reset_state(), _no_block(), \
            _patch(S, "_now_ist", lambda: now), \
            _patch(S, "run_job", lambda: {"backfill": {"blocked": True, "days": 0}}), \
            _patch(S, "_save_last_run", lambda d, n: None):
        ran = S._tick()
        assert ran is False
        assert S._state["lastRunDate"] is None


def test_tick_skips_when_not_due():
    now = _at(THU, 15, 0)   # before the run time

    def boom():
        raise AssertionError("run_job must not fire before the scheduled time")

    with _attrs(ENABLED=True, RUN_HOUR=16, RUN_MIN=0), _reset_state(), _no_block(), \
            _patch(S, "_now_ist", lambda: now), _patch(S, "run_job", boom):
        assert S._tick() is False


# ---------------------------------------------------------------------------
# status — UI/health shape
# ---------------------------------------------------------------------------
def test_status_shape():
    st = S.status()
    for k in ("enabled", "started", "running", "runAt", "days", "digest",
              "nowIst", "dueToday", "lastRunDate"):
        assert k in st
