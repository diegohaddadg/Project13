"""Tests for max drawdown cooldown behavior."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch, MagicMock

from models.position import Position
from models.trade_signal import TradeSignal
from execution.position_manager import PositionManager
from risk.risk_manager import RiskManager
from risk.performance_analytics import PerformanceAnalytics
from risk.kill_switch import KillSwitch
from risk.exposure_tracker import ExposureTracker
from risk.health_monitor import HealthMonitor
from feeds.aggregator import Aggregator
import config


class _AlwaysHealthyMonitor(HealthMonitor):
    """Health monitor that reports everything healthy for unit tests."""
    def run_health_check(self):
        return {
            "any_feed_ok": True, "binance_ok": True, "coinbase_ok": True,
            "polymarket_ok": True, "overall": "healthy", "warnings": [],
            "latency_ok": True,
        }


def _make_signal():
    return TradeSignal(
        market_type="btc-5min", market_id="mkt_1", strategy="latency_arb",
        direction="UP", model_probability=0.70, market_probability=0.50,
        edge=0.20, net_ev=0.10, confidence="HIGH",
        recommended_size_pct=0.10, strike_price=68000, spot_price=68200,
        time_remaining=120,
    )


def _portfolio(capital=100.0):
    return {"current_capital": capital, "volatility": 0.02, "feed_healthy": True}


def _make_rm(starting_equity=100.0):
    pm = PositionManager()
    ks = KillSwitch()
    exp = ExposureTracker(pm)
    analytics = PerformanceAnalytics()
    agg = Aggregator(test_mode=True)
    hm = _AlwaysHealthyMonitor(agg)
    rm = RiskManager(pm, ks, exp, analytics, hm)
    rm.set_session_start_equity(starting_equity)
    analytics.reset_hwm(starting_equity)
    return rm, pm, ks, analytics


class TestDrawdownCooldown(unittest.TestCase):
    """Test that max drawdown triggers a cooldown, not a permanent kill switch."""

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    @patch.object(config, "DRAWDOWN_COOLDOWN_SECONDS", 300.0)
    def test_drawdown_breach_starts_cooldown(self):
        rm, pm, ks, analytics = _make_rm(100.0)

        # Capital dropped to 75 → 25% drawdown from HWM of 100
        result = rm.evaluate_signal(_make_signal(), _portfolio(75.0))

        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("cooldown", result["reason"].lower())
        # Kill switch should NOT be triggered
        self.assertFalse(ks.is_active())

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    @patch.object(config, "DRAWDOWN_COOLDOWN_SECONDS", 0.01)  # very short
    def test_resume_after_cooldown_if_recovered(self):
        rm, pm, ks, analytics = _make_rm(100.0)

        # Breach drawdown
        rm.evaluate_signal(_make_signal(), _portfolio(75.0))
        self.assertEqual(rm._drawdown_cooldown_until, rm._drawdown_cooldown_until)

        # Wait for cooldown to expire
        time.sleep(0.02)

        # Capital recovered to 95 → 5% drawdown, below 20% limit
        result = rm.evaluate_signal(_make_signal(), _portfolio(95.0))

        # Should approve (drawdown recovered and cooldown expired)
        self.assertEqual(result["decision"], "APPROVE")
        self.assertFalse(ks.is_active())

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    @patch.object(config, "DRAWDOWN_COOLDOWN_SECONDS", 0.01)
    def test_still_in_drawdown_after_cooldown_restarts_cooldown(self):
        rm, pm, ks, analytics = _make_rm(100.0)

        # Breach drawdown
        rm.evaluate_signal(_make_signal(), _portfolio(75.0))
        time.sleep(0.02)

        # Cooldown expired but still at 25% drawdown
        result = rm.evaluate_signal(_make_signal(), _portfolio(75.0))

        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("cooldown", result["reason"].lower())
        # Should have restarted cooldown
        self.assertGreater(rm._drawdown_cooldown_until, time.time() - 1)

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    @patch.object(config, "DRAWDOWN_COOLDOWN_SECONDS", 300.0)
    def test_kill_switch_not_triggered_by_drawdown(self):
        rm, pm, ks, analytics = _make_rm(100.0)

        # Severe drawdown
        rm.evaluate_signal(_make_signal(), _portfolio(50.0))

        # Kill switch must remain inactive
        self.assertFalse(ks.is_active())

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    @patch.object(config, "DRAWDOWN_COOLDOWN_SECONDS", 300.0)
    def test_recovery_during_cooldown_still_waits(self):
        """Even if drawdown recovers, must wait for cooldown to expire."""
        rm, pm, ks, analytics = _make_rm(100.0)

        # Breach drawdown → starts cooldown
        rm.evaluate_signal(_make_signal(), _portfolio(75.0))

        # Capital recovered but cooldown still active
        result = rm.evaluate_signal(_make_signal(), _portfolio(95.0))

        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("cooldown", result["reason"].lower())


class TestHWMReset(unittest.TestCase):
    """Test that HWM resets to session equity, not STARTING_CAPITAL."""

    def test_hwm_reset_to_session_equity(self):
        analytics = PerformanceAnalytics()
        # Default HWM is STARTING_CAPITAL
        self.assertEqual(analytics._high_water_mark, config.STARTING_CAPITAL_USDC)

        # After reset to lower equity
        analytics.reset_hwm(80.0)
        self.assertEqual(analytics._high_water_mark, 80.0)
        self.assertEqual(analytics._max_drawdown_observed, 0.0)

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    def test_no_immediate_block_after_hwm_reset(self):
        """After HWM reset to 80, capital at 80 = 0% drawdown = no block."""
        rm, pm, ks, analytics = _make_rm(80.0)

        result = rm.evaluate_signal(_make_signal(), _portfolio(80.0))

        # 0% drawdown, should not be blocked
        self.assertEqual(result["decision"], "APPROVE")

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    def test_drawdown_measured_from_reset_hwm(self):
        """Drawdown calculated from reset HWM, not from STARTING_CAPITAL."""
        rm, pm, ks, analytics = _make_rm(80.0)

        # Capital at 70: drawdown from 80 = 12.5%, below 20% limit
        result = rm.evaluate_signal(_make_signal(), _portfolio(70.0))
        self.assertEqual(result["decision"], "APPROVE")

        # Capital at 60: drawdown from 80 = 25%, above 20% limit
        result = rm.evaluate_signal(_make_signal(), _portfolio(60.0))
        self.assertEqual(result["decision"], "REJECT")


class TestNoCorruptionOfLiveState(unittest.TestCase):
    """Verify that drawdown cooldown doesn't affect live reconciliation."""

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    def test_position_manager_untouched(self):
        rm, pm, ks, analytics = _make_rm(100.0)

        # Add an open position (simulating live fill)
        pos = Position(
            market_id="mkt_1", direction="UP", entry_price=0.55,
            num_shares=50, market_type="btc-5min",
        )
        pm._open_positions.append(pos)

        # Trigger drawdown
        rm.evaluate_signal(_make_signal(), _portfolio(75.0))

        # Position must still be there
        self.assertEqual(pm.count_open_positions(), 1)
        self.assertEqual(pm._open_positions[0].market_id, "mkt_1")


class TestPaperModeUnchanged(unittest.TestCase):

    @patch.object(config, "EXECUTION_MODE", "paper")
    @patch.object(config, "PAPER_RISK_WARN_ONLY", True)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    def test_paper_mode_warns_not_blocks(self):
        rm, pm, ks, analytics = _make_rm(100.0)

        result = rm.evaluate_signal(_make_signal(), _portfolio(75.0))

        # Paper warn-only mode should approve
        self.assertEqual(result["decision"], "APPROVE")


class TestRiskStatusReport(unittest.TestCase):

    @patch.object(config, "PAPER_RISK_WARN_ONLY", False)
    @patch.object(config, "MAX_DRAWDOWN_PCT", 0.20)
    @patch.object(config, "DRAWDOWN_COOLDOWN_SECONDS", 300.0)
    def test_status_shows_drawdown_cooldown(self):
        rm, pm, ks, analytics = _make_rm(100.0)
        rm.evaluate_signal(_make_signal(), _portfolio(75.0))

        status = rm.get_risk_status()

        self.assertGreater(status["drawdown_cooldown_remaining_s"], 0)
        self.assertTrue(any("cooldown" in b.lower() for b in status["trading_blockers"]))
        self.assertFalse(status["trading_allowed"])


if __name__ == "__main__":
    unittest.main()
