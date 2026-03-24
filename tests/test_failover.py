"""Failover simulation tests.

Verifies that the aggregator correctly handles:
- Binance disconnect (no ticks → stale → failover to Coinbase)
- Binance recovery (fresh ticks → failback to Binance)
- Both feeds stale (keeps last active source)
"""

from __future__ import annotations

import asyncio
import time
import unittest

from models.price_tick import PriceTick
from feeds.aggregator import Aggregator
import config


class TestFailoverSimulation(unittest.TestCase):
    """End-to-end failover simulation using test_mode aggregator."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_agg(self):
        agg = Aggregator(test_mode=True)
        agg.skip_warmup()
        return agg

    def _make_tick(self, source: str, price: float, age_seconds: float = 0.0) -> PriceTick:
        now = time.time()
        return PriceTick(
            timestamp=now - age_seconds,
            price=price,
            source=source,
            local_timestamp=now - age_seconds,
        )

    def test_simulate_binance_disconnect(self):
        """Simulate Binance going silent — aggregator should failover."""
        agg = self._make_agg()
        now = time.time()

        # Both feeds sending data
        self._run(agg.inject_tick(self._make_tick("binance", 68000.0)))
        self._run(agg.inject_tick(self._make_tick("coinbase", 67999.0)))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 1)
        agg._check_staleness(agg.latest_coinbase_tick, "Coinbase", now + 1)
        agg._select_source()
        self.assertEqual(agg.current_active_source, "binance")

        # Binance stops sending — simulate 6s passing without new Binance tick
        fresh_cb = PriceTick(
            timestamp=now + 5.5, price=67998.0, source="coinbase",
            local_timestamp=now + 5.5,
        )
        self._run(agg.inject_tick(fresh_cb))

        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        agg._check_staleness(agg.latest_coinbase_tick, "Coinbase", now + 6.0)
        agg._select_source()

        self.assertTrue(agg.latest_binance_tick.is_stale)
        self.assertFalse(agg.latest_coinbase_tick.is_stale)
        self.assertEqual(agg.current_active_source, "coinbase")
        self.assertEqual(agg.failover_events, 1)
        self.assertEqual(agg.stale_events, 1)

        current = agg.get_current_price()
        self.assertEqual(current.source, "coinbase")

    def test_simulate_binance_recovery(self):
        """After failover, Binance recovery should trigger failback."""
        agg = self._make_agg()
        now = time.time()

        self._run(agg.inject_tick(self._make_tick("binance", 68000.0)))
        self._run(agg.inject_tick(self._make_tick("coinbase", 67999.0)))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        agg._select_source()
        self.assertEqual(agg.current_active_source, "coinbase")

        recovery_tick = PriceTick(
            timestamp=now + 7.0, price=68005.0, source="binance",
            local_timestamp=now + 7.0,
        )
        self._run(agg.inject_tick(recovery_tick))
        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 7.5)
        agg._select_source()

        self.assertEqual(agg.current_active_source, "binance")
        self.assertFalse(agg.latest_binance_tick.is_stale)
        self.assertEqual(agg.failover_events, 2)

    def test_both_feeds_stale_keeps_last_source(self):
        """If both feeds go stale, keep the last active source."""
        agg = self._make_agg()
        now = time.time()

        self._run(agg.inject_tick(self._make_tick("binance", 68000.0)))
        self._run(agg.inject_tick(self._make_tick("coinbase", 67999.0)))

        agg._check_staleness(agg.latest_binance_tick, "Binance", now + 6.0)
        agg._check_staleness(agg.latest_coinbase_tick, "Coinbase", now + 6.0)

        agg._select_source()
        self.assertEqual(agg.current_active_source, "binance")
        self.assertTrue(agg.latest_binance_tick.is_stale)
        self.assertTrue(agg.latest_coinbase_tick.is_stale)


if __name__ == "__main__":
    unittest.main()
