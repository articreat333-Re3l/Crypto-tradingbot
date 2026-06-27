"""
confluence.py
=============
Combines every "additional filter" from the brief (liquidity sweep,
order block, FVG, volume, ATR, EMA trend, HTF confirmation, level
quality) into a single point-scored confluence check, instead of a
hard AND-chain across every factor.

Why scoring instead of hard AND: stacking seven-plus independent gates
across three timeframes (HTF trend AND structure trend AND volume AND
ATR AND OB present AND FVG present AND sweep present AND EMA aligned)
will pass almost nothing in live trading -- each gate that's individually
reasonable compounds into a setup that essentially never fires. Scoring
keeps every factor meaningful while staying tunable via one number
(CONFLUENCE_THRESHOLD).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from config import settings
from models import (
    BreakoutEvent,
    ConfluenceResult,
    Direction,
    Level,
    TrendDirection,
    Zone,
)
from utils import atr as atr_series_fn
from utils import ema as ema_fn

MAX_SCORE = 10


def had_prior_liquidity_sweep(
    df: pd.DataFrame,
    breakout: BreakoutEvent,
    opposite_extreme: Optional[float],
    lookback: int = 10,
) -> bool:
    """
    True if, in the candles leading up to the breakout, price wicked beyond
    the relevant opposite-side swing extreme and closed back before the
    breakout candle then drove through structure -- i.e. liquidity was
    swept before the genuine move, which is a bullish sign for breakout
    quality (stops were already cleared, less fuel left to drive a reversal).
    """
    if opposite_extreme is None:
        return False

    idx = breakout.candle_index
    start = max(0, idx - lookback)
    window = df.iloc[start:idx]
    if window.empty:
        return False

    if breakout.direction is Direction.BULLISH:
        # look for a sweep BELOW recent support before the bullish breakout
        swept = (window["low"] < opposite_extreme).any()
    else:
        swept = (window["high"] > opposite_extreme).any()

    return bool(swept)


def evaluate_confluence(
    df_execution: pd.DataFrame,
    breakout: BreakoutEvent,
    swing_zone: Zone,
    order_block_zone: Optional[Zone],
    fvg_zone: Optional[Zone],
    htf_trend: TrendDirection,
    execution_trend: TrendDirection,
    opposite_swing_extreme: Optional[float],
    threshold: int = None,
) -> ConfluenceResult:
    threshold = settings.confluence_threshold if threshold is None else threshold
    breakdown: dict = {}
    score = 0
    direction = breakout.direction
    wanted_trend = TrendDirection.UP if direction is Direction.BULLISH else TrendDirection.DOWN

    # 1) HTF trend alignment (worth more -- this is the dominant bias filter)
    if htf_trend == wanted_trend:
        breakdown["htf_trend_aligned"] = 2
        score += 2
    else:
        breakdown["htf_trend_aligned"] = 0

    # 2) Execution timeframe structure trend alignment
    if execution_trend == wanted_trend:
        breakdown["execution_trend_aligned"] = 1
        score += 1
    else:
        breakdown["execution_trend_aligned"] = 0

    # 3) Volume confirmation
    if breakout.volume_ratio >= settings.min_volume_ratio:
        breakdown["volume_confirmation"] = 1
        score += 1
    else:
        breakdown["volume_confirmation"] = 0

    # 4) ATR volatility filter -- current ATR shouldn't be in a dead-market
    # slump relative to its own recent history (avoids chop-driven false signals)
    atr_full = atr_series_fn(df_execution, period=settings.atr_period)
    baseline = atr_full.rolling(50).mean()
    idx = breakout.candle_index
    healthy_volatility = False
    if idx < len(baseline) and pd.notna(baseline.iloc[idx]) and baseline.iloc[idx] > 0:
        healthy_volatility = breakout.atr >= 0.7 * float(baseline.iloc[idx])
    if healthy_volatility:
        breakdown["atr_volatility_healthy"] = 1
        score += 1
    else:
        breakdown["atr_volatility_healthy"] = 0

    # 5) EMA trend filter
    ema_series = ema_fn(df_execution, period=settings.ema_period)
    ema_aligned = False
    if idx < len(ema_series) and pd.notna(ema_series.iloc[idx]):
        ema_val = float(ema_series.iloc[idx])
        ema_aligned = (
            breakout.close_price > ema_val
            if direction is Direction.BULLISH
            else breakout.close_price < ema_val
        )
    if ema_aligned:
        breakdown["ema_trend_aligned"] = 1
        score += 1
    else:
        breakdown["ema_trend_aligned"] = 0

    # 6) Order block confluence -- does a detected OB overlap the swing zone?
    if order_block_zone is not None and swing_zone.overlaps(order_block_zone):
        breakdown["order_block_confluence"] = 1
        score += 1
    else:
        breakdown["order_block_confluence"] = 0

    # 7) Fair value gap confluence
    if fvg_zone is not None and swing_zone.overlaps(fvg_zone):
        breakdown["fvg_confluence"] = 1
        score += 1
    else:
        breakdown["fvg_confluence"] = 0

    # 8) Prior liquidity sweep (stops already cleared before the real move)
    swept = had_prior_liquidity_sweep(df_execution, breakout, opposite_swing_extreme)
    if swept:
        breakdown["prior_liquidity_sweep"] = 1
        score += 1
    else:
        breakdown["prior_liquidity_sweep"] = 0

    # 9) Level quality -- more than the bare minimum touches required to form it
    extra_quality = breakout.level.touches >= (settings.min_level_touches + 1)
    if extra_quality:
        breakdown["level_quality"] = 1
        score += 1
    else:
        breakdown["level_quality"] = 0

    return ConfluenceResult(
        score=score,
        max_score=MAX_SCORE,
        threshold=threshold,
        passed=score >= threshold,
        breakdown=breakdown,
    )
