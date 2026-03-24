"""Tests for Phase 6 dashboard components."""

from __future__ import annotations

import time
import unittest

from feeds.aggregator import Aggregator
from strategies.signal_engine import SignalEngine
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from risk.risk_manager import RiskManager
from risk.kill_switch import KillSwitch
from risk.exposure_tracker import ExposureTracker
from risk.performance_analytics import PerformanceAnalytics
from risk.health_monitor import HealthMonitor
from dashboard.state_adapter import StateAdapter
import config


def _make_adapter():
    """Create a StateAdapter with test-mode components."""
    agg = Aggregator(test_mode=True)
    engine = SignalEngine()
    pm = PositionManager()

    # Redirect trade log to avoid pollution
    import tempfile, os
    tmpdir = tempfile.mkdtemp()
    orig = config.TRADE_LOG_PATH
    config.TRADE_LOG_PATH = os.path.join(tmpdir, "test.jsonl")
    om = OrderManager(pm)
    config.TRADE_LOG_PATH = orig

    ks = KillSwitch()
    exp = ExposureTracker(pm)
    analytics = PerformanceAnalytics()
    hm = HealthMonitor(agg)
    rm = RiskManager(pm, ks, exp, analytics, hm)
    return StateAdapter(agg, engine, om, pm, rm, ks, analytics, hm), ks


class TestStateAdapter(unittest.TestCase):

    def test_get_status_snapshot(self):
        adapter, ks = _make_adapter()
        s = adapter.get_status_snapshot()
        self.assertIn("execution_mode", s)
        self.assertIn("trading_enabled", s)
        self.assertIn("kill_switch_active", s)
        self.assertIn("uptime_seconds", s)
        self.assertEqual(s["execution_mode"], "paper")

    def test_get_price_snapshot(self):
        adapter, ks = _make_adapter()
        p = adapter.get_price_snapshot()
        self.assertIn("price", p)
        self.assertIn("source", p)
        self.assertIn("binance", p)
        self.assertIn("coinbase", p)
        self.assertIn("sparkline", p)

    def test_get_market_snapshot(self):
        adapter, ks = _make_adapter()
        m = adapter.get_market_snapshot()
        self.assertIn("btc_5min", m)
        self.assertIn("btc_15min", m)
        # Both None in test mode (no feeds)
        self.assertIsNone(m["btc_5min"])

    def test_get_signal_snapshot(self):
        adapter, ks = _make_adapter()
        s = adapter.get_signal_snapshot()
        self.assertIn("active_strategies", s)
        self.assertIn("recent_signals", s)
        self.assertIsInstance(s["recent_signals"], list)

    def test_get_positions_snapshot(self):
        adapter, ks = _make_adapter()
        p = adapter.get_positions_snapshot()
        self.assertIn("available_capital", p)
        self.assertIn("open_positions", p)
        self.assertIn("recent_fills", p)

    def test_get_performance_snapshot(self):
        adapter, ks = _make_adapter()
        p = adapter.get_performance_snapshot()
        self.assertIn("total_trades", p)
        self.assertIn("win_rate", p)
        self.assertIn("strategy_breakdown", p)

    def test_get_risk_snapshot(self):
        adapter, ks = _make_adapter()
        r = adapter.get_risk_snapshot()
        self.assertIn("kill_switch", r)
        self.assertIn("drawdown_pct", r)
        self.assertIn("daily_pnl", r)

    def test_get_health_snapshot(self):
        adapter, ks = _make_adapter()
        h = adapter.get_health_snapshot()
        self.assertIn("any_feed_ok", h)
        self.assertIn("warnings", h)

    def test_get_full_snapshot(self):
        adapter, ks = _make_adapter()
        full = adapter.get_full_snapshot()
        self.assertIn("ts", full)
        self.assertIn("status", full)
        self.assertIn("prices", full)
        self.assertIn("markets", full)
        self.assertIn("signals", full)
        self.assertIn("positions", full)
        self.assertIn("performance", full)
        self.assertIn("risk", full)
        self.assertIn("health", full)

    def test_full_snapshot_serializable(self):
        """Full snapshot must be JSON-serializable."""
        import json
        adapter, ks = _make_adapter()
        full = adapter.get_full_snapshot()
        serialized = json.dumps(full, default=str)
        self.assertIsInstance(serialized, str)
        parsed = json.loads(serialized)
        self.assertIn("status", parsed)

    def test_sparkline_recording(self):
        adapter, ks = _make_adapter()
        adapter.record_price(68000.0, time.time())
        adapter.record_price(68001.0, time.time())
        p = adapter.get_price_snapshot()
        self.assertEqual(len(p["sparkline"]), 2)


class TestKillSwitchActivationOnly(unittest.TestCase):
    """Verify kill switch activation endpoint behavior."""

    def test_kill_switch_activates(self):
        _, ks = _make_adapter()
        self.assertFalse(ks.is_active())
        ks.activate("Dashboard test")
        self.assertTrue(ks.is_active())
        self.assertIn("Dashboard", ks.trigger_reason)

    def test_kill_switch_idempotent(self):
        """Activating an already-active kill switch should not change reason."""
        _, ks = _make_adapter()
        ks.activate("First reason")
        ks.activate("Second reason")
        self.assertEqual(ks.trigger_reason, "First reason")


class TestDashboardDisable(unittest.TestCase):
    """Verify dashboard can be disabled cleanly."""

    def test_dashboard_config_exists(self):
        self.assertTrue(hasattr(config, "DASHBOARD_ENABLED"))
        self.assertTrue(hasattr(config, "DASHBOARD_PORT"))
        self.assertTrue(hasattr(config, "DASHBOARD_AUTH_TOKEN"))


if __name__ == "__main__":
    unittest.main()
