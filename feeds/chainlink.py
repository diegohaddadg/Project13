"""Chainlink on-chain BTC/USD price feed reader for strike estimation.

Reads the Chainlink BTC/USD aggregator proxy on Polygon mainnet via
a public RPC eth_call. This is the standard Chainlink Price Feed — NOT
identical to Chainlink Data Streams (which Polymarket uses for resolution),
but validated to ~$3 median / ~$5 mean error vs Polymarket's priceToBeat
across 22 consecutive 5-minute windows.

The feed updates every ~27-33 seconds (heartbeat) or on 0.1% deviation.
We read latestRoundData() to get the most recent on-chain BTC/USD price.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Optional

from utils.logger import get_logger
import config

log = get_logger("chainlink")

# latestRoundData() selector
_LATEST_ROUND_DATA = "0xfeaf968c"


class ChainlinkFeed:
    """Reads Chainlink BTC/USD on-chain price feed from Polygon."""

    def __init__(self):
        self._last_price: float = 0.0
        self._last_updated_at: int = 0
        self._last_round_id: int = 0
        self._last_fetch_ts: float = 0.0
        self._errors: int = 0
        self._enabled: bool = config.STRIKE_CHAINLINK_ENABLED

    async def start(self) -> None:
        if not self._enabled:
            log.info("[CHAINLINK] Disabled via STRIKE_CHAINLINK_ENABLED=false")
            return
        log.info(
            f"[CHAINLINK] Reading BTC/USD from Polygon on-chain feed "
            f"rpc={config.STRIKE_CHAINLINK_RPC_URL[:40]}... "
            f"aggregator={config.STRIKE_CHAINLINK_AGGREGATOR}"
        )
        # Initial read to verify connectivity
        price = self.read_latest_price()
        if price and price > 0:
            log.warning(f"[CHAINLINK] Initial read: ${price:,.2f}")
        else:
            log.warning("[CHAINLINK] Initial read failed — will retry on demand")

    async def stop(self) -> None:
        pass

    def read_latest_price(self) -> Optional[float]:
        """Read latestRoundData() from the Chainlink aggregator.

        Returns the BTC/USD price (8 decimals), or None on failure.
        Caches for 5 seconds to avoid hammering the RPC.
        """
        if not self._enabled:
            return None

        # Cache: don't re-fetch within 5 seconds
        now = time.time()
        if self._last_price > 0 and (now - self._last_fetch_ts) < 5.0:
            return self._last_price

        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [
                    {
                        "to": config.STRIKE_CHAINLINK_AGGREGATOR,
                        "data": _LATEST_ROUND_DATA,
                    },
                    "latest",
                ],
                "id": 1,
            }
            req = urllib.request.Request(
                config.STRIKE_CHAINLINK_RPC_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            result = json.loads(resp.read())

            if "error" in result:
                self._errors += 1
                return None

            hex_data = result.get("result", "")
            if not hex_data or len(hex_data) < 322:
                self._errors += 1
                return None

            d = hex_data[2:]
            round_id = int(d[0:64], 16)
            answer = int(d[64:128], 16)
            updated_at = int(d[192:256], 16)
            price = answer / 1e8

            if price <= 0:
                self._errors += 1
                return None

            self._last_price = price
            self._last_updated_at = updated_at
            self._last_round_id = round_id
            self._last_fetch_ts = now
            return price

        except Exception as e:
            self._errors += 1
            if self._errors <= 3 or self._errors % 100 == 0:
                log.warning(f"[CHAINLINK] RPC error ({self._errors} total): {e}")
            return None

    @property
    def last_price(self) -> float:
        return self._last_price

    @property
    def last_updated_at(self) -> int:
        return self._last_updated_at

    @property
    def price_age_seconds(self) -> float:
        if self._last_updated_at <= 0:
            return float("inf")
        return time.time() - self._last_updated_at

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "last_price": self._last_price,
            "last_updated_at": self._last_updated_at,
            "price_age_s": round(self.price_age_seconds, 1),
            "errors": self._errors,
        }
