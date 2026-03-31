"""Tests for latency_arb strict 1-entry-per-window rule.

Verifies that latency_arb can only open one position per market window,
while sniper remains unaffected and direction-conflict protection is preserved.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from models.trade_signal import TradeSignal
from models.market_state import MarketState
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
import config


def _make_signal(
    market_id="mkt_A",
    direction="UP",
    strategy="latency_arb",
    edge=0.20,
):
    return TradeSignal(
        market_type="btc-5min",
        market_id=market_id,
        strategy=strategy,
        direction=direction,
        model_probability=0.70,
        market_probability=0.50,
        edge=edge,
        net_ev=0.10,
        confidence="HIGH",
        recommended_size_pct=0.10,
        strike_price=68000,
        spot_price=68200,
        time_remaining=120,
    )


def _make_snapshot(market_id="mkt_A"):
    return MarketState(
        market_id=market_id,
        condition_id="0xcond",
        market_type="btc-5min",
        strike_price=68000,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        time_remaining_seconds=120,
        is_active=True,
        up_token_id="1" * 70,
        down_token_id="2" * 70,
    )


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = config.TRADE_LOG_PATH
        self._orig_mode = config.EXECUTION_MODE
        config.TRADE_LOG_PATH = os.path.join(self._tmpdir.name, "test.jsonl")
        config.EXECUTION_MODE = "paper"

    def tearDown(self):
        config.TRADE_LOG_PATH = self._orig_path
        config.EXECUTION_MODE = self._orig_mode
        self._tmpdir.cleanup()


class TestLatencyArbSingleEntry(_Isolated):
    """latency_arb is limited to 1 entry per market window."""

    def test_first_entry_allowed(self):
        """First latency_arb entry in a window succeeds."""
        pm = PositionManager()
        om = OrderManager(pm)
        sig = _make_signal(strategy="latency_arb")
        snap = _make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "FILLED")

    def test_second_entry_blocked(self):
        """Second latency_arb entry in same window is rejected."""
        pm = PositionManager()
        om = OrderManager(pm)
        snap = _make_snapshot()

        sig1 = _make_signal(strategy="latency_arb")
        order1 = om.execute_signal(sig1, snap)
        self.assertIsNotNone(order1)
        self.assertEqual(order1.status, "FILLED")

        # Second attempt — same market, same direction
        sig2 = _make_signal(strategy="latency_arb")
        sig2.timestamp = time.time()  # fresh signal
        order2 = om.execute_signal(sig2, snap)
        self.assertIsNone(order2)

    def test_opposite_direction_second_entry_blocked(self):
        """Opposite-direction latency_arb entry in same window is also rejected.

        The direction_conflict check fires BEFORE the single-entry check,
        so opposite direction is blocked regardless.
        """
        pm = PositionManager()
        om = OrderManager(pm)
        snap = _make_snapshot()

        sig1 = _make_signal(strategy="latency_arb", direction="UP")
        order1 = om.execute_signal(sig1, snap)
        self.assertIsNotNone(order1)

        sig2 = _make_signal(strategy="latency_arb", direction="DOWN")
        sig2.timestamp = time.time()
        order2 = om.execute_signal(sig2, snap)
        self.assertIsNone(order2)

    def test_sniper_unaffected_by_latency_arb_limit(self):
        """Sniper can still enter a window where latency_arb already has a position."""
        pm = PositionManager()
        om = OrderManager(pm)
        snap = _make_snapshot()

        # latency_arb enters first
        sig_la = _make_signal(strategy="latency_arb", direction="UP")
        order_la = om.execute_signal(sig_la, snap)
        self.assertIsNotNone(order_la)

        # sniper enters same window, same direction — should be allowed
        sig_sn = _make_signal(strategy="sniper", direction="UP")
        sig_sn.timestamp = time.time()
        order_sn = om.execute_signal(sig_sn, snap)
        self.assertIsNotNone(order_sn)
        self.assertEqual(order_sn.status, "FILLED")

    def test_sniper_blocked_by_direction_conflict(self):
        """Sniper is still blocked by direction conflict (not by latency_arb rule)."""
        pm = PositionManager()
        om = OrderManager(pm)
        snap = _make_snapshot()

        sig_la = _make_signal(strategy="latency_arb", direction="UP")
        order_la = om.execute_signal(sig_la, snap)
        self.assertIsNotNone(order_la)

        # sniper opposite direction — blocked by direction_conflict, not by lat_arb rule
        sig_sn = _make_signal(strategy="sniper", direction="DOWN")
        sig_sn.timestamp = time.time()
        order_sn = om.execute_signal(sig_sn, snap)
        self.assertIsNone(order_sn)

    def test_different_window_allowed(self):
        """latency_arb can enter a different market window even if one already has a position."""
        pm = PositionManager()
        om = OrderManager(pm)

        snap_a = _make_snapshot(market_id="mkt_A")
        sig_a = _make_signal(strategy="latency_arb", market_id="mkt_A")
        order_a = om.execute_signal(sig_a, snap_a)
        self.assertIsNotNone(order_a)

        snap_b = _make_snapshot(market_id="mkt_B")
        sig_b = _make_signal(strategy="latency_arb", market_id="mkt_B")
        sig_b.timestamp = time.time()
        order_b = om.execute_signal(sig_b, snap_b)
        self.assertIsNotNone(order_b)
        self.assertEqual(order_b.status, "FILLED")


if __name__ == "__main__":
    unittest.main()
