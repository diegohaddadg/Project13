"""Tests for live entry cap / anti-overstacking (softened v2).

Cap is LIVE_MAX_ENTRIES_PER_WINDOW (default 2).
Same-direction adds are ALLOWED within the cap.
Duplicate direction block is REMOVED.
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
    reconciler._stale = False
    reconciler._last_reconcile_ts = time.time()

    trader.set_reconciler(reconciler)
    return trader, reconciler, om, pm


class TestMaxEntriesPerWindow(unittest.TestCase):
    """Test that LIVE_MAX_ENTRIES_PER_WINDOW (2) is enforced."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "LIVE_MAX_ENTRIES_PER_WINDOW", 2)
    def test_blocks_at_cap_with_live_orders(self):
        """2 LIVE orders for same market → 3rd entry blocked."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        for i in range(2):
            om._order_history.append(_make_order(
                order_id=f"existing_{i}",
                status="LIVE",
                direction=["UP", "DOWN"][i],
                metadata={"exchange_order_id": f"0xex{i}"},
            ))

        order = _make_order(direction="UP")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: market entry cap reached", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "LIVE_MAX_ENTRIES_PER_WINDOW", 2)
    def test_blocks_at_cap_with_mixed_pending_and_filled(self):
        """1 LIVE order + 1 open position = 2 → 3rd blocked."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="live_1", status="LIVE", direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        filled = _make_order(order_id="filled_1", status="FILLED", fill_price=0.55, direction="DOWN")
        pos = pm.open_position(filled)
        pos.metadata["execution_mode"] = "live"

        order = _make_order(direction="UP")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: market entry cap reached", result.metadata.get("rejection_reason", ""))


class TestSameDirectionAllowed(unittest.TestCase):
    """Same-direction adds are allowed within the cap."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "LIVE_MAX_ENTRIES_PER_WINDOW", 2)
    def test_same_direction_allowed_within_cap(self):
        """UP already open, another UP allowed (1 < cap of 2)."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="existing_up", status="LIVE", direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(direction="UP")
        result = trader.execute(order)

        # Should NOT be blocked — same direction is allowed within cap
        reason = result.metadata.get("rejection_reason", "")
        self.assertNotIn("BLOCKED: market entry cap", reason)
        self.assertNotIn("BLOCKED: duplicate direction", reason)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "LIVE_MAX_ENTRIES_PER_WINDOW", 2)
    def test_opposite_direction_allowed_within_cap(self):
        """UP already open, DOWN allowed (1 < cap of 2)."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="existing_up", status="LIVE", direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        reason = result.metadata.get("rejection_reason", "")
        self.assertNotIn("BLOCKED:", reason)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "LIVE_MAX_ENTRIES_PER_WINDOW", 2)
    def test_different_market_not_counted(self):
        """Entries in different market don't count toward this market's cap."""
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        om._order_history.append(_make_order(
            order_id="other_mkt", market_id="mkt_OTHER",
            status="LIVE", direction="UP",
            metadata={"exchange_order_id": "0xex1"},
        ))

        order = _make_order(market_id="mkt_window_1", direction="UP")
        result = trader.execute(order)

        reason = result.metadata.get("rejection_reason", "")
        self.assertNotIn("BLOCKED: market entry cap", reason)


class TestStaleReconciliationBlock(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_stale_blocks(self):
        trader = LiveTrader()
        trader._clob_client = MagicMock()
        reconciler = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        # _stale = True by default
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
        trader = LiveTrader()
        trader._clob_client = MagicMock()

        order = _make_order()
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: no live reconciler", result.metadata.get("rejection_reason", ""))


class TestRestartWithExposure(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    @patch.object(config, "LIVE_MAX_ENTRIES_PER_WINDOW", 2)
    def test_restored_positions_count(self):
        trader, recon, om, pm = _make_ready_trader_with_reconciler()

        for i in range(2):
            filled = _make_order(order_id=f"restored_{i}", status="FILLED",
                                 fill_price=0.55, direction="UP")
            pos = pm.open_position(filled)
            pos.metadata["execution_mode"] = "live"

        order = _make_order(direction="DOWN")
        result = trader.execute(order)

        self.assertEqual(result.status, "REJECTED")
        self.assertIn("BLOCKED: market entry cap reached", result.metadata.get("rejection_reason", ""))


class TestPaperModeUnchanged(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_paper_no_cap(self):
        from execution.paper_trader import PaperTrader
        trader = PaperTrader()
        order = _make_order(execution_mode="paper")
        result = trader.execute(order)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.execution_mode, "paper")


class TestExposureQuery(unittest.TestCase):

    def test_empty(self):
        recon = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        exp = recon.get_live_market_exposure("mkt_1")
        self.assertEqual(exp["total_entries"], 0)

    def test_counts_live_orders(self):
        om = _MockOrderManager()
        om._order_history.append(_make_order(status="LIVE", direction="UP"))
        om._order_history.append(_make_order(order_id="o2", status="LIVE", direction="UP"))

        recon = LiveReconciler(MagicMock(), PositionManager(), om)
        exp = recon.get_live_market_exposure("mkt_window_1")

        self.assertEqual(exp["total_entries"], 2)
        self.assertEqual(exp["up_count"], 2)
        self.assertEqual(exp["down_count"], 0)


if __name__ == "__main__":
    unittest.main()
