"""
stats.py
========
Computes the statistics block from the brief, sourced entirely from
archived (terminal-state) trades in the database -- never from "signals
sent", since a sent signal that never triggered or got stopped out isn't
a win.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

import persistence
from models import TradeState


def compute_stats(lookback_days: Optional[int] = None) -> dict:
    trades = persistence.get_archived_trades()
    if lookback_days:
        cutoff = time.time() - lookback_days * 86400
        trades = [t for t in trades if t.created_ts >= cutoff]

    decided = [t for t in trades if t.state in (TradeState.TP_HIT, TradeState.SL_HIT)]
    total_decided = len(decided)
    tp_count = sum(1 for t in decided if t.state is TradeState.TP_HIT)
    sl_count = total_decided - tp_count
    win_rate = (tp_count / total_decided * 100) if total_decided else 0.0
    loss_rate = (sl_count / total_decided * 100) if total_decided else 0.0

    rr_values = [t.realized_rr for t in decided if t.realized_rr is not None]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    by_symbol = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})
    for t in decided:
        bucket = by_symbol[t.symbol]
        if t.state is TradeState.TP_HIT:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["total_r"] += t.realized_rr or 0.0

    ranked = sorted(by_symbol.items(), key=lambda kv: kv[1]["total_r"], reverse=True)
    best_symbols = ranked[:5]
    worst_symbols = list(reversed(ranked[-5:])) if ranked else []

    by_direction: dict = defaultdict(float)
    for t in decided:
        by_direction[t.direction.value] += t.realized_rr or 0.0
    most_profitable_direction = (
        max(by_direction.items(), key=lambda kv: kv[1])[0] if by_direction else None
    )

    durations = [
        (t.closed_ts - t.triggered_ts)
        for t in decided
        if t.closed_ts and t.triggered_ts
    ]
    avg_duration_seconds = sum(durations) / len(durations) if durations else 0.0

    if trades:
        span_days = max((time.time() - min(t.created_ts for t in trades)) / 86400, 1.0)
        signals_per_day = len(trades) / span_days
    else:
        signals_per_day = 0.0

    return {
        "total_signals": len(trades),
        "total_decided": total_decided,
        "tp_count": tp_count,
        "sl_count": sl_count,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "tp_hit_pct": win_rate,
        "sl_hit_pct": loss_rate,
        "avg_rr": avg_rr,
        "best_symbols": best_symbols,
        "worst_symbols": worst_symbols,
        "most_profitable_direction": most_profitable_direction,
        "avg_duration_seconds": avg_duration_seconds,
        "signals_per_day": signals_per_day,
        "expired": sum(1 for t in trades if t.state is TradeState.EXPIRED),
        "invalidated": sum(1 for t in trades if t.state is TradeState.INVALIDATED),
    }


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0m"
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"
