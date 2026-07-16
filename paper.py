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
from datetime import datetime, timedelta, timezone

import nse_client as nse

# NSE trades in IST; stamp order timestamps in IST so they line up with the sim,
# ideas journal, snapshots and DB rows rather than the host's local clock (N5).
IST = timezone(timedelta(hours=5, minutes=30))

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")
STARTING_CASH = 1_000_000.0  # Rs 10 lakh virtual capital
FUT_MARGIN_RATE = 0.15  # approx SPAN+exposure margin (~6.6x leverage) for paper

_lock = threading.Lock()


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


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


def place_option_order(underlying, expiry, strike, opt_type, side, lots):
    """
    Simulated option order sized in LOTS (real F&O sizing). Fills at the current
    premium (LTP) from the option chain. Total quantity = lots x the underlying's
    market lot; cost = premium x quantity. Positions are keyed by the full
    contract so CE/PE and each strike are tracked separately.
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
        lots = int(lots)
        strike = float(strike)
    except (TypeError, ValueError):
        return False, "Invalid strike or lots", None
    if lots <= 0:
        return False, "Lots must be positive", None
    if not expiry:
        return False, "Expiry is required", None

    lot_size = nse.get_lot_size(underlying) or 1
    qty = lots * lot_size

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
                        f"Insufficient virtual cash: need Rs {cost:,.0f} "
                        f"({lots} lot(s) x {lot_size}), have Rs {state['cash']:,.0f}", None)
            state["cash"] -= cost
            if pos:
                total_qty = pos["qty"] + qty
                pos["avgPrice"] = (pos["avgPrice"] * pos["qty"] + cost) / total_qty
                pos["qty"] = total_qty
                pos["lots"] = pos.get("lots", 0) + lots
            else:
                state["positions"][key] = {
                    "qty": qty, "avgPrice": price, "kind": "option",
                    "label": label, "underlying": underlying, "expiry": expiry,
                    "strike": strike, "optType": opt_type,
                    "lots": lots, "lotSize": lot_size,
                }
        else:  # SELL
            held = pos["qty"] if pos else 0
            if qty > held:
                held_lots = held // lot_size if lot_size else held
                return False, f"Cannot sell {lots} lot(s) of {label}: you hold {held_lots} lot(s)", None
            state["cash"] += cost
            pos["qty"] -= qty
            pos["lots"] = pos.get("lots", 0) - lots
            if pos["qty"] == 0:
                del state["positions"][key]

        order = {
            "time": _now(), "symbol": label, "side": side, "qty": qty,
            "lots": lots, "lotSize": lot_size,
            "price": round(price, 2), "value": round(cost, 2),
        }
        state["orders"].append(order)
        _save(state)
        return True, f"Order executed: {lots} lot(s) x {lot_size} = {qty} units", order


def place_futures_order(symbol, side, lots):
    """
    Simulated stock/index FUTURES order, sized in LOTS and margin-based (not full
    notional). Supports long AND short: BUY adds long / covers short, SELL adds
    short / reduces long. On open we post margin (~FUT_MARGIN_RATE of notional)
    from cash; on close we release margin proportionally and realize P&L. Fills
    at the live near-month futures price.
    """
    import nse_quote

    symbol = (symbol or "").upper().strip()
    side = (side or "").upper().strip()
    if side not in ("BUY", "SELL"):
        return False, "Side must be BUY or SELL", None
    try:
        lots = int(lots)
    except (TypeError, ValueError):
        return False, "Lots must be a whole number", None
    if lots <= 0:
        return False, "Lots must be positive", None

    fut = nse_quote.get_near_future(symbol)
    if not fut or fut.get("ltp") is None:
        return False, f"No live futures price for {symbol} (F&O only)", None
    price = fut["ltp"]
    expiry = fut.get("expiry")
    lot_size = nse.get_lot_size(symbol) or 1
    units = lots * lot_size
    signed = units if side == "BUY" else -units

    key = f"FUT|{symbol}|{expiry}"
    label = f"{symbol} FUT {expiry}"

    with _lock:
        state = _load()
        pos = state["positions"].get(key)
        old_qty = pos["qty"] if pos else 0  # signed units

        realized = 0.0
        margin_freed = 0.0
        margin_needed = 0.0

        # Portion that closes/reduces the existing opposite position.
        closing = 0
        if pos and (old_qty > 0) != (signed > 0) and old_qty != 0:
            closing = min(abs(signed), abs(old_qty))
        opening = abs(signed) - closing

        if closing:
            # Realize P&L on the closed units and free proportional margin.
            direction = 1 if old_qty > 0 else -1
            realized = (price - pos["avgPrice"]) * closing * direction
            # .get: legacy future positions predate margin tracking (AUDIT2 N4).
            margin_freed = pos.get("margin", 0.0) * (closing / abs(old_qty))

        if opening:
            margin_needed = opening * price * FUT_MARGIN_RATE
            avail = state["cash"] + margin_freed + realized
            if margin_needed > avail:
                return (False,
                        f"Insufficient margin: need Rs {margin_needed:,.0f} "
                        f"({lots} lot(s) x {lot_size} @ {FUT_MARGIN_RATE*100:.0f}%), "
                        f"have Rs {avail:,.0f}", None)

        state["cash"] += margin_freed + realized - margin_needed
        new_qty = old_qty + signed

        if new_qty == 0:
            if pos:
                del state["positions"][key]
        else:
            if not pos:
                pos = {"kind": "future", "symbol": symbol, "expiry": expiry,
                       "label": label, "lotSize": lot_size, "qty": 0,
                       "avgPrice": price, "margin": 0.0}
                state["positions"][key] = pos
            # Recompute avg price: if we flipped sides or added same side.
            if closing and opening:
                # Flipped through zero → new leg starts fresh at fill price.
                pos["avgPrice"] = price
                pos["margin"] = margin_needed
            elif opening and (old_qty == 0 or (old_qty > 0) == (signed > 0)):
                # Adding to same side → weighted average.
                pos["avgPrice"] = (
                    (pos["avgPrice"] * abs(old_qty)) + price * opening
                ) / (abs(old_qty) + opening)
                pos["margin"] = pos.get("margin", 0.0) + margin_needed
            else:
                # Pure reduction → avg unchanged, margin reduced.
                pos["margin"] = pos.get("margin", 0.0) - margin_freed
            pos["qty"] = new_qty
            pos["lots"] = int(round(abs(new_qty) / lot_size)) if lot_size else abs(new_qty)

        order = {
            "time": _now(), "symbol": label, "side": side, "qty": units,
            "lots": lots, "lotSize": lot_size, "kind": "future",
            "price": round(price, 2), "value": round(units * price, 2),
            "realized": round(realized, 2) if closing else None,
        }
        state["orders"].append(order)
        _save(state)
        rmsg = f" · realized Rs {realized:,.0f}" if closing else ""
        return True, f"{side} {lots} lot(s) x {lot_size} {symbol} FUT @ {price:g}{rmsg}", order


def _reprice(key, pos):
    """Current LTP for a position, re-fetching option premiums as needed."""
    if pos.get("kind") == "future":
        import nse_quote
        fut = nse_quote.get_near_future(pos.get("symbol"))
        return fut["ltp"] if fut and fut.get("ltp") is not None else pos["avgPrice"]
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
        if pos.get("kind") == "future":
            # Margin-based: signed MTM; equity contribution = margin + unrealized.
            qty = pos["qty"]
            pnl = (ltp - pos["avgPrice"]) * qty  # qty signed → shorts profit on drop
            margin = pos.get("margin", 0.0)
            mkt = margin + pnl
            cost = margin
            holdings_value += mkt
            invested += cost
        else:
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
                "lots": pos.get("lots"),
                "lotSize": pos.get("lotSize"),
                "margin": round(pos.get("margin", 0.0), 2) if pos.get("kind") == "future" else None,
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
