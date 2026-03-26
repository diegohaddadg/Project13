"""Tests for live reconciliation layer.

All tests use mocks — no real API calls.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from models.order import Order
from models.position import Position
from execution.position_manager import PositionManager
from execution.live_reconciler import LiveReconciler, _safe_float
import config


def _make_live_order(**overrides) -> Order:
    """Create a realistic LIVE order for testing."""
    defaults = dict(
        order_id="test_live_001",
        signal_id="sig_001",
        market_id="mkt_abc",
        market_type="btc-5min",
        direction="UP",
        side="BUY",
        token_id="21742633143463906290569050155826241533067272736897614950488156847949938836455",
        price=0.55,
        size_usdc=27.50,
        num_shares=50.0,
        order_type="LIMIT",
        status="LIVE",
        execution_mode="live",
        metadata={
            "strategy": "latency_arb",
            "exchange_order_id": "0xexchange123",
            "condition_id": "0xcond456",
        },
    )
    defaults.update(overrides)
    return Order(**defaults)


class _MockOrderManager:
    """Minimal mock for OrderManager interface used by reconciler."""

    def __init__(self):
        self._order_history = []
        self._trade_log = []

    def get_order_history(self):
        return list(self._order_history)

    def _append_trade_log(self, order):
        self._trade_log.append(order.to_dict())

    def _log_lifecycle(self, order, position):
        pass

    def sync_order_pnl_from_position(self, order_id, pnl):
        for o in self._order_history:
            if o.order_id == order_id:
                o.pnl = pnl


class TestReconcileFillDetection(unittest.TestCase):
    """Test that LIVE orders transition to FILLED when exchange confirms."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_live_order_becomes_filled(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)

        # Exchange says: MATCHED (fully filled)
        client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "50.0",
            "original_size": "50.0",
            "price": "0.55",
        }

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        self.assertEqual(order.status, "FILLED")
        self.assertIsNotNone(order.fill_price)
        self.assertEqual(summary["fills_this_cycle"], 1)
        self.assertEqual(pm.count_open_positions(), 1)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_filled_order_creates_position(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order(direction="DOWN")
        om._order_history.append(order)

        client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "50.0",
            "original_size": "50.0",
            "price": "0.55",
        }

        recon = LiveReconciler(client, pm, om)
        recon.reconcile()

        positions = pm.get_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].direction, "DOWN")
        self.assertEqual(positions[0].entry_price, 0.55)
        self.assertEqual(positions[0].num_shares, 50.0)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_already_filled_not_double_counted(self):
        """A FILLED order should not be processed again."""
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order(status="FILLED")  # already filled
        om._order_history.append(order)

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        # Should not have checked any orders (none in LIVE status)
        self.assertEqual(summary["orders_checked"], 0)
        client.get_order.assert_not_called()


class TestReconcileCancelDetection(unittest.TestCase):
    """Test that cancelled exchange orders are reflected locally."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_cancelled_order_detected(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)

        client.get_order.return_value = {
            "status": "CANCELLED",
            "size_matched": "0",
            "original_size": "50.0",
        }

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        self.assertEqual(order.status, "CANCELLED")
        self.assertEqual(summary["cancels_this_cycle"], 1)
        self.assertEqual(pm.count_open_positions(), 0)


class TestPartialFill(unittest.TestCase):
    """Test partial fill tracking."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_partial_fill_logged_not_finalized(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)

        client.get_order.return_value = {
            "status": "LIVE",  # still on book
            "size_matched": "20.0",  # but partially filled
            "original_size": "50.0",
            "price": "0.55",
        }

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        # Order should still be LIVE (partial fills don't finalize)
        self.assertEqual(order.status, "LIVE")
        self.assertEqual(order.metadata.get("partial_fill_size"), 20.0)
        self.assertEqual(pm.count_open_positions(), 0)


class TestReconcileErrorHandling(unittest.TestCase):
    """Test that API errors don't crash the reconciler."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_api_error_graceful(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)

        client.get_order.side_effect = ConnectionError("timeout")

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        # Order should remain LIVE (not corrupted)
        self.assertEqual(order.status, "LIVE")
        self.assertEqual(summary["errors_this_cycle"], 1)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_none_response_ignored(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)

        client.get_order.return_value = None

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        self.assertEqual(order.status, "LIVE")
        self.assertEqual(summary["fills_this_cycle"], 0)


class TestRedemptionDetection(unittest.TestCase):
    """Test that resolved winning positions trigger redemption."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_winning_position_redeemed(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        # Create a filled order and open position
        order = _make_live_order(status="FILLED", fill_price=0.55)
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["execution_mode"] = "live"

        # Exchange says market resolved, this token won
        client.get_market.return_value = {
            "closed": True,
            "resolved": True,
            "tokens": [
                {"token_id": "tok_winner", "winner": 1.0},
                {"token_id": "tok_loser", "winner": 0.0},
            ],
        }
        # Redemption method exists and succeeds
        client.redeem = MagicMock(return_value={"success": True})

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        self.assertEqual(summary["redemptions_this_cycle"], 1)
        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertEqual(len(closed), 1)
        self.assertGreater(closed[0].pnl, 0)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_losing_position_closed(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order(status="FILLED", fill_price=0.55)
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_loser"
        pos.metadata["execution_mode"] = "live"

        client.get_market.return_value = {
            "closed": True,
            "resolved": True,
            "tokens": [
                {"token_id": "tok_winner", "winner": 1.0},
                {"token_id": "tok_loser", "winner": 0.0},
            ],
        }

        recon = LiveReconciler(client, pm, om)
        recon.reconcile()

        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertEqual(len(closed), 1)
        self.assertLess(closed[0].pnl, 0)


class TestRedemptionFailureRetry(unittest.TestCase):
    """Test that failed redemptions are retried with backoff."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    @patch.object(config, "LIVE_REDEEM_RETRY_BACKOFF_SECONDS", 0.01)
    def test_redemption_failure_schedules_retry(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order(status="FILLED", fill_price=0.55)
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["execution_mode"] = "live"

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }
        # No redeem method available, all attempts fail
        client.redeem = MagicMock(side_effect=Exception("not implemented"))
        if hasattr(client, 'merge_positions'):
            del client.merge_positions

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        # Position should still be open (redemption failed)
        self.assertEqual(pm.count_open_positions(), 1)
        # But retry metadata should be set
        self.assertGreater(pos.metadata.get("redeem_retry_count", 0), 0)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_already_redeemed_not_double_redeemed(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order(status="FILLED", fill_price=0.55)
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["execution_mode"] = "live"
        pos.metadata["redeemed"] = True  # already redeemed

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }

        recon = LiveReconciler(client, pm, om)
        summary = recon.reconcile()

        # Should NOT attempt redemption
        self.assertEqual(summary["redemptions_this_cycle"], 0)
        client.redeem.assert_not_called() if hasattr(client, 'redeem') else None


class TestReconcileSkipConditions(unittest.TestCase):
    """Test that reconciliation skips correctly when disabled."""

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_skip_in_paper_mode(self):
        recon = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        result = recon.reconcile()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["reason"], "not_live_mode")

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", False)
    def test_skip_when_disabled(self):
        recon = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        result = recon.reconcile()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["reason"], "reconciliation_disabled")

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    def test_skip_when_no_client(self):
        recon = LiveReconciler(None, PositionManager(), _MockOrderManager())
        result = recon.reconcile()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["reason"], "no_clob_client")


class TestReconcilerStatus(unittest.TestCase):
    """Test dashboard status reporting."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_status_dict_complete(self):
        recon = LiveReconciler(MagicMock(), PositionManager(), _MockOrderManager())
        status = recon.get_status()

        self.assertIn("enabled", status)
        self.assertIn("auto_redeem_enabled", status)
        self.assertIn("reconcile_count", status)
        self.assertIn("stale", status)
        self.assertIn("fills_detected", status)
        self.assertIn("cancels_detected", status)
        self.assertIn("redeemed_count", status)
        self.assertIn("redeem_failures", status)
        self.assertIn("errors", status)
        self.assertTrue(status["stale"])  # No reconciliation yet


class TestPaperModeUntouched(unittest.TestCase):
    """Verify paper mode is completely unaffected."""

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_paper_mode_no_reconciler(self):
        from execution.paper_trader import PaperTrader
        trader = PaperTrader()
        order = Order(
            market_type="btc-5min", direction="UP", side="BUY",
            price=0.50, size_usdc=25.0, num_shares=50.0,
            execution_mode="paper",
        )
        result = trader.execute(order)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.execution_mode, "paper")


class TestStartupSync(unittest.TestCase):
    """Test startup sync behavior."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", False)
    def test_startup_sync_reconciles_pending(self):
        pm = PositionManager()
        om = _MockOrderManager()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)

        client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "50.0",
            "original_size": "50.0",
            "price": "0.55",
        }

        recon = LiveReconciler(client, pm, om)
        recon.startup_sync()

        # Should have filled the order during startup
        self.assertEqual(order.status, "FILLED")
        self.assertEqual(pm.count_open_positions(), 1)


class TestSafeFloat(unittest.TestCase):
    def test_string_float(self):
        self.assertEqual(_safe_float("50.0"), 50.0)

    def test_none(self):
        self.assertEqual(_safe_float(None), 0.0)

    def test_invalid(self):
        self.assertEqual(_safe_float("abc"), 0.0)

    def test_int(self):
        self.assertEqual(_safe_float(50), 50.0)


if __name__ == "__main__":
    unittest.main()
