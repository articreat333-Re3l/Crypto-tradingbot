"""
risk_manager.py
================
Stop loss / take profit calculation and the canonical performance calculator.

Rule: every module that reports, stores, or logs an R-multiple must call
compute_trade_performance().  No other code should compute risk, reward, or
realized RR independently.  This is the single source of truth.

Why compute_trade_performance() exists
---------------------------------------
The previous code called compute_rr(entry, stop, target) at trigger time
(projecting a closed-trade RR before the trade had closed) AND at close time
(using the actual exit).  Both calls mixed planned_entry-based prices with
actual_entry-based prices, producing wrong realized R values.

The correct calculation requires ONLY:
    risk   = abs(actual_entry – stop_loss)           # your actual dollar risk
    reward = abs(exit_price   – actual_entry)        # your actual dollar gain
    RR     = signed_reward / risk                    # positive = win, negative = loss

where signed_reward is direction-adjusted so SL exits always produce -1.0.
"""

from __future__ import annotations

from typing import Dict, Optional

from config import settings
from models import Direction, Zone


# ---------------------------------------------------------------------------
# Canonical performance calculator — the ONLY place that computes realized R
# ---------------------------------------------------------------------------

def compute_trade_performance(
    direction: Direction,
    actual_entry: float,
    stop_loss: float,
    exit_price: float,
) -> Dict[str, float]:
    """
    Compute every performance metric for a completed trade.

    Arguments use actual executed prices only.  Never pass projected,
    zone-midpoint, or candle-close values that aren't the real fill price.

    Returns
    -------
    risk_distance   abs(actual_entry – stop_loss)           always positive
    reward_distance abs(exit_price – actual_entry)          always positive
    realized_rr     signed R  (+ve = gain, –ve = loss)
    pnl_pct         raw price PnL %  (direction-adjusted, no leverage)

    Examples
    --------
    Bullish TP:  entry=100 sl=98 exit=105 → risk=2 reward=5 RR=+2.50
    Bullish SL:  entry=100 sl=98 exit=98  → risk=2 reward=2 RR=-1.00
    Bearish TP:  entry=100 sl=102 exit=95 → risk=2 reward=5 RR=+2.50
    Bearish SL:  entry=100 sl=102 exit=102→ risk=2 reward=2 RR=-1.00
    """
    risk = abs(actual_entry - stop_loss)

    # Direction-signed reward: positive = favourable move, negative = adverse
    if direction is Direction.BULLISH:
        signed_reward = exit_price - actual_entry
    else:
        signed_reward = actual_entry - exit_price

    reward_distance = abs(exit_price - actual_entry)

    realized_rr = signed_reward / risk if risk > 0 else 0.0

    pnl_pct = (signed_reward / actual_entry * 100) if actual_entry > 0 else 0.0

    return {
        "risk_distance":   risk,
        "reward_distance": reward_distance,
        "realized_rr":     realized_rr,
        "pnl_pct":         pnl_pct,
    }


def compute_actual_target(
    direction: Direction,
    actual_entry: float,
    stop_loss: float,
    planned_rr: float,
) -> Optional[float]:
    """
    Reanchor the take-profit to the actual fill price so hitting the target
    always produces exactly planned_rr.

    At signal creation the TP is anchored to zone.midpoint (the hoped-for
    entry).  The confirmation-mode actual entry is the candle that closes
    outside the zone — always different from zone.midpoint.  If we keep the
    zone.midpoint-anchored TP, hitting it produces a lower RR than planned
    because the risk distance has grown.

    This function returns a TP that is exactly planned_rr away from
    actual_entry in risk units, preserving the trader's intended R target.
    """
    risk = abs(actual_entry - stop_loss)
    if risk <= 0 or planned_rr <= 0:
        return None
    if direction is Direction.BULLISH:
        return actual_entry + risk * planned_rr
    return actual_entry - risk * planned_rr


# ---------------------------------------------------------------------------
# Signal-creation helpers (unchanged public API)
# ---------------------------------------------------------------------------

def calculate_stop_loss(
    zone: Zone,
    atr: float,
    direction: Direction,
    atr_mult: float = None,
) -> float:
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
    Compute the *planned* take-profit anchored to zone.midpoint.
    This value is stored on the Trade for reference and for the pending
    alert message.  The *actual* TP used for exit detection and P&L
    reporting is compute_actual_target(), called at trigger time.
    """
    min_rr = settings.min_risk_reward if min_rr is None else min_rr
    default_target_rr = settings.default_target_rr if default_target_rr is None else default_target_rr

    risk = abs(planned_entry - stop_loss)
    if risk <= 0:
        return None

    if liquidity_target is not None:
        if direction is Direction.BULLISH and liquidity_target > planned_entry:
            if (liquidity_target - planned_entry) / risk >= min_rr:
                return liquidity_target
        elif direction is Direction.BEARISH and liquidity_target < planned_entry:
            if (planned_entry - liquidity_target) / risk >= min_rr:
                return liquidity_target

    if direction is Direction.BULLISH:
        return planned_entry + risk * default_target_rr
    return planned_entry - risk * default_target_rr


def compute_rr(
    direction: Direction,
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> float:
    """
    Compute unsigned RR ratio.  Used only for the PLANNED RR at signal
    creation (scanner.py).  All closed-trade reporting must use
    compute_trade_performance() instead.
    """
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    return reward / risk if risk > 0 else 0.0


def passes_min_rr(rr: float, min_rr: float = None) -> bool:
    min_rr = settings.min_risk_reward if min_rr is None else min_rr
    return rr >= min_rr
