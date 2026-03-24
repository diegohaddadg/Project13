#!/usr/bin/env python3
"""Test Binance WebSocket reachability from this machine.

Usage:
    python3 scripts/test_binance_ws.py
    python3 scripts/test_binance_ws.py --url wss://stream.binance.us:9443/ws/btcusdt@trade
    python3 scripts/test_binance_ws.py --url wss://data-stream.binance.vision/ws/btcusdt@trade
"""

import argparse
import asyncio
import json
import ssl
import sys
import time

URLS = [
    ("Global", "wss://stream.binance.com:9443/ws/btcusdt@trade"),
    ("Binance.US", "wss://stream.binance.us:9443/ws/btcusdt@trade"),
    ("Data mirror", "wss://data-stream.binance.vision/ws/btcusdt@trade"),
]


async def test_url(label: str, url: str, timeout: float = 10.0) -> None:
    """Test a single Binance WebSocket URL."""
    print(f"\n{'='*50}")
    print(f"Testing: {label}")
    print(f"URL:     {url}")
    print(f"{'='*50}")

    try:
        import websockets
    except ImportError:
        print("ERROR: websockets not installed. Run: pip install websockets")
        return

    t0 = time.time()
    try:
        async with websockets.connect(url, open_timeout=timeout, close_timeout=5) as ws:
            connect_ms = (time.time() - t0) * 1000
            print(f"  Connected in {connect_ms:.0f}ms")

            # Wait for first message
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                recv_ms = (time.time() - t0) * 1000
                data = json.loads(raw)
                price = data.get("p", "?")
                print(f"  First message in {recv_ms:.0f}ms")
                print(f"  BTC price: ${float(price):,.2f}" if price != "?" else f"  Data: {raw[:100]}")
                print(f"  RESULT: OK")
            except asyncio.TimeoutError:
                print(f"  Connected but no message received in 5s")
                print(f"  RESULT: PARTIAL (connection OK, no data)")

    except ssl.SSLError as e:
        print(f"  SSL error: {e}")
        print(f"  RESULT: FAILED (SSL)")
    except OSError as e:
        elapsed = (time.time() - t0) * 1000
        print(f"  Network error after {elapsed:.0f}ms: {e}")
        print(f"  RESULT: FAILED (network/DNS)")
    except asyncio.TimeoutError:
        print(f"  Connect timeout after {timeout}s")
        print(f"  RESULT: FAILED (timeout)")
    except Exception as e:
        print(f"  Error: {type(e).__name__}: {e}")
        print(f"  RESULT: FAILED")


async def main(url: str = None):
    if url:
        await test_url("Custom", url)
    else:
        print("Binance WebSocket Connectivity Test")
        print("Testing all known endpoints...")
        for label, u in URLS:
            await test_url(label, u)

    print(f"\n{'='*50}")
    print("Done. Use --url to test a specific endpoint.")
    print("If Global fails, try adding to .env:")
    print("  BINANCE_WS_URL=wss://data-stream.binance.vision/ws/btcusdt@trade")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Test Binance WebSocket reachability")
    p.add_argument("--url", help="Specific URL to test")
    args = p.parse_args()
    asyncio.run(main(args.url))
