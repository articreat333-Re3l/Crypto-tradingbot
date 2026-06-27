"""
trade_manager.py
================
Steps 6-7: the pending trade object and the retest engine, plus Step 9's
running-trade monitor. This is the core difference from the old bot --
nothing fires immediately. A breakout creates a Trade in PENDING state;
every loop iteration re-checks every open trade (pending or running)
against fresh candles until it resolves to a terminal state.

The retest scan is intentionally stateless: it re-examines a short
trailing window of confirmation-timeframe candles each call rather than
relying on an in-memory "have we already touched the zone" flag, so a bot
restart mid-retest can't lose track of what already happened.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

import pandas as pd

import persistence
from config import settings
from logger import get_logger
from models import Direction, RetestMode, Trade, TradeState, Zone, ZoneSource
from risk_manager import compute_rr
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

    State machine:
      1. Find the first candle that overlaps the zone (touch).
      2. The first subsequent candle that closes back *outside* the zone
         in the trade's direction triggers entry.
      3. A close back outside *against* the trade direction invalidates.

    Bug fixed: previously, if the zone was touched more than `window`
    candles ago the scan started `touched=False` and could never confirm,
    because the touch was outside the window.  Now `touched_ts_ms` carries
    forward the persisted touch timestamp.  When supplied, the scan starts
    from the first candle at-or-after that timestamp with `touched=True`,
    so the confirmation check is never blocked by a stale window.
    """
    _, idx = last_closed_candle(df)
    start = max(0, idx - window + 1)
    touched = False

    # If a touch was recorded in a prior loop, anchor the scan to that point
    # so candles between the touch and the current window aren't skipped.
    if touched_ts_ms is not None and "time" in df.columns:
        # Find the first candle whose timestamp is >= the touch timestamp.
        for j in range(start, idx + 1):
            if int(df["time"].iloc[j]) >= touched_ts_ms:
                start = j
                break
        # If touched_ts_ms is older than all candles in the window, start
        # is unchanged but all window candles are post-touch -- correct.
        touched = True

    for i in range(start, idx + 1):
        c = df.iloc[i]
        low, high, close = float(c["low"]), float(c["high"]), float(c["close"])

        if not touched:
            if zone.overlaps_range(low, high):
                touched = True
            continue  # always skip to next candle until zone is entered

        # Post-touch: look for a close that exits the zone decisively.
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
    Advance a single PENDING trade by one tick. Returns an event string
    ("triggered" / "expired" / "invalidated") describing what changed
    this call, or None if nothing changed (still waiting).
    """
    now = time.time()

    if now > trade.expiry_ts:
        trade.state = TradeState.EXPIRED
        trade.closed_ts = now
        persistence.save_trade(trade)
        return "expired"

    # Early invalidation: if price has already closed beyond the planned
    # stop loss on the execution timeframe, the setup failed structurally
    # -- no point waiting out the rest of the expiry window.
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
        # Pass the persisted touch timestamp so the confirmation scan is not
        # blocked if the touch candle has scrolled outside the window.
        touched_ts_ms = int(trade.touched_ts * 1000) if trade.touched_ts else None
        outcome, entry_price = _scan_confirmation(
            df_confirmation, zone, trade.direction,
            settings.confirmation_window_candles,
            touched_ts_ms=touched_ts_ms,
        )

    if outcome == "triggered":
        trade.state = TradeState.RUNNING
        trade.entry_price = entry_price
        trade.triggered_ts = now
        trade.touched_ts = trade.touched_ts or now
        trade.realized_rr = compute_rr(trade.direction, entry_price, trade.stop_loss, trade.take_profit)
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


def process_running_trade(trade: Trade, df_confirmation: Optional[pd.DataFrame]) -> Optional[str]:
    """
    Monitor a RUNNING trade against TP/SL on the confirmation timeframe.

    Bug fixed: previously only the single last closed candle was checked.
    If the bot was down (restart, crash) while a running trade hit TP or SL,
    that outcome was permanently missed and the trade stayed RUNNING forever.

    Now all candles since triggered_ts are scanned in chronological order.
    SL is checked before TP on any single candle (conservative tie-break).
    """
    if df_confirmation is None or df_confirmation.empty:
        return None

    now = time.time()

    # Build the scan slice: all candles with a close time strictly after
    # the trade's entry timestamp.  This covers any gap from a restart.
    if trade.triggered_ts is not None and "time" in df_confirmation.columns:
        triggered_ms = int(trade.triggered_ts * 1000)
        scan_df = df_confirmation[df_confirmation["time"] > triggered_ms]
        if scan_df.empty:
            return None   # no new candles since entry yet
    else:
        # Fallback: check only the last closed candle (original behaviour)
        _, last_idx = last_closed_candle(df_confirmation)
        scan_df = df_confirmation.iloc[[last_idx]]

    for _, row in scan_df.iterrows():
        high, low = float(row["high"]), float(row["low"])

        if trade.direction is Direction.BEARISH:
            sl_hit = high >= trade.stop_loss
            tp_hit = low <= trade.take_profit
        else:
            sl_hit = low <= trade.stop_loss
            tp_hit = high >= trade.take_profit

        # SL takes priority: if price hit both on the same candle we assume
        # the adverse move happened first (conservative, avoids overstating P&L).
        if sl_hit:
            trade.state = TradeState.SL_HIT
            trade.exit_price = trade.stop_loss
            trade.closed_ts = now
            trade.realized_rr = -1.0
            persistence.save_trade(trade)
            return "sl_hit"

        if tp_hit:
            trade.state = TradeState.TP_HIT
            trade.exit_price = trade.take_profit
            trade.closed_ts = now
            trade.realized_rr = compute_rr(
                trade.direction, trade.entry_price, trade.stop_loss, trade.take_profit
            )
            persistence.save_trade(trade)
            return "tp_hit"

    return None


def has_open_trade(symbol: str, direction: Direction) -> bool:
    # Note: TradeState.TRIGGERED is included in the query for forward-compat
    # but is never actually set by the current code (process_pending_trade
    # moves PENDING → RUNNING directly).
    open_states = [TradeState.PENDING, TradeState.TRIGGERED, TradeState.RUNNING]
    existing = persistence.get_trades(symbol=symbol, states=open_states)
    # Use == not `is`: Direction is a str-Enum whose members are singletons
    # in CPython, but == is the correct operator for value comparison.
    return any(t.direction == direction for t in existing)
