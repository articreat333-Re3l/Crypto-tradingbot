"""
risk_manager.py
================
Step 8-9: stop loss and take profit calculation.

Stop loss is now zone + ATR buffer (never the breakout candle wick --
that was the single biggest cause of the old bot's premature stop-outs).
Take profit prefers the nearest liquidity / opposing structure level
beyond entry, falling back to a configurable RR multiple when no such
level exists or it doesn't clear the minimum RR.
"""

from __future__ import annotations

from typing import Optional

from config import settings
from models import Direction, Zone


def calculate_stop_loss(zone: Zone, atr: float, direction: Direction, atr_mult: float = None) -> float:
    atr_mult = settings.atr_sl_buffer_mult if atr_mult is None else atr_mult
    buffer = atr * atr_mult
    if direction is Direction.BEARISH:
        return zone.top + buffer
    return zone.bottom - buffer


def calculate_take_profit(
    direction: Direction,
    planned_entry: float,
    stop_loss: float,
    liquidity_target: Optional[float] = None,
    min_rr: float = None,
    default_target_rr: float = None,
) -> Optional[float]:
    """
    `liquidity_target` is the nearest opposing structure level beyond
    entry (next resistance above for a bullish trade, next support below
    for a bearish trade), if the scanner found one. Used as TP only if it
    still clears the minimum RR -- otherwise we fall back to a
    configurable RR multiple so a too-close level can't produce a
    sub-minimum-RR trade.
    """
    min_rr = settings.min_risk_reward if min_rr is None else min_rr
    default_target_rr = settings.default_target_rr if default_target_rr is None else default_target_rr

    risk = abs(planned_entry - stop_loss)
    if risk <= 0:
        return None

    if liquidity_target is not None:
        if direction is Direction.BULLISH and liquidity_target > planned_entry:
            reward = liquidity_target - planned_entry
            if reward / risk >= min_rr:
                return liquidity_target
        elif direction is Direction.BEARISH and liquidity_target < planned_entry:
            reward = planned_entry - liquidity_target
            if reward / risk >= min_rr:
                return liquidity_target

    if direction is Direction.BULLISH:
        return planned_entry + risk * default_target_rr
    return planned_entry - risk * default_target_rr


def compute_rr(direction: Direction, entry: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    return reward / risk if risk > 0 else 0.0


def passes_min_rr(rr: float, min_rr: float = None) -> bool:
    min_rr = settings.min_risk_reward if min_rr is None else min_rr
    return rr >= min_rr
