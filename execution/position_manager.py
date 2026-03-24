"""Position and capital tracking.

Accounting identity (paper):
- Starting cash: config.STARTING_CAPITAL_USDC
- Free capital (available cash): cash not tied up in open positions — after each fill,
  subtract order.size_usdc; on resolution, add payout = resolution_price * num_shares.
- Deployed capital (open positions): sum(entry_price * num_shares) for OPEN positions
  (cost basis at entry; not mark-to-market).
- Total equity: free_capital + deployed_capital
  (= starting_capital + sum(realized PnL from closed positions); see get_total_equity).

Unrealized PnL is not modeled; open positions are carried at entry cost in equity.
"""

from __future__ import annotations

from typing import Optional

from models.order import Order
from models.position import Position
from utils.logger import get_logger
import config

log = get_logger("position_mgr")


def _accounting_log(phase: str, **kwargs) -> None:
    """Temporary validation trail; prefix [ACCOUNTING] for grep."""
    parts = " ".join(f"{k}={v}" for k, v in kwargs.items())
    log.info(f"[ACCOUNTING] {phase} | {parts}")
    # region agent log
    try:
        import json
        import time as _time
        from pathlib import Path

        _row = {
            "sessionId": "16560d",
            "hypothesisId": "ACC",
            "location": "position_manager._accounting_log",
            "message": phase,
            "data": dict(kwargs),
            "timestamp": int(_time.time() * 1000),
        }
        Path("/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log").open("a").write(
            json.dumps(_row) + "\n"
        )
    except Exception:
        pass
    # endregion


class PositionManager:
    """Tracks positions, capital, and cumulative execution statistics."""

    def __init__(self):
        self._open_positions: list[Position] = []
        self._closed_positions: list[Position] = []
        self._available_capital: float = config.STARTING_CAPITAL_USDC
        _accounting_log(
            "init",
            starting=config.STARTING_CAPITAL_USDC,
            free=self._available_capital,
            deployed=0.0,
            equity=self._available_capital,
        )

    # --- Capital ---

    def get_available_capital(self) -> float:
        return self._available_capital

    def set_capital(self, amount: float) -> None:
        """Set capital directly (used when restoring from trade log)."""
        self._available_capital = amount

    def has_sufficient_capital(self, size_usdc: float) -> bool:
        return size_usdc <= self._available_capital

    # --- Positions ---

    def open_position(self, order: Order) -> Position:
        """Create a position from a filled order. Deducts capital."""
        pos = Position(
            order_id=order.order_id,
            signal_id=order.signal_id,
            market_id=order.market_id,
            market_type=order.market_type,
            direction=order.direction,
            entry_price=order.fill_price or order.price,
            num_shares=order.num_shares,
            entry_timestamp=order.fill_timestamp or order.timestamp,
            status="OPEN",
            metadata={
                "execution_mode": order.execution_mode,
                "strike": order.metadata.get("strike", 0),
                "strategy": order.metadata.get("strategy", ""),
            },
        )
        self._open_positions.append(pos)
        self._available_capital -= order.size_usdc
        dep = sum(p.entry_price * p.num_shares for p in self._open_positions)
        _accounting_log(
            "after_fill",
            free=self._available_capital,
            deployed=round(dep, 4),
            equity=round(self.get_total_equity(), 4),
            order_usdc=order.size_usdc,
        )
        log.info(
            f"[{order.execution_mode.upper()}] Position opened: {pos.summary()} "
            f"| Capital: ${self._available_capital:.2f}"
        )
        return pos

    def restore_open_position_from_order(self, order: Order) -> Position:
        """Reconstruct an open position from a persisted FILLED order (no capital change)."""
        pos = Position(
            order_id=order.order_id,
            signal_id=order.signal_id,
            market_id=order.market_id,
            market_type=order.market_type,
            direction=order.direction,
            entry_price=order.fill_price or order.price,
            num_shares=order.num_shares,
            entry_timestamp=order.fill_timestamp or order.timestamp,
            status="OPEN",
            metadata={
                "execution_mode": order.execution_mode,
                "strike": order.metadata.get("strike", 0),
                "strategy": order.metadata.get("strategy", ""),
            },
        )
        self._open_positions.append(pos)
        log.info(f"[RESTORE] Open position restored from trade log: {pos.summary()}")
        return pos

    def close_position(self, position_id: str, resolution_price: float) -> Optional[Position]:
        """Close a position with resolution outcome. Returns capital + payout."""
        pos = None
        for p in self._open_positions:
            if p.position_id == position_id:
                pos = p
                break

        if pos is None:
            log.warning(f"Position {position_id} not found in open positions")
            return None

        pos.pnl = pos.calculate_pnl(resolution_price)
        pos.resolution_price = resolution_price
        pos.status = "RESOLVED"

        # Return capital: entry cost + PnL
        payout = resolution_price * pos.num_shares
        self._available_capital += payout

        self._open_positions.remove(pos)
        self._closed_positions.append(pos)

        result = "WIN" if pos.pnl > 0 else "LOSS"
        mode = pos.metadata.get("execution_mode", "paper").upper()
        dep_after = sum(p.entry_price * p.num_shares for p in self._open_positions)
        _accounting_log(
            "after_resolution",
            free=self._available_capital,
            deployed=round(dep_after, 4),
            equity=round(self.get_total_equity(), 4),
            pnl=round(pos.pnl or 0.0, 4),
            payout=round(payout, 4),
            realized_cumulative=round(self.get_total_pnl(), 4),
        )
        log.info(
            f"[{mode}] Position resolved: {result} {pos.summary()} "
            f"| Capital: ${self._available_capital:.2f}"
        )
        return pos

    def get_open_positions(self) -> list[Position]:
        return list(self._open_positions)

    def get_closed_positions(self) -> list[Position]:
        return list(self._closed_positions)

    def count_positions_for_market(self, market_id: str) -> int:
        return sum(1 for p in self._open_positions if p.market_id == market_id)

    def count_open_positions(self) -> int:
        return len(self._open_positions)

    def get_total_equity(self) -> float:
        """Total equity = available capital + value of open positions at entry price.

        This represents the total portfolio value. Open positions are valued
        at entry cost (conservative — actual value depends on market outcome).
        """
        open_value = sum(p.entry_price * p.num_shares for p in self._open_positions)
        return self._available_capital + open_value

    # --- Statistics ---

    def get_total_pnl(self) -> float:
        return sum(p.pnl for p in self._closed_positions if p.pnl is not None)

    def get_win_rate(self) -> float:
        resolved = [p for p in self._closed_positions if p.pnl is not None]
        if not resolved:
            return 0.0
        wins = sum(1 for p in resolved if p.pnl > 0)
        return wins / len(resolved)

    def get_stats(self) -> dict:
        resolved = [p for p in self._closed_positions if p.pnl is not None]
        pnls = [p.pnl for p in resolved]
        return {
            "total_trades": len(resolved),
            "wins": sum(1 for pnl in pnls if pnl > 0),
            "losses": sum(1 for pnl in pnls if pnl <= 0),
            "win_rate": self.get_win_rate(),
            "total_pnl": self.get_total_pnl(),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "best_trade": max(pnls) if pnls else 0.0,
            "worst_trade": min(pnls) if pnls else 0.0,
            "open_positions": self.count_open_positions(),
            "available_capital": self._available_capital,
        }
