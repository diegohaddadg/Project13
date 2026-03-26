"""Tests for on-chain CTF/NegRisk redemption.

All tests mock web3 — no real blockchain calls.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from execution.onchain_redeemer import OnchainRedeemer, _to_bytes32


class TestToBytes32(unittest.TestCase):
    """Test condition_id hex to bytes32 conversion."""

    def test_valid_hex_with_prefix(self):
        result = _to_bytes32("0xabcdef")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 32)

    def test_valid_hex_without_prefix(self):
        result = _to_bytes32("abcdef1234567890")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 32)

    def test_full_64_char_hex(self):
        hex_str = "0x" + "ab" * 32
        result = _to_bytes32(hex_str)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 32)
        self.assertEqual(result, bytes.fromhex("ab" * 32))

    def test_empty_string(self):
        self.assertIsNone(_to_bytes32(""))

    def test_none(self):
        self.assertIsNone(_to_bytes32(None))

    def test_invalid_hex(self):
        self.assertIsNone(_to_bytes32("not_hex_at_all!"))


class TestOnchainRedeemerInit(unittest.TestCase):
    """Test redeemer initialization."""

    def test_fails_without_web3(self):
        """When web3 is not installed, init returns False."""
        redeemer = OnchainRedeemer()
        # web3 is not installed locally, so this should fail gracefully
        result = redeemer.initialize()
        self.assertFalse(result)
        self.assertFalse(redeemer.is_ready)
        self.assertIn("web3", redeemer._init_error.lower())

    @patch.dict("os.environ", {}, clear=True)
    def test_fails_without_private_key(self):
        """Without POLYMARKET_PRIVATE_KEY, init fails."""
        import sys
        mock_web3_mod = MagicMock()
        mock_web3_class = MagicMock()
        mock_web3_mod.Web3 = mock_web3_class
        mock_middleware = MagicMock()
        mock_web3_mod.middleware = mock_middleware
        mock_middleware.geth_poa_middleware = MagicMock()

        with patch.dict(sys.modules, {
            "web3": mock_web3_mod,
            "web3.middleware": mock_middleware,
        }):
            redeemer = OnchainRedeemer()
            result = redeemer.initialize()
            self.assertFalse(result)
            self.assertIn("PRIVATE_KEY", redeemer._init_error)


class TestOnchainRedeemerRedeem(unittest.TestCase):
    """Test redemption execution with mocked web3."""

    def _make_ready_redeemer(self, is_proxy=True):
        """Create a redeemer with mocked web3 internals."""
        redeemer = OnchainRedeemer()
        redeemer._ready = True

        mock_w3 = MagicMock()
        mock_account = MagicMock()
        mock_account.address = "0xTestSigner"
        mock_account.key = b"\x00" * 32

        redeemer._w3 = mock_w3
        redeemer._account = mock_account
        redeemer._funder = "0xTestFunder" if is_proxy else "0xTestSigner"
        redeemer._is_proxy = is_proxy

        # Mock NegRisk contract (for encodeABI and direct calls)
        mock_neg_risk = MagicMock()
        mock_fn = MagicMock()
        mock_fn.build_transaction.return_value = {
            "from": "0xTestSigner", "nonce": 0, "gas": 300000,
            "gasPrice": 30000000000, "chainId": 137,
        }
        mock_neg_risk.functions.redeemPositions.return_value = mock_fn
        mock_neg_risk.encodeABI.return_value = "0xdeadbeef"
        redeemer._neg_risk_contract = mock_neg_risk

        # Mock proxy contract (for proxy wallets)
        if is_proxy:
            mock_proxy = MagicMock()
            mock_proxy_fn = MagicMock()
            mock_proxy_fn.build_transaction.return_value = {
                "from": "0xTestSigner", "nonce": 0, "gas": 400000,
                "gasPrice": 30000000000, "chainId": 137,
            }
            mock_proxy.functions.execute.return_value = mock_proxy_fn
            redeemer._proxy_contract = mock_proxy

        return redeemer

    def _patch_web3_import(self):
        """Mock the web3 module so `from web3 import Web3` works in methods."""
        import sys
        mock_web3_mod = MagicMock()
        mock_web3_mod.Web3 = MagicMock()
        return patch.dict(sys.modules, {"web3": mock_web3_mod, "web3.middleware": MagicMock()})

    def test_redeem_success(self):
        redeemer = self._make_ready_redeemer()

        mock_signed = MagicMock()
        mock_signed.raw_transaction = b"\x00"
        redeemer._w3.eth.account.sign_transaction.return_value = mock_signed
        redeemer._w3.eth.send_raw_transaction.return_value = b"\xab" * 32
        redeemer._w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 1, "gasUsed": 150000,
        }

        with self._patch_web3_import():
            result = redeemer.redeem("0x" + "ab" * 32)

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["tx_hash"])
        self.assertEqual(result["gas_used"], 150000)
        self.assertIsNone(result["error"])

    def test_redeem_reverted(self):
        redeemer = self._make_ready_redeemer()

        mock_signed = MagicMock()
        mock_signed.raw_transaction = b"\x00"
        redeemer._w3.eth.account.sign_transaction.return_value = mock_signed
        redeemer._w3.eth.send_raw_transaction.return_value = b"\xab" * 32
        redeemer._w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 0, "gasUsed": 100000,  # reverted
        }

        with self._patch_web3_import():
            result = redeemer.redeem("0x" + "ab" * 32)

        self.assertFalse(result["success"])
        self.assertIn("reverted", result["error"])

    def test_redeem_not_ready(self):
        redeemer = OnchainRedeemer()
        result = redeemer.redeem("0xabc")
        self.assertFalse(result["success"])
        self.assertIn("not initialized", result["error"])

    def test_redeem_invalid_condition_id(self):
        redeemer = self._make_ready_redeemer()
        result = redeemer.redeem("not_valid_hex!!!")
        self.assertFalse(result["success"])
        self.assertIn("Invalid condition_id", result["error"])

    def test_no_fake_success_on_exception(self):
        """Network/RPC error must not produce success=True."""
        redeemer = self._make_ready_redeemer()
        redeemer._w3.eth.account.sign_transaction.side_effect = Exception("RPC timeout")

        result = redeemer.redeem("0x" + "ab" * 32)

        self.assertFalse(result["success"])
        self.assertIsNone(result["tx_hash"])


class TestReconcilerOnchainIntegration(unittest.TestCase):
    """Test that the reconciler correctly uses on-chain redemption."""

    @patch.object(__import__("config"), "EXECUTION_MODE", "live")
    @patch.object(__import__("config"), "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(__import__("config"), "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_redeem_failure_keeps_claimable(self):
        """When on-chain redeem fails and no fallback works, position stays CLAIMABLE."""
        from execution.live_reconciler import LiveReconciler
        from execution.position_manager import PositionManager
        from models.order import Order

        pm = PositionManager()
        om = MagicMock()
        om.get_order_history.return_value = []
        om.sync_order_pnl_from_position = MagicMock()
        client = MagicMock()

        order = Order(
            order_id="redeem_test", market_id="mkt", market_type="btc-5min",
            direction="UP", status="FILLED", fill_price=0.55, num_shares=50,
            size_usdc=27.50, execution_mode="live",
        )
        om._order_history = [order]
        pos = pm.open_position(order)
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["execution_mode"] = "live"

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }
        # No redeem methods work
        del client.redeem
        del client.merge_positions

        recon = LiveReconciler(client, pm, om)
        # Skip on-chain init (web3 not available)
        recon._onchain_redeemer_init_attempted = True
        recon._stale = False
        recon._last_reconcile_ts = __import__("time").time()

        recon.reconcile()

        # Position should be CLAIMABLE, not RESOLVED, not redeemed
        self.assertEqual(pm.count_open_positions(), 1)
        self.assertEqual(pos.status, "CLAIMABLE")
        self.assertIsNone(pos.pnl)

    @patch.object(__import__("config"), "EXECUTION_MODE", "live")
    @patch.object(__import__("config"), "LIVE_RECONCILIATION_ENABLED", True)
    @patch.object(__import__("config"), "LIVE_AUTO_REDEEM_ENABLED", True)
    def test_successful_onchain_redeem_closes_position(self):
        """When on-chain redeem succeeds, position is closed with correct PnL."""
        from execution.live_reconciler import LiveReconciler
        from execution.position_manager import PositionManager
        from models.order import Order

        pm = PositionManager()
        om = MagicMock()
        om.get_order_history.return_value = []
        om.sync_order_pnl_from_position = MagicMock()
        om._append_trade_log = MagicMock()
        om._log_lifecycle = MagicMock()
        client = MagicMock()

        order = Order(
            order_id="redeem_ok", market_id="mkt", market_type="btc-5min",
            direction="UP", status="FILLED", fill_price=0.55, num_shares=50,
            size_usdc=27.50, execution_mode="live",
        )
        pos = pm.open_position(order)
        pos.metadata["condition_id"] = "0xcond456"
        pos.metadata["token_id"] = "tok_winner"
        pos.metadata["execution_mode"] = "live"

        client.get_market.return_value = {
            "closed": True, "resolved": True,
            "tokens": [{"token_id": "tok_winner", "winner": 1.0}],
        }
        # Mock on-chain redeemer that succeeds
        mock_redeemer = MagicMock()
        mock_redeemer.is_ready = True
        mock_redeemer.redeem.return_value = {
            "success": True, "tx_hash": "0xtxhash123", "error": None, "gas_used": 150000,
        }

        recon = LiveReconciler(client, pm, om)
        recon._onchain_redeemer = mock_redeemer
        recon._onchain_redeemer_init_attempted = True
        recon._stale = False
        recon._last_reconcile_ts = __import__("time").time()

        recon.reconcile()

        # Position should be closed and redeemed
        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0].metadata.get("redeemed"))
        self.assertEqual(closed[0].metadata.get("redeem_tx_hash"), "0xtxhash123")
        self.assertGreater(closed[0].pnl, 0)


if __name__ == "__main__":
    unittest.main()
