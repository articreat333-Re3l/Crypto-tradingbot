"""
optimizer.py
============
Tests different parameter configurations against historical journal data
and ranks them by win rate, profit factor, expectancy, and drawdown.

How it works
------------
The journal records every signal's confluence_score, planned RR, ATR, and
the trade outcome (Win / Loss / Expired / Invalidated).  By filtering these
records with different threshold values we can answer questions like:

  "What if I had required confluence >= 7 instead of 6?
   Would win rate have improved?  How many trades would I have missed?"

Parameters tested
-----------------
  confluence_threshold : which signals would have been accepted
  min_rr               : minimum planned RR to accept a signal
  retest_mode          : all (both modes), or filter by how many had
                         quick entries (touch) vs slow (confirmation)

Parameters that genuinely require backtesting (ATR mult, pending expiry)
cannot be meaningfully tested from historical outcomes alone.  Those require
running the full backtest harness in backtest.py with different settings.

Run with:
    python optimizer.py
    python optimizer.py --days 30
    python optimizer.py --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import itertools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

import journal

W = 72


def _header(title: str) -> None:
    print("=" * W)
    print(f"  {title}")
    print("=" * W)


def _fetch_all_decided(since: Optional[str], symbol: Optional[str]) -> List[Dict]:
    """Pull every decided trade from the journal with its signal metadata."""
    filters = ["t.result IS NOT NULL"]
    params: list = []
    if since:
        filters.append("s.timestamp >= ?")
        params.append(since)
    if symbol:
        filters.append("s.symbol = ?")
        params.append(symbol)
    where = "WHERE " + " AND ".join(filters)
    return journal.query(
        f"""
        SELECT t.result, t.profit_r, t.duration_minutes,
               s.confluence_score, s.risk_reward, s.atr, s.symbol,
               s.direction, s.session, s.order_block_present,
               s.fair_value_gap_present, s.liquidity_sweep_present
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        {where}
        """,
        tuple(params),
    )


def _evaluate(subset: List[Dict]) -> Dict[str, Any]:
    """Compute the standard performance metrics for a subset of trades."""
    if not subset:
        return {
            "count": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "expectancy": 0.0, "net_r": 0.0, "max_dd": 0.0,
        }
    wins = [t for t in subset if t["result"] == "Win"]
    losses = [t for t in subset if t["result"] == "Loss"]
    w = len(wins)
    l = len(losses)
    total = w + l
    gp = sum(t["profit_r"] for t in wins if t["profit_r"] is not None)
    gl = abs(sum(t["profit_r"] for t in losses if t["profit_r"] is not None))
    net = gp - gl
    pf = gp / gl if gl > 0 else float("inf")
    wr = w / total * 100 if total else 0.0
    exp = net / total if total else 0.0

    # Max drawdown in R from equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in subset:
        equity += t.get("profit_r") or 0.0
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return {
        "count": total,
        "win_rate": round(wr, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else float("inf"),
        "expectancy": round(exp, 4),
        "net_r": round(net, 3),
        "max_dd": round(max_dd, 3),
    }


def run_confluence_optimization(
    all_trades: List[Dict],
    thresholds: List[int],
) -> None:
    """Test each confluence threshold value and print a comparison table."""
    print()
    print("  CONFLUENCE THRESHOLD OPTIMISATION")
    print(f"  {'Threshold':<12} {'Trades':>7} {'Win%':>8} {'PF':>9} "
          f"{'Expectancy':>12} {'Net R':>9} {'MaxDD':>8}")
    print(f"  {'─'*12} {'─'*7} {'─'*8} {'─'*9} {'─'*12} {'─'*9} {'─'*8}")

    results = []
    for thresh in thresholds:
        subset = [t for t in all_trades if (t.get("confluence_score") or 0) >= thresh]
        m = _evaluate(subset)
        pf_s = f"{m['profit_factor']:.3f}" if m["profit_factor"] != float("inf") else "     ∞"
        results.append((thresh, m))
        print(f"  {thresh:<12} {m['count']:>7} {m['win_rate']:>7.1f}% {pf_s:>9} "
              f"{m['expectancy']:>+12.4f} {m['net_r']:>+9.3f} {m['max_dd']:>8.3f}")

    # Highlight best
    best = max(results, key=lambda x: x[1]["profit_factor"]
               if x[1]["profit_factor"] != float("inf") else 0)
    print(f"\n  → Best by Profit Factor: threshold = {best[0]}")


def run_rr_optimization(
    all_trades: List[Dict],
    min_rr_values: List[float],
) -> None:
    """Test each minimum-RR filter and print a comparison table."""
    print()
    print("  MINIMUM RR FILTER OPTIMISATION")
    print(f"  {'Min RR':<10} {'Trades':>7} {'Win%':>8} {'PF':>9} "
          f"{'Expectancy':>12} {'Net R':>9} {'MaxDD':>8}")
    print(f"  {'─'*10} {'─'*7} {'─'*8} {'─'*9} {'─'*12} {'─'*9} {'─'*8}")

    results = []
    for rr in min_rr_values:
        subset = [t for t in all_trades if (t.get("risk_reward") or 0) >= rr]
        m = _evaluate(subset)
        pf_s = f"{m['profit_factor']:.3f}" if m["profit_factor"] != float("inf") else "     ∞"
        results.append((rr, m))
        print(f"  {rr:<10.1f} {m['count']:>7} {m['win_rate']:>7.1f}% {pf_s:>9} "
              f"{m['expectancy']:>+12.4f} {m['net_r']:>+9.3f} {m['max_dd']:>8.3f}")

    best = max(results, key=lambda x: (x[1]["expectancy"]))
    print(f"\n  → Best by Expectancy: min_rr = {best[0]:.1f}")


def run_combined_grid(all_trades: List[Dict]) -> None:
    """
    Grid search over (confluence_threshold × min_rr) combinations.
    Prints the top 10 by profit factor.
    """
    thresholds = [4, 5, 6, 7, 8, 9]
    min_rrs = [1.5, 2.0, 2.5, 3.0]
    combos = list(itertools.product(thresholds, min_rrs))

    print()
    print("  COMBINED GRID  (confluence × min_rr)  — Top 10 by Profit Factor")
    print(f"  {'Conf':>5} {'MinRR':>6} {'Trades':>7} {'Win%':>8} {'PF':>9} "
          f"{'Expectancy':>12} {'Net R':>9}")
    print(f"  {'─'*5} {'─'*6} {'─'*7} {'─'*8} {'─'*9} {'─'*12} {'─'*9}")

    ranked = []
    for conf, rr in combos:
        subset = [
            t for t in all_trades
            if (t.get("confluence_score") or 0) >= conf
            and (t.get("risk_reward") or 0) >= rr
        ]
        m = _evaluate(subset)
        if m["count"] >= 5:   # require at least 5 trades for meaningful stats
            ranked.append((conf, rr, m))

    ranked.sort(
        key=lambda x: x[2]["profit_factor"] if x[2]["profit_factor"] != float("inf") else 0,
        reverse=True,
    )

    for conf, rr, m in ranked[:10]:
        pf_s = f"{m['profit_factor']:.3f}" if m["profit_factor"] != float("inf") else "     ∞"
        print(f"  {conf:>5} {rr:>6.1f} {m['count']:>7} {m['win_rate']:>7.1f}% {pf_s:>9} "
              f"{m['expectancy']:>+12.4f} {m['net_r']:>+9.3f}")

    if ranked:
        best = ranked[0]
        print(f"\n  → Best overall: confluence >= {best[0]}, min_rr >= {best[1]:.1f}")
        print(f"    {best[2]['count']} trades | {best[2]['win_rate']:.1f}% WR | "
              f"PF {best[2]['profit_factor']:.3f} | Expectancy {best[2]['expectancy']:+.4f}R")


def run_session_analysis(all_trades: List[Dict]) -> None:
    """Break down performance by session."""
    print()
    print("  PERFORMANCE BY SESSION")
    sessions: Dict[str, List] = {}
    for t in all_trades:
        sess = t.get("session") or "Unknown"
        sessions.setdefault(sess, []).append(t)
    print(f"  {'Session':<12} {'Trades':>7} {'Win%':>8} {'PF':>9} {'Net R':>9}")
    print(f"  {'─'*12} {'─'*7} {'─'*8} {'─'*9} {'─'*9}")
    for sess, trades in sorted(sessions.items()):
        m = _evaluate(trades)
        pf_s = f"{m['profit_factor']:.3f}" if m["profit_factor"] != float("inf") else "     ∞"
        print(f"  {sess:<12} {m['count']:>7} {m['win_rate']:>7.1f}% {pf_s:>9} {m['net_r']:>+9.3f}")


def run_note_on_backtest_params() -> None:
    print()
    print("  NOTE ON ATR MULTIPLIER / PENDING EXPIRY OPTIMISATION")
    print("  ─" * 30)
    print("  These parameters affect which trades were created and how they")
    print("  were managed.  They cannot be tested by replaying historical")
    print("  outcomes alone -- use backtest.py with --optimize for those.")
    print()
    print("    python backtest.py --symbol BTCUSDT --tf 15m --lookback 800 --optimize")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Optimise parameters from journal history")
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument("--symbol", default=None, help="Filter to a single symbol")
    args = parser.parse_args()

    if not journal.DB_PATH.exists():
        print(f"Journal database not found at {journal.DB_PATH}")
        print("Run the bot first to accumulate trade history, then optimise.")
        sys.exit(1)

    since = None
    if args.days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    all_trades = _fetch_all_decided(since, args.symbol)

    if not all_trades:
        print("No decided trades found for the specified period / symbol.")
        print("The journal needs at least a few completed trades before optimisation is meaningful.")
        sys.exit(0)

    period_str = f"last {args.days}d" if args.days else "all time"
    sym_str = f" | {args.symbol}" if args.symbol else ""
    _header(f"PARAMETER OPTIMISER  ({period_str}{sym_str})  —  {len(all_trades)} trades")

    run_confluence_optimization(all_trades, thresholds=[4, 5, 6, 7, 8, 9])
    run_rr_optimization(all_trades, min_rr_values=[1.5, 2.0, 2.5, 3.0])
    run_combined_grid(all_trades)
    run_session_analysis(all_trades)
    run_note_on_backtest_params()

    print("=" * W)


if __name__ == "__main__":
    main()
