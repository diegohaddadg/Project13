"""Tests for hard live entry cap / anti-overstacking.

All tests use mocks — no real API calls.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from models.order import Order
from models.position import Position
from execution.position_manager import PositionManager
from execution.live_trader import LiveTrader
from execution.live_reconciler import LiveReconciler
import config


_REAL_TOKEN = "21742633143463906290569050155826241533067272736897614950488156847949938836455"


def _make_order(**overrides) -> Order:
    defaults = dict(
        order_id="cap_test_001",
        signal_id="sig_001",
        market_id="mkt_window_1",
        market_type="btc-5min",
        direction="UP",
        side="BUY",
        token_id=_REAL_TOKEN,
        price=0.55,
        size_usdc=27.50,
        num_shares=50.0,
        order_type="LIMIT",
        status="PENDING",
        execution_mode="live",
        metadata={"strategy": "latency_arb", "condition_id": "0xcond"},
    )
    defaults.update(overrides)
    return Order(**defaults)


class _MockOrderManager:
    def __init__(self):
        self._order_history = []

    def get_order_history(self):
        return list(self._order_history)

    def _append_trade_log(self, order):
        pass

    def _log_lifecycle(self, order, pos):
        pass

    def sync_order_pnl_from_position(self, order_id, pnl):
        pass


def _make_ready_trader_with_reconciler(om=None, pm=None):
    """Create a LiveTrader with mocked CLOB client and a real reconciler."""
    if pm is None:
        pm = PositionManager()
    if om is None:
        om = _MockOrderManager()

    trader = LiveTrader()
    trader._clob_client = MagicMock()

    reconciler = LiveReconciler(
        clob_client=MagicMock(),
        position_manager=pm,
        order_manager=om,
    )
    # Mark reconciler as fresh (not stale)
    reconciler._stale = False
    reconciler._last_reconcile_ts = time.time()

    trader.set_reconciler(reconciler)
    return trader, reconciler, om, pm


class TestMaxEntriesPerWindow(unittest.TestCase):
    """Test that max 3 entries per market window is enforced."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_blocks_at_cap_with_live_orders(self):
        """3 LIVE orders for same market → 4th entry blocked."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        # 3 existing LIVE orders for this market
        for i in range(3):
            om._order_history.append(_make_order(
                order_id=f"existing_{i}",
                status="LIVE",
                direction=["UP", "DOWN", "UP"][i],
                metadata={"exchange_order_id": f"0xex{i}"},
            ))

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: market entry cap reached", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_blocks_at_cap_with_mixed_pending_and_filled(self):
        """2 LIVE orders + 1 open position = 3 → 4th blocked."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        # 2 LIVE orders
        for i in range(2):
            om._order_history.append(_make_order(
                order_id=f"live_{i}",
                status="LIVE",
                direction=["UP", "DOWN"][i],
                metadata={"exchange_order_id": f"0xex{i}"},
            ))

        # 1 filled position
        filled_order = _make_order(order_id="filled_1", status="FILLED", fill_price=0.55, direction="UP")
        pos = pm.open_position(filled_order)
        pos.metadata["execution_mode"] = "live"

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: market entry cap reached", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_allows_entry_below_cap(self):
        """1 existing entry → 2nd entry allowed (below cap of 3)."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="existing_1",
            status="LIVE",
            direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        # Should NOT be blocked by cap (1 < 3)
        # But will be blocked by duplicate direction check since UP is already there
        # and we're sending DOWN which is different, so should pass direction check
        # May fail at CLOB submit (mocked), but should not fail at cap check
        reason = result.metadata.get("rejection_reason", "")
        self.assertNotIn("BLOCKED: market entry cap reached", reason)


class TestDuplicateDirectionBlock(unittest.TestCase):
    """Test that duplicate direction in same market is blocked."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_blocks_same_direction_live_order(self):
        """UP already pending → another UP blocked."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="existing_up",
            status="LIVE",
            direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(direction="UP")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: duplicate direction UP", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_blocks_same_direction_filled_position(self):
        """UP position already open → another UP blocked."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        filled = _make_order(order_id="filled_up", status="FILLED", fill_price=0.55, direction="UP")
        pos = pm.open_position(filled)
        pos.metadata["execution_mode"] = "live"

        order = _make_order(direction="UP")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: duplicate direction UP", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_allows_opposite_direction(self):
        """UP already open → DOWN allowed (different direction)."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="existing_up",
            status="LIVE",
            direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        # Should NOT be blocked by direction check
        reason = result.metadata.get("rejection_reason", "")
        self.assertNotIn("BLOCKED: duplicate direction", reason)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_different_market_not_blocked(self):
        """UP in market_A → UP in market_B is fine."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="existing_up",
            market_id="mkt_OTHER",  # different market
            status="LIVE",
            direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(market_id="mkt_window_1", direction="UP")
        result = trader.execute(order)

        reason = result.metadata.get("rejection_reason", "")
        self.assertNotIn("BLOCKED: duplicate direction", reason)
        self.assertNotIn("BLOCKED: market entry cap", reason)


class TestStaleReconciliationBlock(unittest.TestCase):
    """Test that stale reconciliation blocks new live entries."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_stale_reconciliation_blocks(self):
        """Reconciler has never synced → block."""
        trader = LiveTrader()
        trader._clob_client = MagicMock()

        reconciler = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        # _stale = True by default (never synced)
        trader.set_reconciler(reconciler)

        order = _make_order()
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: reconciliation stale", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_no_reconciler_blocks(self):
        """No reconciler attached → block."""
        trader = LiveTrader()
        trader._clob_client = MagicMock()
        # No set_reconciler() call

        order = _make_order()
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: no live reconciler", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_old_reconciliation_blocks(self):
        """Reconciler last synced >30s ago → block."""
        trader = LiveTrader()
        trader._clob_client = MagicMock()

        reconciler = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        reconciler._stale = False
        reconciler._last_reconcile_ts = time.time() - 60  # 60s ago
        trader.set_reconciler(reconciler)

        order = _make_order()
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: reconciliation data is", result.metadata.get("rejection_reason", ""))


class TestRestartWithExistingExposure(unittest.TestCase):
    """Test that existing live exposure from before restart is respected."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "MAX_ENTRIES_PER_WINDOW", 3)
    def test_restored_positions_count_toward_cap(self):
        """Positions restored from trade log count toward the cap."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        # Simulate 3 restored positions (as if loaded from trade log)
        for i in range(3):
            filled = _make_order(
                order_id=f"restored_{i}",
                status="FILLED", fill_price=0.55,
                direction=["UP", "DOWN", "UP"][i],
            )
            pos = pm.open_position(filled)
            pos.metadata["execution_mode"] = "live"

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: market entry cap reached", result.metadata.get("rejection_reason", ""))


class TestPaperModeUnchanged(unittest.TestCase):
    """Verify paper mode is completely unaffected by live entry caps."""

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_paper_mode_no_cap_check(self):
        from execution.paper_trader import PaperTrader
        trader = PaperTrader()
        order = _make_order(execution_mode="paper")
        result = trader.execute(order)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.execution_mode, "paper")


class TestExposureQuery(unittest.TestCase):
    """Test the get_live_market_exposure method directly."""

    def test_empty_exposure(self):
        recon = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        exp = recon.get_live_market_exposure("mkt_1")
        self.assertEqual(exp["total_entries"], 0)
        self.assertEqual(exp["pending_orders"], 0)
        self.assertEqual(exp["open_positions"], 0)

    def test_counts_live_orders(self):
        om = _MockOrderManager()
        om._order_history.append(_make_order(status="LIVE", direction="UP"))
        om._order_history.append(_make_order(order_id="o2", status="LIVE", direction="DOWN"))

        recon = LiveReconciler(MagicMock(), PositionManager(), om)
        exp = recon.get_live_market_exposure("mkt_window_1")

        self.assertEqual(exp["total_entries"], 2)
        self.assertEqual(exp["pending_orders"], 2)
        self.assertEqual(exp["up_count"], 1)
        self.assertEqual(exp["down_count"], 1)

    def test_counts_filled_positions(self):
        om = _MockOrderManager()
        pm = PositionManager()

        filled = _make_order(status="FILLED", fill_price=0.55)
        pos = pm.open_position(filled)
        pos.metadata["execution_mode"] = "live"

        recon = LiveReconciler(MagicMock(), pm, om)
        exp = recon.get_live_market_exposure("mkt_window_1")

        self.assertEqual(exp["total_entries"], 1)
        self.assertEqual(exp["open_positions"], 1)
        self.assertEqual(exp["pending_orders"], 0)

    def test_ignores_different_market(self):
        om = _MockOrderManager()
        om._order_history.append(_make_order(market_id="OTHER_MKT", status="LIVE"))

        recon = LiveReconciler(MagicMock(), PositionManager(), om)
        exp = recon.get_live_market_exposure("mkt_window_1")

        self.assertEqual(exp["total_entries"], 0)

    def test_ignores_non_live_orders(self):
        om = _MockOrderManager()
        om._order_history.append(_make_order(status="FILLED"))  # not LIVE
        om._order_history.append(_make_order(order_id="o2", status="CANCELLED"))

        recon = LiveReconciler(MagicMock(), PositionManager(), om)
        exp = recon.get_live_market_exposure("mkt_window_1")

        self.assertEqual(exp["pending_orders"], 0)


if __name__ == "__main__":
    unittest.main()
