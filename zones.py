"""
zones.py
========
Step 5: build the supply/demand zone rectangle from a confirmed breakout.

Implementation note / interpretation called out explicitly: the brief's
wording ("bearish breakout -> find the resistance that broke") only makes
sense if "the zone" is the ORIGIN of the impulsive move that broke
structure, not the broken level itself. That's also the standard SMC
reading (a retest trade returns to the base the move came from, not to
the line it crossed). So here:

  Bullish breakout (closed above a resistance built from swing highs):
    the broken resistance swing IS the zone top, and the zone bottom is
    the wick extreme around the most recent swing LOW before it -- the
    demand origin of the rally that produced the breakout.

  Bearish breakout (closed below a support built from swing lows):
    the broken support swing IS the zone bottom, and the zone top is the
    wick extreme around the most recent swing HIGH before it -- the
    supply origin of the decline.

This also means zones built here naturally tend to overlap with genuine
order blocks (see order_blocks.py), which is exactly what the confluence
scorer wants to see.
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from config import settings
from models import BreakoutEvent, Direction, Swing, Zone, ZoneSource


def _wick_extreme(df: pd.DataFrame, around_index: int, window: int, col: str, pick_max: bool) -> Optional[float]:
    lo = max(0, around_index - window)
    hi = min(len(df), around_index + window + 1)
    sliced = df[col].iloc[lo:hi]
    if sliced.empty:
        return None
    return float(sliced.max()) if pick_max else float(sliced.min())


def build_zone_from_breakout(
    breakout: BreakoutEvent,
    swing_highs: List[Swing],
    swing_lows: List[Swing],
    df: pd.DataFrame,
) -> Optional[Zone]:
    direction = breakout.direction
    level = breakout.level
    anchor_swing = level.formed_by_swing  # the swing that defines the broken level

    if direction is Direction.BULLISH:
        # anchor_swing is the swing HIGH that formed the broken resistance.
        # Find the most recent swing LOW before it -- the demand origin.
        candidates = [s for s in swing_lows if s.index < anchor_swing.index]
        if not candidates:
            return None
        origin = max(candidates, key=lambda s: s.index)
        zone_top = anchor_swing.price
        zone_bottom = _wick_extreme(df, origin.index, settings.swing_left, "low", pick_max=False)
        if zone_bottom is None:
            zone_bottom = origin.price
    else:
        # anchor_swing is the swing LOW that formed the broken support.
        # Find the most recent swing HIGH before it -- the supply origin.
        candidates = [s for s in swing_highs if s.index < anchor_swing.index]
        if not candidates:
            return None
        origin = max(candidates, key=lambda s: s.index)
        zone_bottom = anchor_swing.price
        zone_top = _wick_extreme(df, origin.index, settings.swing_left, "high", pick_max=True)
        if zone_top is None:
            zone_top = origin.price

    if zone_top <= zone_bottom:
        return None

    return Zone(
        symbol=breakout.symbol,
        direction=direction,
        top=zone_top,
        bottom=zone_bottom,
        source=ZoneSource.SWING,
        created_index=breakout.candle_index,
    )
