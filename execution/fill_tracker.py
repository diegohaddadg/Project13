"""Fill tracker — monitors market resolution and closes positions."""

from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from models.position import Position

if TYPE_CHECKING:
    from execution.order_manager import OrderManager
from models.market_state import MarketState
from execution.position_manager import PositionManager
from feeds.aggregator import Aggregator
from utils.logger import get_logger
import config

log = get_logger("fill_tracker")

# Paper mode resolution timeouts by market type
PAPER_RESOLUTION_TIMEOUT = {
    "btc-5min": 300,    # 5 minutes
    "btc-15min": 900,   # 15 minutes
}
DEFAULT_TIMEOUT = 600   # 10 minutes for unknown types


class FillTracker:
    """Monitors open positions and closes them when markets resolve."""

    def __init__(
        self,
        position_manager: PositionManager,
        aggregator: Aggregator,
        order_manager: Optional["OrderManager"] = None,
    ):
        self._pm = position_manager
        self._agg = aggregator
        self._om = order_manager

    def check_resolutions(
        self,
        market_state_5m: Optional[MarketState],
        market_state_15m: Optional[MarketState],
    ) -> list[Position]:
        """Check if any open positions' markets should be resolved."""
        closed = []
        # Iterate over a copy since we may modify the list
        for pos in list(self._pm.get_open_positions()):
            if pos.market_type == "btc-5min":
                state = market_state_5m
            elif pos.market_type == "btc-15min":
                state = market_state_15m
            else:
                continue

            resolution = self._check_position_resolution(pos, state)
            if resolution is not None:
                resolved_pos = self._pm.close_position(pos.position_id, resolution)
                if resolved_pos:
                    closed.append(resolved_pos)
                    self._log_resolution(resolved_pos)
                    if (
                        self._om
                        and resolved_pos.order_id
                        and resolved_pos.pnl is not None
                    ):
                        self._om.sync_order_pnl_from_position(
                            resolved_pos.order_id, resolved_pos.pnl
                        )

        return closed

    def _check_position_resolution(
        self, pos: Position, state: Optional[MarketState]
    ) -> Optional[float]:
        """Determine if a position's market has resolved.

        Resolution triggers (paper mode):
        1. Market cycled (different market_id active now)
        2. Market time expired (time_remaining <= 0)
        3. Paper timeout (held longer than the market window duration)

        All use spot vs strike for outcome determination.
        """
        spot = self._agg.get_model_spot_price()
        strike = pos.metadata.get("strike", 0)

        # Case 1: Market cycled — position's contract no longer tracked
        if state is not None and state.market_id != pos.market_id:
            return self._resolve_spot_vs_strike(pos, spot, strike, "market_cycled")

        # Case 2: Market expired
        if state is not None and state.time_remaining_seconds <= 0:
            # Use market's strike if position's is missing
            if strike <= 0:
                strike = state.strike_price
            return self._resolve_spot_vs_strike(pos, spot, strike, "market_expired")

        # Case 3: Paper timeout — position held past expected window duration
        timeout = PAPER_RESOLUTION_TIMEOUT.get(pos.market_type, DEFAULT_TIMEOUT)
        hold_time = pos.hold_duration_seconds()
        if hold_time > timeout:
            return self._resolve_spot_vs_strike(pos, spot, strike, f"timeout_{hold_time:.0f}s")

        return None

    def _resolve_spot_vs_strike(
        self, pos: Position, spot: Optional[float], strike: float, reason: str
    ) -> float:
        """Resolve using spot vs strike comparison."""
        if spot and strike > 0:
            won = (pos.direction == "UP" and spot >= strike) or \
                  (pos.direction == "DOWN" and spot < strike)
            result = 1.0 if won else 0.0
        else:
            result = 0.0  # Conservative if data missing

        outcome = "WIN" if result == 1.0 else "LOSS"
        log.info(
            f"Position resolved [{reason}]: {pos.position_id} "
            f"{pos.direction} {pos.market_type} → {outcome} "
            f"(spot=${spot:,.2f} vs strike=${strike:,.2f})" if spot and strike > 0 else
            f"Position resolved [{reason}]: {pos.position_id} → {outcome} (no price data)"
        )
        return result

    def _log_resolution(self, pos: Position) -> None:
        """Log resolved position to audit file."""
        try:
            entry = {
                "timestamp": time.time(),
                "position_id": pos.position_id,
                "market_type": pos.market_type,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "num_shares": pos.num_shares,
                "pnl": pos.pnl,
                "resolution_price": pos.resolution_price,
                "hold_seconds": pos.hold_duration_seconds(),
                "status": pos.status,
            }
            path = Path("logs/execution_consistency_audit.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(f"RESOLVED: {json.dumps(entry)}\n")
        except Exception:
            pass
