"""Position data model for tracking open and resolved positions."""

from __future__ import annotations

import uuid
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    """Tracks a position from entry through resolution.

    PnL mechanics:
    - Winning outcome: each share pays out 1.0 USDC
    - Losing outcome: each share pays out 0.0 USDC
    - PnL = (resolution_price - entry_price) * num_shares
    """

    position_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    order_id: str = ""
    signal_id: str = ""
    market_id: str = ""
    market_type: str = ""
    direction: str = ""            # "UP" | "DOWN"
    entry_price: float = 0.0
    num_shares: float = 0.0
    entry_timestamp: float = field(default_factory=time.time)
    status: str = "OPEN"           # "OPEN" | "CLAIMABLE" | "RESOLVED" | "CLOSED"
    resolution_price: Optional[float] = None  # 1.0 (won) or 0.0 (lost)
    pnl: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def is_open(self) -> bool:
        return self.status == "OPEN"

    def calculate_pnl(self, resolution_price: float) -> float:
        """Calculate PnL based on resolution outcome.

        Args:
            resolution_price: 1.0 if the position's direction won, 0.0 if lost.
        """
        return (resolution_price - self.entry_price) * self.num_shares

    def hold_duration_seconds(self) -> float:
        """Seconds since position was opened."""
        return time.time() - self.entry_timestamp

    def to_dict(self) -> dict:
        """Serialize to dict for logging."""
        return {
            "position_id": self.position_id,
            "order_id": self.order_id,
            "signal_id": self.signal_id,
            "market_id": self.market_id,
            "market_type": self.market_type,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "num_shares": self.num_shares,
            "entry_timestamp": self.entry_timestamp,
            "status": self.status,
            "resolution_price": self.resolution_price,
            "pnl": self.pnl,
        }

    def summary(self) -> str:
        pnl_str = f" PnL={self.pnl:+.2f}" if self.pnl is not None else ""
        return (
            f"{self.direction} {self.market_type} "
            f"{self.num_shares:.1f}sh @{self.entry_price:.3f} "
            f"status={self.status}{pnl_str}"
        )

    def __repr__(self) -> str:
        return f"Position({self.position_id} {self.summary()})"
