"""Tests for external claim detection (manual Polymarket UI claim sync)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from models.order import Order
from models.position import Position
from execution.position_manager import PositionManager
from execution.live_reconciler import LiveReconciler
import config


def _make_live_order(**overrides) -> Order:
    defaults = dict(
        order_id="claim_test_001", signal_id="sig_001",
        market_id="mkt_abc", market_type="btc-5min", direction="UP",
        side="BUY", token_id="tok_winner",
        price=0.55, size_usdc=27.50, num_shares=50.0,
        order_type="LIMIT", status="FILLED", fill_price=0.55,
        execution_mode="live",
        metadata={"strategy": "latency_arb", "exchange_order_id": "0xex1",
                  "condition_id": "0xcond456"},
    )
    defaults.update(overrides)
    return Order(**defaults)


class _MockOM:
    def __init__(self):
        self._order_history = []
    def get_order_history(self):
        return list(self._order_history)
    def _append_trade_log(self, order):
        pass
    def _log_lifecycle(self, order, pos):
        pass
    def sync_order_pnl_from_position(self, order_id, pnl):
        for o in self._order_history:
            if o.order_id == order_id:
                o.pnl = pnl


class TestExternalClaimDetection(unittest.TestCase):
    """When a CLAIMABLE winner is claimed manually, local state must sync."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    @patch.object(config, "LIVE_REDEEM_MAX_RETRIES", 2)
    def test_claimable_winner_closed_after_max_retries(self):
        """CLAIMABLE position past max retries → detected as externally claimed → closed."""
        pm = PositionManager()
        om = _MockOM()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.status = "CLAIMABLE"
        pos.metadata["execution_mode"] = "live"
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["redeem_retry_count"] = 2  # >= max retries
        pos.metadata["claimable_since"] = time.time() - 60

        initial_capital = pm.get_available_capital()

        # Market resolved, this token won
        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [
                {"token_id": "tok_winner", "winner": 1.0},
                {"token_id": "tok_loser", "winner": 0.0},
            ],
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer_init_attempted = True  # skip web3 init
        summary = recon.reconcile()

        # Position should be closed
        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0].metadata.get("external_claim"))
        self.assertTrue(closed[0].metadata.get("redeemed"))
        self.assertGreater(closed[0].pnl, 0)

        # Capital should be restored (payout = 1.0 * 50 shares = 50)
        self.assertGreater(pm.get_available_capital(), initial_capital)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    @patch.object(config, "LIVE_REDEEM_MAX_RETRIES", 2)
    def test_capital_restored_correctly_after_external_claim(self):
        """Verify the exact capital math after external claim."""
        pm = PositionManager()
        om = _MockOM()
        client = MagicMock()

        # Starting capital = 100, order costs 10 USDC at price 0.50
        order = _make_live_order(size_usdc=10.0, num_shares=20.0, price=0.50, fill_price=0.50)
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.status = "CLAIMABLE"
        pos.metadata["execution_mode"] = "live"
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["redeem_retry_count"] = 2
        pos.metadata["claimable_since"] = time.time() - 30

        # After opening: capital = 100 - 10 = 90
        self.assertAlmostEqual(pm.get_available_capital(), 90.0, places=2)

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer_init_attempted = True
        recon.reconcile()

        # Payout = 1.0 * 20 shares = 20. Capital = 90 + 20 = 110
        self.assertAlmostEqual(pm.get_available_capital(), 110.0, places=2)
        # PnL = (1.0 - 0.50) * 20 = 10
        closed = pm.get_closed_positions()
        self.assertAlmostEqual(closed[0].pnl, 10.0, places=2)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    @patch.object(config, "LIVE_REDEEM_MAX_RETRIES", 5)
    def test_claimable_below_max_retries_not_closed(self):
        """CLAIMABLE position below max retries should NOT be auto-closed."""
        pm = PositionManager()
        om = _MockOM()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.status = "CLAIMABLE"
        pos.metadata["execution_mode"] = "live"
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["redeem_retry_count"] = 2  # below max of 5

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer_init_attempted = True
        recon._redeem_cooldowns[pos.position_id] = time.time() + 3600  # far future
        recon.reconcile()

        # Should still be open (retries not exhausted, in cooldown)
        self.assertEqual(pm.count_open_positions(), 1)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    @patch.object(config, "LIVE_REDEEM_MAX_RETRIES", 2)
    def test_no_double_credit_on_repeated_reconciliation(self):
        """Running reconcile twice after external claim must not double-credit."""
        pm = PositionManager()
        om = _MockOM()
        client = MagicMock()

        order = _make_live_order(size_usdc=10.0, num_shares=20.0, price=0.50, fill_price=0.50)
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.status = "CLAIMABLE"
        pos.metadata["execution_mode"] = "live"
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["redeem_retry_count"] = 2
        pos.metadata["claimable_since"] = time.time() - 30

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer_init_attempted = True

        # First reconcile: closes the position
        recon.reconcile()
        capital_after_first = pm.get_available_capital()

        # Second reconcile: position already closed, should be a no-op
        recon.reconcile()
        capital_after_second = pm.get_available_capital()

        self.assertEqual(capital_after_first, capital_after_second)
        self.assertEqual(pm.count_open_positions(), 0)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    @patch.object(config, "LIVE_REDEEM_MAX_RETRIES", 2)
    def test_losing_positions_still_correct(self):
        """Losing resolved positions close at 0.0, no external claim logic."""
        pm = PositionManager()
        om = _MockOM()
        client = MagicMock()

        order = _make_live_order(direction="DOWN", token_id="tok_loser")
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.metadata["execution_mode"] = "live"
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_loser"

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [
                {"token_id": "tok_winner", "winner": 1.0},
                {"token_id": "tok_loser", "winner": 0.0},
            ],
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer_init_attempted = True
        recon.reconcile()

        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertEqual(len(closed), 1)
        self.assertLess(closed[0].pnl, 0)
        self.assertFalse(closed[0].metadata.get("external_claim", False))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(config, "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_auto_redeem_success_still_works(self):
        """Auto-redeem success path is not broken by external claim logic."""
        pm = PositionManager()
        om = _MockOM()
        client = MagicMock()

        order = _make_live_order()
        om._order_history.append(order)
        pos = pm.open_position(order)
        pos.metadata["execution_mode"] = "live"
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }

        mock_redeemer = MagicMock()
        mock_redeemer.is_ready = True
        mock_redeemer.redeem.return_value = {
            "success": True, "tx_hash": "0xtx", "error": None, "gas_used": 150000,
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer = mock_redeemer
        recon._onchain_redeemer_init_attempted = True
        recon.reconcile()

        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertTrue(closed[0].metadata.get("redeemed"))
        self.assertFalse(closed[0].metadata.get("external_claim", False))


class TestPaperModeUnchanged(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_paper_unaffected(self):
        from execution.paper_trader import PaperTrader
        trader = PaperTrader()
        order = Order(
            market_type="btc-5min", direction="UP", side="BUY",
            price=0.50, size_usdc=25.0, num_shares=50.0,
            execution_mode="paper",
        )
        result = trader.execute(order)
        self.assertEqual(result.status, "FILLED")


if __name__ == "__main__":
    unittest.main()
