"""
Unit tests for observability.py — the terminal access log + OTel gating.

The pure formatters (redact / human_size / _clock / format_access) are asserted
directly; the OTel layer is checked for its *gating* (idle / disabled -> no-op) so
tests never spin up real exporters; and the access-log hooks are driven end-to-end
through a throwaway Flask app + test client (status, duration, token redaction, and
that a 500 still logs via teardown).

Run: python test_observability.py   (also works under pytest)
"""

import contextlib
import logging
import os
import re

import flask

import observability as obs


@contextlib.contextmanager
def _env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def _capture_access():
    h = _Capture()
    alog = logging.getLogger("access")
    alog.addHandler(h)
    try:
        yield h
    finally:
        alog.removeHandler(h)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_redact_strips_token_only():
    assert obs.redact("/api/x?a=1&token=SECRET&b=2") == "/api/x?a=1&token=***&b=2"
    assert obs.redact("/api/x?token=abc123") == "/api/x?token=***"
    assert obs.redact("/api/x?a=1&b=2") == "/api/x?a=1&b=2"     # untouched
    assert obs.redact("/api/x") == "/api/x"


def test_redact_is_case_insensitive_and_capped():
    assert obs.redact("/x?Token=abc") == "/x?Token=***"          # keeps original case
    long = "/x?" + "a" * 400
    out = obs.redact(long)
    assert len(out) <= obs._TARGET_MAX + 1 and out.endswith("…")


def test_human_size():
    assert obs.human_size(None) == "-"
    assert obs.human_size(0) == "-"
    assert obs.human_size(512) == "512B"
    assert obs.human_size(1536) == "1.5kB"
    assert obs.human_size(2_000_000) == "1.9MB"


def test_clock_shape():
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d\d\d", obs._clock(1_700_000_000.123))


def test_format_access_has_entry_exit_and_vitals():
    line = obs.format_access("GET", "/api/sim/summary?token=SECRET", 200,
                             1_700_000_000.0, 44.0, "127.0.0.1", 12345, "abc")
    assert "->" in line                       # entry -> exit
    assert "GET" in line and "/api/sim/summary" in line
    assert "token=***" in line and "SECRET" not in line
    assert "200" in line and "44.0ms" in line
    assert "ip=127.0.0.1" in line and "trace=abc" in line
    assert "12.1kB" in line


def test_current_trace_id_dash_without_span():
    assert obs.current_trace_id() == "-"


# ---------------------------------------------------------------------------
# OTel gating (no real exporters spun up)
# ---------------------------------------------------------------------------
def test_setup_otel_idle_when_unconfigured():
    app = flask.Flask(__name__)
    with _env(OTEL_EXPORTER_OTLP_ENDPOINT=None, OTEL_CONSOLE=None, OTEL_SDK_DISABLED=None):
        assert obs.setup_otel(app, logging.getLogger("test")) is False


def test_setup_otel_disabled_switch():
    app = flask.Flask(__name__)
    with _env(OTEL_SDK_DISABLED="1", OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"):
        assert obs.setup_otel(app, logging.getLogger("test")) is False


def test_otel_mode_reads_env():
    with _env(OTEL_EXPORTER_OTLP_ENDPOINT="http://c:4318", OTEL_CONSOLE="1", OTEL_SDK_DISABLED=None):
        assert obs._otel_mode() == ("http://c:4318", True)
    with _env(OTEL_SDK_DISABLED="1", OTEL_EXPORTER_OTLP_ENDPOINT="http://c:4318", OTEL_CONSOLE="1"):
        assert obs._otel_mode() == ("", False)


# ---------------------------------------------------------------------------
# access-log hooks end-to-end (throwaway app)
# ---------------------------------------------------------------------------
def _mk_app():
    app = flask.Flask(__name__)
    with _env(OTEL_EXPORTER_OTLP_ENDPOINT=None, OTEL_CONSOLE=None):
        obs.init(app, logging.getLogger("test"), serving=False)   # no stdout handler

    @app.route("/ok")
    def _ok():
        return "hello world"

    @app.route("/boom")
    def _boom():
        raise RuntimeError("kaboom")

    return app


def test_access_log_emits_line_with_status_and_timing():
    app = _mk_app()
    with _capture_access() as cap:
        assert app.test_client().get("/ok").status_code == 200
    assert len(cap.lines) == 1
    line = cap.lines[0]
    assert "GET" in line and "/ok" in line and " 200 " in line
    assert "->" in line and "ms" in line and "ip=" in line


def test_access_log_redacts_token_in_query():
    app = _mk_app()
    with _capture_access() as cap:
        app.test_client().get("/ok?token=TOPSECRET&x=1")
    line = cap.lines[0]
    assert "token=***" in line and "TOPSECRET" not in line and "x=1" in line


def test_access_log_records_500_via_teardown():
    # A view that raises never runs after_request, so the status is filled in by the
    # teardown hook — the request must still be logged (as 500), not dropped.
    app = _mk_app()
    with _capture_access() as cap:
        app.test_client().get("/boom")
    assert len(cap.lines) == 1 and " 500 " in cap.lines[0] and "/boom" in cap.lines[0]


def test_init_is_idempotent():
    app = _mk_app()
    before = len(app.before_request_funcs.get(None, []))
    obs.init(app, logging.getLogger("test"), serving=False)   # second call: no-op
    after = len(app.before_request_funcs.get(None, []))
    assert before == after


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"\n{len(fns)} tests passed")
    sys.exit(0)
