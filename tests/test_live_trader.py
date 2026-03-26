"""Tests for live trader CLOB submission.

All tests use mocks — no real API calls are made.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch, MagicMock

from models.order import Order
from models.market_state import MarketState
from execution.live_trader import LiveTrader
import config


class _MockOrderArgs:
    """Stand-in for py_clob_client.clob_types.OrderArgs."""
    def __init__(self, token_id="", price=0.0, size=0.0, side="BUY", **kwargs):
        self.token_id = token_id
        self.price = price
        self.size = size
        self.side = side


def _make_order(**overrides) -> Order:
    defaults = dict(
        order_id="test_001",
        signal_id="sig_001",
        market_id="mkt_abc",
        market_type="btc-5min",
        direction="UP",
        side="BUY",
        token_id="0x" + "a" * 64,
        price=0.50,
        size_usdc=25.0,
        num_shares=50.0,
        order_type="LIMIT",
        status="PENDING",
        execution_mode="live",
        metadata={"strategy": "latency_arb"},
    )
    defaults.update(overrides)
    return Order(**defaults)


def _make_snapshot(**overrides) -> MarketState:
    defaults = dict(
        market_id="mkt_abc",
        market_type="btc-5min",
        condition_id="0xcond",
        strike_price=68000,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        time_remaining_seconds=120,
        is_active=True,
        timestamp=time.time(),
    )
    defaults.update(overrides)
    return MarketState(**defaults)


class TestLiveTraderSafetyGates(unittest.TestCase):
    """Test that all safety gates reject correctly."""

    def setUp(self):
        self.trader = LiveTrader()

    @patch.object(config, "EXECUTION_MODE", "paper")
    def test_rejects_when_mode_not_live(self):
        order = _make_order()
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("EXECUTION_MODE", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", False)
    def test_rejects_when_trading_disabled(self):
        order = _make_order()
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("TRADING_ENABLED", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "wrong_phrase")
    def test_rejects_when_confirmation_wrong(self):
        order = _make_order()
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("LIVE_TRADING_CONFIRMATION", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 10.0)
    def test_rejects_when_size_exceeds_max(self):
        order = _make_order(size_usdc=50.0)
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("exceeds max", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    def test_rejects_when_token_empty(self):
        order = _make_order(token_id="")
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("token_id", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    def test_rejects_when_market_inactive(self):
        order = _make_order()
        snapshot = _make_snapshot(is_active=False)
        result = self.trader.execute(order, snapshot)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("not active", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    def test_rejects_when_snapshot_stale(self):
        order = _make_order()
        snapshot = _make_snapshot(timestamp=time.time() - 30)
        result = self.trader.execute(order, snapshot)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("stale", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    def test_rejects_when_price_invalid(self):
        order = _make_order(price=0.0)
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("Price", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    def test_rejects_when_shares_zero(self):
        order = _make_order(num_shares=0)
        result = self.trader.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("num_shares", result.metadata.get("rejection_reason", ""))

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    def test_fails_when_client_not_initialized(self):
        order = _make_order()
        result = self.trader.execute(order)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("not initialized", result.metadata.get("failure_reason", ""))


class TestLiveTraderInitialization(unittest.TestCase):
    """Test CLOB client initialization."""

    @patch("utils.polymarket_auth.validate_live_credentials", return_value=(False, ["POLYMARKET_PRIVATE_KEY"]))
    def test_init_fails_on_missing_creds(self, _mock):
        trader = LiveTrader()
        result = trader.initialize()
        self.assertFalse(result)
        self.assertFalse(trader.is_ready)
        self.assertIn("Missing credentials", trader._init_error)

    @patch("utils.polymarket_auth.validate_live_credentials", return_value=(True, []))
    @patch("utils.polymarket_auth.get_clob_client", side_effect=ImportError("no module"))
    def test_init_fails_on_missing_package(self, _mock_client, _mock_creds):
        trader = LiveTrader()
        result = trader.initialize()
        self.assertFalse(result)
        self.assertIn("not installed", trader._init_error)

    @patch("utils.polymarket_auth.validate_live_credentials", return_value=(True, []))
    @patch("utils.polymarket_auth.get_clob_client", return_value=MagicMock())
    def test_init_succeeds(self, _mock_client, _mock_creds):
        trader = LiveTrader()
        result = trader.initialize()
        self.assertTrue(result)
        self.assertTrue(trader.is_ready)


class TestLiveTraderSubmission(unittest.TestCase):
    """Test real order submission with mocked CLOB client."""

    def _make_ready_trader(self):
        """Create a LiveTrader with a mocked CLOB client."""
        trader = LiveTrader()
        trader._clob_client = MagicMock()
        return trader

    def _patch_clob_imports(self):
        """Create mock modules for py_clob_client so imports succeed."""
        import sys
        mock_clob_types = MagicMock()
        mock_clob_types.OrderArgs = _MockOrderArgs
        mock_clob_types.OrderType = MagicMock()
        mock_clob_types.OrderType.GTC = "GTC"

        mock_constants = MagicMock()
        mock_constants.BUY = "BUY"

        mock_order_builder = MagicMock()
        mock_order_builder.constants = mock_constants

        mods = {
            "py_clob_client": MagicMock(),
            "py_clob_client.clob_types": mock_clob_types,
            "py_clob_client.order_builder": mock_order_builder,
            "py_clob_client.order_builder.constants": mock_constants,
        }
        return patch.dict(sys.modules, mods)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_successful_submission(self):
        trader = self._make_ready_trader()

        mock_signed = MagicMock()
        trader._clob_client.create_order.return_value = mock_signed
        trader._clob_client.post_order.return_value = {
            "orderID": "0xabc123def456",
            "success": True,
        }

        order = _make_order()
        with self._patch_clob_imports():
            result = trader.execute(order)

        self.assertEqual(result.status, "LIVE")
        self.assertEqual(result.metadata["exchange_order_id"], "0xabc123def456")
        self.assertEqual(result.metadata["exchange_status"], "accepted")
        self.assertEqual(trader._orders_submitted, 1)

        # Verify create_order was called with correct args
        trader._clob_client.create_order.assert_called_once()
        args = trader._clob_client.create_order.call_args[0][0]
        self.assertEqual(args.token_id, order.token_id)
        self.assertEqual(args.price, 0.50)
        self.assertEqual(args.size, 50.0)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_exchange_rejection(self):
        trader = self._make_ready_trader()

        mock_signed = MagicMock()
        trader._clob_client.create_order.return_value = mock_signed
        trader._clob_client.post_order.return_value = {
            "success": False,
            "errorMsg": "insufficient balance",
        }

        order = _make_order()
        with self._patch_clob_imports():
            result = trader.execute(order)

        self.assertEqual(result.status, "FAILED")
        self.assertIn("insufficient balance", result.metadata.get("failure_reason", ""))
        self.assertEqual(trader._orders_failed, 1)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_network_error_becomes_failed(self):
        trader = self._make_ready_trader()

        trader._clob_client.create_order.side_effect = ConnectionError("timeout")

        order = _make_order()
        with self._patch_clob_imports():
            result = trader.execute(order)

        self.assertEqual(result.status, "FAILED")
        self.assertIn("CLOB submission error", result.metadata.get("failure_reason", ""))
        self.assertEqual(trader._orders_failed, 1)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_does_not_mark_filled(self):
        """CRITICAL: live submission must NOT fake a fill."""
        trader = self._make_ready_trader()

        mock_signed = MagicMock()
        trader._clob_client.create_order.return_value = mock_signed
        trader._clob_client.post_order.return_value = {
            "orderID": "0xabc",
            "success": True,
        }

        order = _make_order()
        with self._patch_clob_imports():
            result = trader.execute(order)

        self.assertNotEqual(result.status, "FILLED")
        self.assertIsNone(result.fill_price)
        self.assertIsNone(result.fill_timestamp)

    @patch.object(config, "EXECUTION_MODE", "live")
    @patch.object(config, "TRADING_ENABLED", True)
    @patch.object(config, "LIVE_TRADING_CONFIRMATION", "I_UNDERSTAND")
    @patch.object(config, "MAX_ORDER_SIZE_USDC", 500.0)
    def test_metadata_logging(self):
        """Verify all required metadata fields are recorded."""
        trader = self._make_ready_trader()

        mock_signed = MagicMock()
        trader._clob_client.create_order.return_value = mock_signed
        trader._clob_client.post_order.return_value = {
            "orderID": "0xorder123",
            "success": True,
        }

        order = _make_order()
        with self._patch_clob_imports():
            result = trader.execute(order)

        meta = result.metadata
        self.assertIn("live_submit_ts", meta)
        self.assertIn("live_price_sent", meta)
        self.assertIn("live_size_sent", meta)
        self.assertIn("live_response_ts", meta)
        self.assertIn("live_response", meta)
        self.assertIn("exchange_order_id", meta)
        self.assertEqual(meta["live_price_sent"], 0.50)
        self.assertEqual(meta["live_size_sent"], 50.0)


class TestLiveTraderIsComplete(unittest.TestCase):
    """Verify LIVE status is not treated as complete."""

    def test_live_status_not_complete(self):
        order = _make_order(status="LIVE")
        self.assertFalse(order.is_complete())

    def test_filled_is_complete(self):
        order = _make_order(status="FILLED")
        self.assertTrue(order.is_complete())

    def test_failed_is_complete(self):
        order = _make_order(status="FAILED")
        self.assertTrue(order.is_complete())


class TestCredentialValidation(unittest.TestCase):
    """Test credential validation helper."""

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_private_key(self):
        from utils.polymarket_auth import validate_live_credentials
        ok, missing = validate_live_credentials()
        self.assertFalse(ok)
        self.assertIn("POLYMARKET_PRIVATE_KEY", missing)

    @patch.dict("os.environ", {"POLYMARKET_PRIVATE_KEY": "0xabc"}, clear=True)
    def test_private_key_only_is_sufficient(self):
        from utils.polymarket_auth import validate_live_credentials
        ok, missing = validate_live_credentials()
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    @patch.dict("os.environ", {
        "POLYMARKET_PRIVATE_KEY": "0xabc",
        "POLYMARKET_API_KEY": "key",
    }, clear=True)
    def test_partial_api_creds_flagged(self):
        from utils.polymarket_auth import validate_live_credentials
        ok, missing = validate_live_credentials()
        self.assertFalse(ok)
        self.assertIn("POLYMARKET_API_SECRET", missing)
        self.assertIn("POLYMARKET_PASSPHRASE", missing)

    @patch.dict("os.environ", {
        "POLYMARKET_PRIVATE_KEY": "0xabc",
        "POLYMARKET_API_KEY": "key",
        "POLYMARKET_API_SECRET": "secret",
        "POLYMARKET_PASSPHRASE": "pass",
    }, clear=True)
    def test_all_creds_present(self):
        from utils.polymarket_auth import validate_live_credentials
        ok, missing = validate_live_credentials()
        self.assertTrue(ok)
        self.assertEqual(missing, [])


class TestPaperModeUnchanged(unittest.TestCase):
    """Verify paper mode is completely unaffected."""

    def test_paper_trader_still_works(self):
        from execution.paper_trader import PaperTrader
        trader = PaperTrader()
        order = _make_order(execution_mode="paper")
        result = trader.execute(order)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.execution_mode, "paper")
        self.assertIsNotNone(result.fill_price)


if __name__ == "__main__":
    unittest.main()
