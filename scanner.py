"""
scanner.py
==========
Per-symbol pipeline. Called once per loop iteration for each symbol.

New breakout scan (Steps 1-9):
  1. Fetch HTF + execution + confirmation candles (cached)
  2. Detect swings + trend on execution TF
  3. Detect trend on HTF
  4. Build S/R levels
  5. Confirm breakout on execution TF
  6. Build swing zone, OB, FVG
  7. Score confluence
  8. Calculate SL/TP
  9. Create pending trade if score clears threshold + no dupe + not on cooldown

Open trade maintenance (runs every loop, not just on new signals):
  - Process every PENDING trade for this symbol → send entry / expiry / invalidation alerts
  - Process every RUNNING trade for this symbol → send TP/SL outcome alerts

Everything is wrapped in try/except so one bad symbol never stops the loop.
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd

import journal
import persistence
import telegram_bot as tg
import trade_manager
from breakout import confirm_breakout
from config import settings
from confluence import evaluate_confluence
from fvg import detect_fair_value_gap
from logger import get_logger
from market_data import fetch_candles
from models import Direction, Level, Trade, TradeState
from order_blocks import detect_order_block
from risk_manager import (
    calculate_stop_loss,
    calculate_take_profit,
    compute_rr,
    passes_min_rr,
)
from structure import (
    determine_trend,
    detect_support_resistance,
    find_swing_high,
    find_swing_low,
)
from utils import atr as atr_series_fn, last_closed_candle
from zones import build_zone_from_breakout

log = get_logger(__name__)


def _nearest_opposing_level(
    levels: list,
    price: float,
    direction: Direction,
) -> Optional[float]:
    """
    Nearest resistance above entry (bullish TP target) or nearest support
    below entry (bearish TP target).

    Bug fixed: the bearish case previously returned min(candidates), which
    is the FURTHEST support below price, not the nearest.  The correct
    value for bearish is max(candidates) -- the highest support level that
    is still below the planned entry.
    """
    if direction is Direction.BULLISH:
        candidates = [lvl.price for lvl in levels if lvl.price > price and lvl.is_resistance]
        return min(candidates) if candidates else None   # nearest resistance above
    else:
        candidates = [lvl.price for lvl in levels if lvl.price < price and not lvl.is_resistance]
        return max(candidates) if candidates else None   # nearest support below  (was: min — wrong)


def _zone_height_ok(zone, atr_val: float) -> bool:
    """Reject zones that are too narrow (noise traps) relative to current ATR."""
    if atr_val <= 0:
        return True
    return zone.height >= settings.min_zone_atr_mult * atr_val


def run_symbol(symbol: str) -> None:
    """Full pipeline for one symbol -- never raises, catches all exceptions internally."""
    try:
        _process_symbol(symbol)
    except Exception as e:
        log.error("Unhandled error on %s: %s", symbol, e, exc_info=True)
        try:
            journal.record_error(symbol, "scanner", "run_symbol", e)
        except Exception:
            pass


def _process_symbol(symbol: str) -> None:
    df_htf = fetch_candles(symbol, settings.htf_timeframe, limit=settings.structure_lookback_candles)
    df_exec = fetch_candles(symbol, settings.execution_timeframe, limit=settings.structure_lookback_candles)
    df_conf = fetch_candles(symbol, settings.confirmation_timeframe, limit=60)

    # --- Maintain open trades first (priority over new signal generation) ---
    _process_open_trades(symbol, df_exec, df_conf)

    # --- New signal scan ---
    if df_exec is None or len(df_exec) < settings.min_candles_required:
        return

    sh_exec = find_swing_high(df_exec)
    sl_exec = find_swing_low(df_exec)
    exec_trend = determine_trend(sh_exec, sl_exec)

    htf_trend = exec_trend  # fallback: use execution trend if HTF fetch failed
    if df_htf is not None and len(df_htf) >= settings.min_candles_required:
        sh_htf = find_swing_high(df_htf)
        sl_htf = find_swing_low(df_htf)
        htf_trend = determine_trend(sh_htf, sl_htf)

    resistance_levels, support_levels = detect_support_resistance(sh_exec, sl_exec)

    breakout = confirm_breakout(symbol, df_exec, resistance_levels, support_levels)
    if breakout is None:
        return

    # Zone validity checks
    if breakout.atr <= 0:
        return

    zone = build_zone_from_breakout(breakout, sh_exec, sl_exec, df_exec)
    if zone is None:
        return

    if not _zone_height_ok(zone, breakout.atr):
        log.debug("%s zone too narrow vs ATR, skipping", symbol)
        return

    ob_zone = detect_order_block(df_exec, breakout)
    fvg_zone = detect_fair_value_gap(df_exec, breakout)

    # Opposite-side swing extreme for prior-sweep confluence check
    if breakout.direction is Direction.BULLISH:
        opposite_extreme = sl_exec[-1].price if sl_exec else None
    else:
        opposite_extreme = sh_exec[-1].price if sh_exec else None

    confluence = evaluate_confluence(
        df_execution=df_exec,
        breakout=breakout,
        swing_zone=zone,
        order_block_zone=ob_zone,
        fvg_zone=fvg_zone,
        htf_trend=htf_trend,
        execution_trend=exec_trend,
        opposite_swing_extreme=opposite_extreme,
    )

    log.info(
        "%s %s confluence %d/%d (pass=%s)",
        symbol, breakout.direction.value, confluence.score, confluence.max_score, confluence.passed,
    )

    if not confluence.passed:
        return

    if trade_manager.has_open_trade(symbol, breakout.direction):
        log.debug("%s already has open %s trade, skipping duplicate", symbol, breakout.direction.value)
        return

    if not tg.cooldown_ok(symbol, breakout.direction):
        log.debug("%s %s on cooldown", symbol, breakout.direction.value)
        return

    # Risk calculation
    sl = calculate_stop_loss(zone, breakout.atr, breakout.direction)
    all_levels = resistance_levels + support_levels

    # Planned entry = zone midpoint (retest target)
    planned_entry = zone.midpoint
    liq_target = _nearest_opposing_level(all_levels, planned_entry, breakout.direction)

    tp = calculate_take_profit(
        direction=breakout.direction,
        planned_entry=planned_entry,
        stop_loss=sl,
        liquidity_target=liq_target,
    )
    if tp is None:
        log.debug("%s could not compute valid TP", symbol)
        return

    rr = compute_rr(breakout.direction, planned_entry, sl, tp)
    if not passes_min_rr(rr):
        log.debug("%s RR %.2f below minimum, skipping", symbol, rr)
        return

    trade = trade_manager.create_pending_trade(
        symbol=symbol,
        direction=breakout.direction,
        zone=zone,
        atr=breakout.atr,
        stop_loss=sl,
        take_profit=tp,
        planned_rr=rr,
        confluence_score=confluence.score,
        source=zone.source.value,
    )

    tg.mark_cooldown(symbol, breakout.direction)
    tg.send_pending_alert(trade)
    log.info("Pending trade created: %s %s score=%d RR=%.2f", symbol, breakout.direction.value, confluence.score, rr)

    # Journal: record the signal for research/analytics
    try:
        nearest_sup = max(
            (lvl.price for lvl in support_levels if lvl.price < planned_entry),
            default=None,
        )
        nearest_res = min(
            (lvl.price for lvl in resistance_levels if lvl.price > planned_entry),
            default=None,
        )
        journal.record_signal(
            trade,
            volume_ratio=breakout.volume_ratio,
            execution_trend=exec_trend.value,
            nearest_support=nearest_sup,
            nearest_resistance=nearest_res,
            order_block_present=ob_zone is not None,
            fair_value_gap_present=fvg_zone is not None,
            liquidity_sweep_present=bool(confluence.breakdown.get("prior_liquidity_sweep")),
        )
    except Exception as _je:
        log.debug("journal.record_signal non-fatal error: %s", _je)


def _process_open_trades(symbol: str, df_exec, df_conf) -> None:
    open_states = [TradeState.PENDING, TradeState.TRIGGERED, TradeState.RUNNING]
    trades = persistence.get_trades(symbol=symbol, states=open_states)

    for trade in trades:
        try:
            if trade.state is TradeState.RUNNING:
                outcome = trade_manager.process_running_trade(trade, df_conf)
                if outcome == "tp_hit":
                    tg.send_outcome_alert(trade)
                    log.info("%s TP hit RR=%.2f", trade.symbol, trade.realized_rr)
                    try:
                        journal.update_signal_status(trade.id, "Completed")
                        journal.record_trade_exit(trade, "TP")
                    except Exception as _je:
                        log.debug("journal tp_hit hook error: %s", _je)
                elif outcome == "sl_hit":
                    tg.send_outcome_alert(trade)
                    log.info("%s SL hit", trade.symbol)
                    try:
                        journal.update_signal_status(trade.id, "Completed")
                        journal.record_trade_exit(trade, "SL")
                    except Exception as _je:
                        log.debug("journal sl_hit hook error: %s", _je)

            elif trade.state in (TradeState.PENDING, TradeState.TRIGGERED):
                outcome = trade_manager.process_pending_trade(trade, df_exec, df_conf)
                if outcome == "triggered":
                    tg.send_entry_alert(trade)
                    log.info("%s entry triggered at %s", trade.symbol, trade.entry_price)
                    try:
                        journal.update_signal_status(trade.id, "Triggered")
                        journal.record_trade_entry(trade)
                    except Exception as _je:
                        log.debug("journal triggered hook error: %s", _je)
                elif outcome == "expired":
                    log.info("%s pending trade expired", trade.symbol)
                    try:
                        journal.update_signal_status(trade.id, "Expired")
                    except Exception as _je:
                        log.debug("journal expired hook error: %s", _je)
                elif outcome == "invalidated":
                    log.info("%s pending trade invalidated (structure broke)", trade.symbol)
                    try:
                        journal.update_signal_status(trade.id, "Invalidated")
                    except Exception as _je:
                        log.debug("journal invalidated hook error: %s", _je)

        except Exception as e:
            log.error("Error processing trade %s for %s: %s", trade.id, symbol, e, exc_info=True)


def run_once(symbols: list) -> None:
    """Iterate over all symbols once. Called from the main loop in botv2.py."""
    for symbol in symbols:
        run_symbol(symbol)
        time.sleep(settings.symbol_delay_seconds)
