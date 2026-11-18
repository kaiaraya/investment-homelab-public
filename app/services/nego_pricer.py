"""Pure price planner for the nego menu (ต่อราคา) — one source of truth for
computing a target price from the live board, used by the N3 postback flow.

The two modes, per the user's decision (2026-10-26):

- ``match``  — cross the spread to fill immediately (aggressive).
- ``lure``   — join the queue at my own side's best price, never crossing
               ("ต่อท้ายคิวที่ราคา bid/offer เดิม" — the one that does NOT match).

SHORT/COVER/SHORT-sell are bound by the SET uptick rule, which is enforced by
``app.services.validator.validator.validate_short_uptick`` — the SSOT. Never
re-derive the rule here.
"""
from __future__ import annotations

from typing import Dict, Optional

from app.services import trading_rules
from app.services.validator import validator


def _f(v: Optional[float]) -> Optional[float]:
    """Coerce a board cell to float, or None when it is unreadable."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def plan_price(
    side: str,
    mode: str,
    bid: Optional[float],
    offer: Optional[float],
    last: Optional[float],
    old_price: Optional[float] = None,
    prior_close: Optional[float] = None,
    *,
    symbol: Optional[str] = None,
    min_ticks: Optional[int] = None,
) -> Dict:
    """Compute the target price for one order.

    Returns ``{"ok": True, "price": float}`` or ``{"ok": False, "reason": str}``.
    ``mode`` is ``"match"`` or ``"lure"`` (or ``"uptick_best"``); ``side`` is BUY/SELL/SHORT/COVER.
    """
    s = (side or "").strip().upper()
    m = (mode or "").strip().lower()
    if m == "passive":
        m = "lure"
    bid = _f(bid)
    offer = _f(offer)
    last = _f(last)
    prior_close = _f(prior_close)
    op = _f(old_price)
    ticks = min_ticks if min_ticks is not None else trading_rules.get_short_uptick_ticks(symbol)

    if m not in ("match", "lure", "uptick_best"):
        return {"ok": False, "reason": f"unknown mode {mode!r}"}
    if s not in ("BUY", "SELL", "SHORT", "COVER"):
        return {"ok": False, "reason": f"unknown side {side!r}"}

    if m == "uptick_best":
        if s != "SHORT":
            m = "match"
        else:
            if last is None:
                return {"ok": False, "reason": "อ่านราคาล่าสุด (last) ไม่ได้ — ไม่ส่ง SHORT (uptick)"}
            target = validator.get_short_min_price(last, symbol=symbol, min_ticks=ticks)
            ok, err = validator.validate_short_uptick(target, last, symbol=symbol, min_ticks=ticks)
            if not ok:
                return {"ok": False, "reason": err or "ไม่สามารถตั้งราคาตามกฎ Short Sell ได้"}
            if op is not None and op <= target:
                return {"ok": False, "reason": "NO_CHANGE"}
            return {"ok": True, "price": round(target, 2)}

    is_buy = s in ("BUY", "COVER")

    # Which board leg does this mode need? A missing leg is fail-closed.
    if m == "match":
        # Aggressive: hit the far side (buy -> offer, sell -> bid).
        leg = offer if is_buy else bid
        leg_name = "offer" if is_buy else "bid"
    else:
        # Passive: join my own side's queue (buy -> bid, sell -> offer).
        leg = bid if is_buy else offer
        leg_name = "bid" if is_buy else "offer"

    if leg is None:
        return {"ok": False, "reason": f"อ่าน {leg_name} ไม่ได้ (board ว่าง)"}

    # In lure mode for SHORT: Rule 3 (Existing Order Stability).
    # If the existing order is already resting at the best offer (Offer 1) or matches last,
    # keep it resting to maximize match chances. Resting orders on the exchange book remain
    # valid and must not be bumped upwards just because a new order would require an uptick.
    if m == "lure" and s == "SHORT" and op is not None:
        if offer is not None and abs(op - offer) < 1e-9:
            return {"ok": False, "reason": "NO_CHANGE"}
        if last is not None and abs(op - last) < 1e-9:
            return {"ok": False, "reason": "NO_CHANGE"}

    price = leg

    # SHORT is a sell side bound by SET Short Sell price rule (+0 Zero-Plus or +1 Strict Uptick).
    if s == "SHORT":
        if last is None:
            return {"ok": False, "reason": "อ่านราคาล่าสุด (last) ไม่ได้ — ไม่ส่ง SHORT (uptick)"}
        ok, err = validator.validate_short_uptick(price, last, symbol=symbol, min_ticks=ticks)
        if not ok:
            if m == "match":
                # Match at bid is legal only when bid satisfies the active Short Sell rule.
                return {"ok": False, "reason": f"Match ไม่ได้ตามกฎ uptick — {err}"}
            price = validator.get_short_min_price(last, symbol=symbol, min_ticks=ticks)
            ok_floor, err_floor = validator.validate_short_uptick(price, last, symbol=symbol, min_ticks=ticks)
            if not ok_floor:
                return {"ok": False, "reason": f"ไม่สามารถตั้งราคาตามกฎ Short Sell — {err_floor}"}

    # Yellow Price (Prior Close) Boundaries for Lure mode:
    # 1. Buy / Cover: Do not chase upwards above yellow price -> clamp ceiling to prior_close.
    # 2. Sell / Short: Do not chase downwards below yellow price -> clamp floor to prior_close.
    if m == "lure" and prior_close is not None:
        if is_buy:
            price = min(price, prior_close)
        else:
            price = max(price, prior_close)
            if s == "SHORT":
                ok_clamp, _ = validator.validate_short_uptick(price, last, symbol=symbol, min_ticks=ticks)
                if not ok_clamp:
                    price = validator.get_short_min_price(last, symbol=symbol, min_ticks=ticks)

    # For SHORT in lure mode: amending to a higher price moves the order further
    # away from the market and reduces match probability. If the calculated legal
    # uptick price is greater than or equal to the existing resting price, keep the
    # existing order as-is.
    if m == "lure" and s == "SHORT" and op is not None and price >= op:
        return {"ok": False, "reason": "NO_CHANGE"}

    # A target equal to the current price is a no-op amend — skip, never submit.
    if old_price is not None and abs(price - float(old_price)) < 1e-9:
        return {"ok": False, "reason": "NO_CHANGE"}

    return {"ok": True, "price": round(price, 2)}


__all__ = ["plan_price"]
