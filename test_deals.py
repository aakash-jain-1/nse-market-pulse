"""
Unit tests for deals.py — NSE bulk/block-deal parsing + cached fetch.

The PURE parser (parse_deals) is driven with hand-built CSV text using the REAL
NSE column names (incl. the "NO RECORDS" sentinel block files ship on quiet days),
so a format change fails loudly here. The fetch/cache layer is exercised with a
stubbed bhavcopy._download (a url→bytes map) so no network is touched, and the
30-min TTL / force-refresh path is verified via a call counter.

Run: python test_deals.py   (also works under pytest)
"""

import contextlib

import bhavcopy
import deals as D


@contextlib.contextmanager
def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def _reset_cache():
    saved = {k: dict(v) for k, v in D._cache.items()}
    for k in D._cache:
        D._cache[k].update(ts=0.0, deals=[], date=None)
    try:
        yield
    finally:
        for k, v in saved.items():
            D._cache[k].clear()
            D._cache[k].update(v)


_HEADER = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
           "Trade Price / Wght. Avg. Price,Remarks")


def _row(sym, side, qty, price, date="17-Jul-2026", client="BIG FUND", name="Acme Ltd"):
    return f"{date},{sym},{name},{client},{side},{qty},{price},-"


def _csv(rows):
    return "\n".join([_HEADER, *rows]) + "\n"


# ---------------------------------------------------------------------------
# parse_deals (pure)
# ---------------------------------------------------------------------------
def test_parse_deals_basic():
    text = _csv([
        _row("ACME", "BUY", "100000", "251.5"),
        _row("BETA", "SELL", "50000", "1234.75"),
    ])
    out = D.parse_deals(text)
    assert len(out) == 2
    a = out[0]
    assert a["symbol"] == "ACME" and a["side"] == "BUY"
    assert a["qty"] == 100000.0 and a["price"] == 251.5
    assert a["client"] == "BIG FUND" and a["date"] == "17-Jul-2026"
    assert out[1]["price"] == 1234.75


def test_num_strips_thousands_commas():
    # Defensive: some NSE renderings carry grouped digits.
    assert D._num("1,00,000") == 100000.0
    assert D._num("1,234.75") == 1234.75
    assert D._num("-") is None and D._num("") is None and D._num(None) is None


def test_parse_deals_no_records_sentinel():
    # Block-deal files on a quiet day ship a single "NO RECORDS" line.
    assert D.parse_deals("NO RECORDS\n") == []
    assert D.parse_deals(_HEADER + "\n") == []


def test_parse_deals_skips_blank_symbol_and_bad_numbers():
    text = _csv([
        _row("", "BUY", "100", "10"),          # blank symbol → skipped
        _row("GOODX", "buy", "-", "NA"),       # unparseable numbers → None, still kept
    ])
    out = D.parse_deals(text)
    assert len(out) == 1
    assert out[0]["symbol"] == "GOODX" and out[0]["side"] == "BUY"
    assert out[0]["qty"] is None and out[0]["price"] is None


# ---------------------------------------------------------------------------
# latest / recent / by_symbol — cached fetch (stubbed _download)
# ---------------------------------------------------------------------------
def test_latest_caches_and_force_refreshes():
    calls = {"n": 0}
    body = _csv([_row("ACME", "BUY", "1000", "10")]).encode()

    def fake_dl(url):
        calls["n"] += 1
        return body

    with _reset_cache(), _patch(bhavcopy, "_download", fake_dl):
        c1 = D.latest("bulk")
        assert calls["n"] == 1 and len(c1["deals"]) == 1 and c1["date"] == "17-Jul-2026"
        D.latest("bulk")                       # within TTL → served from cache
        assert calls["n"] == 1
        D.latest("bulk", force=True)           # force → refetch
        assert calls["n"] == 2


def test_recent_shape_and_limit():
    body = _csv([_row(f"S{i}", "BUY", "10", "1") for i in range(5)]).encode()
    with _reset_cache(), _patch(bhavcopy, "_download", lambda url: body):
        r = D.recent("bulk", limit=3)
    assert r["kind"] == "bulk" and r["count"] == 5 and len(r["deals"]) == 3
    assert r["date"] == "17-Jul-2026"


def test_by_symbol_groups_and_block_kind():
    body = _csv([
        _row("ACME", "BUY", "10", "1"),
        _row("ACME", "SELL", "20", "2"),
        _row("BETA", "BUY", "30", "3"),
    ]).encode()
    with _reset_cache(), _patch(bhavcopy, "_download", lambda url: body):
        m = D.by_symbol("block")               # kind routes to the block URL
    assert set(m) == {"ACME", "BETA"} and len(m["ACME"]) == 2


def test_latest_empty_on_download_failure():
    with _reset_cache(), _patch(bhavcopy, "_download", lambda url: None):
        c = D.latest("block")
    assert c["deals"] == [] and c["date"] is None


def test_status_shape():
    body = _csv([_row("ACME", "BUY", "10", "1")]).encode()
    with _reset_cache(), _patch(bhavcopy, "_download", lambda url: body):
        st = D.status(refresh=True)
    assert set(st) >= {"bulk", "block", "ttlSec", "source"}
    assert st["bulk"]["count"] == 1 and st["bulk"]["cached"] is True
    assert st["ttlSec"] == D._TTL


def test_kind_defaults_to_bulk_for_garbage():
    assert D._kind("weird") == "bulk"
    assert D._kind("BLOCK") == "block"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
