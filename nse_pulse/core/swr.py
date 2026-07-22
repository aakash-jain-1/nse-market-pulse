"""Stale-while-revalidate (SWR) cache for expensive, cacheable computations.

Serves the last computed value INSTANTLY and recomputes in a background daemon
thread when the value is stale, so a request never blocks on a cold/expired
recompute. This generalises the hand-rolled pattern already used in
``sim.current_regime()`` so the heavy analytics endpoints
(``strategy_of_day`` / the conviction ``board``) can share one tested implementation.

Semantics of ``get(...)`` for a given argument key:

* **fresh** (age < ``ttl``): return the cached value.
* **stale** (cached but older than ``ttl``): return the stale value immediately AND
  kick a single-flight background refresh (at most one in-flight refresh per key).
* **cold** (never computed): kick the refresh and return ``placeholder`` (a static
  value or a callable evaluated with the same args) — typically a small "warming"
  shape the frontend can render harmlessly.

``should_refresh`` (optional, no-arg) can veto *starting* a new refresh — e.g. during
an NSE WAF cooldown — in which case the stale/placeholder value is still served but no
new work is scheduled; the next call retries the veto.

The compute runs OUTSIDE the lock, so serving stale values never blocks behind a
recompute. Only the tiny store read/write is locked.
"""

import threading
import time

_UNSET = object()


class SwrCache:
    """Keyed stale-while-revalidate cache. See module docstring for semantics."""

    def __init__(self, compute, ttl, *, placeholder=None, should_refresh=None,
                 maxsize=32, name="swr"):
        self._compute = compute
        self.ttl = float(ttl)
        self._placeholder = placeholder
        self._should_refresh = should_refresh
        self._maxsize = int(maxsize)
        self._name = name
        self._store = {}        # key -> (ts, value)
        self._running = set()   # keys with an in-flight background refresh
        self._lock = threading.Lock()

    @staticmethod
    def _key(args, kwargs):
        return (args, tuple(sorted(kwargs.items())))

    def _placeholder_for(self, args, kwargs):
        if callable(self._placeholder):
            return self._placeholder(*args, **kwargs)
        return self._placeholder

    def _store_value(self, key, value):
        with self._lock:
            self._store[key] = (time.time(), value)
            # Evict the oldest entries if we're over the cap (filters produce many
            # keys for the board); keep the cache bounded without a hard dependency.
            if len(self._store) > self._maxsize:
                for k in sorted(self._store, key=lambda k: self._store[k][0]
                                )[:len(self._store) - self._maxsize]:
                    self._store.pop(k, None)

    def _refresh(self, key, args, kwargs):
        try:
            value = self._compute(*args, **kwargs)
            self._store_value(key, value)
        except Exception:
            # Keep the last good value; a later call retries. Never let a background
            # compute crash take down the daemon thread silently-with-a-traceback.
            pass
        finally:
            with self._lock:
                self._running.discard(key)

    def _kick(self, key, args, kwargs):
        """Start a single-flight background refresh if allowed. Caller holds no lock."""
        if self._should_refresh is not None:
            try:
                if not self._should_refresh():
                    return
            except Exception:
                pass
        with self._lock:
            if key in self._running:
                return
            self._running.add(key)
        threading.Thread(target=self._refresh, args=(key, args, kwargs),
                         name=self._name, daemon=True).start()

    def get(self, *args, **kwargs):
        """Return fresh value, else stale (kicking a refresh), else the placeholder."""
        key = self._key(args, kwargs)
        now = time.time()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and (now - hit[0]) < self.ttl:
                return hit[1]
        self._kick(key, args, kwargs)
        if hit is not None:
            return hit[1]
        return self._placeholder_for(args, kwargs)

    def peek(self, *args, **kwargs):
        """Cached value if present (any age), else ``None``. Never computes/kicks."""
        with self._lock:
            hit = self._store.get(self._key(args, kwargs))
        return hit[1] if hit is not None else None

    def prime(self, *args, **kwargs):
        """Compute synchronously now and store it (for startup pre-warm). Blocking."""
        value = self._compute(*args, **kwargs)
        self._store_value(self._key(args, kwargs), value)
        return value

    def is_fresh(self, *args, **kwargs):
        with self._lock:
            hit = self._store.get(self._key(args, kwargs))
            return hit is not None and (time.time() - hit[0]) < self.ttl

    def invalidate(self, *args, **kwargs):
        """Drop one key (no args → clear all). Mainly for tests."""
        with self._lock:
            if not args and not kwargs:
                self._store.clear()
            else:
                self._store.pop(self._key(args, kwargs), None)
