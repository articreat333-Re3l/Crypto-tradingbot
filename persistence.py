"""
persistence.py
===============
Durable storage for pending/running/archived trades, alert cooldowns,
and small bits of bot state (last daily-summary date, etc).

Backend: SQLite, always.

SQLAlchemy Core is used for schema management, connection handling, and
query building. The database file is created automatically on first run.
WAL mode is enabled so the Telegram polling thread (reads) and the main
scanner loop (writes) never block each other.
"""

from __future__ import annotations

import json
import threading
import time
from typing import List, Optional

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    update,
)

from config import settings
from logger import get_logger
from models import Trade, TradeState

log = get_logger(__name__)

metadata = MetaData()

trades_table = Table(
    "trades",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("direction", String(16), nullable=False),
    Column("zone_top", Float, nullable=False),
    Column("zone_bottom", Float, nullable=False),
    Column("atr", Float, nullable=False),
    Column("stop_loss", Float, nullable=False),
    Column("take_profit", Float, nullable=False),
    Column("planned_rr", Float, nullable=False),
    Column("confluence_score", Integer, nullable=False),
    Column("retest_mode", String(16), nullable=False),
    Column("created_ts", Float, nullable=False),
    Column("expiry_ts", Float, nullable=False),
    Column("state", String(16), nullable=False, index=True),
    Column("touched_ts", Float, nullable=True),
    Column("entry_price", Float, nullable=True),
    Column("triggered_ts", Float, nullable=True),
    Column("closed_ts", Float, nullable=True),
    Column("exit_price", Float, nullable=True),
    Column("realized_rr", Float, nullable=True),
    Column("source", String(16), nullable=False, default="swing"),
    Column("pattern", String(64), nullable=False, default=""),
    # Accounting columns added in v2.1
    Column("planned_entry",   Float, nullable=True),
    Column("actual_target",   Float, nullable=True),
    Column("slippage",        Float, nullable=True),
    Column("risk_distance",   Float, nullable=True),
    Column("reward_distance", Float, nullable=True),
    Column("exit_reason",     String(16), nullable=True),
)

cooldowns_table = Table(
    "cooldowns",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("last_ts", Float, nullable=False),
)

kv_table = Table(
    "kv_state",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", String(2048), nullable=False),
)


def _build_engine():
    log.info("Persistence backend: SQLite (%s)", settings.sqlite_path)
    return create_engine(
        f"sqlite:///{settings.sqlite_path}",
        connect_args={"check_same_thread": False},
    )


_engine = _build_engine()
_lock = threading.Lock()  # serialises writes; reads are lock-free


def init_db() -> None:
    metadata.create_all(_engine)
    # Migrate existing databases: add new accounting columns if absent.
    # SQLite ignores ADD COLUMN when the column already exists (via try/except).
    _new_columns = [
        ("planned_entry",   "REAL"),
        ("actual_target",   "REAL"),
        ("slippage",        "REAL"),
        ("risk_distance",   "REAL"),
        ("reward_distance", "REAL"),
        ("exit_reason",     "TEXT"),
    ]
    with _engine.begin() as conn:
        for col, col_type in _new_columns:
            try:
                conn.exec_driver_sql(
                    f"ALTER TABLE trades ADD COLUMN {col} {col_type}"
                )
            except Exception:
                pass  # column already exists — safe to ignore
    # WAL mode: readers never block writers and writers never block readers.
    try:
        with _engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    log.info("Database initialised")


def save_trade(trade: Trade) -> None:
    row = trade.to_row()
    with _lock, _engine.begin() as conn:
        result = conn.execute(
            update(trades_table).where(trades_table.c.id == trade.id).values(**row)
        )
        if result.rowcount == 0:
            conn.execute(insert(trades_table).values(**row))


def delete_trade(trade_id: str) -> None:
    with _lock, _engine.begin() as conn:
        conn.execute(delete(trades_table).where(trades_table.c.id == trade_id))


def get_trades(symbol: Optional[str] = None, states: Optional[List[TradeState]] = None) -> List[Trade]:
    query = select(trades_table)
    if symbol is not None:
        query = query.where(trades_table.c.symbol == symbol)
    if states is not None:
        query = query.where(trades_table.c.state.in_([s.value for s in states]))
    with _engine.connect() as conn:
        rows = conn.execute(query).mappings().all()
    return [Trade.from_row(dict(r)) for r in rows]


def get_archived_trades(limit: int = 5000) -> List[Trade]:
    terminal = [TradeState.TP_HIT, TradeState.SL_HIT, TradeState.EXPIRED, TradeState.INVALIDATED]
    query = (
        select(trades_table)
        .where(trades_table.c.state.in_([s.value for s in terminal]))
        .order_by(trades_table.c.created_ts.desc())
        .limit(limit)
    )
    with _engine.connect() as conn:
        rows = conn.execute(query).mappings().all()
    return [Trade.from_row(dict(r)) for r in rows]


# --- Cooldowns -------------------------------------------------------------

def get_cooldown(key: str) -> float:
    with _engine.connect() as conn:
        row = conn.execute(
            select(cooldowns_table.c.last_ts).where(cooldowns_table.c.key == key)
        ).first()
    return float(row[0]) if row else 0.0


def set_cooldown(key: str, ts: float = None) -> None:
    ts = time.time() if ts is None else ts
    with _lock, _engine.begin() as conn:
        result = conn.execute(
            update(cooldowns_table).where(cooldowns_table.c.key == key).values(last_ts=ts)
        )
        if result.rowcount == 0:
            conn.execute(insert(cooldowns_table).values(key=key, last_ts=ts))


# --- Small key/value bot state ---------------------------------------------

def get_kv(key: str, default=None):
    with _engine.connect() as conn:
        row = conn.execute(select(kv_table.c.value).where(kv_table.c.key == key)).first()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return row[0]


def set_kv(key: str, value) -> None:
    serialized = json.dumps(value) if not isinstance(value, str) else value
    with _lock, _engine.begin() as conn:
        result = conn.execute(
            update(kv_table).where(kv_table.c.key == key).values(value=serialized)
        )
        if result.rowcount == 0:
            conn.execute(insert(kv_table).values(key=key, value=serialized))
