"""
breakout.py
===========
Step 4: breakout confirmation.

A breakout is only accepted if the candle has a strong directional body,
above-average volume, a close that clears the level by a meaningful ATR
buffer, and isn't actually a liquidity sweep (a wick that pokes through
the level and closes back on the origin side -- the classic stop-hunt
fake breakout that smart-money traders fade rather than follow).
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from config import settings
from models import BreakoutEvent, Direction, Level
from utils import atr as atr_series_fn
from utils import last_closed_candle


def _body_ratio(candle: pd.Series) -> float:
    rng = float(candle["high"]) - float(candle["low"])
    if rng <= 0:
        return 0.0
    body = abs(float(candle["close"]) - float(candle["open"]))
    return body / rng


def _volume_ratio(df: pd.DataFrame, idx: int, lookback: int) -> float:
    candle_vol = float(df["volume"].iloc[idx])
    prior = df["volume"].iloc[max(0, idx - lookback):idx]
    avg_vol = float(prior.mean()) if len(prior) > 0 else 0.0
    return candle_vol / avg_vol if avg_vol > 0 else 0.0


def _is_liquidity_sweep(candle: pd.Series, level: Level, direction: Direction) -> bool:
    """
    A liquidity sweep on this candle means: the wick poked through the
    OPPOSITE level before this candle's close ended up confirming the
    breakout. We only need to guard against the breakout candle itself
    being a sweep-and-reject of the level we're trying to break (i.e. the
    breakout already failed within the same candle).
    """
    close_px = float(candle["close"])
    high_px = float(candle["high"])
    low_px = float(candle["low"])

    if direction is Direction.BULLISH:
        # swept above resistance then closed back below it -> failed breakout
        return high_px > level.price and close_px < level.price
    else:
        return low_px < level.price and close_px > level.price


def confirm_breakout(
    symbol: str,
    df: pd.DataFrame,
    resistance_levels: List[Level],
    support_levels: List[Level],
) -> Optional[BreakoutEvent]:
    """
    Check the most recently CLOSED candle for a valid breakout of the
    nearest qualifying resistance or support level. Returns None if no
    valid breakout is found (including rejected fake breakouts).
    """
    if not resistance_levels and not support_levels:
        return None

    candle, idx = last_closed_candle(df)
    if idx < settings.volume_lookback:
        return None

    close_px = float(candle["close"])

    atr_full = atr_series_fn(df, period=settings.atr_period)
    current_atr = float(atr_full.iloc[idx]) if idx < len(atr_full) and pd.notna(atr_full.iloc[idx]) else 0.0
    if current_atr <= 0:
        return None

    # Candidate: nearest resistance above current structure that's just been
    # cleared, or nearest support below that's just been broken.
    direction: Optional[Direction] = None
    level: Optional[Level] = None

    broken_resistances = [lvl for lvl in resistance_levels if close_px > lvl.price]
    if broken_resistances:
        candidate = max(broken_resistances, key=lambda lvl: lvl.price)  # nearest below close
        direction, level = Direction.BULLISH, candidate

    broken_supports = [lvl for lvl in support_levels if close_px < lvl.price]
    if broken_supports:
        candidate = min(broken_supports, key=lambda lvl: lvl.price)  # nearest above close
        # If both directions somehow qualify (shouldn't on clean data), prefer
        # whichever break is more decisive (further past the level, in ATR terms).
        if direction is None or abs(close_px - candidate.price) > abs(close_px - level.price):
            direction, level = Direction.BEARISH, candidate

    if direction is None or level is None:
        return None

    if _is_liquidity_sweep(candle, level, direction):
        return None  # this is a sweep-and-reject, not a genuine breakout

    body_ratio = _body_ratio(candle)
    if body_ratio < settings.min_body_ratio:
        return None  # indecisive candle (doji-like), reject

    vol_ratio = _volume_ratio(df, idx, settings.volume_lookback)
    if vol_ratio < settings.min_volume_ratio:
        return None

    breakout_strength = abs(close_px - level.price)
    if breakout_strength < settings.min_breakout_atr_mult * current_atr:
        return None  # didn't clear the level with enough conviction

    return BreakoutEvent(
        symbol=symbol,
        direction=direction,
        level=level,
        candle_index=idx,
        close_price=close_px,
        candle_high=float(candle["high"]),
        candle_low=float(candle["low"]),
        candle_open=float(candle["open"]),
        volume_ratio=vol_ratio,
        body_ratio=body_ratio,
        atr=current_atr,
        liquidity_sweep=False,
    )
