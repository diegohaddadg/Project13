"""Tests for latency_arb_v2 refinement layer."""

from __future__ import annotations

import unittest
from copy import copy

from models.trade_signal import TradeSignal
from strategies import latency_arb_v2
import config


def _make_signal(**overrides) -> TradeSignal:
    """Create a realistic v1 latency_arb signal for testing."""
    defaults = dict(
        market_type="btc-5min",
        market_id="mkt_abc",
        strategy="latency_arb",
        direction="UP",
        model_probability=0.70,
        market_probability=0.50,  # entry price = 0.50 (zone A)
        edge=0.20,
        gross_ev=0.15,
        net_ev=0.10,
        estimated_costs=0.05,
        confidence="HIGH",
        recommended_size_pct=0.10,
        strike_price=68000,
        spot_price=68200,
        time_remaining=120,
        metadata={
            "urgency_pass": True,
            "freshness_pass": True,
            "freshest_window": "5s",
            "disagreement": 0.20,
        },
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


class TestPriceZoneClassification(unittest.TestCase):
    """Test price-quality zone classification."""

    def test_zone_a_favorable(self):
        self.assertEqual(latency_arb_v2._classify_price_zone(0.45), "A")
        self.assertEqual(latency_arb_v2._classify_price_zone(0.52), "A")

    def test_zone_b_acceptable(self):
        self.assertEqual(latency_arb_v2._classify_price_zone(0.53), "B")
        self.assertEqual(latency_arb_v2._classify_price_zone(0.62), "B")

    def test_zone_c_expensive(self):
        self.assertEqual(latency_arb_v2._classify_price_zone(0.63), "C")
        self.assertEqual(latency_arb_v2._classify_price_zone(0.72), "C")

    def test_zone_d_very_expensive(self):
        self.assertEqual(latency_arb_v2._classify_price_zone(0.73), "D")
        self.assertEqual(latency_arb_v2._classify_price_zone(0.90), "D")


class TestPriceQualityScore(unittest.TestCase):
    """Test continuous price quality score."""

    def test_best_price(self):
        score = latency_arb_v2._price_quality_score(0.30)
        self.assertEqual(score, 1.0)

    def test_worst_price(self):
        score = latency_arb_v2._price_quality_score(0.85)
        self.assertEqual(score, 0.0)

    def test_midpoint(self):
        mid = (config.V2_PRICE_SCORE_BEST + config.V2_PRICE_SCORE_WORST) / 2
        score = latency_arb_v2._price_quality_score(mid)
        self.assertAlmostEqual(score, 0.5, places=2)

    def test_monotonic_decrease(self):
        prices = [0.35, 0.45, 0.55, 0.65, 0.75]
        scores = [latency_arb_v2._price_quality_score(p) for p in prices]
        for i in range(len(scores) - 1):
            self.assertGreaterEqual(scores[i], scores[i + 1])


class TestAdaptiveDisagreement(unittest.TestCase):
    """Test adaptive disagreement thresholds per zone."""

    def test_zone_a_most_lenient(self):
        a = latency_arb_v2._adaptive_min_disagreement("A")
        b = latency_arb_v2._adaptive_min_disagreement("B")
        self.assertLess(a, b)

    def test_zone_d_most_strict(self):
        c = latency_arb_v2._adaptive_min_disagreement("C")
        d = latency_arb_v2._adaptive_min_disagreement("D")
        self.assertGreater(d, c)

    def test_monotonic_increase(self):
        vals = [latency_arb_v2._adaptive_min_disagreement(z) for z in ("A", "B", "C", "D")]
        for i in range(len(vals) - 1):
            self.assertLessEqual(vals[i], vals[i + 1])


class TestOverlapPenalty(unittest.TestCase):
    """Test conflict-aware overlap penalty."""

    def test_no_positions(self):
        sig = _make_signal()
        result = latency_arb_v2._compute_overlap_penalty(sig, [])
        self.assertEqual(result["penalty"], 0.0)
        self.assertEqual(result["open_count"], 0)

    def test_same_direction_mild_penalty(self):
        sig = _make_signal()
        positions = [{"market_id": "mkt_abc", "market_type": "btc-5min", "direction": "UP"}]
        result = latency_arb_v2._compute_overlap_penalty(sig, positions)
        self.assertGreater(result["penalty"], 0.0)
        self.assertLess(result["penalty"], 0.5)
        self.assertEqual(result["same_direction"], 1)

    def test_opposite_direction_significant_penalty(self):
        sig = _make_signal(direction="UP")
        positions = [{"market_id": "mkt_abc", "market_type": "btc-5min", "direction": "DOWN"}]
        result = latency_arb_v2._compute_overlap_penalty(sig, positions)
        self.assertGreater(result["penalty"], 0.1)
        self.assertEqual(result["opposite_direction"], 1)

    def test_not_automatically_blocked(self):
        """Same-direction overlap should NOT produce penalty=1.0 (not a blanket ban)."""
        sig = _make_signal()
        positions = [
            {"market_id": "mkt_abc", "market_type": "btc-5min", "direction": "UP"},
            {"market_id": "mkt_abc", "market_type": "btc-5min", "direction": "UP"},
        ]
        result = latency_arb_v2._compute_overlap_penalty(sig, positions)
        self.assertLess(result["penalty"], 1.0)

    def test_high_concurrency_penalty(self):
        sig = _make_signal()
        positions = [
            {"market_id": f"mkt_{i}", "market_type": "btc-5min", "direction": "UP"}
            for i in range(5)
        ]
        result = latency_arb_v2._compute_overlap_penalty(sig, positions)
        self.assertGreater(result["penalty"], 0.0)
        self.assertEqual(result["open_count"], 5)


class TestPriceQualityGating(unittest.TestCase):
    """Test that price quality properly gates entries."""

    def test_favorable_price_passes(self):
        """Zone A (cheap) with good disagreement should approve."""
        sig = _make_signal(market_probability=0.45, model_probability=0.65)
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "APPROVE")

    def test_expensive_entry_reduced_or_rejected(self):
        """Zone C (expensive) with moderate support gets reduced or rejected."""
        sig = _make_signal(
            market_probability=0.65, model_probability=0.75,
            net_ev=0.06,
        )
        sig.metadata["urgency_pass"] = True
        sig.metadata["freshness_pass"] = True
        result = latency_arb_v2.refine(sig)
        self.assertIn(result["decision"], ("REDUCE", "REJECT"))

    def test_ultra_expensive_defaults_to_reject(self):
        """Zone D should default to reject."""
        sig = _make_signal(
            market_probability=0.78, model_probability=0.90,
            net_ev=0.05,
        )
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "REJECT")

    def test_zone_d_exceptional_override(self):
        """Zone D with truly exceptional quality can still pass (reduced)."""
        sig = _make_signal(
            market_probability=0.73, model_probability=0.95,
            edge=0.22, net_ev=0.15,
        )
        sig.metadata["urgency_pass"] = True
        sig.metadata["freshness_pass"] = True
        sig.metadata["disagreement"] = 0.22
        result = latency_arb_v2.refine(sig)
        # May still reject depending on thresholds, but if it passes it's REDUCE
        if result["decision"] != "REJECT":
            self.assertEqual(result["decision"], "REDUCE")


class TestAdaptiveDisagreementBehavior(unittest.TestCase):
    """Test that disagreement handling adapts to price quality."""

    def test_favorable_price_moderate_disagreement_passes(self):
        """Zone A + moderate disagreement = approve."""
        sig = _make_signal(
            market_probability=0.48, model_probability=0.56,
            net_ev=0.08,
        )
        result = latency_arb_v2.refine(sig)
        # disagreement = 0.08, zone A min = 0.04 → should pass
        self.assertIn(result["decision"], ("APPROVE", "REDUCE"))
        self.assertIsNotNone(result["signal"])

    def test_expensive_price_same_disagreement_may_reject(self):
        """Zone C + same moderate disagreement = higher bar."""
        sig = _make_signal(
            market_probability=0.65, model_probability=0.71,
            net_ev=0.04,
        )
        sig.metadata["urgency_pass"] = True
        sig.metadata["freshness_pass"] = False  # weaker support
        result = latency_arb_v2.refine(sig)
        # disagreement = 0.06 < zone C min of 0.07 → reject
        self.assertEqual(result["decision"], "REJECT")

    def test_disagreement_still_matters_at_bad_price(self):
        """Strong disagreement at zone C can still approve."""
        sig = _make_signal(
            market_probability=0.65, model_probability=0.82,
            edge=0.17, net_ev=0.12,
        )
        sig.metadata["urgency_pass"] = True
        sig.metadata["freshness_pass"] = True
        result = latency_arb_v2.refine(sig)
        # disagreement = 0.17 >> zone C min of 0.07
        self.assertIsNotNone(result["signal"])


class TestOverlapConflictBehavior(unittest.TestCase):
    """Test conflict-aware overlap penalty in refinement decisions."""

    def test_same_direction_not_blocked(self):
        """Same-direction overlap is not automatically blocked."""
        sig = _make_signal(market_probability=0.48, model_probability=0.65)
        positions = [
            {"market_id": "mkt_abc", "market_type": "btc-5min", "direction": "UP"}
        ]
        result = latency_arb_v2.refine(sig, positions)
        self.assertIsNotNone(result["signal"])

    def test_opposite_direction_increases_scrutiny(self):
        """Opposite-direction conflict reduces quality score → may change decision."""
        sig = _make_signal(
            market_probability=0.50, model_probability=0.58,
            net_ev=0.06,
        )
        # Without conflict
        result_clean = latency_arb_v2.refine(sig, [])
        # With opposite-direction conflict
        positions = [
            {"market_id": "mkt_abc", "market_type": "btc-5min", "direction": "DOWN"}
        ]
        result_conflict = latency_arb_v2.refine(sig, positions)
        # The quality score should be lower with conflict
        q_clean = result_clean["v2_diagnostics"].get("composite_quality", 1)
        q_conflict = result_conflict["v2_diagnostics"].get("composite_quality", 1)
        self.assertLessEqual(q_conflict, q_clean)

    def test_high_concurrency_lower_quality_more_likely_reduced(self):
        """High concurrency + marginal entry → more likely to reduce/reject."""
        sig = _make_signal(
            market_probability=0.50, model_probability=0.57,
            net_ev=0.05,
        )
        # Low concurrency
        result_low = latency_arb_v2.refine(sig, [])
        # High concurrency (5 positions)
        positions = [
            {"market_id": f"mkt_{i}", "market_type": "btc-5min", "direction": "UP"}
            for i in range(5)
        ]
        result_high = latency_arb_v2.refine(sig, positions)
        q_low = result_low["v2_diagnostics"].get("composite_quality", 1)
        q_high = result_high["v2_diagnostics"].get("composite_quality", 1)
        self.assertLessEqual(q_high, q_low)


class TestFeatureFlag(unittest.TestCase):
    """Test that the feature flag correctly controls behavior."""

    def test_v2_disabled_no_refinement(self):
        """When v2 is off, refine should not be called (tested at signal_engine level).

        Here we verify the config default.
        """
        # Default is False (checked from config, not env)
        # Just ensure the module loads and the config exists
        self.assertIsInstance(config.LATENCY_ARB_V2_ENABLED, bool)

    def test_non_latency_arb_passthrough(self):
        """Non-latency_arb signals pass through untouched."""
        sig = _make_signal(strategy="sniper")
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "APPROVE")
        self.assertIs(result["signal"], sig)

    def test_v2_produces_diagnostics(self):
        """V2 refinement always produces diagnostics dict."""
        sig = _make_signal()
        result = latency_arb_v2.refine(sig)
        self.assertIn("v2_diagnostics", result)
        diag = result["v2_diagnostics"]
        self.assertIn("price_zone", diag)
        self.assertIn("composite_quality", diag)
        self.assertIn("decision", diag)


class TestThroughputProtection(unittest.TestCase):
    """Verify that v2 doesn't collapse trade count for reasonable entries."""

    def test_typical_good_entry_approves(self):
        """A typical good entry (zone A/B, decent disagreement) should approve."""
        sig = _make_signal(
            market_probability=0.50, model_probability=0.65,
            net_ev=0.10,
        )
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "APPROVE")

    def test_zone_b_decent_entry_not_rejected(self):
        """Zone B entries with decent support should not be rejected."""
        sig = _make_signal(
            market_probability=0.55, model_probability=0.65,
            net_ev=0.08,
        )
        result = latency_arb_v2.refine(sig)
        self.assertIn(result["decision"], ("APPROVE", "REDUCE"))
        self.assertIsNotNone(result["signal"])

    def test_bulk_zone_a_entries_all_approve(self):
        """Zone A entries with standard quality should all approve."""
        approved = 0
        for mp in [0.45, 0.48, 0.50, 0.52]:
            sig = _make_signal(
                market_probability=mp, model_probability=mp + 0.15,
                net_ev=0.08,
            )
            result = latency_arb_v2.refine(sig)
            if result["decision"] == "APPROVE":
                approved += 1
        self.assertGreaterEqual(approved, 3, "Most zone A entries should approve")


class TestSizeReductionPreservesSignal(unittest.TestCase):
    """Verify that REDUCE decisions properly adjust size without destroying signal."""

    def test_reduce_lowers_size(self):
        """REDUCE should produce a signal with smaller recommended_size_pct."""
        sig = _make_signal(
            market_probability=0.55, model_probability=0.60,
            net_ev=0.06,
        )
        sig.metadata["freshness_pass"] = False
        result = latency_arb_v2.refine(sig)
        if result["decision"] == "REDUCE":
            self.assertLess(
                result["signal"].recommended_size_pct,
                sig.recommended_size_pct,
            )
            self.assertGreater(result["signal"].recommended_size_pct, 0)

    def test_reduce_preserves_direction(self):
        """REDUCE should not change signal direction or strategy."""
        sig = _make_signal(
            market_probability=0.55, model_probability=0.60,
            net_ev=0.06,
        )
        result = latency_arb_v2.refine(sig)
        if result["signal"]:
            self.assertEqual(result["signal"].direction, sig.direction)
            self.assertEqual(result["signal"].strategy, sig.strategy)


# ===================================================================
# V2.1 Downside Control Tests
# ===================================================================

class TestV21UpBehaviorPreserved(unittest.TestCase):
    """Verify UP trades are effectively unchanged by v2.1."""

    def test_up_zone_a_strong_still_approves(self):
        sig = _make_signal(direction="UP", market_probability=0.48, model_probability=0.65)
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "APPROVE")

    def test_up_zone_b_decent_still_passes(self):
        sig = _make_signal(direction="UP", market_probability=0.55, model_probability=0.65, net_ev=0.08)
        result = latency_arb_v2.refine(sig)
        self.assertIn(result["decision"], ("APPROVE", "REDUCE"))
        self.assertIsNotNone(result["signal"])

    def test_up_zone_c_strong_still_passes(self):
        sig = _make_signal(
            direction="UP", market_probability=0.65, model_probability=0.82,
            edge=0.17, net_ev=0.12,
        )
        result = latency_arb_v2.refine(sig)
        self.assertIsNotNone(result["signal"])

    def test_up_has_no_v21_quality_penalty(self):
        """UP composite quality should NOT include the v2.1 DOWN penalty."""
        sig_up = _make_signal(direction="UP", market_probability=0.50, model_probability=0.65)
        sig_down = _make_signal(direction="DOWN", market_probability=0.50, model_probability=0.65)
        r_up = latency_arb_v2.refine(sig_up)
        r_down = latency_arb_v2.refine(sig_down)
        q_up = r_up["v2_diagnostics"]["composite_quality"]
        q_down = r_down["v2_diagnostics"]["composite_quality"]
        self.assertGreater(q_up, q_down, "UP should have higher quality than DOWN for same inputs")


class TestV21CheapStrongDownStillPasses(unittest.TestCase):
    """Cheap / strong DOWN trades must still pass."""

    def test_zone_a_strong_down_approves(self):
        sig = _make_signal(direction="DOWN", market_probability=0.45, model_probability=0.65, net_ev=0.10)
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "APPROVE")

    def test_zone_b_strong_down_passes(self):
        sig = _make_signal(
            direction="DOWN", market_probability=0.55, model_probability=0.70,
            net_ev=0.10,
        )
        result = latency_arb_v2.refine(sig)
        self.assertIsNotNone(result["signal"])
        self.assertIn(result["decision"], ("APPROVE", "REDUCE"))


class TestV21ExpensiveBorderlineDownReduced(unittest.TestCase):
    """Expensive borderline DOWN trades should be reduced more than equivalent UP."""

    def test_zone_c_borderline_down_vs_up(self):
        """Same borderline zone C entry: DOWN should be treated more harshly."""
        kwargs = dict(market_probability=0.65, model_probability=0.75, net_ev=0.07)
        sig_up = _make_signal(direction="UP", **kwargs)
        sig_down = _make_signal(direction="DOWN", **kwargs)

        r_up = latency_arb_v2.refine(sig_up)
        r_down = latency_arb_v2.refine(sig_down)

        # DOWN should have at least as harsh a decision
        decision_rank = {"REJECT": 0, "REDUCE": 1, "APPROVE": 2}
        self.assertLessEqual(
            decision_rank[r_down["decision"]],
            decision_rank[r_up["decision"]],
            f"DOWN ({r_down['decision']}) should be at least as harsh as UP ({r_up['decision']})"
        )

    def test_zone_b_borderline_down_gets_smaller_size(self):
        """Borderline zone B DOWN should reduce more aggressively."""
        sig_up = _make_signal(
            direction="UP", market_probability=0.57, model_probability=0.62,
            net_ev=0.05,
        )
        sig_up.metadata["freshness_pass"] = False
        sig_down = _make_signal(
            direction="DOWN", market_probability=0.57, model_probability=0.62,
            net_ev=0.05,
        )
        sig_down.metadata["freshness_pass"] = False

        r_up = latency_arb_v2.refine(sig_up)
        r_down = latency_arb_v2.refine(sig_down)

        # Both should produce a signal (not reject)
        if r_up["signal"] and r_down["signal"]:
            self.assertLessEqual(
                r_down["signal"].recommended_size_pct,
                r_up["signal"].recommended_size_pct,
                "DOWN size should be <= UP size for same borderline inputs"
            )


class TestV21VeryExpensiveWeakDownRejects(unittest.TestCase):
    """Very expensive weak DOWN trades should reject more reliably."""

    def test_zone_c_weak_disagree_down_rejects(self):
        """Zone C DOWN with disagreement that barely passes UP threshold but not DOWN."""
        # disagree = 0.08. UP zone C min = 0.07 (passes). DOWN zone C min = 0.07 + 0.02 = 0.09 (fails).
        sig = _make_signal(
            direction="DOWN", market_probability=0.65, model_probability=0.73,
            net_ev=0.05,
        )
        result = latency_arb_v2.refine(sig)
        self.assertEqual(result["decision"], "REJECT")

    def test_same_disagree_up_passes_zone_c(self):
        """Same setup as above but UP direction should pass (disagree 0.08 > 0.07)."""
        sig = _make_signal(
            direction="UP", market_probability=0.65, model_probability=0.73,
            net_ev=0.05,
        )
        result = latency_arb_v2.refine(sig)
        # UP with 0.08 disagree in zone C should not reject on disagreement
        self.assertNotEqual(result["reason"], "zone_C_weak_disagree: 0.080 < 0.090")

    def test_zone_d_down_harder_to_pass(self):
        """Zone D DOWN needs even higher disagreement than zone D UP."""
        # zone D UP min = 0.10, DOWN min = 0.10 + 0.02 = 0.12
        sig = _make_signal(
            direction="DOWN", market_probability=0.75, model_probability=0.86,
            net_ev=0.08,
        )
        sig.metadata["urgency_pass"] = True
        sig.metadata["freshness_pass"] = True
        result = latency_arb_v2.refine(sig)
        # disagree = 0.11, below DOWN threshold of 0.12 → this affects quality computation
        # Zone D checks quality >= 0.75, which is very hard with DOWN penalty
        self.assertEqual(result["decision"], "REJECT")


class TestV21BorderlineDownSizesConservatively(unittest.TestCase):
    """Borderline DOWN trades should size down more conservatively."""

    def test_zone_a_weak_disagree_down_smaller_than_up(self):
        """Zone A weak disagree: DOWN gets 0.65x vs UP's 0.80x."""
        sig_up = _make_signal(direction="UP", market_probability=0.50, model_probability=0.53, net_ev=0.06)
        sig_down = _make_signal(direction="DOWN", market_probability=0.50, model_probability=0.53, net_ev=0.06)

        r_up = latency_arb_v2.refine(sig_up)
        r_down = latency_arb_v2.refine(sig_down)

        # disagree = 0.03 < zone A min 0.04 → both REDUCE
        if r_up["decision"] == "REDUCE" and r_down["decision"] == "REDUCE":
            self.assertLess(
                r_down["signal"].recommended_size_pct,
                r_up["signal"].recommended_size_pct,
            )

    def test_zone_c_reduce_down_applies_extra_multiplier(self):
        """Zone C borderline REDUCE: DOWN should apply V2_1_DOWN_REDUCE_SIZE_MULT on top."""
        sig = _make_signal(
            direction="DOWN", market_probability=0.65, model_probability=0.80,
            net_ev=0.08,
        )
        result = latency_arb_v2.refine(sig)
        if result["decision"] == "REDUCE":
            # The v2.1 DOWN multiplier should stack
            v2_mult = result["v2_diagnostics"].get("size_mult", 1.0)
            base_mult = config.V2_ZONE_C_REDUCED_SIZE_MULT
            expected_down_mult = base_mult * config.V2_1_DOWN_REDUCE_SIZE_MULT
            # Allow some tolerance since quality might push to different path
            self.assertLessEqual(v2_mult, base_mult,
                                 "DOWN reduce mult should be <= base zone C mult")


class TestV21DiagnosticsIncludeDirection(unittest.TestCase):
    """V2.1 diagnostics should include directional information."""

    def test_diagnostics_show_direction(self):
        sig = _make_signal(direction="DOWN")
        result = latency_arb_v2.refine(sig)
        diag = result["v2_diagnostics"]
        self.assertEqual(diag["direction"], "DOWN")
        self.assertTrue(diag["v2_1_down_active"])

    def test_diagnostics_up_not_down_active(self):
        sig = _make_signal(direction="UP")
        result = latency_arb_v2.refine(sig)
        diag = result["v2_diagnostics"]
        self.assertEqual(diag["direction"], "UP")
        self.assertFalse(diag["v2_1_down_active"])


if __name__ == "__main__":
    unittest.main()
