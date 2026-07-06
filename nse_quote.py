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
