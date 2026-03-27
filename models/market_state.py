"""Polymarket market snapshot data model."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Optional


@dataclass
class OrderLevel:
    """Single price level in an orderbook."""
    price: float
    size: float


@dataclass
class MarketState:
    """Normalized snapshot of a Polymarket BTC up/down prediction market.

    Represents the current state of a single 5-minute or 15-minute market,
    including pricing, orderbook depth, and timing information.

    Pricing convention:
    - yes_price / no_price correspond to Up / Down outcomes respectively.
    - For BTC up/down markets: "Up" = BTC finishes >= strike, "Down" = BTC finishes < strike.
    - Prices are in [0, 1] range representing probability/cost per share.
    """

    market_id: str               # Gamma API numeric ID
    condition_id: str            # Hex condition ID for CLOB
    market_type: str             # "btc-5min" | "btc-15min"
    strike_price: float          # BTC reference price at market open
    yes_price: float             # Up outcome price (implied probability of up)
    no_price: float              # Down outcome price (implied probability of down)
    spread: float                # Best ask - best bid
    orderbook_bids: list[OrderLevel] = field(default_factory=list)  # Top bids (Up token)
    orderbook_asks: list[OrderLevel] = field(default_factory=list)  # Top asks (Up token)
    time_remaining_seconds: float = 0.0
    # Gamma API endDate can be far in the future; kept for diagnostics only.
    gamma_end_remaining_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    is_active: bool = True

    # Token IDs for CLOB queries
    up_token_id: str = ""
    down_token_id: str = ""

    # Raw metadata
    question: str = ""
    end_date: str = ""           # ISO datetime string — when market resolves
    event_start_date: str = ""   # ISO datetime string — when BTC observation window opens
    slug: str = ""
    window_started: bool = False  # True if current period's observation window has started
    is_signalable: bool = False   # True if market has valid data for signal evaluation
    time_to_window_seconds: float = 0.0  # Seconds until observation window opens
    # How window_started / time_remaining were derived (slug is authoritative for btc-updown-*)
    timing_source: str = ""       # set in feeds/polymarket: slug_period | gamma_end_date | none

    # Strike confirmation state
    strike_status: str = "waiting"       # "waiting" | "confirmed" | "timeout"
    strike_source: str = "spot_approx"   # "spot_approx" | "prev_finalPrice" | "oracle"
    strike_confirmed_at: float = 0.0     # timestamp when strike was confirmed (0 = not confirmed)

    def implied_up_probability(self) -> float:
        """Implied probability of BTC finishing up (at or above strike).

        Uses the Up outcome price directly as the market-implied probability.
        Note: yes_price + no_price may not sum to 1.0 due to spread/fees.
        """
        return self.yes_price

    def implied_down_probability(self) -> float:
        """Implied probability of BTC finishing down (below strike)."""
        return self.no_price

    def midpoint(self) -> float:
        """Midpoint between best bid and best ask for the Up token."""
        if self.orderbook_bids and self.orderbook_asks:
            best_bid = self.orderbook_bids[0].price
            best_ask = self.orderbook_asks[0].price
            return (best_bid + best_ask) / 2
        return self.yes_price

    def is_near_resolution(self, threshold_seconds: float = 20.0) -> bool:
        """True if market is within threshold_seconds of resolution."""
        return 0 < self.time_remaining_seconds <= threshold_seconds

    def __repr__(self) -> str:
        remaining = f"{self.time_remaining_seconds:.0f}s"
        active_tag = "" if self.is_active else " [CLOSED]"
        return (
            f"MarketState({self.market_type} "
            f"Up={self.yes_price:.3f} Down={self.no_price:.3f} "
            f"spread={self.spread:.3f} "
            f"remaining={remaining}{active_tag})"
        )
