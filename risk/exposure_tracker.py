"""Exposure tracking — monitors portfolio exposure against limits."""

from __future__ import annotations

from execution.position_manager import PositionManager
import config


class ExposureTracker:
    """Tracks real-time portfolio exposure against configured limits."""

    def __init__(self, position_manager: PositionManager):
        self._pm = position_manager

    def get_total_exposure(self) -> float:
        """Total USDC committed in open positions."""
        return sum(
            p.entry_price * p.num_shares
            for p in self._pm.get_open_positions()
        )

    def get_exposure_by_market(self, market_id: str) -> float:
        """USDC exposure for a specific market."""
        return sum(
            p.entry_price * p.num_shares
            for p in self._pm.get_open_positions()
            if p.market_id == market_id
        )

    def get_exposure_by_market_type(self, market_type: str) -> float:
        """USDC exposure for a market type (e.g. 'btc-5min')."""
        return sum(
            p.entry_price * p.num_shares
            for p in self._pm.get_open_positions()
            if p.market_type == market_type
        )

    def get_exposure_pct(self) -> float:
        """Total exposure as percentage of risk equity base."""
        equity = self._pm.get_risk_equity()
        if equity <= 0:
            return 0.0
        return self.get_total_exposure() / equity

    def get_available_capital(self) -> float:
        return self._pm.get_available_capital()

    def would_exceed_limits(
        self, new_order_size: float, market_id: str | None = None
    ) -> bool:
        """Check if a proposed order would breach exposure limits.

        Limits are computed against current total equity (not starting capital),
        so they scale naturally as the account grows.
        """
        equity = self._pm.get_risk_equity()

        # Total exposure check
        new_total = self.get_total_exposure() + new_order_size
        total_limit = equity * config.MAX_TOTAL_EXPOSURE_PCT
        if new_total > total_limit:
            return True

        # Per-market check
        if market_id:
            current_market = self.get_exposure_by_market(market_id)
            market_limit = equity * config.MAX_SINGLE_MARKET_EXPOSURE_PCT
            if current_market + new_order_size > market_limit:
                return True

        return False
