"""
structure.py
============
Step 1-3 of the strategy: swing detection, trend determination from market
structure, and automatic support/resistance level building from repeated
swing reactions.

This deliberately replaces the old single-candle-fractal pivot detection
with a proper N-candle fractal, and replaces "two-point slope" pattern
matching with genuine HH/HL/LH/LL structure reads.
"""

from __future__ import annotations

from typing import List, Tuple

import pandas as pd

from config import settings
from models import Level, Swing, TrendDirection


def find_swing_high(df: pd.DataFrame, left: int = None, right: int = None) -> List[Swing]:
    """
    A swing high is a candle whose high is greater than `left` candles
    before it and `right` candles after it (a true N-candle fractal,
    not a single-candle wiggle).
    """
    left = settings.swing_left if left is None else left
    right = settings.swing_right if right is None else right
    highs = df["high"].values
    n = len(highs)
    swings: List[Swing] = []
    for i in range(left, n - right):
        window_left = highs[i - left:i]
        window_right = highs[i + 1:i + 1 + right]
        if highs[i] > window_left.max() and highs[i] > window_right.max():
            ts = int(df["time"].iloc[i]) if "time" in df.columns else None
            swings.append(Swing(index=i, price=float(highs[i]), is_high=True, timestamp=ts))
    return swings


def find_swing_low(df: pd.DataFrame, left: int = None, right: int = None) -> List[Swing]:
    left = settings.swing_left if left is None else left
    right = settings.swing_right if right is None else right
    lows = df["low"].values
    n = len(lows)
    swings: List[Swing] = []
    for i in range(left, n - right):
        window_left = lows[i - left:i]
        window_right = lows[i + 1:i + 1 + right]
        if lows[i] < window_left.min() and lows[i] < window_right.min():
            ts = int(df["time"].iloc[i]) if "time" in df.columns else None
            swings.append(Swing(index=i, price=float(lows[i]), is_high=False, timestamp=ts))
    return swings


def determine_trend(swing_highs: List[Swing], swing_lows: List[Swing]) -> TrendDirection:
    """
    Determine trend from market structure: compare the two most recent
    swing highs and the two most recent swing lows.

      Higher highs + higher lows  -> uptrend
      Lower highs  + lower lows   -> downtrend
      Anything else               -> range (no clean structure)

    Temporal proximity guard (bug fix): the code previously compared the
    two most recent swing highs against the two most recent swing lows
    without checking whether those pivots were anywhere near each other
    in time.  If the most recent swing high was at bar 140 and the most
    recent swing low was at bar 20, the HH/HL read is meaningless --
    the market structure has completely changed in between.

    Fix: if the two most recent pivots (one high, one low) are more than
    _MAX_PIVOT_GAP bars apart, the structure is too fragmented to classify
    reliably and we return RANGE.
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return TrendDirection.RANGE

    _MAX_PIVOT_GAP = 50   # bars; ~12.5 hours on 15m, 4+ hours on 5m

    latest_high_idx = swing_highs[-1].index
    latest_low_idx  = swing_lows[-1].index
    if abs(latest_high_idx - latest_low_idx) > _MAX_PIVOT_GAP:
        return TrendDirection.RANGE

    hh = swing_highs[-1].price > swing_highs[-2].price
    hl = swing_lows[-1].price  > swing_lows[-2].price
    lh = swing_highs[-1].price < swing_highs[-2].price
    ll = swing_lows[-1].price  < swing_lows[-2].price

    if hh and hl:
        return TrendDirection.UP
    if lh and ll:
        return TrendDirection.DOWN
    return TrendDirection.RANGE


def _cluster_swings(swings: List[Swing], tolerance_pct: float) -> List[List[Swing]]:
    """
    Group swings whose prices sit within `tolerance_pct` of each other.

    Bug fixed: the previous implementation compared each new swing to the
    running AVERAGE of the growing cluster.  As members were added the mean
    drifted, allowing clusters to silently span more than 2 × tolerance_pct
    in total -- merging levels that should be distinct.

    Fix: compare against the FIRST element (anchor price) of the cluster.
    The anchor never moves, so the cluster is guaranteed to span at most
    2 × tolerance_pct from anchor to furthest member.
    """
    if not swings:
        return []
    ordered = sorted(swings, key=lambda s: s.price)
    clusters: List[List[Swing]] = [[ordered[0]]]
    for s in ordered[1:]:
        anchor = clusters[-1][0].price          # fixed reference, never drifts
        if abs(s.price - anchor) / anchor <= tolerance_pct:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    return clusters


def detect_support_resistance(
    swing_highs: List[Swing],
    swing_lows: List[Swing],
    tolerance_pct: float = None,
    min_touches: int = None,
) -> Tuple[List[Level], List[Level]]:
    """
    Build support/resistance levels from clusters of repeated swing
    reactions (Step 3) -- not from a naive rolling max/min.

    Returns (resistance_levels, support_levels), each sorted by touch
    count descending (strongest level first).
    """
    tolerance_pct = settings.sr_touch_tolerance_pct if tolerance_pct is None else tolerance_pct
    min_touches = settings.min_level_touches if min_touches is None else min_touches

    resistance: List[Level] = []
    for cluster in _cluster_swings(swing_highs, tolerance_pct):
        if len(cluster) < min_touches:
            continue
        avg_price = sum(s.price for s in cluster) / len(cluster)
        most_recent = max(cluster, key=lambda s: s.index)
        resistance.append(
            Level(
                price=avg_price,
                is_resistance=True,
                touches=len(cluster),
                last_touch_index=most_recent.index,
                formed_by_swing=most_recent,
            )
        )

    support: List[Level] = []
    for cluster in _cluster_swings(swing_lows, tolerance_pct):
        if len(cluster) < min_touches:
            continue
        avg_price = sum(s.price for s in cluster) / len(cluster)
        most_recent = max(cluster, key=lambda s: s.index)
        support.append(
            Level(
                price=avg_price,
                is_resistance=False,
                touches=len(cluster),
                last_touch_index=most_recent.index,
                formed_by_swing=most_recent,
            )
        )

    resistance.sort(key=lambda lvl: lvl.touches, reverse=True)
    support.sort(key=lambda lvl: lvl.touches, reverse=True)
    return resistance, support
