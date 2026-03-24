"""Real-time BTC/USDT trade stream from Binance WebSocket.

Includes detailed error classification, health diagnostics, and
robust reconnect for production VPS deployment.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Callable, Awaitable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from models.price_tick import PriceTick
from utils.logger import get_logger
import config

log = get_logger("binance")


class BinanceFeed:
    """Real-time BTC/USDT trade stream from Binance WebSocket."""

    def __init__(self, on_tick: Callable[[PriceTick], Awaitable[None]] | None = None):
        self._on_tick = on_tick
        self._ws = None
        self._running = False
        self._tick_count = 0
        self._last_rate_log = time.time()
        self.tick_rate: float = 0.0
        self.reconnect_count: int = 0
        self._connected = False

        # Health diagnostics
        self.last_error: str = ""
        self.last_error_time: float = 0
        self.connected_at: float = 0
        self.first_message_at: float = 0
        self._first_message_logged = False

    @property
    def connected(self) -> bool:
        return self._connected

    def get_health(self) -> dict:
        """Return compact health diagnostic dict."""
        return {
            "connected": self._connected,
            "tick_rate": self.tick_rate,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
            "last_error_age_s": (time.time() - self.last_error_time) if self.last_error_time > 0 else None,
            "connected_at": self.connected_at,
            "first_message_at": self.first_message_at,
            "url": config.BINANCE_WS_URL,
        }

    async def start(self) -> None:
        """Connect and stream trades with auto-reconnect."""
        self._running = True
        delay = config.RECONNECT_BASE_DELAY

        log.info(f"Binance feed starting — URL: {config.BINANCE_WS_URL}")

        while self._running:
            try:
                log.info(f"Connecting to Binance WebSocket...")
                async with websockets.connect(
                    config.BINANCE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._first_message_logged = False
                    self.connected_at = time.time()
                    delay = config.RECONNECT_BASE_DELAY
                    log.info("Binance feed connected")

                    async for raw in ws:
                        if not self._running:
                            break
                        if not self._first_message_logged:
                            self.first_message_at = time.time()
                            self._first_message_logged = True
                            log.info(f"Binance first message received ({self.first_message_at - self.connected_at:.1f}s after connect)")
                        await self._handle_message(raw)

            except ConnectionClosed as e:
                self._record_error(f"ws_closed:{e.code}:{e.reason}")
                log.warning(f"Binance connection closed: code={e.code} reason={e.reason}")
            except InvalidStatusCode as e:
                self._record_error(f"http_status:{e.status_code}")
                log.error(f"Binance HTTP status error: {e.status_code} — may be geo-blocked or rate-limited")
            except ssl.SSLError as e:
                self._record_error(f"ssl_error:{e}")
                log.error(f"Binance SSL error: {e}")
            except OSError as e:
                self._record_error(f"connect_error:{e.errno}:{e}")
                log.error(f"Binance connect/DNS/network error: {e}")
            except asyncio.TimeoutError:
                self._record_error("connect_timeout")
                log.error("Binance connect timeout (10s) — endpoint may be unreachable")
            except Exception as e:
                self._record_error(f"exception:{type(e).__name__}:{e}")
                log.error(f"Binance feed error: {type(e).__name__}: {e}")
            finally:
                self._connected = False

            if not self._running:
                break

            self.reconnect_count += 1
            log.warning(
                f"Binance reconnecting in {delay:.1f}s (attempt #{self.reconnect_count}, "
                f"last_error={self.last_error})"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, config.RECONNECT_MAX_DELAY)

    def _record_error(self, error: str) -> None:
        """Record error for health diagnostics."""
        self.last_error = error
        self.last_error_time = time.time()

    async def _handle_message(self, raw: str) -> None:
        """Parse a Binance trade message into a PriceTick."""
        try:
            data = json.loads(raw)
            local_ts = time.time()
            tick = PriceTick(
                timestamp=data["T"] / 1000.0,
                price=float(data["p"]),
                source="binance",
                local_timestamp=local_ts,
            )

            self._tick_count += 1
            self._update_tick_rate()

            if tick.age_ms() > 500:
                log.warning(f"High exchange-to-local latency: {tick.age_ms():.0f}ms")

            if self._on_tick:
                await self._on_tick(tick)

        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.error(f"Failed to parse Binance message: {e}")

    def _update_tick_rate(self) -> None:
        """Update and periodically log tick rate."""
        now = time.time()
        elapsed = now - self._last_rate_log
        if elapsed >= config.TICK_RATE_LOG_INTERVAL:
            self.tick_rate = self._tick_count / elapsed
            if self.tick_rate < config.TICK_RATE_WARN_THRESHOLD:
                log.warning(f"Low tick rate: {self.tick_rate:.2f} ticks/sec")
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
            log.info("Binance feed stopped")
