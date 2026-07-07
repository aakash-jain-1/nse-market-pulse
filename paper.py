"""
Paper trading engine
=====================
A simple virtual-portfolio simulator. No broker, no real money — orders are
"filled" at the latest live price we can source from NSE (see
nse_client.get_price). State is persisted to a JSON file so it survives
restarts.

This is intentionally broker-agnostic: when a real broker feed is added later,
only the price source / fill logic needs to change, not the portfolio math.
"""

import json
import os
import threading
import time
from datetime import datetime

import nse_client as nse

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")
STARTING_CASH = 1_000_000.0  # Rs 10 lakh virtual capital

_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_state():
    return {
        "cash": STARTING_CASH,
        "startingCash": STARTING_CASH,
        # positions: symbol -> {qty, avgPrice}
        "positions": {},
        # orders: list of executed order dicts (newest last)
        "orders": [],
        "createdAt": _now(),
    }


def _load():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_state()


def _save(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def reset():
    with _lock:
        state = _default_state()
        _save(state)
        return state


def place_order(symbol, side, qty):
    """
    Execute a simulated market order. side is 'BUY' or 'SELL'.
    Returns (ok, message, order_or_None).
    """
    symbol = (symbol or "").upper().strip()
    side = (side or "").upper().strip()
    if side not in ("BUY", "SELL"):
        return False, "Side must be BUY or SELL", None
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return False, "Quantity must be a whole number", None
    if qty <= 0:
        return False, "Quantity must be positive", None

    price = nse.get_price(symbol)
    if price is None:
        return (
            False,
            f"No live price for {symbol}. Paper trading is limited to symbols "
            f"currently in the live lists (most active / gainers / OI).",
            None,
        )

    with _lock:
        state = _load()
        pos = state["positions"].get(symbol)
        cost = price * qty

        if side == "BUY":
            if cost > state["cash"]:
                return (
                    False,
                    f"Insufficient virtual cash: need Rs {cost:,.0f}, "
                    f"have Rs {state['cash']:,.0f}",
                    None,
                )
            state["cash"] -= cost
            if pos:
                total_qty = pos["qty"] + qty
                pos["avgPrice"] = (
                    pos["avgPrice"] * pos["qty"] + cost
                ) / total_qty
                pos["qty"] = total_qty
            else:
                state["positions"][symbol] = {
                    "qty": qty, "avgPrice": price, "kind": "equity",
                    "label": symbol,
                }
        else:  # SELL
            held = pos["qty"] if pos else 0
            if qty > held:
                return (
                    False,
                    f"Cannot sell {qty} {symbol}: you hold {held}",
                    None,
                )
            state["cash"] += cost
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del state["positions"][symbol]

        order = {
            "time": _now(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": round(price, 2),
            "value": round(cost, 2),
        }
        state["orders"].append(order)
        _save(state)
        return True, "Order executed", order


def place_option_order(underlying, expiry, strike, opt_type, side, qty):
    """
    Simulated option order. Fills at the current premium (LTP) from the option
    chain. Positions are keyed by the full contract so CE/PE and each strike are
    tracked separately. Quantity is in units (lot sizes are not enforced — this
    is a simplified paper simulation).
    """
    import nse_quote

    underlying = (underlying or "").upper().strip()
    opt_type = (opt_type or "").upper().strip()
    side = (side or "").upper().strip()
    if opt_type not in ("CE", "PE"):
        return False, "Option type must be CE or PE", None
    if side not in ("BUY", "SELL"):
        return False, "Side must be BUY or SELL", None
    try:
        qty = int(qty)
        strike = float(strike)
    except (TypeError, ValueError):
        return False, "Invalid strike or quantity", None
    if qty <= 0:
        return False, "Quantity must be positive", None
    if not expiry:
        return False, "Expiry is required", None

    price = nse_quote.get_option_price(underlying, expiry, strike, opt_type)
    if price is None or price <= 0:
        return False, f"No live premium for {underlying} {strike:g}{opt_type} {expiry}", None

    key = f"{underlying}|{expiry}|{strike:g}|{opt_type}"
    label = f"{underlying} {strike:g}{opt_type} {expiry}"

    with _lock:
        state = _load()
        pos = state["positions"].get(key)
        cost = price * qty

        if side == "BUY":
            if cost > state["cash"]:
                return (False,
                        f"Insufficient virtual cash: need Rs {cost:,.0f}, "
                        f"have Rs {state['cash']:,.0f}", None)
            state["cash"] -= cost
            if pos:
                total_qty = pos["qty"] + qty
                pos["avgPrice"] = (pos["avgPrice"] * pos["qty"] + cost) / total_qty
                pos["qty"] = total_qty
            else:
                state["positions"][key] = {
                    "qty": qty, "avgPrice": price, "kind": "option",
                    "label": label, "underlying": underlying, "expiry": expiry,
                    "strike": strike, "optType": opt_type,
                }
        else:  # SELL
            held = pos["qty"] if pos else 0
            if qty > held:
                return False, f"Cannot sell {qty} {label}: you hold {held}", None
            state["cash"] += cost
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del state["positions"][key]

        order = {
            "time": _now(), "symbol": label, "side": side, "qty": qty,
            "price": round(price, 2), "value": round(cost, 2),
        }
        state["orders"].append(order)
        _save(state)
        return True, "Order executed", order


def _reprice(key, pos):
    """Current LTP for a position, re-fetching option premiums as needed."""
    if pos.get("kind") == "option":
        import nse_quote
        p = nse_quote.get_option_price(
            pos.get("underlying"), pos.get("expiry"),
            pos.get("strike"), pos.get("optType"),
        )
        return p if p is not None else pos["avgPrice"]
    return nse.get_price_map().get(key, pos["avgPrice"])


def portfolio():
    """Return the portfolio with live mark-to-market P&L."""
    with _lock:
        state = _load()

    positions = []
    holdings_value = 0.0
    invested = 0.0
    for key, pos in state["positions"].items():
        ltp = _reprice(key, pos)
        mkt = ltp * pos["qty"]
        cost = pos["avgPrice"] * pos["qty"]
        pnl = mkt - cost
        holdings_value += mkt
        invested += cost
        positions.append(
            {
                "symbol": pos.get("label", key),
                "kind": pos.get("kind", "equity"),
                "qty": pos["qty"],
                "avgPrice": round(pos["avgPrice"], 2),
                "ltp": round(ltp, 2),
                "value": round(mkt, 2),
                "pnl": round(pnl, 2),
                "pnlPct": round((pnl / cost * 100) if cost else 0, 2),
            }
        )

    positions.sort(key=lambda p: p["pnl"], reverse=True)
    equity = state["cash"] + holdings_value
    total_pnl = equity - state["startingCash"]
    return {
        "cash": round(state["cash"], 2),
        "holdingsValue": round(holdings_value, 2),
        "invested": round(invested, 2),
        "equity": round(equity, 2),
        "startingCash": state["startingCash"],
        "totalPnl": round(total_pnl, 2),
        "totalPnlPct": round(total_pnl / state["startingCash"] * 100, 2),
        "positions": positions,
        "orders": list(reversed(state["orders"]))[:50],
    }
