from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Awaitable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from models.price_tick import PriceTick
from utils.logger import get_logger
import config

log = get_logger("coinbase")

SUBSCRIBE_MSG = json.dumps({
    "type": "subscribe",
    "product_ids": ["BTC-USD"],
    "channels": ["ticker"],
})


class CoinbaseFeed:
    """Real-time BTC/USD ticker stream from Coinbase WebSocket.

    Latency limitation: Coinbase ticker messages do not include a trade-event
    timestamp. Both timestamp and local_timestamp are set to local receive time,
    so age_ms() will be ~0ms and does NOT represent true exchange-to-local latency.
    staleness_ms() remains accurate for feed health monitoring.
    """

    def __init__(self, on_tick: Callable[[PriceTick], Awaitable[None]] | None = None):
        self._on_tick = on_tick
        self._ws = None
        self._running = False
        self._tick_count = 0
        self._last_rate_log = time.time()
        self.tick_rate: float = 0.0
        self.reconnect_count: int = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Connect, subscribe, and stream with auto-reconnect."""
        self._running = True
        delay = config.RECONNECT_BASE_DELAY

        while self._running:
            try:
                log.info("Connecting to Coinbase WebSocket...")
                async with websockets.connect(
                    config.COINBASE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    await ws.send(SUBSCRIBE_MSG)
                    self._connected = True
                    delay = config.RECONNECT_BASE_DELAY
                    log.info("Coinbase feed connected")

                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw)

            except ConnectionClosed as e:
                log.warning(f"Coinbase connection closed: {e.code} {e.reason}")
            except Exception as e:
                log.error(f"Coinbase feed error: {e}")
            finally:
                self._connected = False

            if not self._running:
                break

            self.reconnect_count += 1
            log.warning(f"Coinbase reconnecting in {delay:.1f}s (reconnect #{self.reconnect_count})")
            await asyncio.sleep(delay)
            delay = min(delay * 2, config.RECONNECT_MAX_DELAY)

    async def _handle_message(self, raw: str) -> None:
        """Parse a Coinbase ticker message into a PriceTick."""
        try:
            data = json.loads(raw)
            if data.get("type") != "ticker":
                return

            local_ts = time.time()
            tick = PriceTick(
                timestamp=local_ts,  # Coinbase ticker has no exchange event timestamp
                price=float(data["price"]),
                source="coinbase",
                local_timestamp=local_ts,
            )

            self._tick_count += 1
            self._update_tick_rate()

            if self._on_tick:
                await self._on_tick(tick)

        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.error(f"Failed to parse Coinbase message: {e}")

    def _update_tick_rate(self) -> None:
        """Update and periodically log tick rate. Warn on degradation."""
        now = time.time()
        elapsed = now - self._last_rate_log
        if elapsed >= config.TICK_RATE_LOG_INTERVAL:
            self.tick_rate = self._tick_count / elapsed
            if self.tick_rate < config.TICK_RATE_WARN_THRESHOLD:
                log.warning(f"Low tick rate: {self.tick_rate:.2f} ticks/sec (threshold: {config.TICK_RATE_WARN_THRESHOLD})")
            else:
                log.info(f"Tick rate: {self.tick_rate:.1f} ticks/sec")
            self._tick_count = 0
            self._last_rate_log = now

    async def stop(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._connected = False
            log.info("Coinbase feed stopped")
