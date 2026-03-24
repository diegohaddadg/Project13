"""Order data model for execution tracking."""

from __future__ import annotations

import uuid
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Order:
    """Represents an order through its full lifecycle.

    Token mapping (BTC up/down markets):
    - direction "UP" → buy Up token (clobTokenIds[0], yes_price side)
    - direction "DOWN" → buy Down token (clobTokenIds[1], no_price side)

    PnL mechanics:
    - Winning share resolves to 1.0 USDC payout
    - Losing share resolves to 0.0 USDC payout
    - PnL = (payout_per_share - fill_price) * num_shares
    """

    order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    signal_id: str = ""
    timestamp: float = field(default_factory=time.time)
    market_id: str = ""
    market_type: str = ""          # "btc-5min" | "btc-15min"
    direction: str = ""            # "UP" | "DOWN"
    side: str = "BUY"
    token_id: str = ""             # Polymarket token ID for the chosen outcome
    price: float = 0.0             # Intended limit price
    size_usdc: float = 0.0         # Total USDC commitment
    num_shares: float = 0.0        # size_usdc / price (rounded)
    order_type: str = "LIMIT"      # "LIMIT" | "MARKET"
    status: str = "PENDING"        # PENDING|SUBMITTED|FILLED|PARTIAL|CANCELLED|FAILED|REJECTED
    fill_price: Optional[float] = None
    fill_timestamp: Optional[float] = None
    pnl: Optional[float] = None
    execution_mode: str = "paper"  # "paper" | "live"
    metadata: dict = field(default_factory=dict)

    def is_complete(self) -> bool:
        """True if the order has reached a terminal state."""
        return self.status in ("FILLED", "CANCELLED", "FAILED", "REJECTED")

    def was_profitable(self) -> Optional[bool]:
        """True if PnL > 0, False if <= 0, None if not yet resolved."""
        if self.pnl is None:
            return None
        return self.pnl > 0

    def fill_latency_ms(self) -> Optional[float]:
        """Milliseconds between order creation and fill."""
        if self.fill_timestamp is None:
            return None
        return (self.fill_timestamp - self.timestamp) * 1000

    def to_dict(self) -> dict:
        """Serialize to dict for JSON logging."""
        return {
            "order_id": self.order_id,
            "signal_id": self.signal_id,
            "timestamp": self.timestamp,
            "market_id": self.market_id,
            "market_type": self.market_type,
            "direction": self.direction,
            "side": self.side,
            "token_id": self.token_id,
            "price": self.price,
            "size_usdc": self.size_usdc,
            "num_shares": self.num_shares,
            "order_type": self.order_type,
            "status": self.status,
            "fill_price": self.fill_price,
            "fill_timestamp": self.fill_timestamp,
            "pnl": self.pnl,
            "execution_mode": self.execution_mode,
            "metadata": self.metadata,
        }

    def summary(self) -> str:
        """One-line readable summary."""
        mode_tag = "[PAPER]" if self.execution_mode == "paper" else "[LIVE]"
        pnl_str = f" PnL={self.pnl:+.2f}" if self.pnl is not None else ""
        return (
            f"{mode_tag} {self.direction} {self.market_type} "
            f"${self.size_usdc:.2f} @{self.price:.3f} "
            f"status={self.status}{pnl_str}"
        )

    def __repr__(self) -> str:
        return f"Order({self.order_id} {self.summary()})"
