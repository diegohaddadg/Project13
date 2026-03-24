"""Lightweight tests for Phase 1 core components."""

from __future__ import annotations

import asyncio
import time
import unittest

from models.price_tick import PriceTick
from feeds.aggregator import Aggregator
import config


class TestPriceTick(unittest.TestCase):
    """Tests for PriceTick data model."""

    def test_age_ms_uses_exchange_timestamp(self):
        """age_ms should measure exchange-to-local latency."""
        now = time.time()
        tick = PriceTick(
            timestamp=now - 0.1,       # exchange event 100ms ago
            price=68000.0,
            source="binance",
            local_timestamp=now - 0.02,  # received 20ms ago
        )
        # age_ms = local_timestamp - timestamp = 80ms
        age = tick.age_ms()
        self.assertAlmostEqual(age, 80.0, delta=5.0)

    def test_staleness_ms_uses_wall_clock(self):
        """staleness_ms should measure time since local receipt."""
        now = time.time()
        tick = PriceTick(
            timestamp=now - 1.0,
            price=68000.0,
            source="binance",
            local_timestamp=now - 0.05,
        )
        staleness = tick.staleness_ms()
        self.assertGreaterEqual(staleness, 40.0)
        self.assertLess(staleness, 200.0)  # generous upper bound

    def test_coinbase_age_ms_near_zero(self):
        """Coinbase ticks have same timestamp and local_timestamp, so age_ms ≈ 0."""
        now = time.time()
        tick = PriceTick(
            timestamp=now,
            price=68000.0,
            source="coinbase",
            local_timestamp=now,
        )
        self.assertAlmostEqual(tick.age_ms(), 0.0, delta=1.0)

    def test_repr_includes_source_and_price(self):
        tick = PriceTick(timestamp=time.time(), price=67500.50, source="binance")
        r = repr(tick)
        self.assertIn("binance", r)
        self.assertIn("67,500.50", r)

    def test_repr_shows_stale_tag(self):
        tick = PriceTick(timestamp=time.time(), price=67500.0, source="binance", is_stale=True)
        self.assertIn("[STALE]", repr(tick))


class TestAggregatorStaleDetection(unittest.TestCase):
    """Tests for aggregator staleness and failover logic."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_agg(self):
        agg = Aggregator(test_mode=True)
        agg.skip_warmup()
        return agg

    def test_fresh_tick_not_stale(self):
        agg = self._make_agg()
        now = time.time()
        tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        self._run(agg.inject_tick(tick))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 1.0)
        self.assertFalse(agg.latest_binance_tick.is_stale)

    def test_old_tick_goes_stale(self):
        agg = self._make_agg()
        now = time.time()
        tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        self._run(agg.inject_tick(tick))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        self.assertTrue(agg.latest_binance_tick.is_stale)
        self.assertEqual(agg.stale_events, 1)

    def test_stale_event_counted_once(self):
        """Repeated staleness checks should only count one stale event."""
        agg = self._make_agg()
        now = time.time()
        tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        self._run(agg.inject_tick(tick))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 7.0)
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 8.0)
        self.assertEqual(agg.stale_events, 1)

    def test_failover_to_coinbase(self):
        """When Binance goes stale, source should switch to Coinbase."""
        agg = self._make_agg()
        now = time.time()

        b_tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        c_tick = PriceTick(timestamp=now, price=67999.0, source="coinbase", local_timestamp=now)
        self._run(agg.inject_tick(b_tick))
        self._run(agg.inject_tick(c_tick))
        agg._select_source()
        self.assertEqual(agg.current_active_source, "binance")

        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        agg._select_source()
        self.assertEqual(agg.current_active_source, "coinbase")
        self.assertEqual(agg.failover_events, 1)

    def test_failback_to_binance(self):
        """When Binance recovers, source should switch back."""
        agg = self._make_agg()
        now = time.time()

        b_old = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        c_tick = PriceTick(timestamp=now, price=67999.0, source="coinbase", local_timestamp=now)
        self._run(agg.inject_tick(b_old))
        self._run(agg.inject_tick(c_tick))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        agg._select_source()
        self.assertEqual(agg.current_active_source, "coinbase")

        b_new = PriceTick(timestamp=now + 7.0, price=68001.0, source="binance", local_timestamp=now + 7.0)
        self._run(agg.inject_tick(b_new))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 7.5)
        agg._select_source()
        self.assertEqual(agg.current_active_source, "binance")


class TestWarmup(unittest.TestCase):
    """Tests for warmup phase behavior."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_warmup_suppresses_metrics(self):
        """During warmup, ticks are received but metrics are not recorded."""
        agg = Aggregator(test_mode=True)
        # Do NOT call skip_warmup — warmup is active
        now = time.time()
        agg._warmup_until = now + 100  # far future

        tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        self._run(agg.inject_tick(tick))

        # Tick is stored for display but stats are not recorded
        self.assertIsNotNone(agg.latest_binance_tick)
        self.assertEqual(agg.binance_tick_count, 0)
        self.assertEqual(len(agg.binance_latencies), 0)
        self.assertIsNone(agg.get_volatility())

    def test_post_warmup_records_metrics(self):
        """After warmup, ticks should be recorded normally."""
        agg = Aggregator(test_mode=True)
        agg.skip_warmup()
        now = time.time()

        tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
        self._run(agg.inject_tick(tick))

        self.assertEqual(agg.binance_tick_count, 1)
        self.assertEqual(len(agg.binance_latencies), 1)

    def test_skip_warmup(self):
        agg = Aggregator(test_mode=True)
        self.assertTrue(agg.warming_up)
        agg.skip_warmup()
        self.assertFalse(agg.warming_up)


class TestVolatility(unittest.TestCase):
    """Tests for rolling volatility calculation."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_agg(self):
        agg = Aggregator(test_mode=True)
        agg.skip_warmup()
        return agg

    def test_insufficient_data_returns_none(self):
        agg = self._make_agg()
        self.assertIsNone(agg.get_volatility())

        now = time.time()
        for i in range(10):
            tick = PriceTick(timestamp=now, price=68000.0 + i, source="binance", local_timestamp=now)
            self._run(agg.inject_tick(tick))
        self.assertIsNone(agg.get_volatility())

    def test_constant_prices_zero_volatility(self):
        agg = self._make_agg()
        now = time.time()
        for _ in range(25):
            tick = PriceTick(timestamp=now, price=68000.0, source="binance", local_timestamp=now)
            self._run(agg.inject_tick(tick))
        self.assertAlmostEqual(agg.get_volatility(), 0.0, delta=0.001)

    def test_varying_prices_positive_volatility(self):
        agg = self._make_agg()
        now = time.time()
        prices = [68000.0 + (i % 5) * 10 for i in range(30)]
        for p in prices:
            tick = PriceTick(timestamp=now, price=p, source="binance", local_timestamp=now)
            self._run(agg.inject_tick(tick))
        vol = agg.get_volatility()
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0.0)


if __name__ == "__main__":
    unittest.main()
