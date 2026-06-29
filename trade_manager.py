"""
trade_manager.py
================
Retest engine and trade lifecycle state machine.

Accounting fix (v2.1)
----------------------
The previous code set trade.realized_rr at TRIGGER time by calling
compute_rr(actual_entry, planned_stop, planned_tp).  This is wrong for
two reasons:

  1. realized_rr should only be set when the trade CLOSES, not when it
     opens — projecting a closed-trade metric before the trade exists is
     misleading.

  2. The planned TP is anchored to zone.midpoint.  The actual entry in
     CONFIRMATION mode is the candle close outside the zone boundary —
     always different from zone.midpoint.  Using actual_entry with the
     zone.midpoint-anchored TP underestimates reward and overstates risk,
     producing RR values far below planned (0.39 on a planned 2.50 trade).

Fix applied here
-----------------
At trigger time:
  • actual_target is computed from actual_entry using the same planned_rr
    multiplier:  actual_target = actual_entry ± risk * planned_rr
  • slippage and risk_distance are recorded for audit/debug
  • realized_rr is NOT set (trade is open, no realised P&L exists)

At close time:
  • compute_trade_performance() is called with actual_entry and the real
    exit_price (actual_target on TP, stop_loss on SL)
  • This is the ONLY place realized_rr is set
  • SL exits produce exactly -1.00 by the math (exit == stop_loss)
  • TP exits produce exactly planned_rr by the math (actual_target was
    constructed to give planned_rr from actual_entry)
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

import pandas as pd

import persistence
from config import settings
from logger import get_logger
from models import Direction, RetestMode, Trade, TradeState, Zone, ZoneSource
from risk_manager import compute_actual_target, compute_trade_performance
from utils import last_closed_candle

log = get_logger(__name__)


def create_pending_trade(
    symbol: str,
    direction: Direction,
    zone: Zone,
    atr: float,
    stop_loss: float,
    take_profit: float,
    planned_rr: float,
    confluence_score: int,
    source: str,
    planned_entry: float,        # zone.midpoint — stored for slippage calculation
    pattern: str = "",
) -> Trade:
    now = time.time()
    trade = Trade(
        symbol=symbol,
        direction=direction,
        zone_top=zone.top,
        zone_bottom=zone.bottom,
        atr=atr,
        stop_loss=stop_loss,
        take_profit=take_profit,
        planned_rr=planned_rr,
        confluence_score=confluence_score,
        retest_mode=settings.retest_mode,
        created_ts=now,
        expiry_ts=now + settings.pending_trade_expiry_seconds,
        source=source,
        pattern=pattern,
        planned_entry=planned_entry,
    )
    persistence.save_trade(trade)
    return trade


def _zone_from_trade(trade: Trade) -> Zone:
    return Zone(
        symbol=trade.symbol,
        direction=trade.direction,
        top=trade.zone_top,
        bottom=trade.zone_bottom,
        source=ZoneSource(trade.source) if trade.source else ZoneSource.SWING,
        created_index=0,
    )


def _scan_touch(df: pd.DataFrame, zone: Zone) -> Tuple[Optional[str], Optional[float]]:
    candle, _ = last_closed_candle(df)
    low, high, close = float(candle["low"]), float(candle["high"]), float(candle["close"])
    if zone.overlaps_range(low, high):
        return "triggered", close
    return None, None


def _scan_confirmation(
    df: pd.DataFrame,
    zone: Zone,
    direction: Direction,
    window: int,
    touched_ts_ms: Optional[int] = None,
) -> Tuple[Optional[str], Optional[float]]:
    """
    Scan the last `window` closed candles for a retest confirmation.
    touched_ts_ms carries forward the persisted touch timestamp so candles
    between the touch and the current window are not skipped.
    """
    _, idx = last_closed_candle(df)
    start = max(0, idx - window + 1)
    touched = False

    if touched_ts_ms is not None and "time" in df.columns:
        for j in range(start, idx + 1):
            if int(df["time"].iloc[j]) >= touched_ts_ms:
                start = j
                break
        touched = True

    for i in range(start, idx + 1):
        c = df.iloc[i]
        low, high, close = float(c["low"]), float(c["high"]), float(c["close"])

        if not touched:
            if zone.overlaps_range(low, high):
                touched = True
            continue

        if direction is Direction.BEARISH:
            if close < zone.bottom:
                return "triggered", close
            if close > zone.top:
                return "invalidated", close
        else:
            if close > zone.top:
                return "triggered", close
            if close < zone.bottom:
                return "invalidated", close

    return ("touched", None) if touched else (None, None)


def process_pending_trade(
    trade: Trade,
    df_execution: Optional[pd.DataFrame],
    df_confirmation: Optional[pd.DataFrame],
) -> Optional[str]:
    """
    Advance a PENDING trade by one tick.
    Returns "triggered" / "expired" / "invalidated" / None.
    """
    now = time.time()

    if now > trade.expiry_ts:
        trade.state = TradeState.EXPIRED
        trade.closed_ts = now
        persistence.save_trade(trade)
        return "expired"

    if df_execution is not None and not df_execution.empty:
        candle, _ = last_closed_candle(df_execution)
        close_px = float(candle["close"])
        invalidated = (
            (trade.direction is Direction.BEARISH and close_px > trade.stop_loss)
            or (trade.direction is Direction.BULLISH and close_px < trade.stop_loss)
        )
        if invalidated:
            trade.state = TradeState.INVALIDATED
            trade.closed_ts = now
            persistence.save_trade(trade)
            return "invalidated"

    if df_confirmation is None or df_confirmation.empty:
        return None

    zone = _zone_from_trade(trade)

    if trade.retest_mode is RetestMode.TOUCH:
        outcome, entry_price = _scan_touch(df_confirmation, zone)
    else:
        touched_ts_ms = int(trade.touched_ts * 1000) if trade.touched_ts else None
        outcome, entry_price = _scan_confirmation(
            df_confirmation, zone, trade.direction,
            settings.confirmation_window_candles,
            touched_ts_ms=touched_ts_ms,
        )

    if outcome == "triggered":
        trade.state    = TradeState.RUNNING
        trade.entry_price  = entry_price
        trade.triggered_ts = now
        trade.touched_ts   = trade.touched_ts or now

        # --- Accounting fix: anchor TP to actual entry, record slippage ---
        actual_target = compute_actual_target(
            trade.direction, entry_price, trade.stop_loss, trade.planned_rr
        )
        trade.actual_target  = actual_target
        trade.risk_distance  = abs(entry_price - trade.stop_loss)
        trade.slippage       = (entry_price - (trade.planned_entry or entry_price))
        trade.realized_rr    = None   # not closed — never project realized RR

        log.debug(
            "[ENTRY] %s %s | planned_entry=%.6f actual_entry=%.6f slippage=%.6f "
            "planned_stop=%.6f actual_target=%.6f planned_tp=%.6f "
            "risk_distance=%.6f planned_rr=%.4f",
            trade.symbol, trade.direction.value,
            trade.planned_entry or 0, entry_price, trade.slippage,
            trade.stop_loss, actual_target or 0, trade.take_profit,
            trade.risk_distance, trade.planned_rr,
        )

        persistence.save_trade(trade)
        return "triggered"

    if outcome == "invalidated":
        trade.state = TradeState.INVALIDATED
        trade.closed_ts = now
        persistence.save_trade(trade)
        return "invalidated"

    if outcome == "touched" and trade.touched_ts is None:
        trade.touched_ts = now
        persistence.save_trade(trade)

    return None


def process_running_trade(
    trade: Trade,
    df_confirmation: Optional[pd.DataFrame],
) -> Optional[str]:
    """
    Monitor a RUNNING trade against TP/SL.

    TP detection uses actual_target (TP anchored to actual_entry) when
    available, falling back to the original planned take_profit for trades
    created before v2.1.

    All P&L metrics are computed by compute_trade_performance() — the single
    canonical calculator — using only actual executed prices.
    """
    if df_confirmation is None or df_confirmation.empty:
        return None

    now = time.time()

    # Prefer actual_target; fall back to planned take_profit for legacy trades.
    tp_price = trade.actual_target if trade.actual_target is not None else trade.take_profit

    if trade.triggered_ts is not None and "time" in df_confirmation.columns:
        triggered_ms = int(trade.triggered_ts * 1000)
        scan_df = df_confirmation[df_confirmation["time"] > triggered_ms]
        if scan_df.empty:
            return None
    else:
        _, last_idx = last_closed_candle(df_confirmation)
        scan_df = df_confirmation.iloc[[last_idx]]

    for _, row in scan_df.iterrows():
        high, low = float(row["high"]), float(row["low"])

        if trade.direction is Direction.BEARISH:
            sl_hit = high >= trade.stop_loss
            tp_hit = low  <= tp_price
        else:
            sl_hit = low  <= trade.stop_loss
            tp_hit = high >= tp_price

        # SL takes priority on same-candle hits (conservative tie-break).
        if sl_hit:
            exit_px = trade.stop_loss
            perf = compute_trade_performance(
                trade.direction, trade.entry_price, trade.stop_loss, exit_px
            )
            trade.state          = TradeState.SL_HIT
            trade.exit_price     = exit_px
            trade.closed_ts      = now
            trade.realized_rr    = perf["realized_rr"]    # always -1.00 when exit == SL
            trade.reward_distance = perf["reward_distance"]
            persistence.save_trade(trade)

            log.debug(
                "[EXIT-SL] %s | entry=%.6f sl=%.6f exit=%.6f "
                "risk=%.6f reward=%.6f realized_rr=%.4f",
                trade.symbol, trade.entry_price, trade.stop_loss, exit_px,
                perf["risk_distance"], perf["reward_distance"], perf["realized_rr"],
            )
            return "sl_hit"

        if tp_hit:
            exit_px = tp_price
            perf = compute_trade_performance(
                trade.direction, trade.entry_price, trade.stop_loss, exit_px
            )
            trade.state          = TradeState.TP_HIT
            trade.exit_price     = exit_px
            trade.closed_ts      = now
            trade.realized_rr    = perf["realized_rr"]    # equals planned_rr by construction
            trade.reward_distance = perf["reward_distance"]
            persistence.save_trade(trade)

            log.debug(
                "[EXIT-TP] %s | entry=%.6f actual_target=%.6f exit=%.6f "
                "risk=%.6f reward=%.6f realized_rr=%.4f planned_rr=%.4f",
                trade.symbol, trade.entry_price, tp_price, exit_px,
                perf["risk_distance"], perf["reward_distance"],
                perf["realized_rr"], trade.planned_rr,
            )
            return "tp_hit"

    return None


def has_open_trade(symbol: str, direction: Direction) -> bool:
    open_states = [TradeState.PENDING, TradeState.TRIGGERED, TradeState.RUNNING]
    existing = persistence.get_trades(symbol=symbol, states=open_states)
    return any(t.direction == direction for t in existing)
