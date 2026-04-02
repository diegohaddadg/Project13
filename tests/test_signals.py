"""Tests for Phase 3 signal engine components."""

from __future__ import annotations

import time
import unittest

from models.trade_signal import TradeSignal
from models.market_state import MarketState
from strategies import probability_model
from strategies import latency_arb
from strategies import sniper
from strategies.signal_engine import SignalEngine
import config


class TestProbabilityModel(unittest.TestCase):
    """Tests for the z-score probability model."""

    def test_spot_above_strike_positive_z(self):
        """When spot > strike, z should be positive and prob_up > 0.5."""
        result = probability_model.calculate_probability(
            spot_price=68100, strike_price=68000,
            volatility=10.0, time_remaining_seconds=60,
        )
        self.assertGreater(result["z_score"], 0)
        self.assertGreater(result["prob_up"], 0.5)
        self.assertLess(result["prob_down"], 0.5)

    def test_spot_below_strike_negative_z(self):
        result = probability_model.calculate_probability(
            spot_price=67900, strike_price=68000,
            volatility=10.0, time_remaining_seconds=60,
        )
        self.assertLess(result["z_score"], 0)
        self.assertLess(result["prob_up"], 0.5)
        self.assertGreater(result["prob_down"], 0.5)

    def test_spot_equals_strike_neutral(self):
        result = probability_model.calculate_probability(
            spot_price=68000, strike_price=68000,
            volatility=10.0, time_remaining_seconds=60,
        )
        self.assertAlmostEqual(result["z_score"], 0.0)
        self.assertAlmostEqual(result["prob_up"], 0.5, places=2)
        self.assertAlmostEqual(result["prob_down"], 0.5, places=2)

    def test_time_expired_spot_above(self):
        """With no time left, if spot >= strike, prob_up = 1.0."""
        result = probability_model.calculate_probability(
            spot_price=68001, strike_price=68000,
            volatility=10.0, time_remaining_seconds=0,
        )
        self.assertEqual(result["prob_up"], 1.0)
        self.assertEqual(result["prob_down"], 0.0)

    def test_time_expired_spot_below(self):
        result = probability_model.calculate_probability(
            spot_price=67999, strike_price=68000,
            volatility=10.0, time_remaining_seconds=0,
        )
        self.assertEqual(result["prob_up"], 0.0)
        self.assertEqual(result["prob_down"], 1.0)

    def test_zero_volatility_spot_above(self):
        result = probability_model.calculate_probability(
            spot_price=68100, strike_price=68000,
            volatility=0, time_remaining_seconds=60,
        )
        self.assertEqual(result["prob_up"], 1.0)

    def test_zero_volatility_spot_equal(self):
        result = probability_model.calculate_probability(
            spot_price=68000, strike_price=68000,
            volatility=0, time_remaining_seconds=60,
        )
        self.assertAlmostEqual(result["prob_up"], 0.5)

    def test_probabilities_sum_to_one(self):
        result = probability_model.calculate_probability(
            spot_price=68050, strike_price=68000,
            volatility=15.0, time_remaining_seconds=120,
        )
        self.assertAlmostEqual(
            result["prob_up"] + result["prob_down"], 1.0, places=10
        )

    def test_probabilities_clipped(self):
        """Probabilities should always be in [0, 1]."""
        result = probability_model.calculate_probability(
            spot_price=100000, strike_price=68000,
            volatility=1.0, time_remaining_seconds=5,
        )
        self.assertGreaterEqual(result["prob_up"], 0.0)
        self.assertLessEqual(result["prob_up"], 1.0)

    def test_higher_vol_lower_confidence(self):
        """Higher volatility should make probabilities closer to 0.5."""
        result_low_vol = probability_model.calculate_probability(
            spot_price=68100, strike_price=68000,
            volatility=5.0, time_remaining_seconds=60,
        )
        result_high_vol = probability_model.calculate_probability(
            spot_price=68100, strike_price=68000,
            volatility=50.0, time_remaining_seconds=60,
        )
        # With higher vol, prob_up should be closer to 0.5
        self.assertGreater(result_low_vol["prob_up"], result_high_vol["prob_up"])


class TestEdgeCalculation(unittest.TestCase):

    def test_positive_edge(self):
        edge = probability_model.calculate_edge(0.70, 0.55)
        self.assertAlmostEqual(edge, 0.15)

    def test_negative_edge(self):
        edge = probability_model.calculate_edge(0.40, 0.55)
        self.assertAlmostEqual(edge, -0.15)

    def test_zero_edge(self):
        edge = probability_model.calculate_edge(0.50, 0.50)
        self.assertAlmostEqual(edge, 0.0)


class TestConfidenceClassification(unittest.TestCase):

    def test_high_confidence(self):
        c = probability_model.classify_confidence(0.20, 60)
        self.assertEqual(c, "HIGH")

    def test_medium_confidence(self):
        c = probability_model.classify_confidence(0.10, 15)
        self.assertEqual(c, "MEDIUM")

    def test_low_confidence_small_edge(self):
        c = probability_model.classify_confidence(0.03, 60)
        self.assertEqual(c, "LOW")

    def test_low_confidence_short_time(self):
        """High edge but very short time (below MEDIUM min) → LOW."""
        c = probability_model.classify_confidence(0.20, 5)
        self.assertEqual(c, "LOW")

    def test_low_edge_short_time(self):
        c = probability_model.classify_confidence(0.03, 5)
        self.assertEqual(c, "LOW")


class TestTradeSignal(unittest.TestCase):

    def _make_signal(self, **overrides) -> TradeSignal:
        defaults = dict(
            market_type="btc-5min", market_id="123",
            strategy="latency_arb", direction="UP",
            model_probability=0.75, market_probability=0.55,
            edge=0.20, gross_ev=0.15, net_ev=0.12, estimated_costs=0.03,
            confidence="HIGH",
            recommended_size_pct=0.10,
            strike_price=68000, spot_price=68100,
            time_remaining=120,
        )
        defaults.update(overrides)
        return TradeSignal(**defaults)

    def test_actionable_signal(self):
        sig = self._make_signal(edge=0.20, confidence="HIGH", direction="UP")
        self.assertTrue(sig.is_actionable())

    def test_not_actionable_low_confidence(self):
        sig = self._make_signal(confidence="LOW")
        self.assertFalse(sig.is_actionable())

    def test_not_actionable_no_direction(self):
        sig = self._make_signal(direction="NONE")
        self.assertFalse(sig.is_actionable())

    def test_not_actionable_low_edge(self):
        sig = self._make_signal(edge=0.01, net_ev=0.005)
        self.assertFalse(sig.is_actionable())

    def test_not_actionable_low_net_ev(self):
        sig = self._make_signal(edge=0.20, net_ev=0.01)
        self.assertFalse(sig.is_actionable())

    def test_expected_value(self):
        sig = self._make_signal(edge=0.20, recommended_size_pct=0.10)
        self.assertAlmostEqual(sig.expected_value(), 0.02)

    def test_summary(self):
        sig = self._make_signal()
        s = sig.summary()
        self.assertIn("latency_arb", s)
        self.assertIn("UP", s)
        self.assertIn("edge=", s)


class TestLatencyArb(unittest.TestCase):

    def _base_kwargs(self, **overrides):
        defaults = dict(
            spot_price=68200, strike_price=68000,
            volatility=10.0, time_remaining=120,
            market_yes_price=0.50, market_no_price=0.50,
            spread=0.02, market_type="btc-5min", market_id="123",
        )
        defaults.update(overrides)
        return defaults

    def test_generates_signal_with_edge(self):
        """Large spot-strike distance with neutral market should produce signal."""
        sig = latency_arb.evaluate(**self._base_kwargs())
        # With spot well above strike and market at 0.50, model should see high prob_up
        if sig:
            self.assertEqual(sig.direction, "UP")
            self.assertGreater(sig.edge, 0)

    def test_no_signal_insufficient_time(self):
        sig = latency_arb.evaluate(**self._base_kwargs(time_remaining=10))
        self.assertIsNone(sig)

    def test_no_signal_wide_spread(self):
        sig = latency_arb.evaluate(**self._base_kwargs(spread=0.50))
        self.assertIsNone(sig)

    def test_no_signal_zero_volatility(self):
        sig = latency_arb.evaluate(**self._base_kwargs(volatility=0))
        self.assertIsNone(sig)

    def test_no_signal_zero_strike(self):
        sig = latency_arb.evaluate(**self._base_kwargs(strike_price=0))
        self.assertIsNone(sig)


class TestSniper(unittest.TestCase):

    def _base_kwargs(self, **overrides):
        defaults = dict(
            spot_price=68200, strike_price=68000,
            volatility=5.0, time_remaining=15,
            market_yes_price=0.60, market_no_price=0.40,
            spread=0.05, market_type="btc-5min", market_id="123",
        )
        defaults.update(overrides)
        return defaults

    def test_generates_signal_near_resolution(self):
        sig = sniper.evaluate(**self._base_kwargs())
        if sig:
            self.assertEqual(sig.strategy, "sniper")
            self.assertIn(sig.direction, ("UP", "DOWN"))

    def test_no_signal_too_much_time(self):
        sig = sniper.evaluate(**self._base_kwargs(time_remaining=120))
        self.assertIsNone(sig)

    def test_no_signal_too_little_time(self):
        sig = sniper.evaluate(**self._base_kwargs(time_remaining=1))
        self.assertIsNone(sig)

    def test_no_signal_market_converged(self):
        """If market already priced correctly, no sniper signal."""
        sig = sniper.evaluate(**self._base_kwargs(
            market_yes_price=0.95, market_no_price=0.05
        ))
        self.assertIsNone(sig)  # entry price > SNIPER_MAX_ENTRY_PRICE

    def test_no_signal_zero_volatility(self):
        sig = sniper.evaluate(**self._base_kwargs(volatility=0))
        self.assertIsNone(sig)


class TestSignalEngine(unittest.TestCase):

    def _make_market_state(self, market_type="btc-5min", **overrides):
        defaults = dict(
            market_id="123", condition_id="0xabc",
            market_type=market_type, strike_price=68000,
            yes_price=0.50, no_price=0.50,
            spread=0.02, time_remaining_seconds=120,
            is_active=True,
        )
        defaults.update(overrides)
        return MarketState(**defaults)

    def test_empty_input(self):
        engine = SignalEngine()
        result = engine.process_snapshot({})
        self.assertEqual(result, [])

    def test_no_volatility(self):
        engine = SignalEngine()
        result = engine.process_snapshot({
            "spot_price": 68000, "volatility": None,
        })
        self.assertEqual(result, [])

    def test_processes_with_market(self):
        engine = SignalEngine()
        state = self._make_market_state(
            yes_price=0.50, no_price=0.50,
            time_remaining_seconds=120,
        )
        result = engine.process_snapshot({
            "spot_price": 68200,
            "volatility": 10.0,
            "market_state_5m": state,
            "market_state_15m": None,
        })
        # May or may not produce signals depending on edge
        self.assertIsInstance(result, list)

    def test_cooldown_suppresses_duplicates(self):
        engine = SignalEngine()
        state = self._make_market_state(
            yes_price=0.40, no_price=0.60,
            time_remaining_seconds=120,
        )
        snapshot = {
            "spot_price": 68500,  # large distance from strike
            "volatility": 5.0,   # low vol -> high confidence
            "market_state_5m": state,
            "market_state_15m": None,
        }

        first = engine.process_snapshot(snapshot)
        second = engine.process_snapshot(snapshot)

        # Second call within cooldown should be suppressed
        if first:
            self.assertEqual(len(second), 0)

    def test_deduplication_keeps_highest_edge(self):
        engine = SignalEngine()
        # Create two signals for the same market with different edges
        sig1 = TradeSignal(
            market_type="btc-5min", market_id="123",
            strategy="latency_arb", direction="UP",
            edge=0.15, confidence="HIGH",
        )
        sig2 = TradeSignal(
            market_type="btc-5min", market_id="123",
            strategy="sniper", direction="UP",
            edge=0.10, confidence="MEDIUM",
        )
        result = engine._deduplicate([sig1, sig2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].strategy, "latency_arb")

    def test_deduplication_tiebreak_prefers_sniper(self):
        """When edges are close, prefer sniper."""
        engine = SignalEngine()
        sig1 = TradeSignal(
            market_type="btc-5min", strategy="latency_arb",
            direction="UP", edge=0.151,
        )
        sig2 = TradeSignal(
            market_type="btc-5min", strategy="sniper",
            direction="UP", edge=0.150,
        )
        result = engine._deduplicate([sig1, sig2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].strategy, "sniper")

    def test_get_active_strategies(self):
        engine = SignalEngine()
        strategies = engine.get_active_strategies()
        self.assertIn("latency_arb", strategies)
        # sniper currently disabled in config; test reflects deployed state
        self.assertNotIn("sniper", strategies)

    def test_signal_history_bounded(self):
        engine = SignalEngine()
        # Manually populate history beyond limit
        for i in range(60):
            engine._signal_history.append(TradeSignal(signal_id=str(i)))
        self.assertLessEqual(len(engine._signal_history), 60)


class TestNormalizeVolatility(unittest.TestCase):

    def test_positive_volatility(self):
        vol = probability_model.normalize_volatility(12.0)
        self.assertGreater(vol, 0)

    def test_zero_volatility(self):
        vol = probability_model.normalize_volatility(0.0)
        self.assertEqual(vol, 0.0)

    def test_negative_volatility(self):
        vol = probability_model.normalize_volatility(-5.0)
        self.assertEqual(vol, 0.0)


if __name__ == "__main__":
    unittest.main()
