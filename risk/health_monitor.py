"""Health monitor — lightweight system health assessment."""

from __future__ import annotations

import time
from typing import Optional

from feeds.aggregator import Aggregator
from utils.logger import get_logger
import config

log = get_logger("health")


class HealthMonitor:
    """Monitors overall system health and emits warnings."""

    def __init__(self, aggregator: Aggregator):
        self._agg = aggregator
        self._last_polymarket_success: float = time.time()
        self._last_signal_time: float = 0.0
        self._last_execution_time: float = 0.0

    def record_signal(self) -> None:
        self._last_signal_time = time.time()

    def record_execution(self) -> None:
        self._last_execution_time = time.time()

    def record_polymarket_success(self) -> None:
        self._last_polymarket_success = time.time()

    def run_health_check(self) -> dict:
        """Run all health checks and return status dict."""
        now = time.time()
        binance_ok = self._check_feed(self._agg.latest_binance_tick, "Binance")
        coinbase_ok = self._check_feed(self._agg.latest_coinbase_tick, "Coinbase")
        any_feed_ok = binance_ok or coinbase_ok

        poly_age = now - self._last_polymarket_success
        poly_ok = poly_age < config.KILL_SWITCH_FEED_TIMEOUT
        if self._agg.polymarket_feed:
            if self._agg.polymarket_feed.poll_count > 0 and self._agg.polymarket_feed.api_errors == 0:
                poly_ok = True
            self._last_polymarket_success = now  # Reset if feed object exists and is polling

        latency_ok = True
        tick = self._agg.get_current_price()
        feed_latency = None
        if tick:
            # Use age_ms() for Binance (true exchange-to-local latency)
            # For Coinbase age_ms() ≈ 0 (no exchange timestamp), use staleness
            if tick.source == "binance":
                feed_latency = tick.age_ms()
            else:
                feed_latency = tick.staleness_ms()
            if feed_latency > config.MAX_ACCEPTABLE_LATENCY_MS:
                latency_ok = False

        return {
            "binance_ok": binance_ok,
            "coinbase_ok": coinbase_ok,
            "any_feed_ok": any_feed_ok,
            "polymarket_ok": poly_ok,
            "latency_ok": latency_ok,
            "feed_latency_ms": feed_latency,
            "polymarket_age_s": poly_age,
            "volatility_available": self._agg.get_volatility() is not None,
            "warming_up": self._agg.warming_up,
        }

    def is_system_healthy(self) -> bool:
        """Quick health check — True if core systems are operational."""
        status = self.run_health_check()
        return (
            status["any_feed_ok"]
            and status["latency_ok"]
            and not status["warming_up"]
        )

    def get_warnings(self) -> list[str]:
        """Return list of current warning messages."""
        status = self.run_health_check()
        warnings = []
        if not status["binance_ok"]:
            warnings.append("Binance feed unhealthy")
        if not status["coinbase_ok"]:
            warnings.append("Coinbase feed unhealthy")
        if not status["any_feed_ok"]:
            warnings.append("ALL feeds unhealthy — no price data")
        if not status["polymarket_ok"]:
            warnings.append(f"Polymarket stale ({status['polymarket_age_s']:.0f}s)")
        if not status["latency_ok"]:
            warnings.append(f"High latency: {status['feed_latency_ms']:.0f}ms")
        if not status["volatility_available"]:
            warnings.append("Volatility data not yet available")
        # Price source divergence check (USDT vs USD basis — $15-50 is normal)
        gap = self._agg.get_price_source_gap()
        if gap is not None and gap > config.PRICE_SOURCE_DIVERGENCE_FAIL_USD:
            warnings.append(f"BTC price source gap CRITICAL: ${gap:.0f} — possible feed error")
        elif gap is not None and gap > config.PRICE_SOURCE_DIVERGENCE_WARN_USD:
            warnings.append(f"BTC USDT/USD basis elevated: ${gap:.0f} (normal: $15-50)")
        return warnings

    def _check_feed(self, tick, label: str) -> bool:
        """Check if a feed tick is recent and not stale."""
        if tick is None:
            return False
        if tick.is_stale:
            return False
        age = time.time() - tick.local_timestamp
        return age < config.STALE_THRESHOLD * 2
