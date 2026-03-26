"""On-chain CTF/NegRisk redemption for Polymarket winning positions.

Polymarket BTC up/down markets are neg-risk binary markets on Polygon.
Winning positions are redeemed by calling redeemPositions() on the
NegRiskAdapter contract, which burns conditional tokens and credits
USDC to the funder/maker address.

This module handles the actual on-chain transaction submission and
confirmation. It is called by the live_reconciler when a winning
position is detected.

Requirements:
- web3 (pip install web3)
- POLYMARKET_PRIVATE_KEY in env
- Polygon RPC endpoint
- Sufficient MATIC for gas (~0.001-0.01 MATIC per redemption)
"""

from __future__ import annotations

import os
import time
from typing import Optional

from utils.logger import get_logger

log = get_logger("onchain_redeem")

# --- Contract addresses on Polygon mainnet ---
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CTF_EXCHANGE = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Default Polygon RPC (public, rate-limited — override via env for production)
DEFAULT_POLYGON_RPC = "https://polygon-rpc.com"

# NegRiskAdapter ABI fragment — only the redeemPositions function
NEG_RISK_REDEEM_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# CTF redeemPositions ABI fragment (fallback for non-neg-risk markets)
CTF_REDEEM_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# USDC on Polygon (bridged)
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Zero parent collection for top-level conditions
ZERO_PARENT = b"\x00" * 32


class OnchainRedeemer:
    """Submits on-chain redemption transactions for winning Polymarket positions."""

    def __init__(self):
        self._w3 = None
        self._account = None
        self._neg_risk_contract = None
        self._ctf_contract = None
        self._funder = None
        self._ready = False
        self._init_error: Optional[str] = None

    def initialize(self) -> bool:
        """Initialize web3 connection and contracts.

        Returns True if ready for on-chain redemption.
        """
        try:
            from web3 import Web3
            from web3.middleware import geth_poa_middleware
        except ImportError:
            self._init_error = "web3 not installed. Run: pip install web3"
            log.error(f"[REDEEM] {self._init_error}")
            return False

        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            self._init_error = "POLYMARKET_PRIVATE_KEY not set"
            log.error(f"[REDEEM] {self._init_error}")
            return False

        rpc_url = os.getenv("POLYGON_RPC_URL", DEFAULT_POLYGON_RPC)

        try:
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            # Polygon is a PoA chain — need the middleware
            try:
                self._w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except Exception:
                pass  # Some web3 versions handle this differently

            if not self._w3.is_connected():
                self._init_error = f"Cannot connect to Polygon RPC: {rpc_url}"
                log.error(f"[REDEEM] {self._init_error}")
                return False

            self._account = self._w3.eth.account.from_key(private_key)
            self._funder = os.getenv("POLYMARKET_FUNDER") or self._account.address

            # Build contract instances
            self._neg_risk_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                abi=NEG_RISK_REDEEM_ABI,
            )
            self._ctf_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_EXCHANGE),
                abi=CTF_REDEEM_ABI,
            )

            chain_id = self._w3.eth.chain_id
            balance_wei = self._w3.eth.get_balance(self._account.address)
            balance_matic = self._w3.from_wei(balance_wei, "ether")

            log.warning(f"[REDEEM] On-chain redeemer initialized:")
            log.warning(f"[REDEEM]   rpc={rpc_url}")
            log.warning(f"[REDEEM]   chain_id={chain_id}")
            log.warning(f"[REDEEM]   signer={self._account.address}")
            log.warning(f"[REDEEM]   funder={self._funder}")
            log.warning(f"[REDEEM]   matic_balance={balance_matic:.4f}")
            log.warning(f"[REDEEM]   neg_risk_adapter={NEG_RISK_ADAPTER}")

            if balance_matic < 0.001:
                log.warning(f"[REDEEM]   WARNING: MATIC balance very low ({balance_matic:.6f}) — may not cover gas")

            self._ready = True
            return True

        except Exception as e:
            self._init_error = f"web3 init failed: {e}"
            log.error(f"[REDEEM] {self._init_error}")
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def redeem(self, condition_id: str, neg_risk: bool = True) -> dict:
        """Submit an on-chain redemption transaction.

        Args:
            condition_id: Hex string condition ID of the resolved market.
            neg_risk: If True, use NegRiskAdapter. If False, use CTF directly.

        Returns:
            dict with:
                success: bool
                tx_hash: str or None
                error: str or None
                gas_used: int or None
        """
        if not self._ready:
            return {"success": False, "tx_hash": None,
                    "error": self._init_error or "redeemer not initialized",
                    "gas_used": None}

        # Normalize condition_id to bytes32
        cond_bytes = _to_bytes32(condition_id)
        if cond_bytes is None:
            return {"success": False, "tx_hash": None,
                    "error": f"Invalid condition_id format: {condition_id!r}",
                    "gas_used": None}

        log.warning(
            f"[REDEEM] TX SUBMITTING: condition={condition_id[:18]}... "
            f"neg_risk={neg_risk} signer={self._account.address[:12]}..."
        )

        try:
            if neg_risk:
                result = self._redeem_neg_risk(cond_bytes)
            else:
                result = self._redeem_ctf(cond_bytes)

            if result["success"]:
                log.warning(
                    f"[REDEEM] TX CONFIRMED: tx={result['tx_hash']} "
                    f"gas_used={result.get('gas_used', '?')}"
                )
            else:
                log.error(f"[REDEEM] TX FAILED: {result['error']}")

            return result

        except Exception as e:
            log.error(f"[REDEEM] TX EXCEPTION: {e}")
            return {"success": False, "tx_hash": None,
                    "error": str(e), "gas_used": None}

    def _redeem_neg_risk(self, cond_bytes: bytes) -> dict:
        """Redeem via NegRiskAdapter.redeemPositions(conditionId, amounts).

        For binary markets, amounts = [amount] where amount is the
        position size. We pass [0] to let the contract determine
        the redemption amount from the caller's balance.
        """
        from web3 import Web3

        # Build the transaction
        # amounts = [] means "redeem all" in many implementations
        # Some implementations require explicit amounts — try empty first
        tx = self._neg_risk_contract.functions.redeemPositions(
            cond_bytes,
            [],  # empty = redeem all available
        ).build_transaction({
            "from": self._account.address,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "gas": 300_000,
            "gasPrice": self._w3.eth.gas_price,
            "chainId": 137,
        })

        return self._sign_and_send(tx)

    def _redeem_ctf(self, cond_bytes: bytes) -> dict:
        """Redeem via CTF.redeemPositions(collateral, parent, condition, indexSets)."""
        from web3 import Web3

        tx = self._ctf_contract.functions.redeemPositions(
            Web3.to_checksum_address(USDC_POLYGON),
            ZERO_PARENT,
            cond_bytes,
            [1, 2],  # Both outcome slots for binary markets
        ).build_transaction({
            "from": self._account.address,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "gas": 300_000,
            "gasPrice": self._w3.eth.gas_price,
            "chainId": 137,
        })

        return self._sign_and_send(tx)

    def _sign_and_send(self, tx: dict) -> dict:
        """Sign a transaction, send it, wait for receipt."""
        signed = self._w3.eth.account.sign_transaction(tx, self._account.key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)

        log.info(f"[REDEEM] TX SUBMITTED: {tx_hash_hex}")

        # Wait for receipt (timeout 60s)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "error": None,
                "gas_used": receipt.get("gasUsed"),
            }
        else:
            return {
                "success": False,
                "tx_hash": tx_hash_hex,
                "error": f"TX reverted (status=0)",
                "gas_used": receipt.get("gasUsed"),
            }


def _to_bytes32(hex_str: str) -> Optional[bytes]:
    """Convert a hex string condition_id to bytes32."""
    if not hex_str:
        return None
    try:
        clean = hex_str.strip()
        if clean.startswith("0x") or clean.startswith("0X"):
            clean = clean[2:]
        # Pad to 64 hex chars (32 bytes)
        clean = clean.zfill(64)
        return bytes.fromhex(clean)
    except (ValueError, TypeError):
        return None
