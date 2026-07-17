"""
Unit tests for paper.py — the virtual-portfolio engine (financial math).

Covers equity buy/sell (averaging, cash guard, oversell), options (lot sizing,
premium fills), and the tricky FUTURES path: margin posting, weighted-average
adds, proportional margin release + realized P&L on reduce/close, short selling,
and flip-through-zero. Plus portfolio() mark-to-market (incl. shorts profiting on
a drop) and reset().

Everything is stubbed: STATE_FILE points at a temp file and the price sources
(nse.get_price / get_lot_size / get_price_map, nse_quote.get_option_price /
get_near_future) are monkeypatched — no network, no shared state.

Run: python test_paper.py   (also works under pytest)
"""

import contextlib
import os
import shutil
import tempfile
import types

import nse_client as nse
import nse_quote
import paper


@contextlib.contextmanager
def _paper():
    """Isolate paper state + stub every price source. Yields a mutable store."""
    d = tempfile.mkdtemp(prefix="nse_paper_test_")
    saved_file = paper.STATE_FILE
    saved = (nse.get_price, nse.get_lot_size, nse.get_price_map,
             nse_quote.get_option_price, nse_quote.get_near_future)
    store = types.SimpleNamespace(price={}, pmap={}, lot={}, opt={}, fut={})
    paper.STATE_FILE = os.path.join(d, "paper_state.json")
    nse.get_price = lambda s: store.price.get(s)
    nse.get_lot_size = lambda s: store.lot.get(s)
    nse.get_price_map = lambda: store.pmap
    nse_quote.get_option_price = lambda u, e, st, ot: store.opt.get((u, e, float(st), ot))
    nse_quote.get_near_future = lambda s: store.fut.get(s)
    try:
        yield store
    finally:
        (nse.get_price, nse.get_lot_size, nse.get_price_map,
         nse_quote.get_option_price, nse_quote.get_near_future) = saved
        paper.STATE_FILE = saved_file
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------
def test_equity_buy_creates_position():
    with _paper() as s:
        s.price["RELIANCE"] = 100.0
        ok, msg, order = paper.place_order("reliance", "buy", 10)
        assert ok, msg
        pf = paper.portfolio()
        assert pf["cash"] == 1_000_000.0 - 1000
        pos = pf["positions"][0]
        assert pos["symbol"] == "RELIANCE" and pos["qty"] == 10
        assert pos["avgPrice"] == 100.0 and pos["kind"] == "equity"
        assert order["value"] == 1000.0


def test_equity_buy_averages_price():
    with _paper() as s:
        s.price["X"] = 100.0
        paper.place_order("X", "BUY", 10)
        s.price["X"] = 120.0
        paper.place_order("X", "BUY", 10)
        pf = paper.portfolio()
        pos = pf["positions"][0]
        assert pos["qty"] == 20 and pos["avgPrice"] == 110.0
        assert pf["cash"] == 1_000_000.0 - 1000 - 1200


def test_equity_sell_reduces_and_closes():
    with _paper() as s:
        s.price["X"] = 100.0
        paper.place_order("X", "BUY", 10)
        s.price["X"] = 130.0
        ok, _, _ = paper.place_order("X", "SELL", 4)
        assert ok
        pf = paper.portfolio()
        assert pf["positions"][0]["qty"] == 6
        # full close removes the position
        paper.place_order("X", "SELL", 6)
        assert paper.portfolio()["positions"] == []


def test_equity_oversell_rejected():
    with _paper() as s:
        s.price["X"] = 100.0
        paper.place_order("X", "BUY", 5)
        ok, msg, _ = paper.place_order("X", "SELL", 6)
        assert not ok and "hold" in msg


def test_equity_insufficient_cash():
    with _paper() as s:
        s.price["X"] = 100.0
        ok, msg, _ = paper.place_order("X", "BUY", 20000)  # 2,000,000 > 1,000,000
        assert not ok and "Insufficient" in msg


def test_equity_bad_inputs():
    with _paper() as s:
        s.price["X"] = 100.0
        assert paper.place_order("X", "HOLD", 1)[0] is False
        assert paper.place_order("X", "BUY", 0)[0] is False
        assert paper.place_order("X", "BUY", "abc")[0] is False


def test_equity_no_price():
    with _paper():
        ok, msg, _ = paper.place_order("GHOST", "BUY", 1)
        assert not ok and "No live price" in msg


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
def test_option_buy_lot_sized():
    with _paper() as s:
        s.lot["NIFTY"] = 50
        s.opt[("NIFTY", "2026-07-30", 25000.0, "CE")] = 120.0
        ok, msg, order = paper.place_option_order("NIFTY", "2026-07-30", 25000, "CE", "BUY", 2)
        assert ok, msg
        assert order["qty"] == 100 and order["lots"] == 2   # 2 lots x 50
        pf = paper.portfolio()
        assert pf["cash"] == 1_000_000.0 - 120.0 * 100
        pos = pf["positions"][0]
        assert pos["kind"] == "option" and pos["lots"] == 2 and pos["qty"] == 100


def test_option_bad_type_and_side():
    with _paper():
        assert paper.place_option_order("N", "2026-07-30", 100, "XX", "BUY", 1)[0] is False
        assert paper.place_option_order("N", "2026-07-30", 100, "CE", "HOLD", 1)[0] is False


def test_option_no_premium():
    with _paper() as s:
        s.lot["NIFTY"] = 50
        ok, msg, _ = paper.place_option_order("NIFTY", "2026-07-30", 25000, "CE", "BUY", 1)
        assert not ok and "premium" in msg


# ---------------------------------------------------------------------------
# Options — writing (sell-to-open a SHORT), cover, MTM, flip
# ---------------------------------------------------------------------------
def _opt_short(s, prem=120.0, spot=25000.0, lot=50):
    s.lot["NIFTY"] = lot
    s.price["NIFTY"] = spot                       # underlying spot → margin base
    s.opt[("NIFTY", "2026-07-30", 25000.0, "CE")] = prem


def test_option_write_short_receives_premium_and_posts_margin():
    with _paper() as s:
        _opt_short(s)
        ok, msg, order = paper.place_option_order(
            "NIFTY", "2026-07-30", 25000, "CE", "SELL", 1)     # no long held!
        assert ok, msg
        assert "short" in msg
        pf = paper.portfolio()
        # received 120*50 = 6000 premium, posted 50*25000*0.15 = 187500 margin
        assert pf["cash"] == 1_000_000.0 + 6000.0 - 187500.0
        pos = pf["positions"][0]
        assert pos["kind"] == "option" and pos["qty"] == -50 and pos["lots"] == 1
        assert pos["margin"] == 187500.0 and pos["avgPrice"] == 120.0
        # freshly opened → no P&L yet, equity still 10 lakh
        assert pos["pnl"] == 0.0 and pf["equity"] == 1_000_000.0


def test_option_short_profits_when_premium_drops():
    with _paper() as s:
        _opt_short(s, prem=120.0)
        paper.place_option_order("NIFTY", "2026-07-30", 25000, "CE", "SELL", 1)
        s.opt[("NIFTY", "2026-07-30", 25000.0, "CE")] = 80.0    # premium decays
        pf = paper.portfolio()
        pos = pf["positions"][0]
        assert pos["pnl"] == 2000.0                              # (120-80)*50
        assert pf["totalPnl"] == 2000.0


def test_option_short_cover_realizes_pnl_and_frees_margin():
    with _paper() as s:
        _opt_short(s, prem=120.0)
        paper.place_option_order("NIFTY", "2026-07-30", 25000, "CE", "SELL", 1)
        s.opt[("NIFTY", "2026-07-30", 25000.0, "CE")] = 80.0
        ok, msg, order = paper.place_option_order(
            "NIFTY", "2026-07-30", 25000, "CE", "BUY", 1)        # buy-to-cover
        assert ok, msg
        assert order["realized"] == 2000.0
        pf = paper.portfolio()
        assert pf["positions"] == []                             # flat
        assert pf["cash"] == 1_002_000.0 and pf["totalPnl"] == 2000.0


def test_option_sell_beyond_long_flips_to_short():
    with _paper() as s:
        _opt_short(s, prem=120.0)
        paper.place_option_order("NIFTY", "2026-07-30", 25000, "CE", "BUY", 1)   # long 1
        ok, msg, _ = paper.place_option_order(
            "NIFTY", "2026-07-30", 25000, "CE", "SELL", 2)       # sell 2 → net short 1
        assert ok, msg
        pos = paper.portfolio()["positions"][0]
        assert pos["qty"] == -50 and pos["margin"] == 187500.0 and pos["avgPrice"] == 120.0


def test_option_short_insufficient_margin_rejected():
    with _paper() as s:
        _opt_short(s, prem=120.0, spot=25000.0)
        # 100 lots → margin 100*50*25000*0.15 ≈ 1.875 cr ≫ 10 lakh, even with premium
        ok, msg, _ = paper.place_option_order(
            "NIFTY", "2026-07-30", 25000, "CE", "SELL", 100)
        assert not ok and "margin" in msg


# ---------------------------------------------------------------------------
# Futures — margin, averaging, realize, short, flip
# ---------------------------------------------------------------------------
def _fut(s, symbol="ACME", ltp=100.0, expiry="2026-07-30", lot=50):
    s.lot[symbol] = lot
    s.fut[symbol] = {"ltp": ltp, "expiry": expiry}


def test_futures_long_posts_margin():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        ok, msg, order = paper.place_futures_order("ACME", "BUY", 1)
        assert ok, msg
        # margin = 50 * 100 * 0.15 = 750
        assert paper.portfolio()["cash"] == 1_000_000.0 - 750.0
        pos = paper.portfolio()["positions"][0]
        assert pos["qty"] == 50 and pos["margin"] == 750.0 and pos["avgPrice"] == 100.0


def test_futures_add_same_side_weighted_avg():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        paper.place_futures_order("ACME", "BUY", 1)
        s.fut["ACME"]["ltp"] = 110.0
        paper.place_futures_order("ACME", "BUY", 1)
        pos = paper.portfolio()["positions"][0]
        assert pos["qty"] == 100 and pos["avgPrice"] == 105.0
        assert pos["margin"] == 750.0 + 825.0                # 50*110*.15 = 825


def test_futures_reduce_realizes_and_frees_margin():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        paper.place_futures_order("ACME", "BUY", 2)          # qty 100, margin 1500
        s.fut["ACME"]["ltp"] = 120.0
        cash_before = paper.portfolio()["cash"]
        paper.place_futures_order("ACME", "SELL", 1)          # close 50 @120
        pos = paper.portfolio()["positions"][0]
        assert pos["qty"] == 50 and pos["avgPrice"] == 100.0  # avg unchanged on reduce
        assert pos["margin"] == 750.0                         # half of 1500 freed
        # realized = (120-100)*50 = 1000; margin freed 750 -> cash += 1750
        assert round(paper.portfolio()["cash"] - cash_before, 2) == 1750.0


def test_futures_full_close_removes_position():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        paper.place_futures_order("ACME", "BUY", 1)
        s.fut["ACME"]["ltp"] = 90.0
        ok, msg, order = paper.place_futures_order("ACME", "SELL", 1)
        assert ok and order["realized"] == -500.0            # (90-100)*50
        assert paper.portfolio()["positions"] == []


def test_futures_short_from_flat():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        ok, _, _ = paper.place_futures_order("ACME", "SELL", 1)
        assert ok
        pos = paper.portfolio()["positions"][0]
        assert pos["qty"] == -50 and pos["margin"] == 750.0


def test_futures_flip_through_zero():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        paper.place_futures_order("ACME", "BUY", 2)           # long 100 @100
        s.fut["ACME"]["ltp"] = 110.0
        ok, msg, order = paper.place_futures_order("ACME", "SELL", 3)  # -150 => flip to -50
        assert ok
        pos = paper.portfolio()["positions"][0]
        assert pos["qty"] == -50                              # net short 50
        assert pos["avgPrice"] == 110.0                       # fresh leg at fill
        assert pos["margin"] == 50 * 110 * 0.15               # 825, new leg only
        assert order["realized"] == (110 - 100) * 100         # 1000 on the closed 100


def test_futures_insufficient_margin():
    with _paper() as s:
        _fut(s, ltp=100.0, lot=50)
        ok, msg, _ = paper.place_futures_order("ACME", "BUY", 2000)  # margin 1.5M > 1M
        assert not ok and "margin" in msg


# ---------------------------------------------------------------------------
# Portfolio MTM
# ---------------------------------------------------------------------------
def test_portfolio_equity_mtm():
    with _paper() as s:
        s.price["X"] = 100.0
        paper.place_order("X", "BUY", 10)
        s.pmap["X"] = 130.0                                   # reprice source for equity
        pf = paper.portfolio()
        pos = pf["positions"][0]
        assert pos["ltp"] == 130.0 and pos["pnl"] == 300.0
        assert pf["equity"] == round(pf["cash"] + 130.0 * 10, 2)
        assert pf["totalPnl"] == 300.0


def test_portfolio_short_future_profits_on_drop():
    with _paper() as s:
        _fut(s, ltp=110.0, lot=50)
        paper.place_futures_order("ACME", "SELL", 1)          # short 50 @110, margin 825
        s.fut["ACME"]["ltp"] = 100.0                          # price drops -> short profit
        pf = paper.portfolio()
        pos = pf["positions"][0]
        assert pos["pnl"] == (100 - 110) * -50                # +500
        assert pos["value"] == round(825.0 + 500.0, 2)        # margin + unrealized
        assert pf["totalPnl"] == 500.0


def test_portfolio_option_mtm():
    with _paper() as s:
        s.lot["NIFTY"] = 50
        s.opt[("NIFTY", "2026-07-30", 25000.0, "CE")] = 100.0
        paper.place_option_order("NIFTY", "2026-07-30", 25000, "CE", "BUY", 1)
        s.opt[("NIFTY", "2026-07-30", 25000.0, "CE")] = 150.0
        pf = paper.portfolio()
        assert pf["positions"][0]["pnl"] == (150 - 100) * 50  # +2500


def test_reset_restores_default():
    with _paper() as s:
        s.price["X"] = 100.0
        paper.place_order("X", "BUY", 10)
        paper.reset()
        pf = paper.portfolio()
        assert pf["cash"] == 1_000_000.0 and pf["positions"] == [] and pf["orders"] == []


def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} paper tests passed")


if __name__ == "__main__":
    _main()
