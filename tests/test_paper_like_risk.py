"""Tests for PAPER_LIKE_RISK_MODE (Strategy B)."""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from execution.position_manager import PositionManager
from risk.exposure_tracker import ExposureTracker
import config


class TestGetRiskEquity(unittest.TestCase):
    """Test the get_risk_equity() method on PositionManager."""

    @patch.object(config, "PAPER_LIKE_RISK_MODE", False)
    def test_disabled_returns_actual_equity(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        self.assertAlmostEqual(pm.get_risk_equity(), 38.0, places=2)
        self.assertEqual(pm.get_risk_equity(), pm.get_total_equity())

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    def test_enabled_small_account_uses_baseline(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        # actual=38 < baseline=100 → should return 100
        self.assertAlmostEqual(pm.get_risk_equity(), 100.0, places=2)

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    def test_enabled_large_account_uses_actual(self):
        pm = PositionManager()
        pm.set_capital(150.0)
        # actual=150 > baseline=100 → should return 150
        self.assertAlmostEqual(pm.get_risk_equity(), 150.0, places=2)

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    def test_enabled_equal_to_baseline(self):
        pm = PositionManager()
        pm.set_capital(100.0)
        self.assertAlmostEqual(pm.get_risk_equity(), 100.0, places=2)

    @patch.object(config, "PAPER_LIKE_RISK_MODE", False)
    def test_get_total_equity_unchanged(self):
        """get_total_equity must always return actual, regardless of mode."""
        pm = PositionManager()
        pm.set_capital(38.0)
        self.assertAlmostEqual(pm.get_total_equity(), 38.0, places=2)


class TestExposureWithPaperLikeMode(unittest.TestCase):
    """Exposure limits use risk equity when paper-like mode is enabled."""

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    @patch.object(config, "MAX_TOTAL_EXPOSURE_PCT", 0.50)
    @patch.object(config, "MAX_SINGLE_MARKET_EXPOSURE_PCT", 0.25)
    def test_exposure_limit_uses_baseline(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        exp = ExposureTracker(pm)

        # With baseline=100, total limit = 100 * 0.50 = $50
        # $10 order should NOT exceed
        self.assertFalse(exp.would_exceed_limits(10.0, "mkt_1"))

        # $55 order WOULD exceed $50 limit
        self.assertTrue(exp.would_exceed_limits(55.0, "mkt_1"))

    @patch.object(config, "PAPER_LIKE_RISK_MODE", False)
    @patch.object(config, "MAX_TOTAL_EXPOSURE_PCT", 0.50)
    @patch.object(config, "MAX_SINGLE_MARKET_EXPOSURE_PCT", 0.25)
    def test_exposure_limit_uses_actual_when_disabled(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        exp = ExposureTracker(pm)

        # With actual=38, total limit = 38 * 0.50 = $19
        # per-market limit = 38 * 0.25 = $9.50
        # $8 should NOT exceed
        self.assertFalse(exp.would_exceed_limits(8.0, "mkt_1"))

        # $20 WOULD exceed $19 total limit
        self.assertTrue(exp.would_exceed_limits(20.0, "mkt_1"))

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    @patch.object(config, "MAX_SINGLE_MARKET_EXPOSURE_PCT", 0.25)
    def test_per_market_limit_uses_baseline(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        exp = ExposureTracker(pm)

        # Per-market limit = 100 * 0.25 = $25
        self.assertFalse(exp.would_exceed_limits(20.0, "mkt_1"))
        self.assertTrue(exp.would_exceed_limits(30.0, "mkt_1"))


class TestAccountingTruthUnchanged(unittest.TestCase):
    """Reconciliation / accounting must use real equity, not paper-like baseline."""

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    def test_available_capital_is_real(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        # Available capital must reflect real state
        self.assertAlmostEqual(pm.get_available_capital(), 38.0, places=2)

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    def test_total_equity_is_real(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        # Total equity must reflect real state
        self.assertAlmostEqual(pm.get_total_equity(), 38.0, places=2)

    @patch.object(config, "PAPER_LIKE_RISK_MODE", True)
    @patch.object(config, "PAPER_LIKE_BASELINE_USDC", 100.0)
    def test_pnl_stats_are_real(self):
        pm = PositionManager()
        pm.set_capital(38.0)
        stats = pm.get_stats()
        self.assertAlmostEqual(stats["available_capital"], 38.0, places=2)


class TestPaperModeUnchanged(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "paper")
    @patch.object(config, "PAPER_LIKE_RISK_MODE", False)
    def test_paper_mode_unaffected(self):
        pm = PositionManager()
        self.assertEqual(pm.get_risk_equity(), pm.get_total_equity())


if __name__ == "__main__":
    unittest.main()
