"""
Unit tests for app.py — Flask behaviour via the test client.

Focus on the cross-cutting middleware and error contract rather than the data
(the backends are stubbed): the CSRF same-origin guard on writes, the optional
shared-token gate (header / cookie / ?token=), the always-on security headers,
the JSON error handler (404/405/500), favicon 204, query-arg parsing on
/api/scanner, the ok→HTTP-status mapping on paper orders, and /api/health.

Nothing here hits the network — every NSE/feed/paper call is monkeypatched.

Run: python test_app.py   (also works under pytest)
"""

import contextlib

import app as webapp

client = webapp.app.test_client()


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ---------------------------------------------------------------------------
# CSRF same-origin guard (writes only)
# ---------------------------------------------------------------------------
def test_csrf_blocks_cross_origin_write():
    r = client.post("/api/paper/reset", headers={"Origin": "http://evil.com"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "cross-origin request blocked"


def test_csrf_allows_same_origin_write():
    with _patch(webapp.paper, "reset", lambda: None):
        r = client.post("/api/paper/reset", headers={"Origin": "http://localhost"})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_csrf_allows_write_without_origin():
    with _patch(webapp.paper, "reset", lambda: None):
        r = client.post("/api/paper/reset")
    assert r.status_code == 200


def test_csrf_ignores_get():
    with _patch(webapp.nse, "get_variations", lambda k, limit=20: []):
        r = client.get("/api/gainers", headers={"Origin": "http://evil.com"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# optional shared-token gate
# ---------------------------------------------------------------------------
def test_token_gate_rejects_without_token():
    with _patch(webapp, "ACCESS_TOKEN", "s3cret"):
        r = client.get("/favicon.ico")
    assert r.status_code == 401 and r.get_json()["error"] == "unauthorized"


def test_token_gate_accepts_header():
    with _patch(webapp, "ACCESS_TOKEN", "s3cret"):
        r = client.get("/favicon.ico", headers={"X-Access-Token": "s3cret"})
    assert r.status_code == 204


def test_token_gate_wrong_token():
    with _patch(webapp, "ACCESS_TOKEN", "s3cret"):
        r = client.get("/favicon.ico", headers={"X-Access-Token": "nope"})
    assert r.status_code == 401


def test_token_query_sets_cookie():
    with _patch(webapp, "ACCESS_TOKEN", "s3cret"):
        r = client.get("/favicon.ico?token=s3cret")
    assert r.status_code == 204
    assert "nse_token" in (r.headers.get("Set-Cookie") or "")


def test_no_token_gate_when_unset():
    # ACCESS_TOKEN is "" by default → open
    r = client.get("/favicon.ico")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# security headers (always present)
# ---------------------------------------------------------------------------
def test_security_headers_present():
    r = client.get("/favicon.ico")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Referrer-Policy"] == "same-origin"


# ---------------------------------------------------------------------------
# error handler / status mapping
# ---------------------------------------------------------------------------
def test_404_is_json():
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and "error" in r.get_json()


def test_405_wrong_method():
    r = client.get("/api/paper/reset")   # reset is POST-only
    assert r.status_code == 405


def test_500_hides_internals():
    def boom(*a, **k):
        raise RuntimeError("secret path /etc/passwd")
    with _patch(webapp.nse, "get_variations", boom):
        r = client.get("/api/gainers")
    assert r.status_code == 500
    assert r.get_json()["error"] == "Internal server error"   # no leak


def test_favicon_204():
    r = client.get("/favicon.ico")
    assert r.status_code == 204 and r.data == b""


# ---------------------------------------------------------------------------
# arg parsing + route wiring
# ---------------------------------------------------------------------------
def test_scanner_arg_parsing():
    seen = {}

    def fake_scanner(**kwargs):
        seen.update(kwargs)
        return []
    with _patch(webapp.nse, "get_scanner", fake_scanner):
        client.get("/api/scanner?direction=up&minChange=2&minVolMult=5&fno=1&oi=long")
    assert seen["direction"] == "up" and seen["min_abs_change"] == 2.0
    assert seen["min_vol_mult"] == 5.0 and seen["fno_only"] is True
    assert seen["oi"] == "long"


def test_scanner_bad_number_is_none():
    seen = {}
    with _patch(webapp.nse, "get_scanner", lambda **k: seen.update(k) or []):
        client.get("/api/scanner?minChange=abc")
    assert seen["min_abs_change"] is None


def test_paper_order_ok_status():
    with _patch(webapp.paper, "place_order", lambda s, side, q: (True, "ok", {"id": 1})):
        r = client.post("/api/paper/order", json={"symbol": "X", "side": "BUY", "qty": 1})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_paper_order_error_status():
    with _patch(webapp.paper, "place_order", lambda s, side, q: (False, "bad", None)):
        r = client.post("/api/paper/order", json={"symbol": "X", "side": "BUY", "qty": 1})
    assert r.status_code == 400 and r.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_health_ok():
    with _patch(webapp.snaplog, "health", lambda: {"healthy": True, "marketHours": True}), \
         _patch(webapp.live_feed, "public_status",
                lambda: {"provider": "nse", "connected": False, "configured": False}):
        r = client.get("/api/health")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    assert set(j) >= {"ok", "logger", "feed", "db", "posture"}
    assert j["posture"]["authRequired"] is False


def test_health_ok_when_market_closed_and_idle():
    with _patch(webapp.snaplog, "health", lambda: {"healthy": False, "marketHours": False}), \
         _patch(webapp.live_feed, "public_status",
                lambda: {"provider": "nse", "connected": False, "configured": False}):
        r = client.get("/api/health")
    assert r.get_json()["ok"] is True    # idle outside market hours is healthy


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} app tests passed")


if __name__ == "__main__":
    _main()
