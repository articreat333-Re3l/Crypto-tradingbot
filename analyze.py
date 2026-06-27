"""
analyze.py
==========
Standalone analytics script.  Reads journal.db and prints a research-grade
report to stdout.  Run with:

    python analyze.py
    python analyze.py --days 30        # last 30 days only
    python analyze.py --symbol BTCUSDT # filter by symbol
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Ensure we can import journal.py from the same directory
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

import journal

W = 62  # report width


def _bar(label: str, value: str, width: int = 38) -> str:
    return f"  {label:<{width}} {value}"


def _section(title: str) -> None:
    print()
    print(f"  {'─' * (W - 4)}")
    print(f"  {title.upper()}")
    print(f"  {'─' * (W - 4)}")


def _header(title: str, subtitle: str = "") -> None:
    print("=" * W)
    print(f"  {title}")
    if subtitle:
        print(f"  {subtitle}")
    print("=" * W)


def _pct(n: float) -> str:
    return f"{n:.1f}%"


def _r(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return f"{n:+.3f}R" if n != 0 else "0.000R"


def _fmt_duration(minutes: Optional[float]) -> str:
    if not minutes:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# Core queries
# ---------------------------------------------------------------------------

def _signals(since: Optional[str], symbol: Optional[str]) -> List[Dict]:
    filters = []
    params: list = []
    if since:
        filters.append("timestamp >= ?")
        params.append(since)
    if symbol:
        filters.append("symbol = ?")
        params.append(symbol)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return journal.query(f"SELECT * FROM signals {where}", tuple(params))


def _trades(since: Optional[str], symbol: Optional[str]) -> List[Dict]:
    filters = ["t.result IS NOT NULL"]
    params: list = []
    if since:
        filters.append("s.timestamp >= ?")
        params.append(since)
    if symbol:
        filters.append("s.symbol = ?")
        params.append(symbol)
    where = f"WHERE {' AND '.join(filters)}"
    return journal.query(
        f"""
        SELECT t.*, s.symbol, s.direction, s.confluence_score, s.session,
               s.atr, s.volume_ratio, s.order_block_present,
               s.fair_value_gap_present, s.liquidity_sweep_present
        FROM trades t JOIN signals s ON s.id = t.signal_id
        {where}
        ORDER BY t.exit_time
        """,
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Streak helpers
# ---------------------------------------------------------------------------

def _streaks(trades: List[Dict]):
    best_win = worst_loss = cur_win = cur_loss = 0
    for t in trades:
        if t["result"] == "Win":
            cur_win += 1
            cur_loss = 0
            best_win = max(best_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            worst_loss = max(worst_loss, cur_loss)
    return best_win, worst_loss


def _max_drawdown_r(trades: List[Dict]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        r = t.get("profit_r") or 0.0
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def run_report(days: Optional[int] = None, symbol: Optional[str] = None) -> None:
    since = None
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sigs = _signals(since, symbol)
    trades = _trades(since, symbol)

    decided = [t for t in trades if t["result"] in ("Win", "Loss")]
    wins = [t for t in decided if t["result"] == "Win"]
    losses = [t for t in decided if t["result"] == "Loss"]

    total_sig = len(sigs)
    total_decided = len(decided)
    win_count = len(wins)
    loss_count = len(losses)

    win_rate = win_count / total_decided * 100 if total_decided else 0.0
    gross_profit = sum(t["profit_r"] for t in wins if t["profit_r"] is not None)
    gross_loss = abs(sum(t["profit_r"] for t in losses if t["profit_r"] is not None))
    net_r = gross_profit - gross_loss
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_rr = gross_profit / win_count if wins else 0.0
    expectancy = net_r / total_decided if total_decided else 0.0

    durations = [t["duration_minutes"] for t in decided if t["duration_minutes"]]
    avg_dur = sum(durations) / len(durations) if durations else 0.0

    max_dd = _max_drawdown_r(decided)
    best_streak, worst_streak = _streaks(decided)

    pending = sum(1 for s in sigs if s["status"] == "Pending")
    expired = sum(1 for s in sigs if s["status"] == "Expired")
    invalidated = sum(1 for s in sigs if s["status"] == "Invalidated")

    period_str = f"Last {days} days" if days else "All time"
    sym_str = f" | Symbol: {symbol}" if symbol else ""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    _header("BOT V2 — TRADING JOURNAL ANALYSIS", f"{period_str}{sym_str}  |  {now_str}")

    # --- Overview ---
    _section("Overview")
    print(_bar("Total signals detected",  str(total_sig)))
    print(_bar("Decided (TP or SL)",       str(total_decided)))
    print(_bar("Pending",                  str(pending)))
    print(_bar("Expired (no retest)",      str(expired)))
    print(_bar("Invalidated",              str(invalidated)))

    # --- Performance ---
    _section("Performance")
    pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"
    print(_bar("Win Rate",                 _pct(win_rate)))
    print(_bar("Loss Rate",                _pct(100 - win_rate) if total_decided else "—"))
    print(_bar("Wins / Losses",            f"{win_count} / {loss_count}"))
    print(_bar("Profit Factor",            pf_str))
    print(_bar("Expectancy",               _r(expectancy)))
    print(_bar("Average Winner RR",        _r(avg_rr)))
    print(_bar("Net R",                    _r(net_r)))
    print(_bar("Max Drawdown (R)",         f"{max_dd:.2f}R"))
    print(_bar("Largest Winning Streak",   str(best_streak)))
    print(_bar("Largest Losing Streak",    str(worst_streak)))
    print(_bar("Average Trade Duration",   _fmt_duration(avg_dur)))

    # --- By direction ---
    _section("Bullish vs Bearish")
    for direction in ("bullish", "bearish"):
        d_trades = [t for t in decided if t["direction"] == direction]
        d_wins = sum(1 for t in d_trades if t["result"] == "Win")
        d_wr = d_wins / len(d_trades) * 100 if d_trades else 0.0
        d_net = sum(t["profit_r"] for t in d_trades if t["profit_r"] is not None)
        print(_bar(f"{direction.capitalize()} ({len(d_trades)} trades)",
                   f"Win {_pct(d_wr)}  Net {_r(d_net)}"))

    # --- By symbol ---
    _section("Top 10 Symbols (by net R)")
    sym_stats: Dict[str, Dict] = {}
    for t in decided:
        s = t["symbol"]
        if s not in sym_stats:
            sym_stats[s] = {"wins": 0, "losses": 0, "net_r": 0.0}
        if t["result"] == "Win":
            sym_stats[s]["wins"] += 1
        else:
            sym_stats[s]["losses"] += 1
        sym_stats[s]["net_r"] += t.get("profit_r") or 0.0

    ranked = sorted(sym_stats.items(), key=lambda x: x[1]["net_r"], reverse=True)
    for sym, d in ranked[:10]:
        wr = d["wins"] / (d["wins"] + d["losses"]) * 100 if (d["wins"] + d["losses"]) else 0
        print(_bar(f"  {sym}",
                   f"{d['wins']}W {d['losses']}L  WR {_pct(wr)}  {_r(d['net_r'])}"))

    _section("Bottom 10 Symbols (by net R)")
    for sym, d in list(reversed(ranked))[:10]:
        wr = d["wins"] / (d["wins"] + d["losses"]) * 100 if (d["wins"] + d["losses"]) else 0
        print(_bar(f"  {sym}",
                   f"{d['wins']}W {d['losses']}L  WR {_pct(wr)}  {_r(d['net_r'])}"))

    # --- By session ---
    _section("Trades by Session")
    sessions_seen: Dict[str, Dict] = {}
    for t in decided:
        sess = t.get("session") or "Unknown"
        if sess not in sessions_seen:
            sessions_seen[sess] = {"wins": 0, "losses": 0, "net_r": 0.0}
        if t["result"] == "Win":
            sessions_seen[sess]["wins"] += 1
        else:
            sessions_seen[sess]["losses"] += 1
        sessions_seen[sess]["net_r"] += t.get("profit_r") or 0.0
    for sess, d in sorted(sessions_seen.items()):
        total_s = d["wins"] + d["losses"]
        wr_s = d["wins"] / total_s * 100 if total_s else 0.0
        print(_bar(f"  {sess}", f"{total_s} trades  WR {_pct(wr_s)}  {_r(d['net_r'])}"))

    # --- By confluence score ---
    _section("Confluence Score Breakdown  (score is 0–10)")
    bands = [(0, 5, "<5"), (5, 6, "5"), (6, 7, "6"), (7, 8, "7"), (8, 9, "8"), (9, 11, "9-10")]
    print(f"  {'Score':<8} {'Trades':>7} {'Win%':>8} {'Avg RR':>9} {'PF':>8} {'Net R':>9}")
    print(f"  {'─'*8} {'─'*7} {'─'*8} {'─'*9} {'─'*8} {'─'*9}")
    for lo, hi, label in bands:
        band = [t for t in decided if t.get("confluence_score") is not None
                and lo <= t["confluence_score"] < hi]
        if not band:
            continue
        bw = sum(1 for t in band if t["result"] == "Win")
        bl = len(band) - bw
        bwr = bw / len(band) * 100
        bg_p = sum(t["profit_r"] for t in band if t["result"] == "Win" and t["profit_r"])
        bg_l = abs(sum(t["profit_r"] for t in band if t["result"] == "Loss" and t["profit_r"]))
        bpf = bg_p / bg_l if bg_l else float("inf")
        bnet = bg_p - bg_l
        bavg = bg_p / bw if bw else 0.0
        pf_s = f"{bpf:.2f}" if bpf != float("inf") else "∞"
        print(f"  {label:<8} {len(band):>7} {_pct(bwr):>8} {bavg:>+9.3f} {pf_s:>8} {bnet:>+9.3f}")

    # --- ATR and volume ---
    _section("Market Condition Averages")
    atrs = [t["atr"] for t in decided if t.get("atr")]
    vols = [t["volume_ratio"] for t in decided if t.get("volume_ratio")]
    print(_bar("Average ATR",          f"{sum(atrs)/len(atrs):.4f}" if atrs else "—"))
    print(_bar("Average Volume Ratio", f"{sum(vols)/len(vols):.2f}" if vols else "—"))
    ob_pct = sum(1 for t in decided if t.get("order_block_present")) / total_decided * 100 if total_decided else 0
    fvg_pct = sum(1 for t in decided if t.get("fair_value_gap_present")) / total_decided * 100 if total_decided else 0
    sweep_pct = sum(1 for t in decided if t.get("liquidity_sweep_present")) / total_decided * 100 if total_decided else 0
    print(_bar("Order Block confluence %",    _pct(ob_pct)))
    print(_bar("FVG confluence %",            _pct(fvg_pct)))
    print(_bar("Liquidity Sweep present %",   _pct(sweep_pct)))

    print()
    print("=" * W)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a trading journal analytics report")
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument("--symbol", default=None, help="Filter to a single symbol")
    args = parser.parse_args()

    if not journal.DB_PATH.exists():
        print(f"Journal database not found at {journal.DB_PATH}")
        print("The bot must run at least once before analysis is possible.")
        sys.exit(1)

    run_report(days=args.days, symbol=args.symbol)


if __name__ == "__main__":
    main()
