"""Market making strategy — stub for future implementation.

Will place bid/ask orders around model fair value and capture spread.
Continuously adjusts quotes based on BTC volatility, orderbook depth,
and time to resolution.

This is a pure computation function — no state, no side effects, no API calls.
"""

from __future__ import annotations

from typing import Optional

from models.trade_signal import TradeSignal


def evaluate(
    spot_price: float,
    strike_price: float,
    volatility: float,
    time_remaining: float,
    market_yes_price: float,
    market_no_price: float,
    spread: float,
    market_type: str,
    market_id: str,
    orderbook_bids: list = None,
    orderbook_asks: list = None,
) -> Optional[TradeSignal]:
    """Evaluate market making opportunity.

    TODO (Phase 5):
    - Calculate model fair value from probability model
    - Determine optimal bid/ask quotes around fair value
    - Assess orderbook depth and liquidity
    - Adjust spread based on volatility regime
    - Factor in time to resolution (tighten near expiry)
    - Generate paired bid/ask signals
    - Inventory management signals when position is skewed
    """
    return None
