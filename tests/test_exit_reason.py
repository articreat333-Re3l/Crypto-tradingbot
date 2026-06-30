"""
tests/test_exit_reason.py
==========================
Validates that every trade terminal state records a human-readable
exit_reason, and that the journal correctly distinguishes decided
outcomes (TP/SL) from non-outcomes (Expired/Invalidated/Cancelled/Manual)
when classifying Win/Loss.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_TOKEN",  "1234:fake")
os.environ.setdefault("SIGNALS_CHAT_ID", "-100TEST")

import pandas as pd

from models import Direction, RetestMode, Trade, TradeState, ZoneSource
import trade_manager as tm


def _make_pending_trade(direction=Direction.BULLISH, expiry_in=10800):
    now = time.time()
    return Trade(
        symbol="TESTUSDT",
        direction=direction,
        zone_top=50200.0,
        zone_bottom=50000.0,
        atr=100.0,
        stop_loss=49800.0,
        take_profit=50800.0,
        planned_rr=2.5,
        confluence_score=7,
        retest_mode=RetestMode.CONFIRMATION,
        created_ts=now,
        expiry_ts=now + expiry_in,
        planned_entry=50100.0,
    )


def _make_running_trade(direction=Direction.BULLISH):
    now = time.time()
    t = Trade(
        symbol="TESTUSDT",
        direction=direction,
        zone_top=50200.0,
        zone_bottom=50000.0,
        atr=100.0,
        stop_loss=49800.0,
        take_profit=50800.0,
        planned_rr=2.5,
        confluence_score=7,
        retest_mode=RetestMode.CONFIRMATION,
        created_ts=now - 600,
        expiry_ts=now + 10800,
        state=TradeState.RUNNING,
        entry_price=50250.0,
        actual_target=51000.0,
        triggered_ts=now - 300,
    )
    return t


def _df(rows):
    """rows: list of dicts with time, open, high, low, close"""
    return pd.DataFrame(rows)


class TestExitReasonExpired(unittest.TestCase):

    def test_expired_sets_exit_reason(self):
        trade = _make_pending_trade(expiry_in=-10)  # already expired
        with mock.patch.object(tm.persistence, "save_trade"):
            outcome = tm.process_pending_trade(trade, df_execution=None, df_confirmation=None)
        self.assertEqual(outcome, "expired")
        self.assertEqual(trade.state, TradeState.EXPIRED)
        self.assertEqual(trade.exit_reason, "Expired")
        self.assertIsNotNone(trade.closed_ts)


class TestExitReasonInvalidatedEarly(unittest.TestCase):

    def test_early_invalidation_sets_exit_reason(self):
        """Structure broke before retest -- execution-tf close beyond stop."""
        trade = _make_pending_trade(direction=Direction.BULLISH, expiry_in=10800)
        # close below stop_loss (49800) invalidates a bullish pending trade
        df_exec = _df([
            {"time": 1, "open": 49900, "high": 49950, "low": 49700, "close": 49750, "confirm": "1"},
        ])
        with mock.patch.object(tm.persistence, "save_trade"):
            outcome = tm.process_pending_trade(trade, df_execution=df_exec, df_confirmation=None)
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(trade.state, TradeState.INVALIDATED)
        self.assertEqual(trade.exit_reason, "Invalidated")


class TestExitReasonInvalidatedRejection(unittest.TestCase):

    def test_rejection_failure_sets_exit_reason(self):
        """Confirmation-mode: price touches zone then closes through the far side."""
        trade = _make_pending_trade(direction=Direction.BULLISH, expiry_in=10800)
        df_conf = _df([
            {"time": 100, "open": 50100, "high": 50150, "low": 50050, "close": 50100, "confirm": "1"},  # touch
            {"time": 200, "open": 50050, "high": 50080, "low": 49950, "close": 49950, "confirm": "1"},  # closes below zone.bottom -> invalidated
        ])
        with mock.patch.object(tm.persistence, "save_trade"):
            outcome = tm.process_pending_trade(trade, df_execution=None, df_confirmation=df_conf)
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(trade.exit_reason, "Invalidated")


class TestExitReasonTPandSL(unittest.TestCase):

    def test_tp_hit_sets_exit_reason(self):
        trade = _make_running_trade(Direction.BULLISH)
        df_conf = _df([
            {"time": int((trade.triggered_ts + 60) * 1000), "open": 50300, "high": 51100, "low": 50200, "close": 51050},
        ])
        with mock.patch.object(tm.persistence, "save_trade"):
            outcome = tm.process_running_trade(trade, df_conf)
        self.assertEqual(outcome, "tp_hit")
        self.assertEqual(trade.exit_reason, "TP")
        self.assertEqual(trade.state, TradeState.TP_HIT)

    def test_sl_hit_sets_exit_reason(self):
        trade = _make_running_trade(Direction.BULLISH)
        df_conf = _df([
            {"time": int((trade.triggered_ts + 60) * 1000), "open": 50000, "high": 50100, "low": 49700, "close": 49750},
        ])
        with mock.patch.object(tm.persistence, "save_trade"):
            outcome = tm.process_running_trade(trade, df_conf)
        self.assertEqual(outcome, "sl_hit")
        self.assertEqual(trade.exit_reason, "SL")
        self.assertEqual(trade.state, TradeState.SL_HIT)


class TestJournalResultClassification(unittest.TestCase):
    """
    Verify journal.record_trade_exit no longer mislabels Expired/Invalidated
    trades as 'Loss'. Only TP -> Win, SL -> Loss; everything else -> None
    (excluded from win-rate stats by the existing `result IS NOT NULL` filters
    in dashboard.py / analyze.py / optimizer.py).
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        import journal
        self._tmp = tempfile.mkdtemp()
        journal._BASE      = Path(self._tmp) / "data"
        journal.DB_PATH     = journal._BASE / "journal.db"
        journal.BACKUP_DIR  = journal._BASE / "backups"
        journal.EXPORT_DIR  = Path(self._tmp) / "exports"
        journal.initialize_database()
        self.journal = journal

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_trade(self, exit_reason, entry=None, exit_price=None, realized_rr=None):
        now = time.time()
        t = Trade(
            id="abc123def456",
            symbol="TESTUSDT",
            direction=Direction.BULLISH,
            zone_top=50200.0, zone_bottom=50000.0, atr=100.0,
            stop_loss=49800.0, take_profit=50800.0, planned_rr=2.5,
            confluence_score=7, retest_mode=RetestMode.CONFIRMATION,
            created_ts=now - 3600, expiry_ts=now + 7200,
            entry_price=entry, exit_price=exit_price,
            triggered_ts=(now - 1800) if entry else None,
            closed_ts=now, realized_rr=realized_rr,
            exit_reason=exit_reason,
        )
        # Satisfy the FK constraint: trades.signal_id references signals.id
        self.journal.record_signal(
            t, volume_ratio=1.5, execution_trend="up",
            nearest_support=None, nearest_resistance=None,
            order_block_present=False, fair_value_gap_present=False,
            liquidity_sweep_present=False,
        )
        return t

    def test_tp_classified_as_win(self):
        t = self._make_trade("TP", entry=50250.0, exit_price=51000.0, realized_rr=2.5)
        self.journal.record_trade_exit(t, "TP")
        row = self.journal.query_one("SELECT result FROM trades WHERE id=?", (t.id,))
        self.assertEqual(row["result"], "Win")

    def test_sl_classified_as_loss(self):
        t = self._make_trade("SL", entry=50250.0, exit_price=49800.0, realized_rr=-1.0)
        self.journal.record_trade_exit(t, "SL")
        row = self.journal.query_one("SELECT result FROM trades WHERE id=?", (t.id,))
        self.assertEqual(row["result"], "Loss")

    def test_expired_not_classified_as_loss(self):
        t = self._make_trade("Expired")
        self.journal.record_trade_exit(t, "Expired")
        row = self.journal.query_one("SELECT result FROM trades WHERE id=?", (t.id,))
        self.assertIsNone(row["result"], "Expired must not be classified as Win or Loss")

    def test_invalidated_not_classified_as_loss(self):
        t = self._make_trade("Invalidated")
        self.journal.record_trade_exit(t, "Invalidated")
        row = self.journal.query_one("SELECT result FROM trades WHERE id=?", (t.id,))
        self.assertIsNone(row["result"], "Invalidated must not be classified as Win or Loss")

    def test_expired_excluded_from_decided_query(self):
        """The dashboard/analyze pattern: WHERE result IS NOT NULL must exclude this row."""
        t = self._make_trade("Expired")
        self.journal.record_trade_exit(t, "Expired")
        decided = self.journal.query(
            "SELECT * FROM trades WHERE signal_id=? AND result IS NOT NULL", (t.id,)
        )
        self.assertEqual(len(decided), 0)

    def test_exit_reason_column_persisted(self):
        t = self._make_trade("Invalidated")
        self.journal.record_trade_exit(t, "Invalidated")
        row = self.journal.query_one("SELECT exit_reason FROM trades WHERE id=?", (t.id,))
        self.assertEqual(row["exit_reason"], "Invalidated")


class TestPersistenceSchema(unittest.TestCase):

    def test_exit_reason_column_exists_in_trades_table(self):
        import persistence
        cols = [c.name for c in persistence.trades_table.columns]
        self.assertIn("exit_reason", cols)

    def test_exit_reason_roundtrips_through_to_row_from_row(self):
        t = _make_running_trade(Direction.BULLISH)
        t.exit_reason = "TP"
        row = t.to_row()
        self.assertEqual(row["exit_reason"], "TP")
        rebuilt = Trade.from_row(row)
        self.assertEqual(rebuilt.exit_reason, "TP")


if __name__ == "__main__":
    unittest.main(verbosity=2)
