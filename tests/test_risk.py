"""Tests for Phase 5 risk engine components."""

from __future__ import annotations

import time
import unittest

from models.trade_signal import TradeSignal
from models.position import Position
from models.order import Order
from execution.position_manager import PositionManager
from risk.kill_switch import KillSwitch
from risk.exposure_tracker import ExposureTracker
from risk.performance_analytics import PerformanceAnalytics
from risk.health_monitor import HealthMonitor
from risk.risk_manager import RiskManager
from feeds.aggregator import Aggregator
import config


class TestKillSwitch(unittest.TestCase):

    def test_initially_inactive(self):
        ks = KillSwitch()
        # Default config has KILL_SWITCH_ACTIVE=False
        if not config.KILL_SWITCH_ACTIVE:
            self.assertFalse(ks.is_active())

    def test_activate(self):
        ks = KillSwitch()
        ks.activate("Test trigger")
        self.assertTrue(ks.is_active())
        self.assertEqual(ks.trigger_reason, "Test trigger")
        self.assertGreater(ks.trigger_time, 0)

    def test_deactivate(self):
        ks = KillSwitch()
        ks.activate("Test")
        ks.deactivate()
        self.assertFalse(ks.is_active())

    def test_duplicate_activate_noop(self):
        ks = KillSwitch()
        ks.activate("First")
        ks.activate("Second")
        self.assertEqual(ks.trigger_reason, "First")

    def test_check_triggers_drawdown(self):
        ks = KillSwitch()
        result = ks.check_triggers(drawdown_breached=True)
        self.assertTrue(result)
        self.assertTrue(ks.is_active())

    def test_check_triggers_daily_limit(self):
        ks = KillSwitch()
        result = ks.check_triggers(daily_limit_hit=True)
        self.assertTrue(result)

    def test_check_triggers_feeds_down(self):
        ks = KillSwitch()
        result = ks.check_triggers(feeds_healthy=False)
        self.assertTrue(result)

    def test_check_triggers_all_ok(self):
        ks = KillSwitch()
        result = ks.check_triggers()
        self.assertFalse(result)

    def test_get_status(self):
        ks = KillSwitch()
        s = ks.get_status()
        self.assertIn("active", s)
        self.assertIn("reason", s)


class TestExposureTracker(unittest.TestCase):

    def test_no_exposure_initially(self):
        pm = PositionManager()
        et = ExposureTracker(pm)
        self.assertEqual(et.get_total_exposure(), 0.0)
        self.assertEqual(et.get_exposure_pct(), 0.0)

    def test_exposure_after_position(self):
        pm = PositionManager()
        et = ExposureTracker(pm)
        order = Order(size_usdc=10, fill_price=0.50, num_shares=20,
                      market_id="123", direction="UP", execution_mode="paper")
        pm.open_position(order)
        self.assertAlmostEqual(et.get_total_exposure(), 10.0)  # 0.50 * 20

    def test_would_exceed_total(self):
        pm = PositionManager()
        et = ExposureTracker(pm)
        # Max total = STARTING_CAPITAL * MAX_TOTAL_EXPOSURE_PCT
        limit = config.STARTING_CAPITAL_USDC * config.MAX_TOTAL_EXPOSURE_PCT
        self.assertTrue(et.would_exceed_limits(limit + 1))
        self.assertFalse(et.would_exceed_limits(limit - 1))

    def test_would_exceed_per_market(self):
        pm = PositionManager()
        et = ExposureTracker(pm)
        market_limit = config.STARTING_CAPITAL_USDC * config.MAX_SINGLE_MARKET_EXPOSURE_PCT
        # Add existing exposure
        order = Order(size_usdc=market_limit - 1, fill_price=0.50,
                      num_shares=(market_limit - 1) / 0.50,
                      market_id="mkt1", direction="UP", execution_mode="paper")
        pm.open_position(order)
        # Adding 5 more to same market should exceed
        self.assertTrue(et.would_exceed_limits(5.0, market_id="mkt1"))


class TestPerformanceAnalytics(unittest.TestCase):

    def _make_pa(self, closed_positions=None):
        """Create a PerformanceAnalytics backed by a mock PositionManager."""
        from unittest.mock import MagicMock
        pm = MagicMock()
        pm.get_closed_positions.return_value = closed_positions or []
        pa = PerformanceAnalytics()
        pa.set_position_manager(pm)
        return pa

    def test_empty_summary(self):
        pa = self._make_pa([])
        s = pa.get_summary()
        self.assertEqual(s["total_trades"], 0)
        self.assertEqual(s["win_rate"], 0.0)

    def test_update_and_summary(self):
        p1 = Position(pnl=5.0, entry_timestamp=time.time() - 60)
        p2 = Position(pnl=-3.0, entry_timestamp=time.time() - 30)
        pa = self._make_pa([p1, p2])
        s = pa.get_summary()
        self.assertEqual(s["total_trades"], 2)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["total_pnl"], 2.0)

    def test_drawdown_tracking(self):
        pa = PerformanceAnalytics()
        pa.update_hwm(110.0)
        pa.update_hwm(95.0)
        dd = pa.get_current_drawdown(95.0)
        self.assertAlmostEqual(dd, 15.0 / 110.0, places=3)

    def test_profit_factor(self):
        p1 = Position(pnl=10.0, entry_timestamp=time.time())
        p2 = Position(pnl=-5.0, entry_timestamp=time.time())
        pa = self._make_pa([p1, p2])
        s = pa.get_summary()
        self.assertAlmostEqual(s["profit_factor"], 2.0)

    def test_profit_factor_finite_when_no_losses(self):
        """Wins-only book must not emit inf (breaks browser JSON.parse on WebSocket)."""
        import json

        p1 = Position(pnl=10.0, entry_timestamp=time.time())
        p2 = Position(pnl=5.0, entry_timestamp=time.time())
        pa = self._make_pa([p1, p2])
        s = pa.get_summary()
        self.assertEqual(s["profit_factor"], 1e6)
        json.dumps(s, allow_nan=False)

    def test_generate_report(self):
        p = Position(pnl=5.0, entry_timestamp=time.time())
        pa = self._make_pa([p])
        report = pa.generate_report(105.0)
        self.assertIn("PERFORMANCE REPORT", report)
        self.assertIn("Win Rate", report)


class _TestHealthMonitor(HealthMonitor):
    """Health monitor that reports everything healthy for unit tests."""
    def run_health_check(self):
        return {
            "binance_ok": True, "coinbase_ok": True, "any_feed_ok": True,
            "polymarket_ok": True, "latency_ok": True,
            "feed_latency_ms": 50, "polymarket_age_s": 1,
            "volatility_available": True, "warming_up": False,
        }
    def is_system_healthy(self):
        return True
    def get_warnings(self):
        return []


class TestRiskManager(unittest.TestCase):

    def _make_rm(self):
        pm = PositionManager()
        ks = KillSwitch()
        exp = ExposureTracker(pm)
        analytics = PerformanceAnalytics()
        analytics.set_position_manager(pm)
        agg = Aggregator(test_mode=True)
        hm = _TestHealthMonitor(agg)  # Always-healthy monitor for unit tests
        rm = RiskManager(pm, ks, exp, analytics, hm)
        rm.set_session_start_equity(config.STARTING_CAPITAL_USDC)
        return rm, pm, ks

    def _make_signal(self, **kw):
        defaults = dict(
            market_type="btc-5min", market_id="123",
            strategy="latency_arb", direction="UP",
            edge=0.20, gross_ev=0.15, net_ev=0.12, estimated_costs=0.03,
            confidence="HIGH",
            recommended_size_pct=0.10,
            time_remaining=120, timestamp=time.time(),
        )
        defaults.update(kw)
        return TradeSignal(**defaults)

    def _portfolio_state(self, capital=100.0, vol=10.0):
        return {"current_capital": capital, "volatility": vol, "feed_healthy": True}

    def test_approve_good_signal(self):
        rm, pm, ks = self._make_rm()
        sig = self._make_signal()
        result = rm.evaluate_signal(sig, self._portfolio_state())
        self.assertEqual(result["decision"], "APPROVE")

    def test_reject_kill_switch(self):
        rm, pm, ks = self._make_rm()
        ks.activate("Test")
        sig = self._make_signal()
        result = rm.evaluate_signal(sig, self._portfolio_state())
        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("Kill switch", result["reason"])

    def test_reject_daily_loss(self):
        orig = config.PAPER_RISK_WARN_ONLY
        config.PAPER_RISK_WARN_ONLY = False  # test hard-block path
        try:
            rm, pm, ks = self._make_rm()
            rm.set_session_start_equity(100.0)
            for _ in range(5):
                rm.record_trade_result(Position(pnl=-5.0, entry_timestamp=time.time()))
            sig = self._make_signal()
            result = rm.evaluate_signal(sig, self._portfolio_state())
            self.assertEqual(result["decision"], "REJECT")
            self.assertIn("Daily loss", result["reason"])
        finally:
            config.PAPER_RISK_WARN_ONLY = orig

    def test_cooldown_after_consecutive_losses(self):
        orig = config.PAPER_RISK_WARN_ONLY
        config.PAPER_RISK_WARN_ONLY = False
        try:
            rm, pm, ks = self._make_rm()
            for _ in range(config.MAX_CONSECUTIVE_LOSSES):
                rm.record_trade_result(Position(pnl=-1.0, entry_timestamp=time.time()))
            sig = self._make_signal()
            result = rm.evaluate_signal(sig, self._portfolio_state())
            self.assertEqual(result["decision"], "REJECT")
            self.assertIn("Cooldown", result["reason"])
        finally:
            config.PAPER_RISK_WARN_ONLY = orig

    def test_consecutive_loss_reset_on_win(self):
        rm, pm, ks = self._make_rm()
        rm.record_trade_result(Position(pnl=-1.0, entry_timestamp=time.time()))
        rm.record_trade_result(Position(pnl=-1.0, entry_timestamp=time.time()))
        # Win resets counter
        rm.record_trade_result(Position(pnl=5.0, entry_timestamp=time.time()))
        sig = self._make_signal()
        result = rm.evaluate_signal(sig, self._portfolio_state())
        self.assertEqual(result["decision"], "APPROVE")

    def test_reject_low_net_ev(self):
        rm, pm, ks = self._make_rm()
        sig = self._make_signal(
            net_ev=0.02,
            edge=0.15,
        )
        result = rm.evaluate_signal(sig, self._portfolio_state())
        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("net_ev", result["reason"].lower())

    def test_reject_sniper_stricter_floor(self):
        rm, pm, ks = self._make_rm()
        sig = self._make_signal(
            strategy="sniper",
            net_ev=0.04,
            edge=0.10,
        )
        result = rm.evaluate_signal(sig, self._portfolio_state())
        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("sniper", result["reason"])

    def test_reject_high_volatility(self):
        rm, pm, ks = self._make_rm()
        sig = self._make_signal()
        result = rm.evaluate_signal(
            sig, self._portfolio_state(vol=config.VOLATILITY_CIRCUIT_BREAKER + 1)
        )
        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("Volatility", result["reason"])

    def test_get_risk_status(self):
        rm, pm, ks = self._make_rm()
        s = rm.get_risk_status()
        self.assertIn("kill_switch", s)
        self.assertIn("drawdown_pct", s)
        self.assertIn("daily_pnl", s)
        self.assertIn("exposure_pct", s)
        self.assertIn("trading_allowed", s)
        self.assertIn("trading_blockers", s)
        self.assertIn("limits_headroom", s)
        self.assertIn("total_equity", s)
        self.assertIn("daily_loss_limit_pct", s)
        self.assertIn("daily_limit_usd", s)
        self.assertIn("drawdown_usd", s)

    def test_daily_loss_limit_usd_from_session_start(self):
        """DAILY_LOSS_LIMIT_PCT of session-start equity."""
        rm, pm, ks = self._make_rm()
        rm.set_session_start_equity(100.0)
        expected = 100.0 * config.DAILY_LOSS_LIMIT_PCT
        self.assertAlmostEqual(rm.daily_loss_limit_usd(), expected)

    def test_paper_warn_only_continues_despite_daily_loss(self):
        """Paper warn-only mode: daily loss breached but signal APPROVED, not REJECTED."""
        orig_mode = config.EXECUTION_MODE
        orig_warn = config.PAPER_RISK_WARN_ONLY
        try:
            config.EXECUTION_MODE = "paper"
            config.PAPER_RISK_WARN_ONLY = True
            rm, pm, ks = self._make_rm()
            rm.set_session_start_equity(100.0)
            # Simulate losses exceeding daily limit ($25 > $15)
            for _ in range(5):
                rm.record_trade_result(Position(pnl=-5.0, entry_timestamp=time.time()))
            sig = self._make_signal()
            result = rm.evaluate_signal(sig, self._portfolio_state())
            # Should NOT reject — paper warn only
            self.assertNotEqual(result["decision"], "REJECT",
                                "Paper warn-only should not hard-block on daily loss")
        finally:
            config.EXECUTION_MODE = orig_mode
            config.PAPER_RISK_WARN_ONLY = orig_warn

    def test_daily_limit_fixed_across_session(self):
        """Daily cap does not change when current equity changes."""
        rm, pm, ks = self._make_rm()
        rm.set_session_start_equity(100.0)
        cap_before = rm.daily_loss_limit_usd()
        # Simulate equity change (doesn't affect the session-start anchor)
        pm.set_capital(80.0)
        cap_after = rm.daily_loss_limit_usd()
        self.assertAlmostEqual(cap_before, cap_after)
        expected = 100.0 * config.DAILY_LOSS_LIMIT_PCT
        self.assertAlmostEqual(cap_after, expected)

    def test_record_trade_result_counts_one_performance_trade_per_resolution(self):
        """One closed position in PM → one Performance row via analytics."""
        rm, pm, ks = self._make_rm()
        pa = rm._analytics
        pos = Position(pnl=-2.5, entry_timestamp=time.time(), status="RESOLVED")
        # In production, pm.close_position() adds to _closed_positions.
        # Simulate that here since we're testing analytics reads from PM.
        pm._closed_positions.append(pos)
        rm.record_trade_result(pos)
        s = pa.get_summary()
        self.assertEqual(s["total_trades"], 1, "each resolution must add exactly one trade")
        self.assertAlmostEqual(s["total_pnl"], -2.5)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["wins"], 0)


class TestHealthMonitor(unittest.TestCase):

    def test_healthy_initially(self):
        agg = Aggregator(test_mode=True)
        hm = HealthMonitor(agg)
        # No feeds connected in test mode, but warming_up check will fail
        status = hm.run_health_check()
        self.assertIsInstance(status, dict)
        self.assertIn("any_feed_ok", status)

    def test_warnings_no_feeds(self):
        agg = Aggregator(test_mode=True)
        hm = HealthMonitor(agg)
        warnings = hm.get_warnings()
        self.assertIsInstance(warnings, list)
        # Should have feed warnings since no ticks
        self.assertGreater(len(warnings), 0)


if __name__ == "__main__":
    unittest.main()
