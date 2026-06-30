"""
journal.py
==========
Research-grade trading journal.  All database logic lives here; nothing
outside this module touches SQLite directly.

Backend:   data/journal.db   (sqlite3 only, no SQLAlchemy, no ORM)
Backups:   data/backups/journal_YYYYMMDD.db   (rolling 30-day window)
Exports:   exports/signals.csv  trades.csv  health.csv  errors.csv

Design decisions
----------------
- Every write uses a module-level threading.Lock so the Telegram polling
  thread and the main scanner thread can't corrupt the DB simultaneously.
- sqlite3 timeout=15 handles the OS-level file lock; WAL mode means readers
  never block writers (and vice-versa) on the same connection.
- All public functions swallow their own exceptions and log them -- the bot
  must never stop because the journal had a problem.
- The journal is append-only for signals and errors; trades and health rows
  are upserted so repeated calls are idempotent.
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import threading
import time
import traceback as tb_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE = Path("data")
DB_PATH = _BASE / "journal.db"
BACKUP_DIR = _BASE / "backups"
EXPORT_DIR = Path("exports")
MAX_BACKUPS = 30

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    _BASE.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _write(sql: str, params: tuple = ()) -> None:
    """Execute a single write statement with automatic rollback on failure."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.error("Journal write failed [%.60s]: %s", sql, exc)
        finally:
            conn.close()


def _write_many(sql: str, rows: List[tuple]) -> None:
    """Batch insert."""
    if not rows:
        return
    with _lock:
        conn = _connect()
        try:
            conn.executemany(sql, rows)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.error("Journal batch write failed: %s", exc)
        finally:
            conn.close()


def _read(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _read_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        return cur.fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trading_session(utc_hour: int) -> str:
    """Classify current UTC hour into a trading session name."""
    if utc_hour >= 22 or utc_hour < 7:
        return "Asia"
    if 7 <= utc_hour < 12:
        return "London"
    if 12 <= utc_hour < 16:
        return "Overlap"
    if 16 <= utc_hour < 22:
        return "New York"
    return "Off"


# ---------------------------------------------------------------------------
# initialize_database
# ---------------------------------------------------------------------------

def initialize_database() -> None:
    """
    Create the data/ directory and journal.db if they don't exist.
    Idempotent -- safe to call on every bot start.
    """
    _ensure_dirs()
    with _lock:
        conn = _connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id                     TEXT PRIMARY KEY,
                    timestamp              TEXT NOT NULL,
                    symbol                 TEXT NOT NULL,
                    direction              TEXT NOT NULL,
                    timeframe              TEXT NOT NULL,
                    trend                  TEXT,
                    support_level          REAL,
                    resistance_level       REAL,
                    zone_top               REAL NOT NULL,
                    zone_bottom            REAL NOT NULL,
                    planned_entry          REAL NOT NULL,
                    stop_loss              REAL NOT NULL,
                    take_profit            REAL NOT NULL,
                    risk_reward            REAL NOT NULL,
                    atr                    REAL,
                    volume_ratio           REAL,
                    confluence_score       INTEGER,
                    order_block_present    INTEGER DEFAULT 0,
                    fair_value_gap_present INTEGER DEFAULT 0,
                    liquidity_sweep_present INTEGER DEFAULT 0,
                    session                TEXT,
                    status                 TEXT NOT NULL DEFAULT 'Pending'
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id                      TEXT PRIMARY KEY,
                    signal_id               TEXT NOT NULL,
                    entry_time              TEXT,
                    exit_time               TEXT,
                    entry_price             REAL,
                    exit_price              REAL,
                    stop_loss               REAL,
                    take_profit             REAL,
                    result                  TEXT,
                    profit_r                REAL,
                    profit_percent          REAL,
                    duration_minutes        REAL,
                    max_drawdown            REAL,
                    max_favorable_excursion REAL,
                    exit_reason             TEXT,
                    planned_entry           REAL,
                    actual_target           REAL,
                    slippage                REAL,
                    risk_distance           REAL,
                    reward_distance         REAL,
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                );

                CREATE TABLE IF NOT EXISTS health (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL,
                    symbols_scanned  INTEGER NOT NULL,
                    pending_trades   INTEGER NOT NULL,
                    active_trades    INTEGER NOT NULL,
                    scan_duration_ms INTEGER NOT NULL,
                    api_errors       INTEGER NOT NULL DEFAULT 0,
                    heartbeat_sent   INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS errors (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         TEXT NOT NULL,
                    symbol            TEXT,
                    module            TEXT,
                    function          TEXT,
                    exception_type    TEXT,
                    exception_message TEXT,
                    traceback         TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_signals_symbol   ON signals(symbol);
                CREATE INDEX IF NOT EXISTS idx_signals_status   ON signals(status);
                CREATE INDEX IF NOT EXISTS idx_signals_ts       ON signals(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_signal_id ON trades(signal_id);
                CREATE INDEX IF NOT EXISTS idx_health_ts        ON health(timestamp);
                CREATE INDEX IF NOT EXISTS idx_errors_ts        ON errors(timestamp);
            """)
            conn.commit()
            log.info("Journal database ready at %s", DB_PATH)
        except Exception as exc:
            log.error("Journal init failed: %s", exc)
        finally:
            conn.close()

    # Migrate existing journal databases: add new accounting columns if absent.
    _journal_new_cols = [
        ("planned_entry",   "REAL"),
        ("actual_target",   "REAL"),
        ("slippage",        "REAL"),
        ("risk_distance",   "REAL"),
        ("reward_distance", "REAL"),
    ]
    with _lock:
        conn = _connect()
        try:
            for col, col_type in _journal_new_cols:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                except Exception:
                    pass  # column already exists
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# record_signal
# ---------------------------------------------------------------------------

def record_signal(
    trade: Any,           # models.Trade  (avoiding circular import)
    *,
    volume_ratio: float,
    execution_trend: str,
    nearest_support: Optional[float],
    nearest_resistance: Optional[float],
    order_block_present: bool,
    fair_value_gap_present: bool,
    liquidity_sweep_present: bool,
) -> None:
    """Insert one row into the signals table when a pending trade is created."""
    try:
        utc_hour = datetime.now(timezone.utc).hour
        _write(
            """
            INSERT OR IGNORE INTO signals (
                id, timestamp, symbol, direction, timeframe, trend,
                support_level, resistance_level,
                zone_top, zone_bottom, planned_entry,
                stop_loss, take_profit, risk_reward,
                atr, volume_ratio, confluence_score,
                order_block_present, fair_value_gap_present, liquidity_sweep_present,
                session, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade.id,
                _now_iso(),
                trade.symbol,
                trade.direction.value,
                "15m",              # execution timeframe constant -- from settings if needed
                execution_trend,
                nearest_support,
                nearest_resistance,
                trade.zone_top,
                trade.zone_bottom,
                (trade.zone_top + trade.zone_bottom) / 2.0,
                trade.stop_loss,
                trade.take_profit,
                trade.planned_rr,
                trade.atr,
                volume_ratio,
                trade.confluence_score,
                int(order_block_present),
                int(fair_value_gap_present),
                int(liquidity_sweep_present),
                _trading_session(utc_hour),
                "Pending",
            ),
        )
    except Exception as exc:
        log.error("record_signal failed for %s: %s", getattr(trade, "id", "?"), exc)


# ---------------------------------------------------------------------------
# update_signal_status
# ---------------------------------------------------------------------------

def update_signal_status(signal_id: str, status: str) -> None:
    """
    Update the lifecycle status of a signal.
    Valid statuses: Pending, Triggered, Expired, Invalidated, Cancelled, Completed
    """
    try:
        _write(
            "UPDATE signals SET status=? WHERE id=?",
            (status, signal_id),
        )
    except Exception as exc:
        log.error("update_signal_status failed for %s: %s", signal_id, exc)


# ---------------------------------------------------------------------------
# record_trade_entry
# ---------------------------------------------------------------------------

def record_trade_entry(trade: Any) -> None:
    """Upsert the trades row when a pending trade is triggered (entry fires)."""
    try:
        entry_time = (
            datetime.fromtimestamp(trade.triggered_ts, tz=timezone.utc).isoformat()
            if trade.triggered_ts else _now_iso()
        )
        _write(
            """
            INSERT INTO trades (id, signal_id, entry_time, entry_price, stop_loss, take_profit)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                entry_time=excluded.entry_time,
                entry_price=excluded.entry_price,
                stop_loss=excluded.stop_loss,
                take_profit=excluded.take_profit
            """,
            (
                trade.id,
                trade.id,       # signal_id == trade.id (1:1 in this architecture)
                entry_time,
                trade.entry_price,
                trade.stop_loss,
                trade.take_profit,
            ),
        )
    except Exception as exc:
        log.error("record_trade_entry failed for %s: %s", getattr(trade, "id", "?"), exc)


# ---------------------------------------------------------------------------
# record_trade_exit
# ---------------------------------------------------------------------------

def record_trade_exit(trade: Any, exit_reason: str) -> None:
    """
    Update the trades row when a trade closes.
    All prices come from actual executed values on the Trade object.
    exit_reason: 'TP' | 'SL' | 'Manual' | 'Expired' | 'Cancelled' | 'Invalidated'
    """
    try:
        exit_time = (
            datetime.fromtimestamp(trade.closed_ts, tz=timezone.utc).isoformat()
            if trade.closed_ts else _now_iso()
        )
        if exit_reason == "TP":
            result = "Win"
        elif exit_reason == "SL":
            result = "Loss"
        else:
            # Expired / Invalidated / Cancelled / Manual never entered a
            # position with a TP/SL resolution -- not a win or a loss.
            result = None

        duration_min: Optional[float] = None
        if trade.triggered_ts and trade.closed_ts:
            duration_min = (trade.closed_ts - trade.triggered_ts) / 60.0

        profit_pct: Optional[float] = None
        if trade.entry_price and trade.exit_price and trade.entry_price != 0:
            if trade.direction.value == "bullish":
                profit_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
            else:
                profit_pct = (trade.entry_price - trade.exit_price) / trade.entry_price * 100

        _write(
            """
            INSERT INTO trades (
                id, signal_id, entry_time, exit_time,
                entry_price, exit_price, stop_loss, take_profit,
                result, profit_r, profit_percent, duration_minutes,
                max_drawdown, max_favorable_excursion, exit_reason,
                planned_entry, actual_target, slippage,
                risk_distance, reward_distance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                exit_time=excluded.exit_time,
                exit_price=excluded.exit_price,
                result=excluded.result,
                profit_r=excluded.profit_r,
                profit_percent=excluded.profit_percent,
                duration_minutes=excluded.duration_minutes,
                exit_reason=excluded.exit_reason,
                actual_target=excluded.actual_target,
                slippage=excluded.slippage,
                risk_distance=excluded.risk_distance,
                reward_distance=excluded.reward_distance
            """,
            (
                trade.id,
                trade.id,
                datetime.fromtimestamp(trade.triggered_ts, tz=timezone.utc).isoformat()
                    if trade.triggered_ts else None,
                exit_time,
                trade.entry_price,
                trade.exit_price,
                trade.stop_loss,
                trade.take_profit,
                result,
                trade.realized_rr,
                profit_pct,
                duration_min,
                None,   # max_drawdown: requires intrabar tracking
                None,   # max_favorable_excursion: requires intrabar tracking
                exit_reason,
                trade.planned_entry,
                trade.actual_target,
                trade.slippage,
                trade.risk_distance,
                trade.reward_distance,
            ),
        )
    except Exception as exc:
        log.error("record_trade_exit failed for %s: %s", getattr(trade, "id", "?"), exc)


# ---------------------------------------------------------------------------
# record_health
# ---------------------------------------------------------------------------

def record_health(
    symbols_scanned: int,
    pending_trades: int,
    active_trades: int,
    scan_duration_ms: int,
    api_errors: int = 0,
    heartbeat_sent: bool = False,
) -> None:
    try:
        _write(
            """
            INSERT INTO health
                (timestamp, symbols_scanned, pending_trades, active_trades,
                 scan_duration_ms, api_errors, heartbeat_sent)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                _now_iso(),
                symbols_scanned,
                pending_trades,
                active_trades,
                scan_duration_ms,
                api_errors,
                int(heartbeat_sent),
            ),
        )
    except Exception as exc:
        log.error("record_health failed: %s", exc)


# ---------------------------------------------------------------------------
# record_error
# ---------------------------------------------------------------------------

def record_error(
    symbol: Optional[str],
    module: str,
    function: str,
    exc: Exception,
    traceback_str: Optional[str] = None,
) -> None:
    """Log an exception to the errors table. Never raises."""
    try:
        tb_str = traceback_str or tb_module.format_exc()
        _write(
            """
            INSERT INTO errors
                (timestamp, symbol, module, function,
                 exception_type, exception_message, traceback)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                _now_iso(),
                symbol,
                module,
                function,
                type(exc).__name__,
                str(exc)[:1000],
                tb_str[:4000],
            ),
        )
    except Exception:
        pass   # truly last resort -- never propagate


# ---------------------------------------------------------------------------
# export_csv
# ---------------------------------------------------------------------------

def export_csv(output_dir: Optional[str] = None) -> None:
    """
    Export all four tables to CSV files.
    Called automatically once per day from botv2.py.
    Also exposed as export_all() for manual use.
    """
    _ensure_dirs()
    out = Path(output_dir) if output_dir else EXPORT_DIR

    tables = ["signals", "trades", "health", "errors"]
    for table in tables:
        try:
            rows = _read(f"SELECT * FROM {table}")
            if not rows:
                continue
            filepath = out / f"{table}.csv"
            with open(filepath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(rows[0].keys())
                writer.writerows([tuple(r) for r in rows])
            log.info("Exported %s → %s (%d rows)", table, filepath, len(rows))
        except Exception as exc:
            log.error("CSV export failed for %s: %s", table, exc)


def export_all(output_dir: Optional[str] = None) -> None:
    """Alias for export_csv -- exports everything immediately."""
    export_csv(output_dir)


# ---------------------------------------------------------------------------
# backup_database
# ---------------------------------------------------------------------------

def backup_database() -> Optional[Path]:
    """
    Copy journal.db to data/backups/journal_YYYYMMDD.db.
    Prunes backups older than MAX_BACKUPS (30).
    Returns the backup path, or None on failure.
    """
    _ensure_dirs()
    if not DB_PATH.exists():
        log.warning("backup_database: journal.db does not exist yet, skipping")
        return None
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        dest = BACKUP_DIR / f"journal_{date_str}.db"
        shutil.copy2(str(DB_PATH), str(dest))
        log.info("Database backed up to %s", dest)

        # Prune: keep only the most recent MAX_BACKUPS files
        backups = sorted(BACKUP_DIR.glob("journal_*.db"))
        while len(backups) > MAX_BACKUPS:
            oldest = backups.pop(0)
            oldest.unlink()
            log.info("Pruned old backup: %s", oldest)

        return dest
    except Exception as exc:
        log.error("backup_database failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Query helpers used by analyze.py / dashboard.py / optimizer.py
# ---------------------------------------------------------------------------

def query(sql: str, params: tuple = ()) -> List[Dict]:
    """Generic read-only query returning a list of dicts.  For use by analytics modules."""
    try:
        rows = _read(sql, params)
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("journal.query failed: %s", exc)
        return []


def query_one(sql: str, params: tuple = ()) -> Optional[Dict]:
    try:
        row = _read_one(sql, params)
        return dict(row) if row else None
    except Exception as exc:
        log.error("journal.query_one failed: %s", exc)
        return None
