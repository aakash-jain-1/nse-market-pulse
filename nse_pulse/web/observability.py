"""
Observability — request logging + OpenTelemetry (CNCF) instrumentation.
=======================================================================
Two independent layers, both safe to leave on; nothing here can crash the app.

1) TERMINAL ACCESS LOG (always on in the serving process, no deps)
   One concise line per HTTP request on stdout (and into logs/app.log):

       12:01:02.345 -> 12:01:02.389  GET    /api/sim/summary?book=fno  200  44.0ms  ip=127.0.0.1  12.3kB  trace=-

   i.e. entry time -> exit time, method, path (the ?token= secret is redacted),
   status, duration, client IP, response size, and the trace id (when OTel is on).

2) OPENTELEMETRY (opt-in via env; the CNCF standard for traces+metrics+logs)
   Auto-instruments Flask (every request -> a server span + the standard
   `http.server.*` RED metrics) and, optionally, the `requests` library and our
   own logs. Exports over OTLP/HTTP to a collector (Jaeger/Tempo/Grafana/…) when
   OTEL_EXPORTER_OTLP_ENDPOINT is set, or prints to the console with OTEL_CONSOLE=1.
   If the opentelemetry packages aren't installed it logs a note and no-ops.

   Enable it, e.g.:
       # one-time collector:  docker run -p4318:4318 -p16686:16686 jaegertracing/all-in-one
       set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
       python app.py            # traces at http://localhost:16686

   Relevant env:
       OTEL_EXPORTER_OTLP_ENDPOINT   OTLP/HTTP base URL (activates export)
       OTEL_CONSOLE=1                print spans/metrics to the console (activates)
       OTEL_SERVICE_NAME             service.name (default "nse-market-pulse")
       OTEL_SDK_DISABLED=1           hard-off switch for the OTel layer
       OTEL_INSTRUMENT_REQUESTS=1    also trace outbound `requests` calls. OFF by
                                     default: it injects W3C traceparent headers into
                                     EVERY outbound request, and NSE's Akamai WAF is
                                     header-sensitive — so we don't touch NSE calls
                                     unless you opt in.
"""

import datetime as _dt
import logging
import os
import re
import sys
import time

from flask import g, request

SERVICE_NAME = (os.getenv("OTEL_SERVICE_NAME") or "nse-market-pulse").strip()

_TARGET_MAX = 200                       # cap logged path length
_TOKEN_RE = re.compile(r"(token=)[^&\s]*", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small, pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def _flag(name):
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def redact(target):
    """Strip the ?token=… shared secret from a request target before logging."""
    if not target:
        return target
    out = _TOKEN_RE.sub(r"\1***", target)
    return out if len(out) <= _TARGET_MAX else out[:_TARGET_MAX] + "…"


def human_size(n):
    """Compact byte-size, or '-' when unknown."""
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _clock(ts):
    """Local wall-clock HH:MM:SS.mmm for a UNIX timestamp."""
    d = _dt.datetime.fromtimestamp(ts)
    return d.strftime("%H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def format_access(method, target, status, start_ts, dur_ms, ip, size, trace_id="-"):
    """The one-line access record: entry -> exit + the request's vitals."""
    return (f"{_clock(start_ts)} -> {_clock(start_ts + dur_ms / 1000.0)}  "
            f"{method:<6} {redact(target)}  {status}  {dur_ms:.1f}ms  "
            f"ip={ip}  {human_size(size)}  trace={trace_id}")


def current_trace_id():
    """Active OTel trace id as 32 hex chars, or '-' when there's no live span."""
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return "-"


def _request_target():
    qs = request.query_string.decode("latin-1", "replace") if request.query_string else ""
    return request.path + ("?" + qs if qs else "")


# ---------------------------------------------------------------------------
# OpenTelemetry setup (opt-in, graceful)
# ---------------------------------------------------------------------------
def _otel_mode():
    """(endpoint, console) activation intent from env; ('', False) means idle."""
    if _flag("OTEL_SDK_DISABLED"):
        return "", False
    return (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip(), _flag("OTEL_CONSOLE")


def setup_otel(app, log):
    """Wire OTel providers + auto-instrumentation. Returns True when active.

    No-ops (returns False) when neither an OTLP endpoint nor OTEL_CONSOLE is set,
    when disabled, or when the packages are missing — the terminal access log does
    not depend on any of this."""
    endpoint, console = _otel_mode()
    if not endpoint and not console:
        if _flag("OTEL_SDK_DISABLED"):
            log.info("OTel: disabled (OTEL_SDK_DISABLED). Terminal access log still on.")
        else:
            log.info("OTel: idle — set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 "
                     "(or OTEL_CONSOLE=1) to export traces/metrics/logs. "
                     "Terminal access log is on regardless.")
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (ConsoleMetricExporter,
                                                      PeriodicExportingMetricReader)
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (BatchSpanProcessor,
                                                    ConsoleSpanExporter)
    except Exception:
        log.warning("OTel: opentelemetry packages missing/broken — skipping export "
                    "(pip install -r requirements.txt). Terminal access log still on.",
                    exc_info=True)
        return False

    resource = Resource.create({"service.name": SERVICE_NAME})

    # --- Traces ---
    tp = TracerProvider(resource=resource)
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads OTEL_* env
    if console:
        tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tp)

    # --- Metrics (RED: http.server.duration histogram + counts, emitted by Flask) ---
    readers = []
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    if console:
        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    # --- Logs (export our INFO+ log records too, when an OTLP endpoint is set) ---
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(lp)
        logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=lp))

    # --- Auto-instrumentation ---
    FlaskInstrumentor().instrument_app(app)
    try:
        LoggingInstrumentor().instrument(set_logging_format=False)  # add trace ids to records
    except Exception:
        pass
    if _flag("OTEL_INSTRUMENT_REQUESTS"):
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()

    where = " ".join(filter(None, [f"otlp={endpoint}" if endpoint else "", "console" if console else ""]))
    log.info("OTel: ACTIVE (traces/metrics%s) service=%s %s",
             "/logs" if endpoint else "", SERVICE_NAME, where)
    return True


# ---------------------------------------------------------------------------
# Terminal access log (always on in the serving process)
# ---------------------------------------------------------------------------
def _install_access_log(app, log, serving):
    alog = logging.getLogger("access")
    alog.setLevel(logging.INFO)
    # Dedicated stdout handler at INFO (the root console handler is WARNING-only, so
    # request lines would otherwise never reach the terminal). propagate=True keeps
    # them flowing to the root file handler (logs/app.log) as well.
    if serving and not getattr(_install_access_log, "_console_added", False):
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(message)s"))
        alog.addHandler(h)
        _install_access_log._console_added = True

    @app.before_request
    def _obs_start():
        # Registered FIRST (init runs before the security guard is defined) so the
        # timer is armed even for requests the guard short-circuits (401/403).
        g._obs_t0 = time.perf_counter()
        g._obs_wall = time.time()
        g._obs_status = None
        g._obs_size = None

    @app.after_request
    def _obs_capture(resp):
        g._obs_status = resp.status_code
        try:
            g._obs_size = resp.calculate_content_length()
        except Exception:
            g._obs_size = None
        return resp

    @app.teardown_request
    def _obs_finish(exc=None):
        t0 = getattr(g, "_obs_t0", None)
        if t0 is None:
            return
        dur_ms = (time.perf_counter() - t0) * 1000.0
        status = getattr(g, "_obs_status", None) or (500 if exc is not None else "-")
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "-"
            line = format_access(request.method, _request_target(), status,
                                 getattr(g, "_obs_wall", time.time()), dur_ms, ip,
                                 getattr(g, "_obs_size", None), current_trace_id())
            alog.info(line)
        except Exception:                      # never let logging break a response
            log.debug("access log failed", exc_info=True)

    return alog


def init(app, log=None, serving=True):
    """Install both layers on `app`. Idempotent per app. Returns True if OTel is
    active. Call this BEFORE any other before_request hooks so the request timer is
    the first thing to run."""
    log = log or logging.getLogger("app")
    if getattr(app, "_obs_inited", False):
        return getattr(app, "_obs_otel", False)
    app._obs_inited = True
    otel_on = False
    try:
        otel_on = setup_otel(app, log)
    except Exception:
        log.warning("OTel setup failed — continuing with the access log only",
                    exc_info=True)
    app._obs_otel = otel_on
    _install_access_log(app, log, serving)
    return otel_on
