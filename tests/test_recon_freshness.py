"""Tests for softened reconciliation freshness check.

Only blocks when recon is badly stale (>30s) or has never synced.
Does NOT change entry caps, stacking, sizing, or direction logic.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from models.order import Order
from execution.live_trader import LiveTrader
from execution.live_reconciler import LiveReconciler
from execution.position_manager import PositionManager
import config


_REAL_TOKEN = "21742633143463906290569050155826241533067272736897614950488156847949938836455"


def _make_order(**overrides) -> Order:
    defaults = dict(
        order_id="fresh_test",
        market_id="mkt_1",
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


class _MockOM:
    def __init__(self):
        self._order_history = []
    def get_order_history(self):
        return []


def _make_trader_with_recon(recon_age_s=0.0, never_synced=False):
    """Create a LiveTrader with a reconciler at a specific freshness."""
    trader = LiveTrader()
    trader._clob_client = MagicMock()

    recon = LiveReconciler(MagicMock(), PositionManager(), _MockOM())
    if never_synced:
        recon._stale = True
        recon._last_reconcile_ts = 0.0
    else:
        recon._stale = False
        recon._last_reconcile_ts = time.time() - recon_age_s

    trader.set_reconciler(recon)
    return trader


class TestReconFreshAllowed(unittest.TestCase):
    """Recon age <= 10s: entry allowed silently."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_fresh_recon_allows_entry(self):
        trader = _make_trader_with_recon(recon_age_s=3.0)
        order = _make_order()
        result = trader.execute(order)
        # Should NOT be blocked by recon freshness
        reason = result.metadata.get("rejection_reason", "") + result.metadata.get("failure_reason", "")
        self.assertNotIn("RECON BLOCK", reason)
        self.assertNotIn("RECON STALE", reason)


class TestReconStaleWarn(unittest.TestCase):
    """Recon age 10-30s: entry allowed with warning."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_mildly_stale_recon_allows_with_warning(self):
        trader = _make_trader_with_recon(recon_age_s=20.0)
        order = _make_order()
        result = trader.execute(order)
        # Should NOT be blocked
        reason = result.metadata.get("rejection_reason", "") + result.metadata.get("failure_reason", "")
        self.assertNotIn("RECON BLOCK", reason)


class TestReconBadlyStaleBlocked(unittest.TestCase):
    """Recon age > 30s: entry blocked."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_badly_stale_recon_blocks(self):
        trader = _make_trader_with_recon(recon_age_s=45.0)
        order = _make_order()
        result = trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("RECON BLOCK", result.metadata.get("rejection_reason", ""))
        self.assertIn("45s", result.metadata.get("rejection_reason", ""))


class TestReconNeverSyncedBlocked(unittest.TestCase):
    """No successful recon ever: entry blocked."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_never_synced_blocks(self):
        trader = _make_trader_with_recon(never_synced=True)
        order = _make_order()
        result = trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("RECON BLOCK", result.metadata.get("rejection_reason", ""))
        self.assertIn("no successful reconciliation", result.metadata.get("rejection_reason", ""))


class TestNoReconAllowed(unittest.TestCase):
    """No reconciler attached: entry allowed (recon may be disabled)."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_no_reconciler_allows(self):
        trader = LiveTrader()
        trader._clob_client = MagicMock()
        # No set_reconciler call
        order = _make_order()
        result = trader.execute(order)
        reason = result.metadata.get("rejection_reason", "") + result.metadata.get("failure_reason", "")
        self.assertNotIn("RECON BLOCK", reason)


class TestNoStrategyBehaviorChange(unittest.TestCase):
    """Verify no entry cap / direction / stacking logic is reintroduced."""

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_no_duplicate_direction_block(self):
        """Same direction should NOT be blocked by the freshness check."""
        trader = _make_trader_with_recon(recon_age_s=2.0)
        order = _make_order(direction="UP")
        result = trader.execute(order)
        reason = result.metadata.get("rejection_reason", "") + result.metadata.get("failure_reason", "")
        self.assertNotIn("duplicate direction", reason.lower())
        self.assertNotIn("BLOCKED", reason)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_no_live_only_cap(self):
        """No LIVE_MAX_ENTRIES_PER_WINDOW or separate cap logic."""
        trader = _make_trader_with_recon(recon_age_s=2.0)
        order = _make_order()
        result = trader.execute(order)
        reason = result.metadata.get("rejection_reason", "") + result.metadata.get("failure_reason", "")
        self.assertNotIn("market entry cap", reason.lower())


class TestPaperModeUnchanged(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_paper_unaffected(self):
        from execution.paper_trader import PaperTrader
        trader = PaperTrader()
        order = _make_order(execution_mode="paper")
        result = trader.execute(order)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.execution_mode, "paper")


if __name__ == "__main__":
    unittest.main()
