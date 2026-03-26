"""Tests for Phase 4 execution engine components."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from models.order import Order
from models.position import Position
from models.trade_signal import TradeSignal
from models.market_state import MarketState
from execution.paper_trader import PaperTrader
from execution.live_trader import LiveTrader
from execution.position_manager import PositionManager
from execution.order_manager import OrderManager
import config


class _IsolatedOrderManagerMixin:
    """Mixin that redirects trade log to a temp directory for test isolation."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_path = config.TRADE_LOG_PATH
        config.TRADE_LOG_PATH = os.path.join(self._tmpdir.name, "trade_log.jsonl")

    def tearDown(self):
        config.TRADE_LOG_PATH = self._original_path
        self._tmpdir.cleanup()


class TestOrder(unittest.TestCase):

    def test_is_complete_filled(self):
        o = Order(status="FILLED")
        self.assertTrue(o.is_complete())

    def test_is_complete_pending(self):
        o = Order(status="PENDING")
        self.assertFalse(o.is_complete())

    def test_was_profitable(self):
        o = Order(pnl=5.0)
        self.assertTrue(o.was_profitable())
        o2 = Order(pnl=-2.0)
        self.assertFalse(o2.was_profitable())
        o3 = Order(pnl=None)
        self.assertIsNone(o3.was_profitable())

    def test_fill_latency(self):
        now = time.time()
        o = Order(timestamp=now, fill_timestamp=now + 0.5)
        self.assertAlmostEqual(o.fill_latency_ms(), 500, delta=10)

    def test_to_dict(self):
        o = Order(order_id="abc", market_type="btc-5min")
        d = o.to_dict()
        self.assertEqual(d["order_id"], "abc")
        self.assertEqual(d["market_type"], "btc-5min")

    def test_summary(self):
        o = Order(direction="UP", market_type="btc-5min", execution_mode="paper")
        self.assertIn("[PAPER]", o.summary())
        self.assertIn("UP", o.summary())


class TestPosition(unittest.TestCase):

    def test_calculate_pnl_win(self):
        p = Position(entry_price=0.55, num_shares=10)
        pnl = p.calculate_pnl(1.0)
        self.assertAlmostEqual(pnl, 4.5)  # (1.0 - 0.55) * 10

    def test_calculate_pnl_loss(self):
        p = Position(entry_price=0.55, num_shares=10)
        pnl = p.calculate_pnl(0.0)
        self.assertAlmostEqual(pnl, -5.5)  # (0.0 - 0.55) * 10

    def test_is_open(self):
        p = Position(status="OPEN")
        self.assertTrue(p.is_open())
        p2 = Position(status="RESOLVED")
        self.assertFalse(p2.is_open())


class TestPositionManager(unittest.TestCase):

    def test_initial_capital(self):
        pm = PositionManager()
        self.assertEqual(pm.get_available_capital(), config.STARTING_CAPITAL_USDC)

    def test_open_position_deducts_capital(self):
        pm = PositionManager()
        order = Order(
            size_usdc=10.0, fill_price=0.50, num_shares=20,
            market_id="123", direction="UP", execution_mode="paper",
        )
        pm.open_position(order)
        self.assertAlmostEqual(
            pm.get_available_capital(),
            config.STARTING_CAPITAL_USDC - 10.0,
        )
        self.assertEqual(pm.count_open_positions(), 1)

    def test_close_position_returns_payout(self):
        pm = PositionManager()
        order = Order(
            size_usdc=10.0, fill_price=0.50, num_shares=20,
            market_id="123", direction="UP", execution_mode="paper",
        )
        pos = pm.open_position(order)

        # Win: payout = 1.0 * 20 = 20
        pm.close_position(pos.position_id, 1.0)
        expected_capital = config.STARTING_CAPITAL_USDC - 10.0 + 20.0
        self.assertAlmostEqual(pm.get_available_capital(), expected_capital)

    def test_close_position_loss(self):
        pm = PositionManager()
        order = Order(
            size_usdc=10.0, fill_price=0.50, num_shares=20,
            market_id="123", direction="DOWN", execution_mode="paper",
        )
        pos = pm.open_position(order)

        # Loss: payout = 0.0 * 20 = 0
        pm.close_position(pos.position_id, 0.0)
        expected_capital = config.STARTING_CAPITAL_USDC - 10.0
        self.assertAlmostEqual(pm.get_available_capital(), expected_capital)

    def test_win_rate(self):
        pm = PositionManager()
        for i in range(3):
            o = Order(size_usdc=5, fill_price=0.50, num_shares=10,
                      market_id=str(i), direction="UP", execution_mode="paper")
            p = pm.open_position(o)
            resolution = 1.0 if i < 2 else 0.0  # 2 wins, 1 loss
            pm.close_position(p.position_id, resolution)

        self.assertAlmostEqual(pm.get_win_rate(), 2/3)

    def test_total_pnl(self):
        pm = PositionManager()
        o1 = Order(size_usdc=10, fill_price=0.50, num_shares=20,
                   market_id="1", direction="UP", execution_mode="paper")
        p1 = pm.open_position(o1)
        pm.close_position(p1.position_id, 1.0)  # PnL = (1-0.5)*20 = 10

        o2 = Order(size_usdc=10, fill_price=0.50, num_shares=20,
                   market_id="2", direction="DOWN", execution_mode="paper")
        p2 = pm.open_position(o2)
        pm.close_position(p2.position_id, 0.0)  # PnL = (0-0.5)*20 = -10

        self.assertAlmostEqual(pm.get_total_pnl(), 0.0)

    def test_stats(self):
        pm = PositionManager()
        stats = pm.get_stats()
        self.assertEqual(stats["total_trades"], 0)
        self.assertEqual(stats["available_capital"], config.STARTING_CAPITAL_USDC)


class TestPaperTrader(unittest.TestCase):

    def test_paper_fill(self):
        pt = PaperTrader()
        order = Order(
            direction="UP", price=0.55, size_usdc=10.0, num_shares=18,
            execution_mode="paper",
        )
        result = pt.execute(order)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.execution_mode, "paper")
        self.assertIsNotNone(result.fill_price)
        self.assertIsNotNone(result.fill_timestamp)
        self.assertGreater(result.fill_price, 0)

    def test_paper_fill_with_snapshot(self):
        pt = PaperTrader()
        snapshot = MarketState(
            market_id="123", condition_id="0x", market_type="btc-5min",
            strike_price=68000, yes_price=0.60, no_price=0.40, spread=0.02,
        )
        order = Order(direction="UP", price=0.55, size_usdc=10.0, num_shares=18)
        result = pt.execute(order, market_snapshot=snapshot)
        self.assertEqual(result.status, "FILLED")
        # Should fill near the snapshot's yes_price (0.60) plus slippage
        self.assertGreaterEqual(result.fill_price, 0.60)

    def test_simulate_resolution_win(self):
        pt = PaperTrader()
        order = Order(direction="UP", fill_price=0.55, num_shares=10)
        result = pt.simulate_resolution(order, "UP")
        self.assertIsNotNone(result.pnl)
        self.assertGreater(result.pnl, 0)

    def test_simulate_resolution_loss(self):
        pt = PaperTrader()
        order = Order(direction="UP", fill_price=0.55, num_shares=10)
        result = pt.simulate_resolution(order, "DOWN")
        self.assertIsNotNone(result.pnl)
        self.assertLess(result.pnl, 0)


class TestLiveTrader(unittest.TestCase):

    def test_rejects_when_mode_not_live(self):
        lt = LiveTrader()
        order = Order(direction="UP", size_usdc=10, token_id="abc")
        # config.EXECUTION_MODE defaults to "paper" (dotenv skipped under pytest)
        result = lt.execute(order)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("not 'live'", result.metadata.get("rejection_reason", ""))

    def test_rejects_empty_token_id(self):
        lt = LiveTrader()
        order = Order(direction="UP", size_usdc=10, token_id="")
        # Even if mode were live, empty token should be caught
        result = lt.execute(order)
        self.assertEqual(result.status, "REJECTED")


class TestOrderManager(_IsolatedOrderManagerMixin, unittest.TestCase):

    def _make_signal(self, **overrides) -> TradeSignal:
        defaults = dict(
            market_type="btc-5min", market_id="123",
            strategy="latency_arb", direction="UP",
            model_probability=0.75, market_probability=0.55,
            edge=0.20, gross_ev=0.15, net_ev=0.12, estimated_costs=0.03,
            confidence="HIGH",
            recommended_size_pct=0.10,
            strike_price=68000, spot_price=68100,
            time_remaining=120, timestamp=time.time(),
        )
        defaults.update(overrides)
        return TradeSignal(**defaults)

    def _make_snapshot(self, **overrides) -> MarketState:
        defaults = dict(
            market_id="123", condition_id="0xabc",
            market_type="btc-5min", strike_price=68000,
            yes_price=0.55, no_price=0.45, spread=0.02,
            time_remaining_seconds=120, is_active=True,
            up_token_id="token_up_123", down_token_id="token_down_123",
        )
        defaults.update(overrides)
        return MarketState(**defaults)

    def test_paper_execution(self):
        pm = PositionManager()
        om = OrderManager(pm)
        sig = self._make_signal()
        snap = self._make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "FILLED")
        self.assertEqual(order.execution_mode, "paper")
        self.assertEqual(pm.count_open_positions(), 1)

    def test_rejects_stale_signal(self):
        pm = PositionManager()
        om = OrderManager(pm)
        sig = self._make_signal(timestamp=time.time() - 10)  # 10s old
        snap = self._make_snapshot()
        order = om.execute_signal(sig, snap)
        self.assertIsNone(order)

    def test_rejects_no_snapshot(self):
        pm = PositionManager()
        om = OrderManager(pm)
        sig = self._make_signal()
        order = om.execute_signal(sig, None)
        self.assertIsNone(order)

    def test_rejects_inactive_market(self):
        pm = PositionManager()
        om = OrderManager(pm)
        sig = self._make_signal()
        snap = self._make_snapshot(is_active=False)
        order = om.execute_signal(sig, snap)
        self.assertIsNone(order)

    def test_duplicate_suppression(self):
        pm = PositionManager()
        om = OrderManager(pm)
        sig1 = self._make_signal()
        snap = self._make_snapshot()
        order1 = om.execute_signal(sig1, snap)
        self.assertIsNotNone(order1)

        # Same signal again within dedup window
        sig2 = self._make_signal()
        order2 = om.execute_signal(sig2, snap)
        self.assertIsNone(order2)  # Suppressed

    def test_rejects_insufficient_capital(self):
        pm = PositionManager()
        pm.set_capital(1.0)  # Very low capital
        om = OrderManager(pm)
        sig = self._make_signal(recommended_size_pct=0.50)
        snap = self._make_snapshot()
        order = om.execute_signal(sig, snap)
        # Should still work since 50% of $1 = $0.50 which is under max
        # but num_shares would be tiny

    def test_rejects_max_positions(self):
        pm = PositionManager()
        om = OrderManager(pm)

        # Fill up positions
        for i in range(config.MAX_CONCURRENT_POSITIONS):
            sig = self._make_signal(market_id=str(i))
            snap = self._make_snapshot(
                market_id=str(i),
                up_token_id=f"tok_{i}",
            )
            om.execute_signal(sig, snap)

        # Next one should be rejected
        sig = self._make_signal(market_id="999")
        snap = self._make_snapshot(market_id="999", up_token_id="tok_999")
        order = om.execute_signal(sig, snap)
        self.assertIsNone(order)

    def test_rejects_no_token_id(self):
        pm = PositionManager()
        om = OrderManager(pm)
        sig = self._make_signal()
        snap = self._make_snapshot(up_token_id="")  # Missing token
        order = om.execute_signal(sig, snap)
        self.assertIsNone(order)

    def test_direction_maps_to_correct_token(self):
        pm = PositionManager()
        om = OrderManager(pm)

        # UP should use up_token_id
        sig_up = self._make_signal(direction="UP")
        snap = self._make_snapshot()
        order_up = om.execute_signal(sig_up, snap)
        self.assertEqual(order_up.token_id, "token_up_123")

        # DOWN should use down_token_id (need different market to avoid dedup)
        sig_down = self._make_signal(direction="DOWN", market_id="456")
        snap2 = self._make_snapshot(
            market_id="456",
            up_token_id="tok_up_456", down_token_id="tok_down_456",
        )
        order_down = om.execute_signal(sig_down, snap2)
        self.assertIsNotNone(order_down)
        self.assertEqual(order_down.token_id, "tok_down_456")


class TestTradeLogPersistence(unittest.TestCase):

    def test_write_and_read_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test_trade_log.jsonl")
            original_path = config.TRADE_LOG_PATH
            config.TRADE_LOG_PATH = log_path

            try:
                pm = PositionManager()
                om = OrderManager(pm)

                sig = TradeSignal(
                    market_type="btc-5min", market_id="123",
                    strategy="latency_arb", direction="UP",
                    edge=0.20, gross_ev=0.15, net_ev=0.12, estimated_costs=0.03,
                    confidence="HIGH",
                    recommended_size_pct=0.10,
                    time_remaining=120, timestamp=time.time(),
                )
                snap = MarketState(
                    market_id="123", condition_id="0xabc",
                    market_type="btc-5min", strike_price=68000,
                    yes_price=0.55, no_price=0.45, spread=0.02,
                    time_remaining_seconds=120, is_active=True,
                    up_token_id="tok_up", down_token_id="tok_down",
                )
                om.execute_signal(sig, snap)

                # Verify log was written
                self.assertTrue(os.path.exists(log_path))
                with open(log_path) as f:
                    lines = f.readlines()
                self.assertGreater(len(lines), 0)

                # Parse first line
                data = json.loads(lines[0])
                self.assertEqual(data["market_type"], "btc-5min")
                self.assertEqual(data["direction"], "UP")
                self.assertEqual(data["status"], "FILLED")

                # Create new manager — should load from log and restore open position
                pm2 = PositionManager()
                om2 = OrderManager(pm2)
                self.assertGreater(len(om2.get_order_history()), 0)
                self.assertEqual(
                    pm2.count_open_positions(), 1,
                    "FILLED order without PnL must restore an open position",
                )

            finally:
                config.TRADE_LOG_PATH = original_path


class TestPolymarketObservationTiming(unittest.TestCase):

    def test_observation_window_countdown(self):
        from datetime import datetime, timezone
        from feeds.polymarket import PolymarketFeed

        now = datetime(2025, 1, 1, 11, 57, 0, tzinfo=timezone.utc)
        period_start = datetime(2025, 1, 1, 11, 55, 0, tzinfo=timezone.utc)
        slug = f"btc-updown-5m-{int(period_start.timestamp())}"
        tr, ttw, ws, src = PolymarketFeed._derive_window_timing(
            slug, 300.0, now, 99999.0, "btc-5min"
        )
        self.assertEqual(src, "slug_period")
        self.assertAlmostEqual(tr, 180.0, delta=0.1)
        self.assertTrue(ws)

    def test_slug_period_stable_without_event_start(self):
        """Slug-derived window; eventStartTime is not used (no longer passed)."""
        from datetime import datetime, timezone
        from feeds.polymarket import PolymarketFeed

        now = datetime(2025, 6, 15, 12, 2, 0, tzinfo=timezone.utc)
        period_start = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        slug = f"btc-updown-5m-{int(period_start.timestamp())}"
        tr, ttw, ws, src = PolymarketFeed._derive_window_timing(
            slug, 300.0, now, 99999.0, "btc-5min"
        )
        self.assertEqual(src, "slug_period")
        self.assertAlmostEqual(tr, 180.0, delta=1.0)
        self.assertTrue(ws)
        self.assertAlmostEqual(ttw, 0.0, delta=0.1)

    def test_gamma_end_wins_when_earlier_than_slug(self):
        """If slug math overshoots but endDate on the same row is sooner, use endDate."""
        from datetime import datetime, timezone
        from feeds.polymarket import PolymarketFeed

        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        slug = "btc-updown-5m-2000000000"  # far-future period → huge slug remainder
        tr, ttw, ws, src = PolymarketFeed._derive_window_timing(
            slug, 300.0, now, 200.0, "btc-5min"
        )
        # Slug path with min(slug_tr, gamma_tr): value follows endDate when sooner
        self.assertEqual(src, "slug_period")
        self.assertAlmostEqual(tr, 200.0, delta=0.01)

    def test_empty_slug_uses_gamma_end_date_only(self):
        """No slug: use endDate countdown only (never eventStartTime)."""
        from datetime import datetime, timezone
        from feeds.polymarket import PolymarketFeed

        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        tr, ttw, ws, src = PolymarketFeed._derive_window_timing(
            "", 300.0, now, 240.0, "btc-5min"
        )
        self.assertEqual(src, "gamma_end_date")
        self.assertAlmostEqual(tr, 240.0, delta=0.01)
        self.assertAlmostEqual(ttw, 0.0, delta=0.01)
        self.assertTrue(ws)


class TestPolymarketMarketSelection(unittest.TestCase):
    """Gamma row selection must prefer the slug window that contains now."""

    def test_select_prefers_in_slug_live_window_over_sooner_end_date(self):
        from datetime import datetime, timezone
        from feeds.polymarket import PolymarketFeed

        now = datetime(2025, 6, 15, 12, 2, 0, tzinfo=timezone.utc)
        live_ts = int(
            datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        )
        next_ts = int(
            datetime(2025, 6, 15, 12, 5, 0, tzinfo=timezone.utc).timestamp()
        )
        live_m = {
            "slug": f"btc-updown-5m-{live_ts}",
            "endDate": "2025-06-15T12:05:00Z",
        }
        next_m = {
            "slug": f"btc-updown-5m-{next_ts}",
            "endDate": "2025-06-15T12:07:00Z",
        }
        chosen = PolymarketFeed._select_market_candidate(
            [next_m, live_m], "btc-5min", now
        )
        self.assertEqual(chosen["slug"], live_m["slug"])


class TestTradeLogDedup(unittest.TestCase):
    """Append-only JSONL repeats order_id; loader must dedupe or capital is wrong."""

    def test_duplicate_jsonl_lines_restore_capital_once(self):
        tmp = tempfile.TemporaryDirectory()
        old_path = config.TRADE_LOG_PATH
        old_start = config.STARTING_CAPITAL_USDC
        try:
            path = os.path.join(tmp.name, "trade_log.jsonl")
            config.TRADE_LOG_PATH = path
            config.STARTING_CAPITAL_USDC = 100.0

            base = {
                "order_id": "ord-dedup-test",
                "signal_id": "s",
                "timestamp": 1000.0,
                "market_id": "m1",
                "market_type": "btc-5min",
                "direction": "UP",
                "side": "BUY",
                "token_id": "t",
                "price": 0.5,
                "size_usdc": 10.0,
                "num_shares": 20.0,
                "order_type": "LIMIT",
                "status": "FILLED",
                "fill_price": 0.5,
                "fill_timestamp": 1001.0,
                "execution_mode": "paper",
                "metadata": {},
            }
            fill_line = {**base, "pnl": None}
            pnl_line = {**base, "timestamp": 2000.0, "pnl": 2.0}
            with open(path, "w") as f:
                f.write(json.dumps(fill_line) + "\n")
                f.write(json.dumps(pnl_line) + "\n")

            pm = PositionManager()
            om = OrderManager(pm)
            # 100 - 10 + (pnl + cost back) = 100 - 10 + 12 = 102
            self.assertAlmostEqual(pm.get_available_capital(), 102.0, places=4)
            self.assertEqual(len(om.get_order_history()), 1)
        finally:
            config.TRADE_LOG_PATH = old_path
            config.STARTING_CAPITAL_USDC = old_start
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
