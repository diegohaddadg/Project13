"""Trade signal data model."""

from __future__ import annotations

import uuid
import time
from dataclasses import dataclass, field
from typing import Optional

import config


@dataclass
class TradeSignal:
    """Structured output from a strategy evaluation.

    Represents a potential trading opportunity with all context needed
    for downstream display and (in Phase 4) execution decisions.
    """

    signal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    market_type: str = ""          # "btc-5min" | "btc-15min"
    market_id: str = ""
    strategy: str = ""             # "latency_arb" | "sniper" | "market_maker"
    direction: str = "NONE"        # "UP" | "DOWN" | "NONE"
    model_probability: float = 0.0
    market_probability: float = 0.0
    edge: float = 0.0
    gross_ev: float = 0.0
    net_ev: float = 0.0
    estimated_costs: float = 0.0
    confidence: str = "LOW"        # "HIGH" | "MEDIUM" | "LOW"
    recommended_size_pct: float = 0.0
    strike_price: float = 0.0
    spot_price: float = 0.0
    time_remaining: float = 0.0
    metadata: dict = field(default_factory=dict)

    def is_actionable(self) -> bool:
        """True only if this signal should be considered for execution.

        Requires:
        - A directional opinion (not NONE)
        - Edge exceeding the configured minimum
        - Confidence above LOW
        """
        return (
            self.direction != "NONE"
            and self.edge > config.MIN_ACTIONABLE_EDGE
            and self.net_ev > config.MIN_NET_EV
            and self.confidence != "LOW"
        )

    def expected_value(self) -> float:
        """Rough expected value estimate: edge * recommended position size."""
        return self.edge * self.recommended_size_pct

    def summary(self) -> str:
        """One-line readable summary for dashboard/log display."""
        return (
            f"[{self.strategy}] {self.direction} {self.market_type} "
            f"edge={self.edge:+.3f} conf={self.confidence} "
            f"size={self.recommended_size_pct:.0%} "
            f"model={self.model_probability:.3f} mkt={self.market_probability:.3f}"
        )

    def __repr__(self) -> str:
        return f"TradeSignal({self.signal_id} {self.summary()})"
