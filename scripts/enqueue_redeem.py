#!/usr/bin/env python3
"""Manually enqueue a redeem candidate into the redeem queue.

Usage:
    python3 scripts/enqueue_redeem.py \
        --position-id abc123 \
        --condition-id 0xabc...def \
        --token-id 12345...890 \
        --market-id 123456 \
        --direction UP \
        --market-type btc-5min \
        --entry-price 0.42 \
        --num-shares 10.0

    # Or from a trade log entry (extracts fields automatically)
    python3 scripts/enqueue_redeem.py --from-trade-log --order-id <order_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    parser = argparse.ArgumentParser(description="Enqueue a redeem candidate")
    parser.add_argument("--position-id", required=False, default="")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--market-id", default="")
    parser.add_argument("--condition-id", required=False, default="")
    parser.add_argument("--token-id", required=False, default="")
    parser.add_argument("--direction", default="")
    parser.add_argument("--market-type", default="")
    parser.add_argument("--entry-price", type=float, default=0.0)
    parser.add_argument("--num-shares", type=float, default=0.0)
    parser.add_argument("--queue", default="data/redeem_queue.jsonl")
    parser.add_argument("--from-trade-log", action="store_true",
                        help="Extract fields from the trade log by order-id")
    parser.add_argument("--trade-log", default=None,
                        help="Path to trade log (used with --from-trade-log)")
    args = parser.parse_args()

    from execution.redeem_queue import RedeemQueue, RedeemQueueItem

    if args.from_trade_log:
        if not args.order_id:
            print("[ERROR] --order-id is required when using --from-trade-log")
            sys.exit(1)
        log_path = _resolve_trade_log(args.trade_log)
        if log_path is None:
            sys.exit(1)
        item = _item_from_trade_log(log_path, args.order_id)
        if item is None:
            sys.exit(1)
    else:
        if not args.position_id or not args.condition_id or not args.token_id:
            print("[ERROR] --position-id, --condition-id, and --token-id are required")
            print("        (or use --from-trade-log --order-id <id>)")
            sys.exit(1)
        item = RedeemQueueItem(
            position_id=args.position_id,
            order_id=args.order_id,
            market_id=args.market_id,
            condition_id=args.condition_id,
            token_id=args.token_id,
            direction=args.direction,
            market_type=args.market_type,
            entry_price=args.entry_price,
            num_shares=args.num_shares,
            source="manual",
        )

    # Validate before enqueue
    err = item.validate()
    if err:
        print(f"[ERROR] Validation failed: {err}")
        sys.exit(1)

    queue = RedeemQueue(args.queue)
    success, msg = queue.enqueue(item)

    if success:
        print(f"[OK] {msg}")
        print(f"     position_id={item.position_id}")
        print(f"     condition_id={item.condition_id[:20]}...")
        print(f"     token_id=...{item.token_id[-12:]}")
        print(f"     queue_file={queue.path}")
    else:
        print(f"[REJECTED] {msg}")
        sys.exit(1)


_DEFAULT_TRADE_LOG_SEARCH = [
    "logs/trade_log.jsonl",
    "data/trade_log.jsonl",
]


def _resolve_trade_log(explicit_path: str | None) -> str | None:
    """Resolve the trade log path.  Returns path or None (with error printed)."""
    from pathlib import Path

    if explicit_path is not None:
        p = Path(explicit_path)
        if p.exists():
            print(f"[INFO] Using trade log: {p}")
            return str(p)
        print(f"[ERROR] Trade log not found: {explicit_path}")
        return None

    for candidate in _DEFAULT_TRADE_LOG_SEARCH:
        p = Path(candidate)
        if p.exists():
            print(f"[INFO] Using trade log: {p}")
            return str(p)

    print("[ERROR] Trade log not found. Checked:")
    for candidate in _DEFAULT_TRADE_LOG_SEARCH:
        print(f"         - {candidate}")
    print("       Use --trade-log <path> to specify explicitly.")
    return None


def _item_from_trade_log(log_path: str, order_id: str):
    """Extract a RedeemQueueItem from a trade log entry."""
    from pathlib import Path
    from execution.redeem_queue import RedeemQueueItem

    p = Path(log_path)
    if not p.exists():
        print(f"[ERROR] Trade log not found: {log_path}")
        return None

    match = None
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("order_id") == order_id:
            match = d
            break

    if match is None:
        print(f"[ERROR] Order {order_id} not found in {log_path}")
        return None

    meta = match.get("metadata", {})
    condition_id = meta.get("condition_id", "")
    token_id = match.get("token_id", "") or meta.get("token_id", "")
    position_id = meta.get("position_id", match.get("order_id", ""))

    if not condition_id:
        print(f"[ERROR] Order {order_id} has no condition_id in metadata")
        return None
    if not token_id:
        print(f"[ERROR] Order {order_id} has no token_id")
        return None

    item = RedeemQueueItem(
        position_id=position_id,
        order_id=order_id,
        market_id=match.get("market_id", ""),
        condition_id=condition_id,
        token_id=token_id,
        direction=match.get("direction", ""),
        market_type=match.get("market_type", ""),
        entry_price=float(match.get("fill_price", match.get("price", 0.0))),
        num_shares=float(match.get("num_shares", 0.0)),
        source="trade_log",
    )

    print(f"[INFO] Extracted from trade log:")
    print(f"       order_id={order_id}")
    print(f"       position_id={item.position_id}")
    print(f"       condition_id={item.condition_id[:20]}...")
    print(f"       token_id=...{item.token_id[-12:]}")
    print(f"       direction={item.direction} market_type={item.market_type}")
    print(f"       entry_price={item.entry_price} num_shares={item.num_shares}")

    return item


if __name__ == "__main__":
    main()
