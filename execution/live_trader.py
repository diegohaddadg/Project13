"""Live trading execution — conservative, fail-closed.

This module touches real money. Every action is guarded by multiple safety checks.
If ANY check fails, the order is REJECTED and the reason is logged.

All actions are clearly tagged [LIVE] in logs.
"""

from __future__ import annotations

from typing import Optional

from models.order import Order
from models.market_state import MarketState
from utils.logger import get_logger
import config

log = get_logger("live_trader")


class LiveTrader:
    """Conservative live order submission via Polymarket CLOB.

    Safety gates (ALL must pass):
    1. config.EXECUTION_MODE == "live"
    2. config.TRADING_ENABLED is True
    3. config.LIVE_TRADING_CONFIRMATION == "I_UNDERSTAND"
    4. order size <= MAX_ORDER_SIZE_USDC
    5. market is active
    6. token_id is valid (non-empty)
    7. market snapshot is recent enough
    """

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
            import time
            snapshot_age = time.time() - market_snapshot.timestamp
            if snapshot_age > 10.0:
                return self._reject(
                    order,
                    f"Market snapshot is {snapshot_age:.0f}s old — too stale for live execution"
                )

        # All gates passed — submit order
        log.warning(
            f"[LIVE] Submitting order: {order.direction} {order.market_type} "
            f"${order.size_usdc:.2f} @{order.price:.3f} token={order.token_id[:20]}..."
        )

        # TODO: Actual CLOB order submission via py-clob-client
        # For now, mark as failed with clear reason
        order.status = "FAILED"
        order.metadata["rejection_reason"] = (
            "Live CLOB order submission not yet implemented. "
            "Use paper mode for testing."
        )
        log.error(
            "[LIVE] Order submission not yet implemented — "
            "install py-clob-client and complete live execution in Phase 5"
        )
        return order

    def check_order_status(self, order_id: str) -> str:
        """Check status of a live order. Not yet implemented."""
        log.warning(f"[LIVE] check_order_status not implemented for {order_id}")
        return "UNKNOWN"

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a live order. Not yet implemented."""
        log.warning(f"[LIVE] cancel_order not implemented for {order_id}")
        return False

    def _reject(self, order: Order, reason: str) -> Order:
        """Reject an order with a clear reason."""
        order.status = "REJECTED"
        order.metadata["rejection_reason"] = reason
        log.warning(f"[LIVE] REJECTED: {reason} | {order.summary()}")
        return order
