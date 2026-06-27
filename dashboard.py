"""
dashboard.py
============
Pure data-access layer -- no GUI, no HTTP server.

Every function queries journal.db and returns plain Python dicts or lists
so a web dashboard (FastAPI, Flask, or any frontend) can call these
functions directly without needing to know the SQL schema.

All functions are read-only and never raise; they return empty structures
on database errors so callers can render gracefully degraded views.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import journal
from logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _period_start(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.isoformat()


def _compute_stats_for_period(start_iso: str) -> Dict[str, Any]:
    """Compute the standard performance block for a given start date."""
    rows = journal.query(
        """
        SELECT t.profit_r, t.result, t.duration_minutes, t.exit_reason,
               s.symbol, s.direction, s.confluence_score, s.session
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        WHERE s.timestamp >= ?
          AND t.result IS NOT NULL
        """,
        (start_iso,),
    )

    decided = [r for r in rows if r["result"] in ("Win", "Loss")]
    wins = [r for r in decided if r["result"] == "Win"]
    losses = [r for r in decided if r["result"] == "Loss"]

    win_count = len(wins)
    loss_count = len(losses)
    total_decided = win_count + loss_count
    win_rate = win_count / total_decided * 100 if total_decided else 0.0

    gross_profit = sum(r["profit_r"] for r in wins if r["profit_r"] is not None)
    gross_loss = abs(sum(r["profit_r"] for r in losses if r["profit_r"] is not None))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_rr = gross_profit / win_count if wins else 0.0
    net_r = gross_profit - gross_loss
    expectancy = (net_r / total_decided) if total_decided else 0.0

    durations = [r["duration_minutes"] for r in decided if r["duration_minutes"] is not None]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    # Signal counts (from signals table for the period)
    signal_counts = journal.query_one(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='Pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status='Triggered' THEN 1 ELSE 0 END) as triggered,
            SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status='Expired' THEN 1 ELSE 0 END) as expired,
            SUM(CASE WHEN status='Invalidated' THEN 1 ELSE 0 END) as invalidated
        FROM signals WHERE timestamp >= ?
        """,
        (start_iso,),
    ) or {}

    return {
        "total_signals": signal_counts.get("total", 0),
        "pending": signal_counts.get("pending", 0),
        "triggered": signal_counts.get("triggered", 0),
        "completed": signal_counts.get("completed", 0),
        "expired": signal_counts.get("expired", 0),
        "invalidated": signal_counts.get("invalidated", 0),
        "total_decided": total_decided,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "net_r": round(net_r, 3),
        "avg_rr": round(avg_rr, 3),
        "expectancy": round(expectancy, 3),
        "avg_duration_minutes": round(avg_duration, 1),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def daily_stats() -> Dict[str, Any]:
    """Performance statistics for the last 24 hours."""
    return _compute_stats_for_period(_period_start(1))


def weekly_stats() -> Dict[str, Any]:
    """Performance statistics for the last 7 days."""
    return _compute_stats_for_period(_period_start(7))


def monthly_stats() -> Dict[str, Any]:
    """Performance statistics for the last 30 days."""
    return _compute_stats_for_period(_period_start(30))


def pending_trades() -> List[Dict]:
    """All signals currently in Pending status."""
    return journal.query(
        """
        SELECT id, timestamp, symbol, direction, zone_top, zone_bottom,
               planned_entry, stop_loss, take_profit, risk_reward,
               confluence_score, session, atr
        FROM signals
        WHERE status = 'Pending'
        ORDER BY timestamp DESC
        """
    )


def active_trades() -> List[Dict]:
    """All trades currently triggered/running (joined with signal data)."""
    return journal.query(
        """
        SELECT t.id, t.entry_time, t.entry_price, t.stop_loss, t.take_profit,
               s.symbol, s.direction, s.confluence_score, s.session,
               s.risk_reward, s.atr
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        WHERE s.status = 'Triggered'
          AND t.exit_time IS NULL
        ORDER BY t.entry_time DESC
        """
    )


def recent_completed(limit: int = 20) -> List[Dict]:
    """Most recent completed trades with P&L."""
    return journal.query(
        """
        SELECT t.id, t.entry_time, t.exit_time, t.entry_price, t.exit_price,
               t.result, t.profit_r, t.profit_percent, t.duration_minutes,
               t.exit_reason, s.symbol, s.direction, s.confluence_score, s.session
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        WHERE t.result IS NOT NULL
        ORDER BY t.exit_time DESC
        LIMIT ?
        """,
        (limit,),
    )


def recent_errors(limit: int = 20) -> List[Dict]:
    """Most recent error records."""
    return journal.query(
        """
        SELECT id, timestamp, symbol, module, function,
               exception_type, exception_message
        FROM errors
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (limit,),
    )


def health_summary(hours: int = 24) -> Dict[str, Any]:
    """Aggregate health metrics for the last N hours."""
    start = _period_start(hours // 24 + 1)
    row = journal.query_one(
        """
        SELECT
            COUNT(*)            as cycles,
            AVG(scan_duration_ms) as avg_duration_ms,
            MAX(scan_duration_ms) as max_duration_ms,
            SUM(api_errors)     as total_api_errors,
            SUM(heartbeat_sent) as heartbeats_sent,
            MIN(pending_trades) as min_pending,
            MAX(pending_trades) as max_pending,
            AVG(active_trades)  as avg_active
        FROM health
        WHERE timestamp >= ?
        """,
        (start,),
    )
    return dict(row) if row else {}


def symbol_leaderboard(limit: int = 10) -> List[Dict]:
    """Best performing symbols by total R."""
    return journal.query(
        """
        SELECT s.symbol,
               COUNT(*) as trades,
               SUM(CASE WHEN t.result='Win' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN t.result='Loss' THEN 1 ELSE 0 END) as losses,
               ROUND(SUM(t.profit_r), 3) as total_r,
               ROUND(AVG(t.profit_r), 3) as avg_r
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        WHERE t.result IS NOT NULL
        GROUP BY s.symbol
        ORDER BY total_r DESC
        LIMIT ?
        """,
        (limit,),
    )
