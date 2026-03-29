#!/usr/bin/env python3
"""Verify Chainlink on-chain BTC/USD feed against Polymarket priceToBeat.

Reads historical rounds from the Chainlink aggregator on Polygon and compares
the closest on-chain price to each resolved window's priceToBeat from the
Gamma events API.

Usage:
    python3 scripts/verify_chainlink_strike.py [--windows N] [--rpc URL]

Output:
    Per-window comparison table and summary statistics showing how closely
    the on-chain feed matches Polymarket's Data Streams-based resolution price.
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

AGGREGATOR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
DEFAULT_RPC = "https://rpc-mainnet.matic.quiknode.pro"


def eth_call(rpc, to, calldata):
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": calldata}, "latest"],
        "id": 1,
    }
    req = urllib.request.Request(
        rpc,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    if "error" in result:
        raise Exception(result["error"])
    return result.get("result", "")


def decode_round(hex_data):
    d = hex_data[2:]
    if len(d) < 320:
        return None
    return {
        "round_id": int(d[0:64], 16),
        "price": int(d[64:128], 16) / 1e8,
        "started_at": int(d[128:192], 16),
        "updated_at": int(d[192:256], 16),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=int, default=20, help="Number of windows to check")
    parser.add_argument("--rpc", default=DEFAULT_RPC, help="Polygon RPC URL")
    args = parser.parse_args()

    rpc = args.rpc
    print(f"RPC: {rpc}")
    print(f"Aggregator: {AGGREGATOR}")
    print()

    # 1. Get latest round
    try:
        latest = decode_round(eth_call(rpc, AGGREGATOR, "0xfeaf968c"))
        print(f"Latest: ${latest['price']:,.2f} at {datetime.fromtimestamp(latest['updated_at'], tz=timezone.utc).strftime('%H:%M:%S UTC')}")
    except Exception as e:
        print(f"ERROR: Cannot read Chainlink: {e}")
        sys.exit(1)

    # 2. Build round history
    print("Fetching round history...")
    rounds = [latest]
    for offset in range(1, 400):
        rid = latest["round_id"] - offset
        call = "0x9a6fc8f5" + hex(rid)[2:].zfill(64)
        try:
            rd = decode_round(eth_call(rpc, AGGREGATOR, call))
            if rd and rd["updated_at"] > 0:
                rounds.append(rd)
        except Exception:
            pass
        if offset % 50 == 0:
            time.sleep(0.3)
            print(f"  ... {len(rounds)} rounds fetched")

    rounds.sort(key=lambda r: r["updated_at"])
    seen = set()
    rounds = [r for r in rounds if r["round_id"] not in seen and not seen.add(r["round_id"])]
    span_min = (rounds[-1]["updated_at"] - rounds[0]["updated_at"]) / 60
    print(f"  {len(rounds)} unique rounds spanning {span_min:.0f} minutes")
    print()

    # 3. Collect priceToBeat
    now = int(time.time())
    current_5m = now - (now % 300)

    print("Fetching resolved priceToBeat values...")
    windows = []
    for i in range(3, 3 + args.windows + 10):
        ts = current_5m - (300 * i)
        slug = f"btc-updown-5m-{ts}"
        url = f"https://gamma-api.polymarket.com/events?slug={slug}&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Project13/1.0"})
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=5).read())
            if data:
                em = data[0].get("eventMetadata") or {}
                ptb = em.get("priceToBeat")
                if ptb is not None:
                    windows.append({"slug": slug, "ts": ts, "ptb": float(ptb)})
                    if len(windows) >= args.windows:
                        break
        except Exception:
            pass

    print(f"  {len(windows)} windows with priceToBeat")
    print()

    # 4. Compare
    print(f"{'Window':<32} {'priceToBeat':>14} {'CL_nearest':>14} {'Delta':>10} {'CL_lag':>8}")
    print("-" * 85)

    deltas = []
    for w in windows:
        best = None
        best_dist = float("inf")
        for r in rounds:
            dist = abs(r["updated_at"] - w["ts"])
            if dist < best_dist:
                best_dist = dist
                best = r

        if best:
            delta = abs(best["price"] - w["ptb"])
            lag = w["ts"] - best["updated_at"]
            deltas.append(delta)
            print(f"  {w['slug']:<30} ${w['ptb']:>12,.2f} ${best['price']:>12,.2f} ${delta:>8,.2f} {lag:>6.0f}s")

    if not deltas:
        print("  No overlapping data — round history may not cover the resolved windows")
        sys.exit(1)

    # 5. Summary
    deltas.sort()
    n = len(deltas)
    print()
    print("=== SUMMARY ===")
    print(f"  Windows compared: {n}")
    print(f"  Mean |delta|:     ${sum(deltas)/n:,.2f}")
    print(f"  Median |delta|:   ${deltas[n//2]:,.2f}")
    print(f"  P75:              ${deltas[int(n*0.75)]:,.2f}")
    print(f"  P90:              ${deltas[int(n*0.9)]:,.2f}")
    print(f"  Max:              ${max(deltas):,.2f}")
    print(f"  Under $5:         {sum(1 for d in deltas if d < 5)}/{n}")
    print(f"  Under $10:        {sum(1 for d in deltas if d < 10)}/{n}")
    print(f"  Under $20:        {sum(1 for d in deltas if d < 20)}/{n}")
    print()

    mean = sum(deltas) / n
    if mean < 10:
        print("VERDICT: Chainlink on-chain feed is a viable strike source")
        print(f"  (~${mean:.0f} mean error vs Polymarket priceToBeat)")
    else:
        print("VERDICT: On-chain feed has significant deviation from Data Streams")
        print(f"  (~${mean:.0f} mean error — consider using with edge buffer)")


if __name__ == "__main__":
    main()
