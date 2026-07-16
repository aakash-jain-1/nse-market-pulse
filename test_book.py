"""
Unit tests for nse_quote.get_book_stats() — the Scanner "Order-book scan" backend.

get_book_stats fans out the 12s-cached get_quote() over a small pool and derives
ΣBid/ΣAsk imbalance + spread per symbol. These tests stub get_quote so nothing
touches the network, and assert the imbalance/spread MATH, symbol sanitisation /
dedupe / cap, empty-book omission, and per-symbol error isolation.

This is the same imbalance formula the frontend mirrors in depthStats() — testing
it here pins the risk-bearing arithmetic.

Run: python test_book.py   (also works under pytest)
"""

import contextlib
import threading

import nse_quote


def _quote(symbol, bids, asks, ltp=100.0):
    """bids/asks are lists of (price, qty) tuples."""
    return {
        "symbol": symbol, "ltp": ltp,
        "depth": {
            "bids": [{"price": p, "qty": q} for p, q in bids],
            "asks": [{"price": p, "qty": q} for p, q in asks],
        },
    }


@contextlib.contextmanager
def _patch_quote(mapping, fail=None):
    """Stub nse_quote.get_quote from a {symbol: quote} map; `fail` symbols raise.

    Records every symbol the code actually fetched (thread-safe) so we can assert
    sanitisation / dedupe / cap behaviour.
    """
    calls, lock = [], threading.Lock()
    orig = nse_quote.get_quote

    def fake(symbol, series="EQ"):
        with lock:
            calls.append(symbol)
        if fail and symbol in fail:
            raise RuntimeError("boom " + symbol)
        return mapping[symbol]

    nse_quote.get_quote = fake
    try:
        yield calls
    finally:
        nse_quote.get_quote = orig


def test_buy_heavy_positive_imbalance():
    # ΣBid 500 vs ΣAsk 100 -> (500-100)/600*100 = 66.7
    q = _quote("AAA", bids=[(99, 100)] * 5, asks=[(101, 20)] * 5)
    with _patch_quote({"AAA": q}):
        out = nse_quote.get_book_stats(["AAA"])
    assert out["AAA"]["imb"] == 66.7
    assert out["AAA"]["totB"] == 500 and out["AAA"]["totA"] == 100


def test_sell_heavy_negative_imbalance():
    q = _quote("BBB", bids=[(99, 20)] * 5, asks=[(101, 100)] * 5)
    with _patch_quote({"BBB": q}):
        out = nse_quote.get_book_stats(["BBB"])
    assert out["BBB"]["imb"] == -66.7


def test_balanced_book_zero_imbalance():
    q = _quote("CCC", bids=[(99, 50)] * 5, asks=[(101, 50)] * 5)
    with _patch_quote({"CCC": q}):
        out = nse_quote.get_book_stats(["CCC"])
    assert out["CCC"]["imb"] == 0.0


def test_spread_bps():
    # best bid 99, best ask 101 -> spread 2, mid 100 -> 200.0 bps
    q = _quote("DDD", bids=[(99, 10)], asks=[(101, 10)])
    with _patch_quote({"DDD": q}):
        out = nse_quote.get_book_stats(["DDD"])
    assert out["DDD"]["spreadBps"] == 200.0
    assert out["DDD"]["bestBid"] == 99 and out["DDD"]["bestAsk"] == 101


def test_empty_book_is_omitted():
    # After hours: all qty 0 -> totB+totA == 0 -> symbol dropped from result.
    q = _quote("EEE", bids=[(0, 0)] * 5, asks=[(0, 0)] * 5)
    with _patch_quote({"EEE": q}):
        out = nse_quote.get_book_stats(["EEE"])
    assert out == {}


def test_ltp_passthrough():
    q = _quote("FFF", bids=[(99, 10)], asks=[(101, 10)], ltp=1234.5)
    with _patch_quote({"FFF": q}):
        out = nse_quote.get_book_stats(["FFF"])
    assert out["FFF"]["ltp"] == 1234.5


def test_sanitises_and_dedupes_symbols():
    q = _quote("AAA", bids=[(99, 10)], asks=[(101, 10)])
    m = {"AAA": q, "BBB": _quote("BBB", [(9, 10)], [(11, 10)])}
    # lowercase upper-cased, junk chars stripped, blanks dropped, dupes collapsed.
    with _patch_quote(m) as calls:
        out = nse_quote.get_book_stats(["aaa", "AAA", "b#b@b", "  ", None])
    assert sorted(calls) == ["AAA", "BBB"]      # exactly the two unique clean names
    assert set(out) == {"AAA", "BBB"}


def test_limit_caps_fanout():
    m = {f"S{i}": _quote(f"S{i}", [(99, 10)], [(101, 10)]) for i in range(40)}
    with _patch_quote(m) as calls:
        out = nse_quote.get_book_stats([f"S{i}" for i in range(40)], limit=5)
    assert len(calls) == 5 and len(out) == 5


def test_hard_cap_at_book_max():
    m = {f"S{i}": _quote(f"S{i}", [(99, 10)], [(101, 10)]) for i in range(60)}
    with _patch_quote(m) as calls:
        # ask for more than the hard cap -> still capped at _BOOK_MAX
        out = nse_quote.get_book_stats([f"S{i}" for i in range(60)], limit=999)
    assert len(calls) == nse_quote._BOOK_MAX
    assert len(out) == nse_quote._BOOK_MAX


def test_per_symbol_error_isolated():
    m = {"OK1": _quote("OK1", [(99, 10)], [(101, 10)]),
         "BAD": _quote("BAD", [(99, 10)], [(101, 10)]),
         "OK2": _quote("OK2", [(99, 10)], [(101, 10)])}
    with _patch_quote(m, fail={"BAD"}):
        out = nse_quote.get_book_stats(["OK1", "BAD", "OK2"])
    assert set(out) == {"OK1", "OK2"}          # BAD raised -> omitted, others fine


def test_empty_input_returns_empty():
    assert nse_quote.get_book_stats([]) == {}
    assert nse_quote.get_book_stats(None) == {}


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} book-stats tests passed")


if __name__ == "__main__":
    _main()
