"""
models.py
=========
Shared dataclasses and enums used across the whole project.

Centralising these avoids every module re-declaring its own version of
"Zone" or "Trade" (which is how most hobby SMC bots end up with three
slightly-different, slowly-diverging definitions of the same concept).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"

    @property
    def opposite(self) -> "Direction":
        return Direction.BEARISH if self is Direction.BULLISH else Direction.BULLISH


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    RANGE = "range"


class ZoneSource(str, Enum):
    SWING = "swing"          # built from the swing that created the broken level (Step 5)
    ORDER_BLOCK = "order_block"
    FVG = "fair_value_gap"


class RetestMode(str, Enum):
    TOUCH = "touch"                  # fire the instant price re-enters the zone
    CONFIRMATION = "confirmation"    # require a rejection candle inside the zone


class TradeState(str, Enum):
    PENDING = "pending"           # zone built, waiting for retest
    TRIGGERED = "triggered"       # retest condition just satisfied this tick
    RUNNING = "running"           # live, being monitored against TP/SL
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    EXPIRED = "expired"           # retest never happened in time
    INVALIDATED = "invalidated"   # structure broke before retest happened
    ARCHIVED = "archived"         # terminal state, counted in stats


TERMINAL_STATES = {
    TradeState.TP_HIT,
    TradeState.SL_HIT,
    TradeState.EXPIRED,
    TradeState.INVALIDATED,
    TradeState.ARCHIVED,
}


@dataclass(frozen=True)
class Swing:
    """A single swing high or swing low pivot."""
    index: int          # integer position inside the dataframe it was detected on
    price: float
    is_high: bool
    timestamp: Optional[int] = None  # epoch ms, when available from the candle


@dataclass(frozen=True)
class Level:
    """An automatically-built support/resistance level (Step 3)."""
    price: float
    is_resistance: bool
    touches: int
    last_touch_index: int
    formed_by_swing: Swing  # the most recent swing that defines this level


@dataclass(frozen=True)
class BreakoutEvent:
    """A confirmed breakout of a Level (Step 4)."""
    symbol: str
    direction: Direction
    level: Level
    candle_index: int
    close_price: float
    candle_high: float
    candle_low: float
    candle_open: float
    volume_ratio: float
    body_ratio: float
    atr: float
    liquidity_sweep: bool  # True if breakout candle also swept the opposite side first
    timestamp: int = field(default_factory=lambda: int(time.time()))


@dataclass
class Zone:
    """
    A supply/demand point-of-interest (Step 5).

    `top` is always the higher price, `bottom` the lower price, regardless of
    direction -- direction tells you which side price is expected to react from.
    """
    symbol: str
    direction: Direction
    top: float
    bottom: float
    source: ZoneSource
    created_index: int

    def __post_init__(self) -> None:
        if self.bottom > self.top:
            self.top, self.bottom = self.bottom, self.top

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    def overlaps(self, other: "Zone", tolerance_pct: float = 0.0015) -> bool:
        pad = self.midpoint * tolerance_pct
        return not (self.top + pad < other.bottom or self.bottom - pad > other.top)

    def contains_price(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def overlaps_range(self, low: float, high: float) -> bool:
        return not (high < self.bottom or low > self.top)


@dataclass
class ConfluenceResult:
    score: int
    max_score: int
    threshold: int
    passed: bool
    breakdown: dict = field(default_factory=dict)  # factor name -> points awarded


@dataclass
class Trade:
    """A trade as it moves through its full lifecycle."""
    symbol: str
    direction: Direction
    zone_top: float
    zone_bottom: float
    atr: float
    stop_loss: float        # planned stop  (set at signal creation, never moved)
    take_profit: float      # planned target (based on zone.midpoint; kept for reference)
    planned_rr: float       # RR computed from zone.midpoint → stop → planned TP
    confluence_score: int
    retest_mode: RetestMode
    created_ts: float
    expiry_ts: float
    state: TradeState = TradeState.PENDING
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    touched_ts: Optional[float] = None
    entry_price: Optional[float] = None      # actual fill price (set at trigger)
    triggered_ts: Optional[float] = None
    closed_ts: Optional[float] = None
    exit_price: Optional[float] = None       # actual exit price (set at close)
    realized_rr: Optional[float] = None      # set only on close, never before
    source: str = ZoneSource.SWING.value
    pattern: str = ""
    # --- Accounting fields (added in v2.1) ---
    planned_entry: Optional[float] = None    # zone.midpoint at signal creation
    actual_target: Optional[float] = None    # TP reanchored to actual_entry at trigger
    slippage: Optional[float] = None         # actual_entry – planned_entry (signed)
    risk_distance: Optional[float] = None    # abs(actual_entry – stop_loss)
    reward_distance: Optional[float] = None  # abs(exit_price – actual_entry)
    exit_reason: Optional[str] = None        # 'TP' | 'SL' | 'Expired' | 'Invalidated' | 'Manual' | 'Cancelled'

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "zone_top": self.zone_top,
            "zone_bottom": self.zone_bottom,
            "atr": self.atr,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "planned_rr": self.planned_rr,
            "confluence_score": self.confluence_score,
            "retest_mode": self.retest_mode.value,
            "created_ts": self.created_ts,
            "expiry_ts": self.expiry_ts,
            "state": self.state.value,
            "touched_ts": self.touched_ts,
            "entry_price": self.entry_price,
            "triggered_ts": self.triggered_ts,
            "closed_ts": self.closed_ts,
            "exit_price": self.exit_price,
            "realized_rr": self.realized_rr,
            "source": self.source,
            "pattern": self.pattern,
            "planned_entry": self.planned_entry,
            "actual_target": self.actual_target,
            "slippage": self.slippage,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
            "exit_reason": self.exit_reason,
        }

    @staticmethod
    def from_row(row: dict) -> "Trade":
        return Trade(
            id=row["id"],
            symbol=row["symbol"],
            direction=Direction(row["direction"]),
            zone_top=row["zone_top"],
            zone_bottom=row["zone_bottom"],
            atr=row["atr"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            planned_rr=row["planned_rr"],
            confluence_score=row["confluence_score"],
            retest_mode=RetestMode(row["retest_mode"]),
            created_ts=row["created_ts"],
            expiry_ts=row["expiry_ts"],
            state=TradeState(row["state"]),
            touched_ts=row.get("touched_ts"),
            entry_price=row.get("entry_price"),
            triggered_ts=row.get("triggered_ts"),
            closed_ts=row.get("closed_ts"),
            exit_price=row.get("exit_price"),
            realized_rr=row.get("realized_rr"),
            source=row.get("source", ZoneSource.SWING.value),
            pattern=row.get("pattern", ""),
            planned_entry=row.get("planned_entry"),
            actual_target=row.get("actual_target"),
            slippage=row.get("slippage"),
            risk_distance=row.get("risk_distance"),
            reward_distance=row.get("reward_distance"),
            exit_reason=row.get("exit_reason"),
        )
