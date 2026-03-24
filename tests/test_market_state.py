"""Tests for MarketState model and Polymarket feed helpers."""

from __future__ import annotations

import time
import unittest

from models.market_state import MarketState, OrderLevel
from feeds.polymarket import PolymarketFeed


class TestMarketState(unittest.TestCase):
    """Tests for the MarketState data model."""

    def _make_state(self, **overrides) -> MarketState:
        defaults = dict(
            market_id="12345",
            condition_id="0xabc123",
            market_type="btc-5min",
            strike_price=68000.0,
            yes_price=0.55,
            no_price=0.47,
            spread=0.02,
            orderbook_bids=[
                OrderLevel(price=0.54, size=100),
                OrderLevel(price=0.53, size=200),
            ],
            orderbook_asks=[
                OrderLevel(price=0.56, size=100),
                OrderLevel(price=0.57, size=200),
            ],
            time_remaining_seconds=120.0,
            timestamp=time.time(),
            is_active=True,
        )
        defaults.update(overrides)
        return MarketState(**defaults)

    def test_implied_probabilities(self):
        state = self._make_state(yes_price=0.60, no_price=0.42)
        self.assertAlmostEqual(state.implied_up_probability(), 0.60)
        self.assertAlmostEqual(state.implied_down_probability(), 0.42)

    def test_midpoint_from_orderbook(self):
        state = self._make_state()
        # Best bid=0.54, best ask=0.56 → midpoint=0.55
        self.assertAlmostEqual(state.midpoint(), 0.55)

    def test_midpoint_fallback_no_orderbook(self):
        state = self._make_state(orderbook_bids=[], orderbook_asks=[])
        # Falls back to yes_price
        self.assertAlmostEqual(state.midpoint(), state.yes_price)

    def test_spread_calculation(self):
        state = self._make_state(spread=0.03)
        self.assertAlmostEqual(state.spread, 0.03)

    def test_near_resolution_true(self):
        state = self._make_state(time_remaining_seconds=15.0)
        self.assertTrue(state.is_near_resolution(threshold_seconds=20.0))

    def test_near_resolution_false(self):
        state = self._make_state(time_remaining_seconds=120.0)
        self.assertFalse(state.is_near_resolution(threshold_seconds=20.0))

    def test_near_resolution_at_zero(self):
        """time_remaining=0 means resolved, not near-resolution."""
        state = self._make_state(time_remaining_seconds=0.0)
        self.assertFalse(state.is_near_resolution())

    def test_repr(self):
        state = self._make_state()
        r = repr(state)
        self.assertIn("btc-5min", r)
        self.assertIn("Up=", r)
        self.assertIn("Down=", r)

    def test_prices_do_not_need_to_sum_to_one(self):
        """YES + NO may not sum to 1.0 due to spread — this is expected."""
        state = self._make_state(yes_price=0.55, no_price=0.47)
        total = state.yes_price + state.no_price
        self.assertNotAlmostEqual(total, 1.0)  # 1.02 — spread included


class TestPolymarketFeedHelpers(unittest.TestCase):
    """Tests for PolymarketFeed static helper methods."""

    def test_compute_time_remaining_future(self):
        """Future end date should return positive seconds."""
        # Use a date far in the future
        remaining = PolymarketFeed._compute_time_remaining("2099-01-01T00:00:00Z")
        self.assertGreater(remaining, 0)

    def test_compute_time_remaining_past(self):
        """Past end date should return 0."""
        remaining = PolymarketFeed._compute_time_remaining("2020-01-01T00:00:00Z")
        self.assertEqual(remaining, 0.0)

    def test_compute_time_remaining_empty(self):
        remaining = PolymarketFeed._compute_time_remaining("")
        self.assertEqual(remaining, 0.0)

    def test_compute_time_remaining_invalid(self):
        remaining = PolymarketFeed._compute_time_remaining("not-a-date")
        self.assertEqual(remaining, 0.0)

    def test_parse_strike_price_legacy(self):
        """Legacy parser returns 0.0 — strike is now captured from BTC spot."""
        price = PolymarketFeed._parse_strike_price({})
        self.assertEqual(price, 0.0)

    def test_spot_price_injection(self):
        """Strike comes from set_spot_price(), not description parsing."""
        feed = PolymarketFeed()
        feed.set_spot_price(68500.0)
        self.assertEqual(feed._latest_spot_price, 68500.0)


class TestPolymarketFeedState(unittest.TestCase):
    """Tests for PolymarketFeed internal state management."""

    def test_initial_state_empty(self):
        feed = PolymarketFeed()
        self.assertEqual(feed.get_active_markets(), {})
        self.assertIsNone(feed.get_market_state("btc-5min"))
        self.assertIsNone(feed.get_market_price("btc-5min"))
        self.assertIsNone(feed.get_orderbook("btc-5min"))
        self.assertIsNone(feed.get_time_remaining("btc-5min"))
        self.assertIsNone(feed.get_strike_price("btc-5min"))

    def test_no_crash_on_missing_market(self):
        """Querying a non-existent market should return None, not crash."""
        feed = PolymarketFeed()
        self.assertIsNone(feed.get_market_state("btc-99min"))


if __name__ == "__main__":
    unittest.main()
