"""
tests/test_accounting.py
========================
Validation tests for trade accounting and R-multiple calculations (spec Step 10).

These tests verify:
  - compute_trade_performance() produces correct risk, reward, and RR
    for bullish TP / bullish SL / bearish TP / bearish SL
  - compute_actual_target() reanchors TP so that hitting it gives exactly
    the planned RR from the actual entry
  - The full trigger→close lifecycle produces the correct realized_rr
  - Slippage is recorded correctly
  - Duration formatting is correct
  - No other module duplicates the RR calculation
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import unittest.mock as mock

# Make sure the project root is on the path regardless of how tests are run
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_TOKEN",  "1234:fake")
os.environ.setdefault("SIGNALS_CHAT_ID", "-100TEST")

from models import Direction, RetestMode, Trade, TradeState, Zone, ZoneSource
from risk_manager import compute_actual_target, compute_trade_performance
from telegram_bot import _fmt_duration


# ---------------------------------------------------------------------------
# compute_trade_performance
# ---------------------------------------------------------------------------

class TestComputeTradePerformance(unittest.TestCase):

    # --- Spec Step 10 examples ---

    def test_bearish_tp(self):
        """Bearish: entry=100, SL=102, TP=95 → risk=2, reward=5, RR=+2.50"""
        p = compute_trade_performance(Direction.BEARISH, 100, 102, 95)
        self.assertAlmostEqual(p["risk_distance"],   2.0,  places=8)
        self.assertAlmostEqual(p["reward_distance"], 5.0,  places=8)
        self.assertAlmostEqual(p["realized_rr"],     2.5,  places=8)

    def test_bullish_tp(self):
        """Bullish: entry=100, SL=98, TP=105 → risk=2, reward=5, RR=+2.50"""
        p = compute_trade_performance(Direction.BULLISH, 100, 98, 105)
        self.assertAlmostEqual(p["risk_distance"],   2.0, places=8)
        self.assertAlmostEqual(p["reward_distance"], 5.0, places=8)
        self.assertAlmostEqual(p["realized_rr"],     2.5, places=8)

    def test_bullish_sl(self):
        """Bullish SL: entry=100, SL=98, exit=98 → RR=-1.00"""
        p = compute_trade_performance(Direction.BULLISH, 100, 98, 98)
        self.assertAlmostEqual(p["realized_rr"], -1.0, places=8)

    def test_bearish_sl(self):
        """Bearish SL: entry=100, SL=102, exit=102 → RR=-1.00"""
        p = compute_trade_performance(Direction.BEARISH, 100, 102, 102)
        self.assertAlmostEqual(p["realized_rr"], -1.0, places=8)

    # --- Direction safety ---

    def test_bullish_adverse_gives_negative_rr(self):
        """Any exit below entry on a bullish trade is negative R."""
        p = compute_trade_performance(Direction.BULLISH, 100, 95, 97)
        self.assertLess(p["realized_rr"], 0)

    def test_bearish_adverse_gives_negative_rr(self):
        """Any exit above entry on a bearish trade is negative R."""
        p = compute_trade_performance(Direction.BEARISH, 100, 105, 103)
        self.assertLess(p["realized_rr"], 0)

    # --- Absolute values ---

    def test_risk_distance_always_positive(self):
        for direction, entry, sl, exit_px in [
            (Direction.BULLISH, 100, 98, 105),
            (Direction.BEARISH, 100, 102, 95),
            (Direction.BULLISH, 100, 98, 98),
        ]:
            p = compute_trade_performance(direction, entry, sl, exit_px)
            self.assertGreaterEqual(p["risk_distance"], 0,
                                    f"risk_distance negative for {direction}")

    def test_reward_distance_always_positive(self):
        for direction, entry, sl, exit_px in [
            (Direction.BULLISH, 100, 98, 105),
            (Direction.BEARISH, 100, 102, 95),
            (Direction.BULLISH, 100, 98, 98),
        ]:
            p = compute_trade_performance(direction, entry, sl, exit_px)
            self.assertGreaterEqual(p["reward_distance"], 0,
                                    f"reward_distance negative for {direction}")

    # --- PnL % sanity ---

    def test_pnl_pct_positive_on_win(self):
        p_bull = compute_trade_performance(Direction.BULLISH, 100, 98, 105)
        p_bear = compute_trade_performance(Direction.BEARISH, 100, 102, 95)
        self.assertGreater(p_bull["pnl_pct"], 0)
        self.assertGreater(p_bear["pnl_pct"], 0)

    def test_pnl_pct_negative_on_loss(self):
        p = compute_trade_performance(Direction.BULLISH, 100, 98, 98)
        self.assertLess(p["pnl_pct"], 0)


# ---------------------------------------------------------------------------
# compute_actual_target — the fix that makes TP hit == planned_rr
# ---------------------------------------------------------------------------

class TestComputeActualTarget(unittest.TestCase):

    def test_bullish_actual_target_gives_planned_rr(self):
        """
        When actual_entry differs from planned_entry (zone.midpoint),
        actual_target adjusts TP so hitting it gives exactly planned_rr.
        """
        planned_rr   = 2.5
        stop_loss    = 49800.0
        actual_entry = 50250.0   # entered above zone.midpoint (50100)
        # Without the fix: take_profit = 50850 (based on zone.midpoint)
        # compute_rr(50250, 49800, 50850) = 600/450 = 1.33  ← WRONG

        actual_target = compute_actual_target(Direction.BULLISH, actual_entry, stop_loss, planned_rr)
        p = compute_trade_performance(Direction.BULLISH, actual_entry, stop_loss, actual_target)
        self.assertAlmostEqual(p["realized_rr"], planned_rr, places=6,
                               msg="TP hit must equal planned_rr")

    def test_bearish_actual_target_gives_planned_rr(self):
        planned_rr   = 2.5
        stop_loss    = 1592.0
        actual_entry = 1575.0   # entered below zone.bottom (zone was [1578, 1590])
        # Without the fix: take_profit = 1564 (based on zone.midpoint 1584)
        # risk = |1575-1592| = 17, reward = |1564-1575| = 11 → RR = 0.647  ← WRONG

        actual_target = compute_actual_target(Direction.BEARISH, actual_entry, stop_loss, planned_rr)
        self.assertIsNotNone(actual_target)
        self.assertLess(actual_target, actual_entry,  # TP must be below entry for bearish
                        "bearish actual_target must be below actual_entry")
        p = compute_trade_performance(Direction.BEARISH, actual_entry, stop_loss, actual_target)
        self.assertAlmostEqual(p["realized_rr"], planned_rr, places=6)

    def test_actual_target_fallback_on_zero_risk(self):
        """If entry == stop_loss (degenerate), actual_target returns None."""
        result = compute_actual_target(Direction.BULLISH, 100.0, 100.0, 2.5)
        self.assertIsNone(result)

    def test_reproduces_original_bug(self):
        """
        Explicitly reproduce the bug from the issue report:
        planned_rr=2.5 but realized_rr≈0.39 on TP hit.
        Confirm the fix resolves it.
        """
        from risk_manager import compute_rr

        # Realistic BEARISH confirmation-mode trade
        zone_top    = 1590.0
        zone_bottom = 1578.0
        midpoint    = (zone_top + zone_bottom) / 2.0   # 1584
        stop_loss   = zone_top + 0.3 * 10.0            # 1593  (ATR buffer)
        planned_tp  = midpoint - abs(midpoint - stop_loss) * 2.5  # based on midpoint

        planned_rr = compute_rr(Direction.BEARISH, midpoint, stop_loss, planned_tp)
        self.assertAlmostEqual(planned_rr, 2.5, places=1)

        # Actual entry: confirmation candle closes well below zone.bottom
        actual_entry = 1570.0  # far below zone

        # --- BUG: old code used planned_tp with actual_entry ---
        buggy_rr = compute_rr(Direction.BEARISH, actual_entry, stop_loss, planned_tp)
        self.assertLess(buggy_rr, 1.5,
                        "Bug not reproduced: expected low RR with old calculation")

        # --- FIX: compute_actual_target reanchors TP ---
        actual_target = compute_actual_target(Direction.BEARISH, actual_entry, stop_loss, planned_rr)
        perf = compute_trade_performance(Direction.BEARISH, actual_entry, stop_loss, actual_target)
        self.assertAlmostEqual(perf["realized_rr"], 2.5, places=6,
                               msg="Fix failed: TP hit should equal planned_rr")


# ---------------------------------------------------------------------------
# Full lifecycle: trigger → close produces correct fields
# ---------------------------------------------------------------------------

class TestTradeLifecycle(unittest.TestCase):

    def _make_trade(self, direction, entry_zone_top, entry_zone_bottom,
                    stop_loss, planned_rr) -> Trade:
        midpoint = (entry_zone_top + entry_zone_bottom) / 2.0
        from risk_manager import compute_rr
        planned_tp = (midpoint + abs(midpoint - stop_loss) * planned_rr
                      if direction is Direction.BULLISH
                      else midpoint - abs(midpoint - stop_loss) * planned_rr)
        return Trade(
            symbol="TESTUSDT",
            direction=direction,
            zone_top=entry_zone_top,
            zone_bottom=entry_zone_bottom,
            atr=50.0,
            stop_loss=stop_loss,
            take_profit=planned_tp,
            planned_rr=planned_rr,
            confluence_score=7,
            retest_mode=RetestMode.CONFIRMATION,
            created_ts=time.time(),
            expiry_ts=time.time() + 10800,
            planned_entry=midpoint,
        )

    def _trigger(self, trade, actual_entry):
        """Simulate what process_pending_trade does at trigger."""
        trade.entry_price  = actual_entry
        trade.triggered_ts = time.time()
        trade.touched_ts   = trade.triggered_ts
        from risk_manager import compute_actual_target
        trade.actual_target  = compute_actual_target(
            trade.direction, actual_entry, trade.stop_loss, trade.planned_rr
        )
        trade.risk_distance  = abs(actual_entry - trade.stop_loss)
        trade.slippage       = actual_entry - (trade.planned_entry or actual_entry)
        trade.realized_rr    = None  # not closed
        trade.state          = TradeState.RUNNING

    def _close_tp(self, trade):
        """Simulate what process_running_trade does on TP hit."""
        tp_price = trade.actual_target or trade.take_profit
        from risk_manager import compute_trade_performance
        perf = compute_trade_performance(
            trade.direction, trade.entry_price, trade.stop_loss, tp_price
        )
        trade.exit_price      = tp_price
        trade.closed_ts       = time.time()
        trade.realized_rr     = perf["realized_rr"]
        trade.reward_distance = perf["reward_distance"]
        trade.state           = TradeState.TP_HIT

    def _close_sl(self, trade):
        """Simulate what process_running_trade does on SL hit."""
        from risk_manager import compute_trade_performance
        perf = compute_trade_performance(
            trade.direction, trade.entry_price, trade.stop_loss, trade.stop_loss
        )
        trade.exit_price      = trade.stop_loss
        trade.closed_ts       = time.time()
        trade.realized_rr     = perf["realized_rr"]
        trade.reward_distance = perf["reward_distance"]
        trade.state           = TradeState.SL_HIT

    def test_bullish_tp_realized_rr_equals_planned(self):
        trade = self._make_trade(Direction.BULLISH, 50200, 50000, 49800, 2.5)
        actual_entry = 50210.0  # confirmation candle closed above zone.top
        self._trigger(trade, actual_entry)
        self.assertIsNone(trade.realized_rr, "realized_rr must be None before close")
        self._close_tp(trade)
        self.assertAlmostEqual(trade.realized_rr, 2.5, places=5)
        self.assertEqual(trade.state, TradeState.TP_HIT)

    def test_bullish_sl_always_minus_one(self):
        trade = self._make_trade(Direction.BULLISH, 50200, 50000, 49800, 2.5)
        self._trigger(trade, 50210.0)
        self._close_sl(trade)
        self.assertAlmostEqual(trade.realized_rr, -1.0, places=8)
        self.assertEqual(trade.state, TradeState.SL_HIT)

    def test_bearish_tp_realized_rr_equals_planned(self):
        trade = self._make_trade(Direction.BEARISH, 1590, 1578, 1593, 2.5)
        actual_entry = 1576.0  # confirmation candle closed below zone.bottom
        self._trigger(trade, actual_entry)
        self.assertIsNone(trade.realized_rr)
        self._close_tp(trade)
        self.assertAlmostEqual(trade.realized_rr, 2.5, places=5)

    def test_bearish_sl_always_minus_one(self):
        trade = self._make_trade(Direction.BEARISH, 1590, 1578, 1593, 2.5)
        self._trigger(trade, 1576.0)
        self._close_sl(trade)
        self.assertAlmostEqual(trade.realized_rr, -1.0, places=8)

    def test_slippage_recorded(self):
        trade = self._make_trade(Direction.BULLISH, 50200, 50000, 49800, 2.5)
        planned = trade.planned_entry  # 50100
        actual  = 50250.0
        self._trigger(trade, actual)
        expected_slippage = actual - planned
        self.assertAlmostEqual(trade.slippage, expected_slippage, places=6)

    def test_risk_distance_recorded(self):
        trade = self._make_trade(Direction.BULLISH, 50200, 50000, 49800, 2.5)
        actual_entry = 50250.0
        self._trigger(trade, actual_entry)
        expected_risk = abs(actual_entry - trade.stop_loss)
        self.assertAlmostEqual(trade.risk_distance, expected_risk, places=6)

    def test_reward_distance_matches_exit(self):
        trade = self._make_trade(Direction.BULLISH, 50200, 50000, 49800, 2.5)
        actual_entry = 50250.0
        self._trigger(trade, actual_entry)
        self._close_tp(trade)
        expected_reward = abs(trade.exit_price - actual_entry)
        self.assertAlmostEqual(trade.reward_distance, expected_reward, places=6)


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

class TestFmtDuration(unittest.TestCase):

    def test_2h_11m(self):
        t0 = 1_700_000_000.0
        t1 = t0 + 2 * 3600 + 11 * 60
        self.assertEqual(_fmt_duration(t0, t1), "2h 11m")

    def test_41m(self):
        t0 = 1_700_000_000.0
        t1 = t0 + 41 * 60
        self.assertEqual(_fmt_duration(t0, t1), "41m")

    def test_none_inputs(self):
        self.assertEqual(_fmt_duration(None, None), "—")
        self.assertEqual(_fmt_duration(None, 1_700_000_000.0), "—")


# ---------------------------------------------------------------------------
# Single calculation site audit
# ---------------------------------------------------------------------------

class TestSingleCalculationSite(unittest.TestCase):
    """
    Verify that compute_rr (the old planner function) is only called from
    risk_manager.py and scanner.py (planning), and that compute_trade_performance
    (the new accounting function) is only called from trade_manager.py.
    """

    def _grep(self, filename, pattern):
        """Find lines containing pattern, excluding comments and docstring content."""
        path = os.path.join(os.path.dirname(__file__), "..", filename)
        results = []
        in_docstring = False
        with open(path) as f:
            for i, line in enumerate(f, 1):
                stripped = line.lstrip()
                # Track triple-quoted blocks (both ''' and """)
                for q in ('"""', "'''"):
                    if q in line:
                        count = line.count(q)
                        if count % 2 == 1:           # odd count flips state
                            in_docstring = not in_docstring
                if in_docstring:
                    continue
                if stripped.startswith("#"):
                    continue
                if pattern in line:
                    results.append(i)
        return results

    def test_compute_rr_not_called_in_trade_manager(self):
        lines = self._grep("trade_manager.py", "compute_rr(")
        self.assertEqual(lines, [],
                         f"compute_rr() still called in trade_manager.py at lines {lines}. "
                         "Use compute_trade_performance() for closed-trade accounting.")

    def test_compute_trade_performance_not_duplicated_in_scanner(self):
        """Scanner only plans trades; it must not compute realized performance."""
        lines = self._grep("scanner.py", "compute_trade_performance(")
        self.assertEqual(lines, [],
                         f"compute_trade_performance() called in scanner.py at lines {lines}")

    def test_compute_trade_performance_not_in_telegram(self):
        """Telegram builds messages; it must not recalculate RR."""
        lines = self._grep("telegram_bot.py", "compute_trade_performance(")
        self.assertEqual(lines, [],
                         f"compute_trade_performance() called in telegram_bot.py at lines {lines}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
