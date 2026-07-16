"""
Unit tests for nse_client._fetch()'s path-keyed TTL micro-cache.

The cache collapses the many duplicate GETs the app makes to the SAME read-only
NSE list endpoint within a cycle (get_scanner + build_context + get_demand_score
all overlap, plus frontend polls vs the 60s logger). These tests stub
get_session so nothing touches the network, and assert dedupe / expiry / ttl=0
bypass / error-not-cached / size-cap behaviour. The fixture restores
get_session + the cache afterwards so it's clean inside the full suite.

Run: python test_fetch_cache.py   (also works under pytest)
"""

import contextlib

import nse_client as nse


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeSession:
    """Counts GETs per URL; optionally raises to simulate an NSE failure."""
    def __init__(self, counter, fail=False):
        self.counter = counter
        self.fail = fail

    def get(self, url, timeout=None):
        self.counter[url] = self.counter.get(url, 0) + 1
        if self.fail:
            raise RuntimeError("boom")
        return _FakeResp({"url": url})


@contextlib.contextmanager
def _patched(fail=False):
    """Swap get_session for a counting fake + isolate the cache; restore after."""
    counter = {}
    orig_gs, orig_cache = nse.get_session, dict(nse._fetch_cache)
    sess = _FakeSession(counter, fail=fail)
    nse.get_session = lambda force=False: sess      # both call sites pass force
    nse._fetch_cache.clear()
    try:
        yield counter
    finally:
        nse.get_session = orig_gs
        nse._fetch_cache.clear()
        nse._fetch_cache.update(orig_cache)


def test_cache_hit_within_ttl():
    with _patched() as counter:
        a = nse._fetch("/api/x")
        b = nse._fetch("/api/x")
        assert a == b == {"url": nse.BASE + "/api/x"}
        assert counter[nse.BASE + "/api/x"] == 1     # 2nd call served from cache


def test_expiry_refetches():
    with _patched() as counter:
        nse._fetch("/api/x")
        ts, data = nse._fetch_cache["/api/x"]         # force stale (no sleep)
        nse._fetch_cache["/api/x"] = (ts - nse._FETCH_TTL - 1, data)
        nse._fetch("/api/x")
        assert counter[nse.BASE + "/api/x"] == 2      # refetched after TTL


def test_distinct_paths_cache_separately():
    with _patched() as counter:
        nse._fetch("/api/a")
        nse._fetch("/api/b")
        nse._fetch("/api/a")
        assert counter[nse.BASE + "/api/a"] == 1
        assert counter[nse.BASE + "/api/b"] == 1


def test_ttl_zero_bypasses_cache():
    with _patched() as counter:
        nse._fetch("/api/x", ttl=0)
        nse._fetch("/api/x", ttl=0)
        assert counter[nse.BASE + "/api/x"] == 2      # never cached
        assert "/api/x" not in nse._fetch_cache


def test_errors_not_cached():
    with _patched(fail=True):
        try:
            nse._fetch("/api/x")
            assert False, "expected _fetch to raise"
        except RuntimeError:
            pass
        assert "/api/x" not in nse._fetch_cache        # nothing cached on failure
    with _patched(fail=False) as counter:              # once NSE recovers, it caches
        nse._fetch("/api/x")
        nse._fetch("/api/x")
        assert counter[nse.BASE + "/api/x"] == 1


def test_cache_size_capped():
    with _patched():
        for i in range(nse._FETCH_CACHE_MAX + 20):
            nse._fetch(f"/api/{i}")
        assert len(nse._fetch_cache) <= nse._FETCH_CACHE_MAX


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} fetch-cache tests passed")


if __name__ == "__main__":
    _main()
