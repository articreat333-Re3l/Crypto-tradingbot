"""
order_blocks.py
================
Detects the order block associated with a breakout: the last candle of
the opposite colour before the impulsive move that produced the
breakout. This is a classic SMC point-of-interest and, in this project,
acts as a confluence factor that strengthens (or fails to confirm) the
swing-based zone built in zones.py.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from models import BreakoutEvent, Direction, Zone, ZoneSource

DEFAULT_LOOKBACK = 15


def detect_order_block(df: pd.DataFrame, breakout: BreakoutEvent, lookback: int = DEFAULT_LOOKBACK) -> Optional[Zone]:
    idx = breakout.candle_index
    direction = breakout.direction
    start = max(0, idx - lookback)

    for i in range(idx - 1, start - 1, -1):
        o = float(df["open"].iloc[i])
        c = float(df["close"].iloc[i])
        is_bearish_candle = c < o
        is_bullish_candle = c > o

        if direction is Direction.BULLISH and is_bearish_candle:
            return Zone(
                symbol=breakout.symbol,
                direction=direction,
                top=float(df["high"].iloc[i]),
                bottom=float(df["low"].iloc[i]),
                source=ZoneSource.ORDER_BLOCK,
                created_index=i,
            )

        if direction is Direction.BEARISH and is_bullish_candle:
            return Zone(
                symbol=breakout.symbol,
                direction=direction,
                top=float(df["high"].iloc[i]),
                bottom=float(df["low"].iloc[i]),
                source=ZoneSource.ORDER_BLOCK,
                created_index=i,
            )

    return None
