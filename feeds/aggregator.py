from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

import numpy as np

from models.price_tick import PriceTick
from models.market_state import MarketState
from feeds.binance import BinanceFeed
from feeds.coinbase import CoinbaseFeed
from feeds.polymarket import PolymarketFeed
from utils.logger import get_logger
import config

log = get_logger("aggregator")


class SpotVsStrikeSnapshot:
    """Lightweight snapshot comparing spot BTC price to a market's strike."""

    def __init__(
        self,
        spot_price: float,
        strike_price: float,
        distance_to_strike: float,
        time_remaining_seconds: float,
        implied_up: float,
        implied_down: float,
        market_type: str,
    ):
        self.spot_price = spot_price
        self.strike_price = strike_price
        self.distance_to_strike = distance_to_strike
        self.time_remaining_seconds = time_remaining_seconds
        self.implied_up = implied_up
        self.implied_down = implied_down
        self.market_type = market_type

    def __repr__(self) -> str:
        return (
            f"SpotVsStrike({self.market_type} "
            f"spot=${self.spot_price:,.2f} strike=${self.strike_price:,.2f} "
            f"dist={self.distance_to_strike:+,.2f} "
            f"remaining={self.time_remaining_seconds:.0f}s)"
        )


class Aggregator:
    """Unified price feed with failover, staleness detection, volatility, and market state."""

    def __init__(self, test_mode: bool = False):
        self.latest_binance_tick: Optional[PriceTick] = None
        self.latest_coinbase_tick: Optional[PriceTick] = None
        self.current_active_source: str = "binance"
        # Separate rolling windows so volatility matches the same venue family as model spot
        self._price_window_binance: deque[float] = deque(maxlen=config.ROLLING_WINDOW_SIZE)
        self._price_window_coinbase: deque[float] = deque(maxlen=config.ROLLING_WINDOW_SIZE)
        # Timestamped price history for momentum computation (model spot source)
        self._spot_history: deque[tuple[float, float]] = deque(maxlen=600)  # (ts, price) ~60s at 10/s
        self._running = False

        # Warmup — suppress metrics, staleness, and volatility during startup
        self._warmup_until: float = 0.0
        self._warmup_complete = False

        # Stats
        self.binance_tick_count: int = 0
        self.coinbase_tick_count: int = 0
        self.stale_events: int = 0
        self.failover_events: int = 0
        self.binance_latencies: list[float] = []
        self.coinbase_latencies: list[float] = []

        # Test mode: don't connect to real feeds
        self._test_mode = test_mode
        if not test_mode:
            self._binance_feed = BinanceFeed(on_tick=self._on_binance_tick)
            self._coinbase_feed = CoinbaseFeed(on_tick=self._on_coinbase_tick)
            self._polymarket_feed = PolymarketFeed()
        else:
            self._binance_feed = None
            self._coinbase_feed = None
            self._polymarket_feed = None

    @property
    def warming_up(self) -> bool:
        return not self._warmup_complete

    async def _on_binance_tick(self, tick: PriceTick) -> None:
        self.latest_binance_tick = tick
        # Use Coinbase BTC/USD for strike if available (closer to Chainlink),
        # fall back to Binance BTC/USDT if Coinbase is down
        if self._polymarket_feed and (self.latest_coinbase_tick is None or self.latest_coinbase_tick.is_stale):
            self._polymarket_feed.set_spot_price(tick.price)
        if self.warming_up:
            return
        self._price_window_binance.append(tick.price)
        self.binance_tick_count += 1
        self.binance_latencies.append(tick.age_ms())

    async def _on_coinbase_tick(self, tick: PriceTick) -> None:
        self.latest_coinbase_tick = tick
        # Coinbase BTC/USD is preferred for strike capture (USD pair, closer to Chainlink)
        if self._polymarket_feed:
            self._polymarket_feed.set_spot_price(tick.price)
        if self.warming_up:
            return
        self.coinbase_tick_count += 1
        self.coinbase_latencies.append(tick.staleness_ms())
        self._price_window_coinbase.append(tick.price)
        # Record for momentum (Coinbase is model spot source)
        self._spot_history.append((tick.local_timestamp, tick.price))

    async def inject_tick(self, tick: PriceTick) -> None:
        """Inject a tick manually (for testing / simulation)."""
        if tick.source == "binance":
            await self._on_binance_tick(tick)
        elif tick.source == "coinbase":
            await self._on_coinbase_tick(tick)

    def skip_warmup(self) -> None:
        """Immediately end the warmup phase (for testing)."""
        self._warmup_until = 0.0
        self._warmup_complete = True

    async def start(self) -> None:
        """Start all feeds and the heartbeat monitor concurrently."""
        self._running = True
        self._warmup_until = time.time() + config.WARMUP_DURATION
        self._warmup_complete = False
        log.info(f"Starting aggregator (warmup {config.WARMUP_DURATION:.0f}s)...")

        if self._test_mode:
            await self._heartbeat_loop()
        else:
            await asyncio.gather(
                self._binance_feed.start(),
                self._coinbase_feed.start(),
                self._polymarket_feed.start(),
                self._heartbeat_loop(),
            )

    async def _heartbeat_loop(self) -> None:
        """Monitor feed health, mark stale feeds, switch active source."""
        while self._running:
            now = time.time()

            if not self._warmup_complete and now >= self._warmup_until:
                self._warmup_complete = True
                log.info("Warmup complete — normal operation started")

            if not self._warmup_complete:
                await asyncio.sleep(0.5)
                continue

            self._check_staleness(self.latest_binance_tick, "Binance", now)
            self._check_staleness(self.latest_coinbase_tick, "Coinbase", now)
            self._select_source()
            await asyncio.sleep(0.5)

    def _check_staleness(self, tick: Optional[PriceTick], label: str, now: float) -> None:
        """Check and update staleness for a feed tick."""
        if tick is None:
            return
        age = now - tick.local_timestamp
        if age > config.STALE_THRESHOLD:
            if not tick.is_stale:
                tick.is_stale = True
                self.stale_events += 1
                log.warning(f"{label} feed STALE ({age:.1f}s since last tick)")
        else:
            tick.is_stale = False

    def _select_source(self) -> None:
        """Select best active source. Prefer Binance unless stale."""
        prev_source = self.current_active_source
        binance_ok = self.latest_binance_tick is not None and not self.latest_binance_tick.is_stale
        coinbase_ok = self.latest_coinbase_tick is not None and not self.latest_coinbase_tick.is_stale

        if binance_ok:
            self.current_active_source = "binance"
        elif coinbase_ok:
            self.current_active_source = "coinbase"

        if self.current_active_source != prev_source:
            self.failover_events += 1
            log.warning(
                f"FAILOVER: {prev_source} → {self.current_active_source} "
                f"(event #{self.failover_events})"
            )

    # --- Price feed methods ---

    def get_current_price(self) -> Optional[PriceTick]:
        """Return the best available price tick based on active source."""
        if self.current_active_source == "binance" and self.latest_binance_tick:
            return self.latest_binance_tick
        if self.latest_coinbase_tick:
            return self.latest_coinbase_tick
        return self.latest_binance_tick

    def get_model_spot_price(self) -> Optional[float]:
        """Return the best USD-denominated spot price for model/strike use.

        Prefers Coinbase BTC/USD (closer to Chainlink settlement).
        Falls back to Binance BTC/USDT if Coinbase is unavailable.
        """
        c = self.latest_coinbase_tick
        if c and not c.is_stale:
            return c.price
        b = self.latest_binance_tick
        if b and not b.is_stale:
            return b.price
        return None

    def get_price_source_gap(self) -> Optional[float]:
        """Return absolute USD gap between Binance and Coinbase prices."""
        b = self.latest_binance_tick
        c = self.latest_coinbase_tick
        if b and c and not b.is_stale and not c.is_stale:
            return abs(b.price - c.price)
        return None

    def get_momentum(self) -> dict:
        """Compute short-horizon momentum from model spot price history.

        Returns moves over 5s, 10s, 30s windows. Values are null if
        insufficient history is available.
        """
        now = time.time()
        result = {}
        current = self.get_model_spot_price()
        if current is None or len(self._spot_history) < 2:
            for w in (5, 10, 30):
                result[f"move_{w}s"] = None
                result[f"abs_move_{w}s"] = None
                result[f"speed_{w}s"] = None
            return result

        for w in (5, 10, 30):
            cutoff = now - w
            past_price = None
            # Find the price closest to `w` seconds ago
            for ts, price in self._spot_history:
                if ts <= cutoff:
                    past_price = price
                else:
                    break
            if past_price is not None:
                move = current - past_price
                result[f"move_{w}s"] = round(move, 2)
                result[f"abs_move_{w}s"] = round(abs(move), 2)
                result[f"speed_{w}s"] = round(move / w, 4)
            else:
                result[f"move_{w}s"] = None
                result[f"abs_move_{w}s"] = None
                result[f"speed_{w}s"] = None
        return result

    def get_volatility(self) -> Optional[float]:
        """Rolling std of prices from the same source family as ``get_model_spot_price``.

        When Coinbase BTC/USD is live (non-stale), uses the Coinbase deque only.
        Otherwise uses the Binance deque. This avoids pairing Coinbase spot with
        Binance-only volatility.
        """
        c = self.latest_coinbase_tick
        if c and not c.is_stale:
            w = self._price_window_coinbase
            if len(w) < 20:
                return None
            return float(np.std(list(w)))
        b = self.latest_binance_tick
        if b and not b.is_stale:
            w = self._price_window_binance
            if len(w) < 20:
                return None
            return float(np.std(list(w)))
        return None

    def get_tick_age_ms(self) -> Optional[float]:
        """Return staleness of current active tick in milliseconds."""
        tick = self.get_current_price()
        return tick.staleness_ms() if tick else None

    # --- Market state methods (Phase 2) ---

    def get_current_market(self, market_type: str) -> Optional[MarketState]:
        """Return the current MarketState for a market type (e.g. 'btc-5min')."""
        if self._polymarket_feed:
            return self._polymarket_feed.get_market_state(market_type)
        return None

    def get_market_probability_snapshot(self, market_type: str) -> Optional[dict]:
        """Return implied probabilities for a market type.

        Returns dict with:
            implied_up, implied_down, midpoint, spread,
            time_remaining_seconds, is_near_resolution
        """
        state = self.get_current_market(market_type)
        if not state:
            return None
        return {
            "implied_up": state.implied_up_probability(),
            "implied_down": state.implied_down_probability(),
            "midpoint": state.midpoint(),
            "spread": state.spread,
            "time_remaining_seconds": state.time_remaining_seconds,
            "is_near_resolution": state.is_near_resolution(config.NEAR_RESOLUTION_THRESHOLD),
        }

    def get_spot_vs_strike_snapshot(self, market_type: str) -> Optional[SpotVsStrikeSnapshot]:
        """Compare current spot BTC price to a market's strike price.

        Returns a lightweight snapshot with spot, strike, distance, time remaining,
        and implied probabilities. Prepares data for Phase 3 signal engine
        without implementing edge detection.
        """
        state = self.get_current_market(market_type)
        tick = self.get_current_price()
        if not state or not tick or state.strike_price <= 0:
            return None

        return SpotVsStrikeSnapshot(
            spot_price=tick.price,
            strike_price=state.strike_price,
            distance_to_strike=tick.price - state.strike_price,
            time_remaining_seconds=state.time_remaining_seconds,
            implied_up=state.implied_up_probability(),
            implied_down=state.implied_down_probability(),
            market_type=market_type,
        )

    # --- Signal layer handoff (Phase 3) ---

    def get_signal_input(self) -> dict:
        """Package current state for the signal engine.

        Uses Coinbase BTC/USD as primary spot for model evaluation
        (closer to Chainlink settlement price).
        """
        tick = self.get_current_price()
        model_spot = self.get_model_spot_price()
        gap = self.get_price_source_gap()
        coinbase_live = (
            self.latest_coinbase_tick is not None and not self.latest_coinbase_tick.is_stale
        )
        vol = self.get_volatility()
        momentum = self.get_momentum()
        return {
            "spot_price": model_spot or (tick.price if tick else None),
            "spot_source": "coinbase_usd" if coinbase_live else "binance_usdt",
            "volatility": vol,
            "vol_source": "coinbase_usd" if coinbase_live else "binance_usdt",
            "market_state_5m": self.get_current_market("btc-5min"),
            "market_state_15m": self.get_current_market("btc-15min"),
            "timestamp": time.time(),
            "feed_healthy": tick is not None and not tick.is_stale if tick else False,
            "price_source_gap": gap,
            "momentum": momentum,
        }

    # --- Feed properties ---

    @property
    def binance_feed(self) -> Optional[BinanceFeed]:
        return self._binance_feed

    @property
    def coinbase_feed(self) -> Optional[CoinbaseFeed]:
        return self._coinbase_feed

    @property
    def polymarket_feed(self) -> Optional[PolymarketFeed]:
        return self._polymarket_feed

    async def stop(self) -> None:
        """Gracefully shut down all feeds."""
        self._running = False
        log.info("Stopping aggregator...")
        if not self._test_mode:
            await asyncio.gather(
                self._binance_feed.stop(),
                self._coinbase_feed.stop(),
                self._polymarket_feed.stop(),
            )
        log.info("Aggregator stopped")
