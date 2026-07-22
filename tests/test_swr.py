"""
Unit tests for nse_pulse.core.swr.SwrCache — the stale-while-revalidate cache
that backs the non-blocking strategy_of_day / conviction board endpoints.

The whole point of the cache is that get() NEVER blocks on a compute, so the
tests drive a deterministic compute (call-counted, optionally gated on an Event)
and assert the fresh / stale / cold behaviours + single-flight + veto + eviction.
No network, no sleeps longer than a cache TTL.
"""

import threading
import time

from nse_pulse.core.swr import SwrCache


def _wait(pred, timeout=3.0):
    """Poll pred() until true or timeout (background refresh completes off-thread)."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class _Compute:
    """Call-counted compute; can block on a gate to exercise single-flight."""

    def __init__(self, gate=None):
        self.calls = 0
        self.lock = threading.Lock()
        self.gate = gate

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.gate is not None:
            self.gate.wait(5)
        return {"n": n, "args": args, "kwargs": kwargs}


def test_cold_returns_placeholder_then_computes_in_background():
    comp = _Compute()
    c = SwrCache(comp, ttl=100, placeholder={"warming": True})
    # First hit: nothing cached → placeholder now, compute kicked off-thread.
    assert c.get(1) == {"warming": True}
    assert _wait(lambda: c.peek(1) is not None), "background compute never stored"
    assert c.peek(1)["n"] == 1
    # Now warm → served from cache, no recompute.
    assert c.get(1)["n"] == 1
    assert comp.calls == 1


def test_placeholder_can_be_callable():
    comp = _Compute()
    c = SwrCache(comp, ttl=100, placeholder=lambda *a, **k: {"warming": a})
    assert c.get(7)["warming"] == (7,)


def test_fresh_hit_does_not_recompute():
    comp = _Compute()
    c = SwrCache(comp, ttl=100)
    c.prime(1)                      # synchronous compute -> calls == 1
    assert comp.calls == 1
    for _ in range(5):
        assert c.get(1)["n"] == 1   # all fresh
    assert comp.calls == 1


def test_stale_serves_old_value_and_refreshes():
    comp = _Compute()
    c = SwrCache(comp, ttl=0.05)
    c.prime(1)                      # v1 (n == 1)
    time.sleep(0.08)                # let it go stale
    stale = c.get(1)                # stale value served immediately...
    assert stale["n"] == 1
    assert _wait(lambda: (c.peek(1) or {}).get("n") == 2), "stale value never refreshed"


def test_single_flight_under_concurrent_cold_gets():
    gate = threading.Event()
    comp = _Compute(gate=gate)
    c = SwrCache(comp, ttl=100)
    # Fire several cold gets while the first compute is blocked on the gate.
    for _ in range(6):
        assert c.get(1) is None     # default placeholder
    time.sleep(0.05)
    gate.set()
    assert _wait(lambda: c.peek(1) is not None)
    assert comp.calls == 1          # only ONE refresh scheduled for the key


def test_should_refresh_veto_blocks_scheduling():
    comp = _Compute()
    c = SwrCache(comp, ttl=100, should_refresh=lambda: False)
    assert c.get(1) is None
    time.sleep(0.1)
    assert comp.calls == 0          # vetoed → never computed
    assert c.peek(1) is None


def test_prime_is_synchronous():
    comp = _Compute()
    c = SwrCache(comp, ttl=100)
    out = c.prime(5, k=9)
    assert out["n"] == 1 and out["args"] == (5,) and out["kwargs"] == {"k": 9}
    assert c.is_fresh(5, k=9)
    assert comp.calls == 1


def test_distinct_keys_are_independent():
    comp = _Compute()
    c = SwrCache(comp, ttl=100)
    c.prime(1)
    c.prime(2)
    assert c.peek(1)["n"] == 1 and c.peek(2)["n"] == 2
    assert c.get(1)["n"] == 1 and c.get(2)["n"] == 2   # both fresh, no recompute
    assert comp.calls == 2


def test_kwargs_key_is_order_independent():
    comp = _Compute()
    c = SwrCache(comp, ttl=100)
    c.prime(a=1, b=2)
    assert c.is_fresh(b=2, a=1)     # same key regardless of kwarg order
    assert c.get(b=2, a=1)["n"] == 1
    assert comp.calls == 1


def test_maxsize_evicts_oldest():
    comp = _Compute()
    c = SwrCache(comp, ttl=100, maxsize=3)
    for i in range(5):
        c.prime(i)
        time.sleep(0.002)           # keep insertion timestamps ordered
    keys_present = [i for i in range(5) if c.peek(i) is not None]
    assert len(keys_present) == 3
    assert keys_present == [2, 3, 4]        # oldest two (0,1) evicted


def test_compute_error_keeps_last_good_value():
    state = {"boom": False}

    def compute(*a, **k):
        if state["boom"]:
            raise RuntimeError("compute failed")
        return {"ok": True}

    c = SwrCache(compute, ttl=0.05)
    c.prime(1)                      # good value stored
    state["boom"] = True
    time.sleep(0.08)
    # Stale: serve the last good value, kick a refresh that will raise + be swallowed.
    assert c.get(1) == {"ok": True}
    time.sleep(0.1)
    assert c.peek(1) == {"ok": True}    # last good value preserved despite the error


def test_invalidate_clears():
    comp = _Compute()
    c = SwrCache(comp, ttl=100)
    c.prime(1)
    c.prime(2)
    c.invalidate(1)
    assert c.peek(1) is None and c.peek(2) is not None
    c.invalidate()                  # no args → clear all
    assert c.peek(2) is None


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
    sys.exit(0)
