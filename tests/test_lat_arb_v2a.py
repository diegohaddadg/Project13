"""Tests for latency_arb V2a live gates (disagreement cap + direction cooldown)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from models.trade_signal import TradeSignal
from models.market_state import MarketState
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
import config


def _make_signal(
    direction="UP",
    strategy="latency_arb",
    model_probability=0.70,
    market_probability=0.50,
    edge=0.20,
    market_id="mkt_A",
):
    return TradeSignal(
        market_type="btc-5min",
        market_id=market_id,
        strategy=strategy,
        direction=direction,
        model_probability=model_probability,
        market_probability=market_probability,
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


class TestV2aFlagOff(_Isolated):
    """When LAT_ARB_V2A_ENABLED=False, behavior is identical to current system."""

    @patch.object(config, "LAT_ARB_V2A_ENABLED", False)
    def test_high_disagreement_allowed_when_flag_off(self):
        pm = PositionManager()
        om = OrderManager(pm)
        # disagreement = 0.45 (exceeds v2a cap of 0.30)
        sig = _make_signal(model_probability=0.90, market_probability=0.45, edge=0.45)
        snap = _make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "FILLED")

    @patch.object(config, "LAT_ARB_V2A_ENABLED", False)
    def test_rapid_direction_flip_allowed_when_flag_off(self):
        pm = PositionManager()
        om = OrderManager(pm)
        snap_a = _make_snapshot(market_id="mkt_A")
        snap_b = _make_snapshot(market_id="mkt_B")

        sig_up = _make_signal(direction="UP", market_id="mkt_A")
        om.execute_signal(sig_up, snap_a)

        # Immediately flip to DOWN in a different window
        sig_down = _make_signal(direction="DOWN", market_id="mkt_B")
        sig_down.timestamp = time.time()
        order = om.execute_signal(sig_down, snap_b)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "FILLED")


class TestV2aDisagreementCap(_Isolated):
    """Rule B: reject latency_arb when disagreement > configured cap."""

    @patch.object(config, "LAT_ARB_V2A_ENABLED", True)
    @patch.object(config, "LAT_ARB_V2A_MAX_DISAGREEMENT", 0.30)
    def test_high_disagreement_blocked(self):
        pm = PositionManager()
        om = OrderManager(pm)
        # disagreement = |0.85 - 0.45| = 0.40 > 0.30
        sig = _make_signal(model_probability=0.85, market_probability=0.45, edge=0.40)
        snap = _make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNone(order)

    @patch.object(config, "LAT_ARB_V2A_ENABLED", True)
    @patch.object(config, "LAT_ARB_V2A_MAX_DISAGREEMENT", 0.30)
    def test_moderate_disagreement_allowed(self):
        pm = PositionManager()
        om = OrderManager(pm)
        # disagreement = |0.70 - 0.50| = 0.20 < 0.30
        sig = _make_signal(model_probability=0.70, market_probability=0.50, edge=0.20)
        snap = _make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "FILLED")

    @patch.object(config, "LAT_ARB_V2A_ENABLED", True)
    @patch.object(config, "LAT_ARB_V2A_MAX_DISAGREEMENT", 0.30)
    def test_disagreement_cap_only_applies_to_latency_arb(self):
        """If sniper were re-enabled, it would NOT be affected by v2a."""
        pm = PositionManager()
        om = OrderManager(pm)
        # High disagreement sniper signal — should pass v2a (not gated)
        sig = _make_signal(
            strategy="sniper",
            model_probability=0.90,
            market_probability=0.45,
            edge=0.45,
        )
        snap = _make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNotNone(order)


class TestV2aDirectionCooldown(_Isolated):
    """Rule C: opposite-direction cooldown for latency_arb."""

    @patch.object(config, "LAT_ARB_V2A_ENABLED", True)
    @patch.object(config, "LAT_ARB_V2A_DIRECTION_COOLDOWN_S", 120)
    def test_opposite_direction_blocked_within_cooldown(self):
        pm = PositionManager()
        om = OrderManager(pm)
        snap_a = _make_snapshot(market_id="mkt_A")
        snap_b = _make_snapshot(market_id="mkt_B")

        # Execute UP
        sig_up = _make_signal(direction="UP", market_id="mkt_A")
        order_up = om.execute_signal(sig_up, snap_a)
        self.assertIsNotNone(order_up)

        # Immediately try DOWN in different window — should be blocked
        sig_down = _make_signal(direction="DOWN", market_id="mkt_B")
        sig_down.timestamp = time.time()
        order_down = om.execute_signal(sig_down, snap_b)
        self.assertIsNone(order_down)

    @patch.object(config, "LAT_ARB_V2A_ENABLED", True)
    @patch.object(config, "LAT_ARB_V2A_DIRECTION_COOLDOWN_S", 0.01)
    def test_opposite_direction_allowed_after_cooldown(self):
        pm = PositionManager()
        om = OrderManager(pm)
        snap_a = _make_snapshot(market_id="mkt_A")
        snap_b = _make_snapshot(market_id="mkt_B")

        sig_up = _make_signal(direction="UP", market_id="mkt_A")
        om.execute_signal(sig_up, snap_a)

        time.sleep(0.02)  # cooldown expires

        sig_down = _make_signal(direction="DOWN", market_id="mkt_B")
        sig_down.timestamp = time.time()
        order_down = om.execute_signal(sig_down, snap_b)
        self.assertIsNotNone(order_down)
        self.assertEqual(order_down.status, "FILLED")

    @patch.object(config, "LAT_ARB_V2A_ENABLED", True)
    @patch.object(config, "LAT_ARB_V2A_DIRECTION_COOLDOWN_S", 120)
    def test_same_direction_not_blocked_by_cooldown(self):
        """Same direction in a different window is NOT blocked by direction cooldown.
        (It's still blocked by single-entry-per-window if same market_id.)"""
        pm = PositionManager()
        om = OrderManager(pm)
        snap_a = _make_snapshot(market_id="mkt_A")
        snap_b = _make_snapshot(market_id="mkt_B")

        sig1 = _make_signal(direction="UP", market_id="mkt_A")
        om.execute_signal(sig1, snap_a)

        # Same direction, different window — should be allowed
        sig2 = _make_signal(direction="UP", market_id="mkt_B")
        sig2.timestamp = time.time()
        order2 = om.execute_signal(sig2, snap_b)
        self.assertIsNotNone(order2)
        self.assertEqual(order2.status, "FILLED")


if __name__ == "__main__":
    unittest.main()
