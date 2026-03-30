#!/usr/bin/env python3
"""Run the isolated redeem worker manually.

Default mode is DRY RUN.  Real on-chain transactions require --submit.

Usage:
    # Dry-run, process once (default, safe)
    python3 scripts/run_redeem_worker.py --mode once

    # Dry-run, poll loop
    python3 scripts/run_redeem_worker.py --mode loop --interval 30

    # REAL on-chain submission (requires --submit flag)
    python3 scripts/run_redeem_worker.py --mode once --submit
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    parser = argparse.ArgumentParser(description="Isolated redeem worker")
    parser.add_argument("--mode", choices=["once", "loop"], default="once",
                        help="Run once or loop (default: once)")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Loop interval in seconds (default: 30)")
    parser.add_argument("--submit", action="store_true", default=False,
                        help="Enable REAL on-chain tx submission (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Explicit dry-run flag (this is already the default)")
    parser.add_argument("--queue", default="data/redeem_queue.jsonl",
                        help="Path to queue file")
    parser.add_argument("--results", default="data/redeem_results.jsonl",
                        help="Path to results file")
    parser.add_argument("--max-retries", type=int, default=5,
                        help="Max on-chain tx retries before FAILED_MANUAL")
    args = parser.parse_args()

    # Default is dry-run unless --submit is explicitly passed
    dry_run = not args.submit

    # Load .env for credentials
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Banner
    if not dry_run:
        print("=" * 60)
        print("  *** WARNING: LIVE ON-CHAIN SUBMISSION ENABLED ***")
        print("  --submit flag is active.")
        print("  Redemption transactions WILL be sent to Polygon mainnet.")
        print("  This spends MATIC gas and interacts with real contracts.")
        print("=" * 60)
        print()
        confirm = input("Type YES to confirm live submission: ").strip()
        if confirm != "YES":
            print("Aborted. Run without --submit for dry-run mode.")
            sys.exit(1)
        print()
    else:
        print("=" * 60)
        print("  REDEEM WORKER — DRY RUN MODE")
        print("  No on-chain transactions will be submitted.")
        print("  Results will show DRY_RUN_WIN / CLOSED_LOSS status.")
        print("=" * 60)
        print()

    from execution.redeem_queue import RedeemQueue
    from execution.redeem_result import RedeemResultLog
    from execution.redeem_worker import RedeemWorker

    queue = RedeemQueue(args.queue)
    results = RedeemResultLog(args.results)

    # Show queue state
    items = queue.load_all()
    latest = results.get_latest_by_queue_id()
    pending = [i for i in items if not (latest.get(i.queue_id) and latest[i.queue_id].is_terminal)]
    print(f"Queue: {len(items)} total items, {len(pending)} pending")
    print(f"Queue file: {queue.path}")
    print(f"Results file: {results.path}")
    print()

    if not items:
        print("Nothing in queue. Use scripts/enqueue_redeem.py to add items.")
        return

    # Initialize redeemer only for live mode
    redeemer = None
    if not dry_run:
        try:
            from execution.onchain_redeemer import OnchainRedeemer
            redeemer = OnchainRedeemer()
            if not redeemer.initialize():
                print("[ERROR] OnchainRedeemer failed to initialize. Aborting.")
                sys.exit(1)
        except ImportError:
            print("[ERROR] web3 not installed. Cannot submit transactions.")
            sys.exit(1)

    # Initialize CLOB client (optional, for fallback resolution check)
    clob_client = None
    try:
        from utils.polymarket_auth import get_clob_client
        clob_client = get_clob_client(authenticated=True)
        print(f"CLOB client: initialized (fallback resolution source)")
    except Exception as e:
        print(f"CLOB client: not available ({e}) — using Gamma API only")
    print()

    worker = RedeemWorker(
        queue=queue,
        results=results,
        redeemer=redeemer,
        clob_client=clob_client,
        dry_run=dry_run,
        max_retries=args.max_retries,
    )

    if args.mode == "once":
        summary = worker.run_once()
        print()
        print("--- Summary ---")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    elif args.mode == "loop":
        worker.run_loop(interval=args.interval)


if __name__ == "__main__":
    main()
