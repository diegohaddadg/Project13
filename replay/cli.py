"""Replay CLI — run offline replay from recorded tape.

Usage:
    python3 -m replay.cli --tape data/live_tape.jsonl --mode fast
    python3 -m replay.cli --tape data/live_tape.jsonl --mode realtime
    python3 -m replay.cli  # uses defaults

After replay, run calibration export on replay logs:
    python3 scripts/calibration_export.py --trade-log data/replay_trade_log.jsonl --signal-trace data/replay_signal_execution_trace.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay.replay_runner import run_replay
from utils.logger import get_logger

log = get_logger("replay_cli")


def main():
    parser = argparse.ArgumentParser(
        description="Project13 Replay — offline replay of recorded live tape"
    )
    parser.add_argument(
        "--tape", default="data/live_tape.jsonl",
        help="Path to recorded tape (default: data/live_tape.jsonl)"
    )
    parser.add_argument(
        "--mode", choices=["fast", "realtime"], default="fast",
        help="Replay mode: fast (no delays) or realtime (original speed)"
    )
    parser.add_argument(
        "--trade-log", default="data/replay_trade_log.jsonl",
        help="Output path for replay trade log"
    )
    parser.add_argument(
        "--signal-trace", default="data/replay_signal_execution_trace.jsonl",
        help="Output path for replay signal traces"
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Speed multiplier for realtime mode (default: 1.0)"
    )
    args = parser.parse_args()

    tape = Path(args.tape)
    if not tape.exists():
        print(f"Error: tape not found at {tape}")
        sys.exit(1)

    line_count = sum(1 for _ in tape.read_text().splitlines() if _.strip())
    print(f"Replay: {line_count} records from {tape}")
    print(f"Mode: {args.mode}, speed: {args.speed}x")
    print(f"Trade log: {args.trade_log}")
    print(f"Signal trace: {args.signal_trace}")
    print()

    stats = run_replay(
        tape_path=str(tape),
        trade_log_path=args.trade_log,
        trace_path=args.signal_trace,
        mode=args.mode,
        sleep_scale=1.0 / args.speed if args.speed > 0 else 1.0,
    )

    print()
    print("=" * 50)
    print("  REPLAY SUMMARY")
    print("=" * 50)
    print(f"  Tape records:      {stats.get('tape_records', 0)}")
    print(f"  Signals generated: {stats.get('signals_generated', 0)}")
    print(f"  Signals approved:  {stats.get('signals_approved', 0)}")
    print(f"  Fills:             {stats.get('fills', 0)}")
    print(f"  Resolutions:       {stats.get('resolutions', 0)}")
    print(f"  Total trades:      {stats.get('total_trades', 0)}")
    wr = stats.get("win_rate", 0)
    print(f"  Win rate:          {wr:.0%}" if wr else "  Win rate:          --")
    print(f"  Total PnL:         ${stats.get('total_pnl', 0):.2f}")
    print(f"  Final equity:      ${stats.get('final_equity', 0):.2f}")
    print("=" * 50)

    if stats.get("error"):
        print(f"\nError: {stats['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
