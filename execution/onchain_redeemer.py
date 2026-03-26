"""On-chain CTF/NegRisk redemption for Polymarket winning positions.

Polymarket BTC up/down markets are neg-risk binary markets on Polygon.
Winning positions are redeemed by calling redeemPositions() on the
NegRiskAdapter contract.

For proxy wallets (signature_type=2), conditional tokens are held by
the funder/proxy address, NOT the signer EOA. The redemption must be
routed THROUGH the proxy contract:

    signer EOA → proxy.execute(negRiskAdapter, redeemPositions_calldata)

This makes msg.sender at the NegRiskAdapter level be the proxy/funder
(which holds the tokens), not the signer EOA.

Requirements:
- web3 (pip install web3)
- POLYMARKET_PRIVATE_KEY in env
- POLYMARKET_FUNDER in env (for proxy wallets)
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
GNOSIS_CTF = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Default Polygon RPC (public, rate-limited — override via POLYGON_RPC_URL)
DEFAULT_POLYGON_RPC = "https://polygon-rpc.com"

# NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] indexSets)
NEG_RISK_REDEEM_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Polymarket proxy wallet execute(address to, bytes data)
PROXY_WALLET_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Gnosis CTF.redeemPositions (fallback for non-neg-risk)
GNOSIS_CTF_REDEEM_ABI = [
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

USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_PARENT = b"\x00" * 32


class OnchainRedeemer:
    """Submits on-chain redemption transactions for winning Polymarket positions."""

    def __init__(self):
        self._w3 = None
        self._account = None
        self._funder = None
        self._is_proxy = False
        self._neg_risk_contract = None
        self._proxy_contract = None
        self._ready = False
        self._init_error: Optional[str] = None

    def initialize(self) -> bool:
        """Initialize web3 connection and contracts."""
        try:
            from web3 import Web3
        except ImportError:
            self._init_error = "web3 not installed. Run: pip install web3"
            log.error(f"[REDEEM] {self._init_error}")
            return False

        try:
            from web3.middleware import geth_poa_middleware
        except ImportError:
            geth_poa_middleware = None

        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            self._init_error = "POLYMARKET_PRIVATE_KEY not set"
            log.error(f"[REDEEM] {self._init_error}")
            return False

        rpc_url = os.getenv("POLYGON_RPC_URL", DEFAULT_POLYGON_RPC)

        try:
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            if geth_poa_middleware:
                try:
                    self._w3.middleware_onion.inject(geth_poa_middleware, layer=0)
                except Exception:
                    pass

            if not self._w3.is_connected():
                self._init_error = f"Cannot connect to Polygon RPC: {rpc_url}"
                log.error(f"[REDEEM] {self._init_error}")
                return False

            self._account = self._w3.eth.account.from_key(private_key)
            funder_env = os.getenv("POLYMARKET_FUNDER")
            self._funder = funder_env or self._account.address
            self._is_proxy = (
                funder_env is not None
                and funder_env.lower() != self._account.address.lower()
            )

            # Build contract instances
            self._neg_risk_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                abi=NEG_RISK_REDEEM_ABI,
            )

            if self._is_proxy:
                self._proxy_contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(self._funder),
                    abi=PROXY_WALLET_ABI,
                )

            chain_id = self._w3.eth.chain_id
            balance_wei = self._w3.eth.get_balance(self._account.address)
            balance_matic = self._w3.from_wei(balance_wei, "ether")

            log.warning(f"[REDEEM] On-chain redeemer initialized:")
            log.warning(f"[REDEEM]   rpc={rpc_url}")
            log.warning(f"[REDEEM]   chain_id={chain_id}")
            log.warning(f"[REDEEM]   signer={self._account.address}")
            log.warning(f"[REDEEM]   funder={self._funder}")
            log.warning(f"[REDEEM]   is_proxy={self._is_proxy}")
            log.warning(f"[REDEEM]   matic_balance={balance_matic:.4f}")

            if balance_matic < 0.001:
                log.warning(f"[REDEEM]   WARNING: low MATIC ({balance_matic:.6f})")

            self._ready = True
            return True

        except Exception as e:
            self._init_error = f"web3 init failed: {e}"
            log.error(f"[REDEEM] {self._init_error}")
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def redeem(self, condition_id: str) -> dict:
        """Submit an on-chain redemption transaction.

        For proxy wallets: routes through proxy.execute()
        For EOA wallets: calls NegRiskAdapter directly
        """
        if not self._ready:
            return {"success": False, "tx_hash": None,
                    "error": self._init_error or "redeemer not initialized",
                    "gas_used": None}

        cond_bytes = _to_bytes32(condition_id)
        if cond_bytes is None:
            return {"success": False, "tx_hash": None,
                    "error": f"Invalid condition_id format: {condition_id!r}",
                    "gas_used": None}

        log.warning(
            f"[REDEEM] TX SUBMITTING: condition={condition_id[:18]}... "
            f"proxy={self._is_proxy} signer={self._account.address[:12]}..."
        )

        try:
            if self._is_proxy:
                result = self._redeem_via_proxy(cond_bytes)
            else:
                result = self._redeem_direct(cond_bytes)

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

    def _redeem_via_proxy(self, cond_bytes: bytes) -> dict:
        """Route redemption through the proxy wallet contract.

        Calls: proxy.execute(negRiskAdapter, redeemPositions_calldata)
        This makes msg.sender at the NegRiskAdapter = proxy/funder address.
        """
        from web3 import Web3

        # Encode the inner redeemPositions call
        redeem_calldata = self._neg_risk_contract.encodeABI(
            fn_name="redeemPositions",
            args=[cond_bytes, [1, 2]],
        )

        log.warning(f"[REDEEM] DETECTED PROXY WALLET:")
        log.warning(f"[REDEEM]   signer_eoa={self._account.address}")
        log.warning(f"[REDEEM]   proxy_funder={self._funder}")
        log.warning(f"[REDEEM]   TARGET CONTRACT: NegRiskAdapter={NEG_RISK_ADAPTER}")
        log.warning(f"[REDEEM]   INNER CALLDATA: redeemPositions(conditionId, [1,2])")
        log.warning(f"[REDEEM]   CALLDATA_HEX: {redeem_calldata[:20]}...({len(redeem_calldata)} chars)")
        log.warning(f"[REDEEM]   PROXY EXECUTE: proxy.execute(negRiskAdapter, calldata)")
        log.warning(f"[REDEEM]   PROXY EXECUTE SIGNATURE: execute(address,bytes)")

        # Wrap in proxy.execute(to, data)
        calldata_bytes = bytes.fromhex(redeem_calldata[2:]) if redeem_calldata.startswith("0x") else bytes.fromhex(redeem_calldata)
        tx = self._proxy_contract.functions.execute(
            Web3.to_checksum_address(NEG_RISK_ADAPTER),
            calldata_bytes,
        ).build_transaction({
            "from": self._account.address,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "gas": 400_000,
            "gasPrice": self._w3.eth.gas_price,
            "chainId": 137,
        })

        log.warning(f"[REDEEM] TX built: from={self._account.address[:12]}... gas=400000")
        return self._sign_and_send(tx)

    def _redeem_direct(self, cond_bytes: bytes) -> dict:
        """Direct call to NegRiskAdapter (for EOA wallets where signer = funder)."""
        tx = self._neg_risk_contract.functions.redeemPositions(
            cond_bytes,
            [1, 2],
        ).build_transaction({
            "from": self._account.address,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "gas": 300_000,
            "gasPrice": self._w3.eth.gas_price,
            "chainId": 137,
        })

        return self._sign_and_send(tx)

    def _sign_and_send(self, tx: dict) -> dict:
        """Sign, send, wait for receipt. Logs detailed diagnostics."""
        signed = self._w3.eth.account.sign_transaction(tx, self._account.key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)

        log.warning(f"[REDEEM] TX SUBMITTED: {tx_hash_hex}")

        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        status = receipt.get("status")
        gas_used = receipt.get("gasUsed")
        log.warning(f"[REDEEM] RECEIPT STATUS: {status} gas_used={gas_used}")

        if status == 1:
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "error": None,
                "gas_used": gas_used,
            }
        else:
            # Try to extract revert reason
            revert_reason = self._get_revert_reason(tx, tx_hash_hex)
            error_msg = f"TX reverted (status=0)"
            if revert_reason:
                error_msg += f" reason: {revert_reason}"
                log.error(f"[REDEEM] REVERT REASON: {revert_reason}")
            return {
                "success": False,
                "tx_hash": tx_hash_hex,
                "error": error_msg,
                "gas_used": gas_used,
            }

    def _get_revert_reason(self, tx: dict, tx_hash: str) -> str:
        """Attempt to extract revert reason from a failed transaction."""
        try:
            # Try eth_call to replay and get error message
            call_tx = {k: v for k, v in tx.items() if k in ("from", "to", "data", "value", "gas")}
            self._w3.eth.call(call_tx)
            return ""  # no revert on replay — timing/state dependent
        except Exception as e:
            reason = str(e)
            # Extract the readable part from web3 error messages
            if "execution reverted" in reason.lower():
                return reason
            if len(reason) > 200:
                return reason[:200] + "..."
            return reason


def _to_bytes32(hex_str: str) -> Optional[bytes]:
    """Convert a hex string condition_id to bytes32."""
    if not hex_str:
        return None
    try:
        clean = hex_str.strip()
        if clean.startswith("0x") or clean.startswith("0X"):
            clean = clean[2:]
        clean = clean.zfill(64)
        return bytes.fromhex(clean)
    except (ValueError, TypeError):
        return None
