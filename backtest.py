"""
backtest.py
===========
Historical replay harness. Extended from single-timeframe to full
multi-timeframe mode while keeping all existing logic intact. All live
strategy modules (structure, breakout, zones, risk_manager, confluence,
order_blocks, fvg) are imported directly -- no duplication.

Replay modes
------------
Default (no --htf):
    Single-timeframe, touch-based retest. Fast, good for rapid iteration.
    Same behaviour as the original version.

--htf 1H:
    Multi-timeframe -- HTF data fetched once, HTF trend alignment gate
    applied at each breakout using the same determine_trend() the live bot
    uses. Timestamp-aligned: only HTF candles whose close precedes the
    execution candle are considered (no lookahead).

--confirm:
    Confirmation-candle retest simulation that mirrors _scan_confirmation
    in trade_manager.py. Zone touch → wait for a candle closing back
    outside in trade direction (entry), or closing back outside against it
    (invalidated). Extends, not replaces, the original touch simulation.

--confluence N:
    Adds the full confluence scoring gate at each breakout candidate.
    Requires the same score threshold used by the live bot (or the
    override value N). Works with or without --htf.

--optimize:
    Grid-searches confluence_threshold × min_rr × swing_left and reports
    the top-10 combos by profit factor. Runs run_backtest() for each
    combo with silent=True so output stays clean.

Run with:
    python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --rr 2.0
    python backtest.py --symbol ETHUSDT --tf 15m --lookback 800 --htf 1H --confirm
    python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --csv trades.csv --equity equity.csv
    python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --optimize

Results include: signals, retest %, win rate, avg RR, profit factor,
                 net R, max drawdown (R), max consecutive losses.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import sys
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from breakout import confirm_breakout
from config import load_settings
from models import Direction, TrendDirection, Zone
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
from confluence import evaluate_confluence
from fvg import detect_fair_value_gap
from order_blocks import detect_order_block
from utils import atr as atr_series_fn, to_okx_inst
from zones import build_zone_from_breakout

settings = load_settings()

_retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=_retry))

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
OKX_HIST_URL = "https://www.okx.com/api/v5/market/history-candles"

_BAR_MAP = {"1H": "1H", "15m": "15m", "5m": "5m", "1D": "1Dutc"}


def fetch_historical(symbol: str, timeframe: str, limit: int = 1000) -> Optional[pd.DataFrame]:
    """Fetch up to `limit` candles using the history endpoint (no TTL cache)."""
    bar = _BAR_MAP.get(timeframe, timeframe)
    params = {"instId": to_okx_inst(symbol), "bar": bar, "limit": min(limit, 300)}
    all_rows = []
    after = None

    while len(all_rows) < limit:
        if after:
            params["after"] = after
        try:
            res = _session.get(OKX_HIST_URL, params=params, timeout=15)
            data = res.json()
        except Exception as e:
            print(f"Fetch error: {e}")
            break
        if data.get("code") != "0" or not data.get("data"):
            break
        batch = data["data"]
        all_rows.extend(batch)
        if len(batch) < 300:
            break
        after = batch[-1][0]  # oldest timestamp in this batch → go further back
        time.sleep(0.3)

    if not all_rows:
        return None

    df = pd.DataFrame(
        all_rows,
        columns=["time", "open", "high", "low", "close", "volume", "volCcy", "volCcyQuote", "confirm"],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["time"] = df["time"].astype("int64")
    df = df.sort_values("time").reset_index(drop=True)
    return df.head(limit)


def _simulate_retest(
    df: pd.DataFrame,
    zone: Zone,
    sl: float,
    tp: float,
    direction: Direction,
    entry_bar_idx: int,
    max_bars: int = 48,
) -> str:
    """
    Walk forward bar by bar after the breakout candle, looking for
    zone retest → entry, then TP or SL. Returns 'win', 'loss', or 'no_retest'.
    """
    n = len(df)
    entered = False
    entry_price = None

    for i in range(entry_bar_idx + 1, min(n, entry_bar_idx + 1 + max_bars)):
        row = df.iloc[i]
        low, high = float(row["low"]), float(row["high"])

        if not entered:
            if zone.overlaps_range(low, high):
                entered = True
                entry_price = zone.midpoint
        else:
            if direction is Direction.BULLISH:
                if low <= sl:
                    return "loss"
                if high >= tp:
                    return "win"
            else:
                if high >= sl:
                    return "loss"
                if low <= tp:
                    return "win"

    return "no_retest"


def _simulate_retest_confirmation(
    df: pd.DataFrame,
    zone: Zone,
    sl: float,
    tp: float,
    direction: Direction,
    entry_bar_idx: int,
    max_bars: int = 48,
) -> str:
    """
    Confirmation-candle retest simulation -- extends the original touch-based
    _simulate_retest to mirror the live _scan_confirmation logic in
    trade_manager.py.

    State machine (identical to the live bot):
      1. Walk forward until any candle overlaps the zone (touched).
      2. After touch, wait for the first candle that closes back outside:
           - In trade direction  → entry triggered, proceed to TP/SL monitoring.
           - Against trade direction → invalidated (rejection candle failed).
      3. Once entered, check TP/SL on each subsequent candle. SL checked
         first on same-candle hits (conservative tie-break, same as live bot).

    Returns 'win', 'loss', 'no_retest', or 'invalidated'.
    """
    n = len(df)
    touched = False
    entered = False

    for i in range(entry_bar_idx + 1, min(n, entry_bar_idx + 1 + max_bars)):
        row = df.iloc[i]
        low, high, close = float(row["low"]), float(row["high"]), float(row["close"])

        if not entered:
            if not touched:
                if zone.overlaps_range(low, high):
                    touched = True
            else:
                # Confirmation-candle phase: first close outside the zone decides
                if direction is Direction.BULLISH:
                    if close > zone.top:
                        entered = True
                    elif close < zone.bottom:
                        return "invalidated"
                else:
                    if close < zone.bottom:
                        entered = True
                    elif close > zone.top:
                        return "invalidated"
        else:
            # Live trade monitoring -- SL checked first
            if direction is Direction.BULLISH:
                if low <= sl:
                    return "loss"
                if high >= tp:
                    return "win"
            else:
                if high >= sl:
                    return "loss"
                if low <= tp:
                    return "win"

    return "no_retest"


def run_backtest(
    symbol: str,
    timeframe: str,
    lookback: int,
    min_rr: float,
    htf_timeframe: str = "",
    use_confirmation: bool = False,
    confluence_threshold_override: int = 0,
    swing_left_override: int = 0,
    csv_path: str = "",
    equity_csv_path: str = "",
    silent: bool = False,
) -> Optional[dict]:
    """
    Run the backtest and return a metrics dict. All new kwargs default to
    their original-behaviour values so existing callers remain unaffected.
    When silent=True no output is printed (used by run_optimize).
    """
    if not silent:
        print(f"\nBacktest: {symbol} | {timeframe} | {lookback} bars | min RR {min_rr}")
        print("Fetching data...", flush=True)
    df = fetch_historical(symbol, timeframe, limit=lookback)
    if df is None or len(df) < settings.min_candles_required:
        if not silent:
            print("Not enough data to run backtest.")
        return None

    # Fetch HTF data once upfront for multi-timeframe replay (if requested)
    df_htf: Optional[pd.DataFrame] = None
    if htf_timeframe:
        htf_limit = max(lookback // 4, 200)
        df_htf = fetch_historical(symbol, htf_timeframe, limit=htf_limit)
        if not silent:
            if df_htf is not None:
                print(f"HTF ({htf_timeframe}): {len(df_htf)} candles loaded.")
            else:
                print(f"HTF ({htf_timeframe}): fetch failed, HTF filter disabled.")

    if not silent:
        print(f"Got {len(df)} candles. Running simulation...\n")

    results: List[dict] = []
    window = settings.min_candles_required  # minimum lookback per iteration

    for i in range(window, len(df) - 1):
        slice_df = df.iloc[:i].copy().reset_index(drop=True)

        _sw = swing_left_override if swing_left_override > 0 else None
        sh = find_swing_high(slice_df, left=_sw, right=_sw)
        sl_swings = find_swing_low(slice_df, left=_sw, right=_sw)
        if len(sh) < 2 or len(sl_swings) < 2:
            continue

        resistance, support = detect_support_resistance(sh, sl_swings)
        if not resistance and not support:
            continue

        breakout = confirm_breakout(symbol, slice_df, resistance, support)
        if breakout is None:
            continue

        zone = build_zone_from_breakout(breakout, sh, sl_swings, slice_df)
        if zone is None:
            continue

        # Multi-timeframe HTF alignment filter (only active when htf_timeframe is set)
        htf_trend = TrendDirection.RANGE
        if df_htf is not None:
            exec_ts = int(slice_df["time"].iloc[-1])
            htf_available = df_htf[df_htf["time"] <= exec_ts].reset_index(drop=True)
            min_htf_bars = (settings.swing_left + settings.swing_right) * 2
            if len(htf_available) >= min_htf_bars:
                sh_htf = find_swing_high(htf_available)
                sl_htf = find_swing_low(htf_available)
                htf_trend = determine_trend(sh_htf, sl_htf)
                wanted_htf = (
                    TrendDirection.UP if breakout.direction is Direction.BULLISH
                    else TrendDirection.DOWN
                )
                # Only filter when HTF has a clear trend (skip RANGE -- no strong bias)
                if htf_trend is not TrendDirection.RANGE and htf_trend != wanted_htf:
                    continue

        # Optional confluence scoring gate (active when --confluence N or --optimize)
        confluence_score = 0
        if confluence_threshold_override > 0:
            exec_trend = determine_trend(sh, sl_swings)
            ob_zone = detect_order_block(slice_df, breakout)
            fvg_zone = detect_fair_value_gap(slice_df, breakout)
            opp_ext = (
                sl_swings[-1].price
                if breakout.direction is Direction.BULLISH and sl_swings
                else sh[-1].price if sh else None
            )
            conf = evaluate_confluence(
                df_execution=slice_df,
                breakout=breakout,
                swing_zone=zone,
                order_block_zone=ob_zone,
                fvg_zone=fvg_zone,
                htf_trend=htf_trend,
                execution_trend=exec_trend,
                opposite_swing_extreme=opp_ext,
                threshold=confluence_threshold_override,
            )
            if not conf.passed:
                continue
            confluence_score = conf.score

        sl_price = calculate_stop_loss(zone, breakout.atr, breakout.direction)
        planned_entry = zone.midpoint
        tp_price = calculate_take_profit(
            direction=breakout.direction,
            planned_entry=planned_entry,
            stop_loss=sl_price,
            min_rr=min_rr,
            default_target_rr=settings.default_target_rr,
        )
        if tp_price is None:
            continue

        rr = compute_rr(breakout.direction, planned_entry, sl_price, tp_price)
        if not passes_min_rr(rr, min_rr):
            continue

        if use_confirmation:
            outcome = _simulate_retest_confirmation(df, zone, sl_price, tp_price, breakout.direction, i)
        else:
            outcome = _simulate_retest(df, zone, sl_price, tp_price, breakout.direction, i)
        results.append({
            "bar": i,
            "symbol": symbol,
            "direction": breakout.direction.value,
            "rr": rr,
            "outcome": outcome,
            # Extended fields (used by CSV export and optimizer)
            "entry_price": round(zone.midpoint, 6),
            "stop_loss": round(sl_price, 6),
            "take_profit": round(tp_price, 6),
            "zone_top": round(zone.top, 6),
            "zone_bottom": round(zone.bottom, 6),
            "confluence_score": confluence_score,
            "timestamp": int(df["time"].iloc[i]) if i < len(df) else 0,
            "volume_ratio": round(breakout.volume_ratio, 3),
            "body_ratio": round(breakout.body_ratio, 3),
        })

    if not results:
        if not silent:
            print("No valid setups found in this dataset.")
        return None

    total = len(results)
    retested = [r for r in results if r["outcome"] != "no_retest"]
    wins = [r for r in retested if r["outcome"] == "win"]
    losses = [r for r in retested if r["outcome"] == "loss"]

    win_count = len(wins)
    loss_count = len(losses)
    retest_pct = len(retested) / total * 100 if total else 0
    win_rate = win_count / len(retested) * 100 if retested else 0.0
    avg_rr = sum(r["rr"] for r in wins) / win_count if wins else 0.0
    gross_profit = sum(r["rr"] for r in wins)
    gross_loss = abs(sum(-1.0 for _ in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    equity_curve = _compute_equity_curve(results)
    dd_stats = _compute_drawdown_stats(equity_curve)

    if not silent:
        print("=" * 48)
        print(f"  Setups detected:     {total}")
        print(f"  Retested:            {len(retested)} ({retest_pct:.1f}%)")
        print(f"  Wins:                {win_count}")
        print(f"  Losses:              {loss_count}")
        print(f"  Win rate:            {win_rate:.1f}%")
        print(f"  Avg winner RR:       {avg_rr:.2f}")
        print(f"  Profit factor:       {profit_factor:.2f}")
        print(f"  Net R:               {gross_profit - gross_loss:.2f}")
        print(f"  Max drawdown (R):    {dd_stats['max_drawdown_r']:.2f}")
        print(f"  Max consec. losses:  {dd_stats['max_consec_losses']}")
        print("=" * 48)
        if csv_path:
            _export_trades_csv(results, csv_path)
        if equity_csv_path:
            _export_equity_csv(equity_curve, equity_csv_path)

    return {
        "total": total,
        "retested": len(retested),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "avg_rr": avg_rr,
        "profit_factor": profit_factor,
        "net_r": gross_profit - gross_loss,
        "max_drawdown_r": dd_stats["max_drawdown_r"],
        "max_consec_losses": dd_stats["max_consec_losses"],
    }


def _compute_equity_curve(results: List[dict]) -> List[dict]:
    """
    Build a bar-by-bar equity curve from decided trades (win/loss only).
    'no_retest' and 'invalidated' trades are excluded from the curve but
    counted separately in the summary so the caller can see setup quality.
    """
    equity = 0.0
    curve: List[dict] = []
    decided = [r for r in results if r["outcome"] in ("win", "loss")]
    for n, r in enumerate(decided, 1):
        delta = r["rr"] if r["outcome"] == "win" else -1.0
        equity += delta
        curve.append({
            "trade_num": n,
            "bar": r["bar"],
            "timestamp": r.get("timestamp", 0),
            "symbol": r["symbol"],
            "direction": r["direction"],
            "outcome": r["outcome"],
            "r_delta": round(delta, 4),
            "equity_r": round(equity, 4),
        })
    return curve


def _compute_drawdown_stats(equity_curve: List[dict]) -> dict:
    """
    Compute max drawdown in R (peak-to-trough on the equity curve),
    max consecutive losses, and the length of the longest drawdown streak
    (number of trades spent below a prior equity peak).
    """
    if not equity_curve:
        return {"max_drawdown_r": 0.0, "max_consec_losses": 0, "longest_dd_streak": 0}

    equities = [e["equity_r"] for e in equity_curve]
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd

    max_consec = 0
    current_consec = 0
    for e in equity_curve:
        if e["outcome"] == "loss":
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    longest_dd_streak = 0
    streak = 0
    running_peak = 0.0
    for e in equity_curve:
        if e["equity_r"] >= running_peak:
            running_peak = e["equity_r"]
            streak = 0
        else:
            streak += 1
            longest_dd_streak = max(longest_dd_streak, streak)

    return {
        "max_drawdown_r": round(max_dd, 4),
        "max_consec_losses": max_consec,
        "longest_dd_streak": longest_dd_streak,
    }


def _export_trades_csv(results: List[dict], filepath: str) -> None:
    """Write the full trade log (all outcomes, all fields) to a CSV file."""
    if not results:
        return
    fieldnames = list(results[0].keys())
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"  Trade log exported → {filepath}  ({len(results)} rows)")


def _export_equity_csv(equity_curve: List[dict], filepath: str) -> None:
    """Write the equity curve (decided trades only) to a CSV file."""
    if not equity_curve:
        return
    fieldnames = list(equity_curve[0].keys())
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(equity_curve)
    print(f"  Equity curve exported → {filepath}  ({len(equity_curve)} rows)")


def run_optimize(
    symbol: str,
    timeframe: str,
    lookback: int,
    htf_timeframe: str = "",
) -> None:
    """
    Grid-search over confluence_threshold × min_rr × swing_left.
    Fetches data implicitly through each run_backtest call. Each run
    executes with silent=True so output stays clean. Reports the top-10
    combos ranked by profit factor, then by net R as a tiebreaker.

    Extend param_grid here to add more sweep dimensions.
    """
    param_grid: Dict[str, list] = {
        "confluence_threshold": [0, 5, 6, 7],   # 0 = disabled
        "min_rr": [1.5, 2.0, 2.5],
        "swing_left": [2, 3, 4],
    }

    keys = list(param_grid.keys())
    combos = list(itertools.product(*[param_grid[k] for k in keys]))
    print(f"\nParameter optimisation: {symbol} | {timeframe} | {lookback} bars")
    print(f"HTF filter: {htf_timeframe or 'disabled'}")
    print(f"Testing {len(combos)} combinations...\n")

    opt_results: List[dict] = []
    for combo in combos:
        params = dict(zip(keys, combo))
        metrics = run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            lookback=lookback,
            min_rr=params["min_rr"],
            htf_timeframe=htf_timeframe,
            use_confirmation=True,   # always use confirmation mode in optimize
            confluence_threshold_override=params["confluence_threshold"],
            swing_left_override=params["swing_left"],
            silent=True,
        )
        if metrics is None or metrics["total"] == 0:
            continue
        metrics.update(params)
        opt_results.append(metrics)

    opt_results.sort(key=lambda r: (r["profit_factor"], r["net_r"]), reverse=True)

    header = f"{'conf':>5} {'rr':>5} {'sw':>4} | {'sigs':>6} {'win%':>7} {'PF':>7} {'net_R':>8} {'maxDD':>8}"
    print(header)
    print("-" * len(header))
    for r in opt_results[:10]:
        conf_str = str(r["confluence_threshold"]) if r["confluence_threshold"] > 0 else "off"
        pf_str = f"{r['profit_factor']:.2f}" if r["profit_factor"] != float("inf") else "  inf"
        print(
            f"{conf_str:>5} {r['min_rr']:>5.1f} {r['swing_left']:>4} |"
            f" {r['total']:>6} {r['win_rate']:>7.1f}%"
            f" {pf_str:>7} {r['net_r']:>8.2f} {r['max_drawdown_r']:>8.2f}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the SMC retest strategy on OKX data")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol to backtest (e.g. BTCUSDT)")
    parser.add_argument("--tf", default="15m", help="Execution timeframe (15m, 1H, etc.)")
    parser.add_argument("--lookback", type=int, default=500, help="Number of candles to fetch")
    parser.add_argument("--rr", type=float, default=2.0, help="Minimum RR filter")
    # Extended flags -- all optional, all backward-compatible
    parser.add_argument("--htf", default="", metavar="TF",
                        help="Enable multi-timeframe replay with this HTF (e.g. 1H)")
    parser.add_argument("--confirm", action="store_true",
                        help="Use confirmation-candle retest simulation (mirrors live bot)")
    parser.add_argument("--confluence", type=int, default=0, metavar="N",
                        help="Enable confluence scoring gate with threshold N (e.g. 6)")
    parser.add_argument("--swing", type=int, default=0, metavar="N",
                        help="Override swing_left/right (e.g. 2, 3, or 4)")
    parser.add_argument("--csv", default="", metavar="FILE",
                        help="Export full trade log to CSV (e.g. trades.csv)")
    parser.add_argument("--equity", default="", metavar="FILE",
                        help="Export equity curve to CSV (e.g. equity.csv)")
    parser.add_argument("--optimize", action="store_true",
                        help="Run parameter optimisation grid search instead of a single backtest")
    args = parser.parse_args()

    if args.optimize:
        run_optimize(args.symbol, args.tf, args.lookback, htf_timeframe=args.htf)
    else:
        run_backtest(
            symbol=args.symbol,
            timeframe=args.tf,
            lookback=args.lookback,
            min_rr=args.rr,
            htf_timeframe=args.htf,
            use_confirmation=args.confirm,
            confluence_threshold_override=args.confluence,
            swing_left_override=args.swing,
            csv_path=args.csv,
            equity_csv_path=args.equity,
        )


if __name__ == "__main__":
    main()
