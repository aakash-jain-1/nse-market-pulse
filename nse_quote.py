"""
Per-stock quote / chart / depth via NSE's newer "NextApi" gateway
=================================================================
NSE's older /api/quote-equity is 403-blocked and /api/chart-databyindex returns
empty. The current website instead uses:

    /api/NextApi/apiClient/GetQuoteApi?functionName=<fn>&...

with a stock-specific Referer (/get-quote/equity/<SYMBOL>/...). That path DOES
work from our warmed session, which finally unlocks:
  - real per-stock quotes (LTP, OHLC, delivery %, price bands)  -> getSymbolData
  - 5-level market depth (order book)                          -> getSymbolData
  - real intraday chart points                                 -> getSymbolChartData

We reuse the session from nse_client and cache results briefly so we don't
hammer NSE (which will bot-block aggressive callers).
"""

import time

import nse_client as nse

NEXT = "/api/NextApi/apiClient/GetQuoteApi?functionName="

_cache = {}          # key -> (ts, data)
_warmed = set()      # symbols we've visited the quote page for this session
_QUOTE_TTL = 12      # seconds
_CHART_TTL = 30


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _referer(symbol):
    return {"Referer": nse.BASE + "/get-quote/equity/" + symbol}


def _warm(symbol):
    """Visit the stock's quote page once so NSE is happy with the context."""
    if symbol in _warmed:
        return
    try:
        nse.get_session().get(
            nse.BASE + "/get-quote/equity/" + symbol, timeout=15
        )
        _warmed.add(symbol)
    except Exception:
        pass


def _call(query, symbol):
    """GET a NextApi function, rebuilding the session once on failure."""
    _warm(symbol)
    url = nse.BASE + NEXT + query
    try:
        r = nse.get_session().get(url, headers=_referer(symbol), timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        _warmed.discard(symbol)
        _warm(symbol)
        r = nse.get_session(force=True).get(
            url, headers=_referer(symbol), timeout=15
        )
        r.raise_for_status()
        return r.json()


def get_quote(symbol, series="EQ"):
    """Normalized quote + 5-level market depth for a single symbol."""
    symbol = symbol.upper().strip()
    key = ("quote", symbol)
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _QUOTE_TTL:
        return hit[1]

    data = _call(
        f"getSymbolData&marketType=N&series={series}&symbol={symbol}", symbol
    )
    resp = (data.get("equityResponse") or [{}])[0]
    meta = resp.get("metaData", {})
    trade = resp.get("tradeInfo", {})
    price = resp.get("priceInfo", {})
    ob = resp.get("orderBook", {})

    bids, asks = [], []
    for i in range(1, 6):
        bids.append({
            "price": _num(ob.get(f"buyPrice{i}")),
            "qty": _num(ob.get(f"buyQuantity{i}")),
        })
        asks.append({
            "price": _num(ob.get(f"sellPrice{i}")),
            "qty": _num(ob.get(f"sellQuantity{i}")),
        })

    out = {
        "symbol": symbol,
        "companyName": meta.get("companyName"),
        "ltp": _num(trade.get("lastPrice")),
        "change": _num(meta.get("change")),
        "pChange": _num(meta.get("pChange")),
        "open": _num(meta.get("open")),
        "dayHigh": _num(meta.get("dayHigh")),
        "dayLow": _num(meta.get("dayLow")),
        "prevClose": _num(meta.get("previousClose")),
        "vwap": _num(meta.get("averagePrice")),
        "volume": _num(trade.get("totalTradedVolume")),
        "value": _num(trade.get("totalTradedValue")),
        "deliveryPct": _num(trade.get("deliveryToTradedQuantity")),
        "yearHigh": _num(price.get("yearHigh")),
        "yearLow": _num(price.get("yearLow")),
        "priceBand": price.get("priceBand"),
        "lastUpdateTime": resp.get("lastUpdateTime"),
        "depth": {"bids": bids, "asks": asks},
    }
    _cache[key] = (time.time(), out)
    return out


def get_chart(symbol, days="1D"):
    """Real intraday price points: list of {t: epoch_ms, price: float}."""
    symbol = symbol.upper().strip()
    key = ("chart", symbol, days)
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CHART_TTL:
        return hit[1]

    ident = symbol + "EQN"
    data = _call(f"getSymbolChartData&symbol={ident}&days={days}", symbol)
    points = []
    for row in data.get("grapthData", []) or []:
        if len(row) >= 2:
            points.append({"t": row[0], "price": _num(row[1])})
    out = {
        "symbol": symbol,
        "name": data.get("name"),
        "prevClose": _num(data.get("closePrice")),
        "points": points,
    }
    _cache[key] = (time.time(), out)
    return out


def get_ltp(symbol):
    """Just the last price, or None. Used as a paper-trading price fallback."""
    try:
        return get_quote(symbol).get("ltp")
    except Exception:
        return None


def _oc_referer(symbol):
    return {"Referer": nse.BASE + "/get-quote/optionchain/" + symbol}


def _oc_warm(symbol):
    if ("oc", symbol) in _warmed:
        return
    try:
        nse.get_session().get(
            nse.BASE + "/get-quote/optionchain/" + symbol, timeout=15
        )
        _warmed.add(("oc", symbol))
    except Exception:
        pass


def _deriv_referer(symbol):
    return {"Referer": nse.BASE + "/get-quote/derivatives?symbol=" + symbol}


def _deriv_warm(symbol):
    if ("deriv", symbol) in _warmed:
        return
    try:
        nse.get_session().get(
            nse.BASE + "/get-quote/derivatives?symbol=" + symbol, timeout=15
        )
        _warmed.add(("deriv", symbol))
    except Exception:
        pass


def get_symbol_futures(symbol):
    """
    Futures contracts (all expiries) for ANY F&O underlying, via
    getSymbolDerivativesData. Returns basis / premium-discount vs spot,
    annualized carry, OI and change-in-OI + a long/short buildup signal.
    Works for the full universe (not just the ~20 most-active names).
    """
    symbol = symbol.upper().strip()
    key = ("fut", symbol)
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _QUOTE_TTL:
        return hit[1]

    _deriv_warm(symbol)
    r = nse.get_session().get(
        nse.BASE + NEXT + f"getSymbolDerivativesData&symbol={symbol}",
        headers=_deriv_referer(symbol), timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    underlying = None
    futs = []
    for row in data.get("data", []) or []:
        itype = row.get("instrumentType")
        if itype not in ("FUTSTK", "FUTIDX"):
            continue
        spot = _num(row.get("underlyingValue"))
        underlying = underlying or spot
        fut = _num(row.get("lastPrice"))
        pchg = _num(row.get("pchange"))
        chg_oi = _num(row.get("changeinOpenInterest"))
        basis = basis_pct = annualized = None
        dte = nse._days_to_expiry(row.get("expiryDate"))
        if fut is not None and spot not in (None, 0):
            basis = fut - spot
            basis_pct = basis / spot * 100
            if dte and dte > 0:
                annualized = basis_pct * (365.0 / dte)
        label, kind = nse._oi_signal((chg_oi or 0) >= 0, pchg)
        futs.append({
            "symbol": symbol,
            "expiry": row.get("expiryDate"),
            "daysToExpiry": dte,
            "ltp": fut,
            "spot": spot,
            "pChange": pchg,
            "basis": round(basis, 2) if basis is not None else None,
            "basisPct": round(basis_pct, 2) if basis_pct is not None else None,
            "annualizedPct": round(annualized, 1) if annualized is not None else None,
            "oi": _num(row.get("openInterest")),
            "changeInOI": chg_oi,
            "volume": _num(row.get("totalTradedVolume")),
            "signal": label,
            "signalKind": kind,
        })
    futs.sort(key=lambda x: (nse._days_to_expiry(x["expiry"]) or 9999))
    out = {"symbol": symbol, "underlying": underlying, "futures": futs}
    _cache[key] = (time.time(), out)
    return out


def get_near_future(symbol):
    """Just the near-month futures row for a symbol (or None)."""
    try:
        futs = get_symbol_futures(symbol).get("futures") or []
        return futs[0] if futs else None
    except Exception:
        return None


def get_option_expiries(symbol):
    """List of expiry dates (e.g. '28-Jul-2026') available for a symbol."""
    symbol = symbol.upper().strip()
    _oc_warm(symbol)
    url = nse.BASE + NEXT + f"getOptionChainDropdown&symbol={symbol}"
    r = nse.get_session().get(url, headers=_oc_referer(symbol), timeout=15)
    r.raise_for_status()
    return r.json().get("expiryDates", []) or []


def _leg(d):
    return {
        "oi": _num(d.get("openInterest")),
        "chgOi": _num(d.get("changeinOpenInterest")),
        "pChgOi": _num(d.get("pchangeinOpenInterest")),
        "ltp": _num(d.get("lastPrice")),
        "change": _num(d.get("change")),
        "iv": _num(d.get("impliedVolatility")),
        "volume": _num(d.get("totalTradedVolume")),
        "bid": _num(d.get("buyPrice1")),
        "ask": _num(d.get("sellPrice1")),
    }


def _max_pain(rows):
    """Strike at which option writers lose the least (expected pinning level)."""
    strikes = [r["strike"] for r in rows if r["strike"] is not None]
    if not strikes:
        return None
    best, best_loss = None, None
    for expiry_price in strikes:
        loss = 0.0
        for r in rows:
            k = r["strike"]
            if k is None:
                continue
            ce_oi = (r["ce"] or {}).get("oi") or 0
            pe_oi = (r["pe"] or {}).get("oi") or 0
            if expiry_price > k:
                loss += ce_oi * (expiry_price - k)   # CE writers pay
            if expiry_price < k:
                loss += pe_oi * (k - expiry_price)   # PE writers pay
        if best_loss is None or loss < best_loss:
            best_loss, best = loss, expiry_price
    return best


def get_option_chain(symbol, expiry=None):
    """
    Normalized option chain for a symbol + expiry, with analytics:
      underlying, expiry, expiries[], rows[{strike, ce, pe}],
      pcr, maxPain, atmStrike, ceTotOI, peTotOI.
    """
    symbol = symbol.upper().strip()
    expiries = get_option_expiries(symbol)
    if not expiry:
        expiry = expiries[0] if expiries else None
    if not expiry:
        return {"symbol": symbol, "expiries": [], "rows": [], "error": "no expiries"}

    key = ("oc", symbol, expiry)
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _QUOTE_TTL:
        return hit[1]

    _oc_warm(symbol)
    q = f"getOptionChainData&symbol={symbol}&params=expiryDate={expiry}"
    r = nse.get_session().get(nse.BASE + NEXT + q, headers=_oc_referer(symbol), timeout=15)
    r.raise_for_status()
    data = r.json()

    underlying = _num(data.get("underlyingValue"))
    rows = []
    ce_tot = pe_tot = 0.0
    for item in data.get("data", []) or []:
        ce = item.get("CE") or {}
        pe = item.get("PE") or {}
        strike = _num((ce or pe).get("strikePrice"))
        ce_leg = _leg(ce) if ce else None
        pe_leg = _leg(pe) if pe else None
        if ce_leg and ce_leg["oi"]:
            ce_tot += ce_leg["oi"]
        if pe_leg and pe_leg["oi"]:
            pe_tot += pe_leg["oi"]
        rows.append({"strike": strike, "ce": ce_leg, "pe": pe_leg})

    rows.sort(key=lambda r: (r["strike"] is None, r["strike"]))
    atm = None
    if underlying and rows:
        atm = min(
            (r["strike"] for r in rows if r["strike"] is not None),
            key=lambda k: abs(k - underlying),
            default=None,
        )

    # Support = strikes with the biggest PUT OI (writers defend below spot).
    # Resistance = strikes with the biggest CALL OI (writers cap above spot).
    def _top(leg, n=3):
        vals = [
            {"strike": r["strike"], "oi": (r[leg] or {}).get("oi") or 0}
            for r in rows if r["strike"] is not None and (r[leg] or {}).get("oi")
        ]
        vals.sort(key=lambda x: -x["oi"])
        return vals[:n]

    out = {
        "symbol": symbol,
        "expiry": expiry,
        "expiries": expiries,
        "underlying": underlying,
        "timestamp": data.get("timestamp"),
        "rows": rows,
        "ceTotOI": ce_tot,
        "peTotOI": pe_tot,
        "pcr": round(pe_tot / ce_tot, 2) if ce_tot else None,
        "maxPain": _max_pain(rows),
        "atmStrike": atm,
        "support": _top("pe"),
        "resistance": _top("ce"),
    }
    _cache[key] = (time.time(), out)
    return out


def get_option_summary(symbol):
    """PCR / max-pain / OI across ALL expiries for a symbol, for comparison."""
    symbol = symbol.upper().strip()
    expiries = get_option_expiries(symbol)
    rows = []
    underlying = None
    for exp in expiries:
        try:
            oc = get_option_chain(symbol, exp)
        except Exception:
            continue
        underlying = underlying or oc.get("underlying")
        rows.append({
            "expiry": exp,
            "pcr": oc.get("pcr"),
            "maxPain": oc.get("maxPain"),
            "ceTotOI": oc.get("ceTotOI"),
            "peTotOI": oc.get("peTotOI"),
        })
    return {"symbol": symbol, "underlying": underlying, "expiries": rows}
