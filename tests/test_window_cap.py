"""Tests for exact-window entry cap in live mode.

Verifies that LIVE (pending) orders count toward MAX_ENTRIES_PER_WINDOW
so the bot cannot overstack in the same market window while fills are pending.
"""

from __future__ import annotations

import os
import time
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from models.order import Order
from models.trade_signal import TradeSignal
from models.market_state import MarketState
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
import config


def _make_signal(market_id="mkt_window_A", direction="UP"):
    return TradeSignal(
        market_type="btc-5min", market_id=market_id, strategy="latency_arb",
        direction=direction, model_probability=0.70, market_probability=0.50,
        edge=0.20, net_ev=0.10, confidence="HIGH",
        recommended_size_pct=0.10, strike_price=68000, spot_price=68200,
        time_remaining=120,
    )


def _make_snapshot(market_id="mkt_window_A"):
    return MarketState(
        market_id=market_id, condition_id="0xcond", market_type="btc-5min",
        strike_price=68000, yes_price=0.50, no_price=0.50, spread=0.02,
        time_remaining_seconds=120, is_active=True,
        up_token_id="1" * 70, down_token_id="2" * 70,
    )


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = config.TRADE_LOG_PATH
        self._orig_mode = config.EXECUTION_MODE
        config.TRADE_LOG_PATH = os.path.join(self._tmpdir.name, "test.jsonl")

    def tearDown(self):
        config.TRADE_LOG_PATH = self._orig_path
        config.EXECUTION_MODE = self._orig_mode
        self._tmpdir.cleanup()


class TestWindowCapLiveMode(_Isolated):
    """LIVE orders count toward the window cap."""

    def test_3_live_orders_blocks_4th(self):
        """3 LIVE orders in same window → 4th signal rejected."""
        config.EXECUTION_MODE = "live"
        pm = PositionManager()
        om = OrderManager(pm)

        # Simulate 3 accepted LIVE orders for same market window
        for i in range(3):
            order = Order(
                order_id=f"live_{i}", market_id="mkt_window_A",
                market_type="btc-5min", direction=["UP", "DOWN", "UP"][i],
                status="LIVE", execution_mode="live",
                price=0.55, size_usdc=10.0, num_shares=18.0,
                metadata={"exchange_order_id": f"0xex{i}"},
            )
            om._order_history.append(order)

        sig = _make_signal("mkt_window_A", "DOWN")
        snap = _make_snapshot("mkt_window_A")
        result = om._validate(sig, snap)

        self.assertIsNotNone(result)
        self.assertIn("Max entries per window", result)

    def test_2_live_orders_allows_3rd(self):
        """2 LIVE orders → 3rd signal allowed."""
        config.EXECUTION_MODE = "live"
        pm = PositionManager()
        om = OrderManager(pm)

        for i in range(2):
            order = Order(
                order_id=f"live_{i}", market_id="mkt_window_A",
                market_type="btc-5min", direction="UP",
                status="LIVE", execution_mode="live",
                price=0.55, size_usdc=10.0, num_shares=18.0,
                metadata={"exchange_order_id": f"0xex{i}"},
            )
            om._order_history.append(order)

        sig = _make_signal("mkt_window_A", "UP")
        snap = _make_snapshot("mkt_window_A")
        result = om._validate(sig, snap)

        # Should pass validation (2 < 3)
        self.assertIsNone(result)

    def test_mixed_filled_and_live_counts_correctly(self):
        """1 filled + 2 LIVE = 3 → 4th blocked."""
        config.EXECUTION_MODE = "live"
        pm = PositionManager()
        om = OrderManager(pm)

        # 1 filled position
        filled = Order(
            order_id="filled_1", market_id="mkt_window_A",
            market_type="btc-5min", direction="UP",
            status="FILLED", fill_price=0.55, execution_mode="live",
            price=0.55, size_usdc=10.0, num_shares=18.0,
        )
        pm.open_position(filled)

        # 2 LIVE orders
        for i in range(2):
            order = Order(
                order_id=f"live_{i}", market_id="mkt_window_A",
                market_type="btc-5min", direction="DOWN",
                status="LIVE", execution_mode="live",
                price=0.55, size_usdc=10.0, num_shares=18.0,
                metadata={"exchange_order_id": f"0xex{i}"},
            )
            om._order_history.append(order)

        sig = _make_signal("mkt_window_A", "UP")
        snap = _make_snapshot("mkt_window_A")
        result = om._validate(sig, snap)

        self.assertIsNotNone(result)
        self.assertIn("Max entries per window", result)

    def test_different_window_not_counted(self):
        """3 LIVE in window A → entry in window B still allowed."""
        config.EXECUTION_MODE = "live"
        pm = PositionManager()
        om = OrderManager(pm)

        for i in range(3):
            order = Order(
                order_id=f"live_{i}", market_id="mkt_window_A",
                market_type="btc-5min", direction="UP",
                status="LIVE", execution_mode="live",
                price=0.55, size_usdc=10.0, num_shares=18.0,
                metadata={"exchange_order_id": f"0xex{i}"},
            )
            om._order_history.append(order)

        sig = _make_signal("mkt_window_B", "UP")
        snap = _make_snapshot("mkt_window_B")
        result = om._validate(sig, snap)

        # Window B should be fine
        self.assertIsNone(result)


class TestWindowCapPaperMode(_Isolated):
    """Paper mode uses only filled positions for counting (unchanged)."""

    def test_paper_mode_ignores_live_orders(self):
        """In paper mode, LIVE orders don't exist — only filled positions count."""
        config.EXECUTION_MODE = "paper"
        pm = PositionManager()
        om = OrderManager(pm)

        # Even if somehow a LIVE order exists, paper mode doesn't count it
        for i in range(3):
            order = Order(
                order_id=f"live_{i}", market_id="mkt_window_A",
                market_type="btc-5min", direction="UP",
                status="LIVE", execution_mode="live",
                price=0.55, size_usdc=10.0, num_shares=18.0,
            )
            om._order_history.append(order)

        sig = _make_signal("mkt_window_A", "UP")
        snap = _make_snapshot("mkt_window_A")
        result = om._validate(sig, snap)

        # Paper mode: 0 filled positions → should pass
        self.assertIsNone(result)

    def test_paper_filled_positions_still_counted(self):
        """Paper mode still enforces cap via filled positions."""
        config.EXECUTION_MODE = "paper"
        pm = PositionManager()
        om = OrderManager(pm)

        for i in range(3):
            filled = Order(
                order_id=f"filled_{i}", market_id="mkt_window_A",
                market_type="btc-5min", direction="UP",
                status="FILLED", fill_price=0.55,
                price=0.55, size_usdc=10.0, num_shares=18.0,
            )
            pm.open_position(filled)

        sig = _make_signal("mkt_window_A", "UP")
        snap = _make_snapshot("mkt_window_A")
        result = om._validate(sig, snap)

        self.assertIsNotNone(result)
        self.assertIn("Max entries per window", result)


if __name__ == "__main__":
    unittest.main()
