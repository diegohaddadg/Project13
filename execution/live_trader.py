"""Live trading execution — real Polymarket CLOB order submission.

This module touches real money. Every action is guarded by multiple safety checks.
If ANY check fails, the order is REJECTED and the reason is logged.

All actions are clearly tagged [LIVE] in logs.
"""

from __future__ import annotations

import time
from typing import Optional

from models.order import Order
from models.market_state import MarketState
from utils.logger import get_logger
import config

log = get_logger("live_trader")


class LiveTrader:
    """Live order submission via Polymarket CLOB.

    Safety gates (ALL must pass):
    1. config.EXECUTION_MODE == "live"
    2. config.TRADING_ENABLED is True
    3. config.LIVE_TRADING_CONFIRMATION == "I_UNDERSTAND"
    4. order size <= MAX_ORDER_SIZE_USDC
    5. market is active
    6. token_id is valid (non-empty)
    7. market snapshot is recent enough
    8. CLOB client initialized with valid credentials
    """

    def __init__(self):
        self._clob_client = None
        self._init_error: Optional[str] = None
        self._orders_submitted: int = 0
        self._orders_failed: int = 0

    def initialize(self) -> bool:
        """Initialize the authenticated CLOB client.

        Call this once at startup when EXECUTION_MODE == "live".
        Returns True if ready, False if initialization failed.
        """
        from utils.polymarket_auth import validate_live_credentials

        ok, missing = validate_live_credentials()
        if not ok:
            self._init_error = f"Missing credentials: {', '.join(missing)}"
            log.error(f"[LIVE] Cannot initialize: {self._init_error}")
            return False

        try:
            from utils.polymarket_auth import get_clob_client
            self._clob_client = get_clob_client(authenticated=True)
            log.info("[LIVE] CLOB client initialized successfully")
            return True
        except ImportError:
            self._init_error = "py-clob-client not installed"
            log.error(f"[LIVE] {self._init_error}. Run: pip install py-clob-client")
            return False
        except Exception as e:
            self._init_error = f"CLOB client init failed: {e}"
            log.error(f"[LIVE] {self._init_error}")
            return False

    @property
    def is_ready(self) -> bool:
        return self._clob_client is not None

    def execute(self, order: Order, market_snapshot: Optional[MarketState] = None) -> Order:
        """Submit a live order after verifying all safety gates."""
        order.execution_mode = "live"

        # Safety gate 1: execution mode
        if config.EXECUTION_MODE != "live":
            return self._reject(order, "EXECUTION_MODE is not 'live'")

        # Safety gate 2: trading enabled
        if not config.TRADING_ENABLED:
            return self._reject(order, "TRADING_ENABLED is False")

        # Safety gate 3: explicit confirmation
        if config.LIVE_TRADING_CONFIRMATION != "I_UNDERSTAND":
            return self._reject(order, "LIVE_TRADING_CONFIRMATION not set to 'I_UNDERSTAND'")

        # Safety gate 4: order size
        if order.size_usdc > config.MAX_ORDER_SIZE_USDC:
            return self._reject(
                order,
                f"Order size ${order.size_usdc:.2f} exceeds max ${config.MAX_ORDER_SIZE_USDC:.2f}"
            )

        # Safety gate 5: market active
        if market_snapshot and not market_snapshot.is_active:
            return self._reject(order, "Market is not active")

        # Safety gate 6: token_id valid
        if not order.token_id:
            return self._reject(order, "token_id is empty — cannot determine which token to buy")

        # Safety gate 7: market snapshot freshness
        if market_snapshot:
            snapshot_age = time.time() - market_snapshot.timestamp
            if snapshot_age > 10.0:
                return self._reject(
                    order,
                    f"Market snapshot is {snapshot_age:.0f}s old — too stale for live execution"
                )

        # Safety gate 8: price sanity
        if order.price <= 0 or order.price >= 1.0:
            return self._reject(order, f"Price {order.price} out of valid range (0, 1)")

        # Safety gate 9: shares sanity
        if order.num_shares <= 0:
            return self._reject(order, f"num_shares {order.num_shares} must be positive")

        # Safety gate 10: CLOB client ready
        if not self.is_ready:
            return self._fail(
                order,
                f"CLOB client not initialized: {self._init_error or 'call initialize() first'}"
            )

        # --- All gates passed — submit to CLOB ---
        return self._submit_clob_order(order)

    def _submit_clob_order(self, order: Order) -> Order:
        """Build and submit a limit order to Polymarket CLOB."""
        # Round price to tick size (Polymarket uses 0.01 ticks for most markets)
        price = round(order.price, 2)
        # Round shares to 2 decimal places
        size = round(order.num_shares, 2)

        log.warning(
            f"[LIVE] SUBMITTING: {order.direction} {order.market_type} "
            f"${order.size_usdc:.2f} | {size} shares @{price:.3f} "
            f"token={order.token_id[:20]}..."
        )

        order.status = "SUBMITTED"
        order.metadata["live_submit_ts"] = time.time()
        order.metadata["live_price_sent"] = price
        order.metadata["live_size_sent"] = size

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                token_id=order.token_id,
                price=price,
                size=size,
                side=BUY,
            )

            signed_order = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed_order, OrderType.GTC)

        except ImportError as e:
            self._orders_failed += 1
            return self._fail(order, f"py-clob-client not available: {e}")
        except Exception as e:
            self._orders_failed += 1
            return self._fail(order, f"CLOB submission error: {e}")

        # --- Parse exchange response ---
        order.metadata["live_response_ts"] = time.time()
        order.metadata["live_response"] = _safe_serialize(resp)

        if isinstance(resp, dict):
            exchange_order_id = resp.get("orderID") or resp.get("order_id") or ""
            success = resp.get("success", False)
        else:
            exchange_order_id = str(resp) if resp else ""
            success = bool(resp)

        order.metadata["exchange_order_id"] = exchange_order_id

        if success and exchange_order_id:
            # Order accepted by the exchange — it's OPEN on the book.
            # We do NOT mark FILLED here. The order is resting.
            # For Polymarket binary markets with tight spreads, limit BUYs
            # at the current price typically fill immediately, but we
            # record honestly: status = LIVE (accepted, awaiting fill).
            order.status = "LIVE"
            order.metadata["exchange_status"] = "accepted"
            self._orders_submitted += 1

            log.warning(
                f"[LIVE] ACCEPTED: {order.direction} {order.market_type} "
                f"exchange_id={exchange_order_id[:24]} "
                f"${order.size_usdc:.2f} @{price:.3f}"
            )
        elif success:
            # Success but no order ID — unusual
            order.status = "LIVE"
            order.metadata["exchange_status"] = "accepted_no_id"
            self._orders_submitted += 1
            log.warning(
                f"[LIVE] ACCEPTED (no order ID): {order.direction} {order.market_type}"
            )
        else:
            # Exchange rejected
            error_msg = ""
            if isinstance(resp, dict):
                error_msg = resp.get("errorMsg") or resp.get("error") or resp.get("message") or ""
            self._orders_failed += 1
            order.metadata["exchange_error"] = error_msg
            return self._fail(
                order,
                f"Exchange rejected: {error_msg or 'unknown reason'} (response: {_safe_serialize(resp)})"
            )

        return order

    def check_order_status(self, exchange_order_id: str) -> Optional[dict]:
        """Check status of a live order on the exchange."""
        if not self.is_ready or not exchange_order_id:
            return None
        try:
            return self._clob_client.get_order(exchange_order_id)
        except Exception as e:
            log.warning(f"[LIVE] check_order_status failed for {exchange_order_id}: {e}")
            return None

    def cancel_order(self, exchange_order_id: str) -> bool:
        """Cancel a live order on the exchange."""
        if not self.is_ready or not exchange_order_id:
            return False
        try:
            resp = self._clob_client.cancel(exchange_order_id)
            cancelled = bool(resp)
            if cancelled:
                log.info(f"[LIVE] Cancelled order {exchange_order_id}")
            return cancelled
        except Exception as e:
            log.warning(f"[LIVE] cancel_order failed for {exchange_order_id}: {e}")
            return False

    def get_stats(self) -> dict:
        return {
            "ready": self.is_ready,
            "init_error": self._init_error,
            "orders_submitted": self._orders_submitted,
            "orders_failed": self._orders_failed,
        }

    def _reject(self, order: Order, reason: str) -> Order:
        """Reject an order with a clear reason (pre-submission gate failure)."""
        order.status = "REJECTED"
        order.metadata["rejection_reason"] = reason
        log.warning(f"[LIVE] REJECTED: {reason} | {order.summary()}")
        return order

    def _fail(self, order: Order, reason: str) -> Order:
        """Mark an order as FAILED (submission attempted but failed)."""
        order.status = "FAILED"
        order.metadata["failure_reason"] = reason
        log.error(f"[LIVE] FAILED: {reason} | {order.summary()}")
        return order


def _safe_serialize(obj) -> str:
    """Serialize an exchange response to a loggable string."""
    if obj is None:
        return "None"
    if isinstance(obj, dict):
        # Truncate large responses
        import json
        try:
            s = json.dumps(obj, default=str)
            return s[:500] if len(s) > 500 else s
        except Exception:
            return str(obj)[:500]
    return str(obj)[:500]
