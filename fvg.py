"""
fvg.py
======
Detects a fair value gap (3-candle imbalance) near the breakout: a
bullish FVG is a gap between candle[i-2].high and candle[i].low where
the middle candle's range never traded; bearish is the mirror image.
Used as a secondary confluence factor alongside the swing zone and the
order block.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from models import BreakoutEvent, Direction, Zone, ZoneSource

DEFAULT_LOOKBACK = 15


def detect_fair_value_gap(df: pd.DataFrame, breakout: BreakoutEvent, lookback: int = DEFAULT_LOOKBACK) -> Optional[Zone]:
    idx = breakout.candle_index
    direction = breakout.direction
    start = max(2, idx - lookback)

    for i in range(idx, start - 1, -1):
        if i - 2 < 0:
            continue
        c0 = df.iloc[i - 2]
        c2 = df.iloc[i]

        if direction is Direction.BULLISH:
            gap_bottom = float(c0["high"])
            gap_top = float(c2["low"])
            if gap_bottom < gap_top:
                return Zone(
                    symbol=breakout.symbol,
                    direction=direction,
                    top=gap_top,
                    bottom=gap_bottom,
                    source=ZoneSource.FVG,
                    created_index=i,
                )
        else:
            gap_top = float(c0["low"])
            gap_bottom = float(c2["high"])
            if gap_top > gap_bottom:
                return Zone(
                    symbol=breakout.symbol,
                    direction=direction,
                    top=gap_top,
                    bottom=gap_bottom,
                    source=ZoneSource.FVG,
                    created_index=i,
                )

    return None
