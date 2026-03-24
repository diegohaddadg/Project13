"""Paper trading execution — simulates fills without real orders.

Sim modes:
  "baseline" (default) — conservative taker fill with slippage, fee = ESTIMATED_FEE_PCT.
    Same behavior as prior baseline. No synthetic maker simulation.

  "maker_first_experimental" — synthetic maker-fill simulation.
    Not grounded in measured execution data. Use only for experimental analysis.

All actions tagged [PAPER] in logs with execution metadata.
"""

from __future__ import annotations

import random
import time
from typing import Optional

from models.order import Order
from models.market_state import MarketState
from utils.logger import get_logger
import config

log = get_logger("paper_trader")


class PaperTrader:
    """Simulates order execution for paper trading."""

    def execute(self, order: Order, market_snapshot: Optional[MarketState] = None) -> Order:
        """Simulate order fill based on configured sim mode."""
        order.execution_mode = "paper"
        order.status = "SUBMITTED"

        base_price = order.price
        if market_snapshot:
            base_price = market_snapshot.yes_price if order.direction == "UP" else market_snapshot.no_price

        sim = config.PAPER_EXECUTION_SIM_MODE

        if sim == "maker_first_experimental":
            return self._execute_maker_first(order, base_price, market_snapshot)
        else:
            return self._execute_baseline(order, base_price, market_snapshot)

    def _execute_baseline(self, order: Order, base_price: float, snapshot) -> Order:
        """Conservative taker fill: slippage + standard fee assumption."""
        slippage = base_price * config.PAPER_SLIPPAGE_PCT
        fill_price = min(base_price + slippage, 0.99)

        if fill_price > 0:
            order.num_shares = round(order.size_usdc / fill_price, 2)

        order.fill_price = fill_price
        order.fill_timestamp = time.time() + (config.PAPER_SIMULATED_LATENCY_MS / 1000.0)
        order.status = "FILLED"

        order.metadata["execution_path"] = "baseline_taker"
        order.metadata["fee_mode"] = "taker"
        order.metadata["estimated_fee_pct"] = config.ESTIMATED_FEE_PCT
        order.metadata["post_only_attempted"] = False
        order.metadata["fallback_reason"] = None
        order.metadata["sim_mode"] = "baseline"

        log.info(
            f"[PAPER] FILLED [BASELINE]: {order.direction} {order.market_type} "
            f"${order.size_usdc:.2f} @{fill_price:.3f} "
            f"({order.num_shares:.1f} shares) "
            f"fee={config.ESTIMATED_FEE_PCT:.3f} "
            f"strategy={order.metadata.get('strategy', '?')}"
        )
        return order

    def _execute_maker_first(self, order: Order, base_price: float, snapshot) -> Order:
        """Experimental maker-first simulation. Not default."""
        maker_resting_price = base_price

        if random.random() < config.PAPER_MAKER_FILL_PROB:
            fill_price = base_price
            execution_path = "maker_experimental"
            fee_mode = "maker"
            fee_pct = config.MAKER_FEE_PCT
            fallback_reason = None
        elif config.ALLOW_TAKER_FALLBACK:
            slippage = base_price * config.PAPER_SLIPPAGE_PCT
            fill_price = min(base_price + slippage, 0.99)
            execution_path = "taker_fallback_experimental"
            fee_mode = "taker"
            fee_pct = config.TAKER_FEE_PCT
            fallback_reason = "maker_fill_prob_miss"
        else:
            order.status = "CANCELLED"
            order.metadata["execution_path"] = "maker_cancelled_experimental"
            order.metadata["sim_mode"] = "maker_first_experimental"
            return order

        if fill_price > 0:
            order.num_shares = round(order.size_usdc / fill_price, 2)

        order.fill_price = fill_price
        order.fill_timestamp = time.time() + (config.PAPER_SIMULATED_LATENCY_MS / 1000.0)
        order.status = "FILLED"

        order.metadata["execution_path"] = execution_path
        order.metadata["post_only_attempted"] = True
        order.metadata["maker_resting_price"] = maker_resting_price
        order.metadata["fee_mode"] = fee_mode
        order.metadata["estimated_fee_pct"] = fee_pct
        order.metadata["fallback_reason"] = fallback_reason
        order.metadata["sim_mode"] = "maker_first_experimental"

        log.info(
            f"[PAPER] FILLED [{execution_path.upper()}]: {order.direction} {order.market_type} "
            f"${order.size_usdc:.2f} @{fill_price:.3f} "
            f"({order.num_shares:.1f} shares) "
            f"fee_mode={fee_mode} fee={fee_pct:.3f} "
            f"strategy={order.metadata.get('strategy', '?')}"
        )
        return order

    def simulate_resolution(self, order: Order, resolved_direction: str) -> Order:
        """Simulate market resolution and compute PnL."""
        if order.fill_price is None:
            return order

        won = order.direction == resolved_direction
        payout_per_share = 1.0 if won else 0.0
        order.pnl = (payout_per_share - order.fill_price) * order.num_shares

        result = "WIN" if won else "LOSS"
        log.info(
            f"[PAPER] RESOLVED: {result} {order.direction} {order.market_type} "
            f"PnL={order.pnl:+.2f} "
            f"(entry={order.fill_price:.3f}, payout={payout_per_share:.1f})"
        )
        return order
