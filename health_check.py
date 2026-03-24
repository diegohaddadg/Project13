"""Project13 — Health Check System.

Runs the data pipeline for a configurable duration and generates a diagnostic report.

Usage:
    python health_check.py              # 60-second check (default)
    python health_check.py --duration 30  # custom duration
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.aggregator import Aggregator
from utils.config_loader import load_env, validate_config
from utils.logger import get_logger
import config

log = get_logger("health_check")


def _stat_summary(values: list[float]) -> dict:
    """Return min/max/avg for a list of floats."""
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
    }


def generate_report(aggregator: Aggregator, duration: float, start_time: float) -> str:
    """Generate a text report from aggregator stats."""
    b_stats = _stat_summary(aggregator.binance_latencies)
    c_stats = _stat_summary(aggregator.coinbase_latencies)
    volatility = aggregator.get_volatility()

    lines = [
        "=" * 60,
        "  PROJECT13 — HEALTH CHECK REPORT",
        "=" * 60,
        f"  Timestamp:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"  Duration:      {duration:.0f}s",
        "",
        "  FEED STATISTICS",
        "  " + "-" * 40,
        f"  Binance ticks:   {aggregator.binance_tick_count}",
        f"  Coinbase ticks:  {aggregator.coinbase_tick_count}",
        "",
        "  BINANCE LATENCY (exchange → local)",
        f"    Avg:  {b_stats['avg']:.1f}ms",
        f"    Min:  {b_stats['min']:.1f}ms",
        f"    Max:  {b_stats['max']:.1f}ms",
        "",
        "  COINBASE LATENCY (staleness only — no exchange timestamp)",
        f"    Avg:  {c_stats['avg']:.1f}ms",
        f"    Min:  {c_stats['min']:.1f}ms",
        f"    Max:  {c_stats['max']:.1f}ms",
        "",
        "  HEALTH EVENTS",
        f"    Stale events:     {aggregator.stale_events}",
        f"    Failover events:  {aggregator.failover_events}",
    ]

    if aggregator.binance_feed:
        lines.append(f"    Binance reconnects:  {aggregator.binance_feed.reconnect_count}")
    if aggregator.coinbase_feed:
        lines.append(f"    Coinbase reconnects: {aggregator.coinbase_feed.reconnect_count}")

    lines += [
        "",
        "  VOLATILITY",
        f"    Rolling σ:  {'${:,.2f}'.format(volatility) if volatility else 'insufficient data'}",
        "",
        "  TICK RATES (last interval)",
    ]
    if aggregator.binance_feed:
        lines.append(f"    Binance:   {aggregator.binance_feed.tick_rate:.1f} ticks/sec")
    if aggregator.coinbase_feed:
        lines.append(f"    Coinbase:  {aggregator.coinbase_feed.tick_rate:.1f} ticks/sec")

    lines += [
        "",
        "=" * 60,
    ]

    # Verdict
    issues = []
    if aggregator.binance_tick_count == 0:
        issues.append("No Binance ticks received")
    if aggregator.coinbase_tick_count == 0:
        issues.append("No Coinbase ticks received")
    if b_stats["avg"] > 200:
        issues.append(f"Binance avg latency {b_stats['avg']:.0f}ms exceeds 200ms target")
    if aggregator.stale_events > 0:
        issues.append(f"{aggregator.stale_events} stale event(s) detected")

    if issues:
        lines.append("  ISSUES FOUND:")
        for issue in issues:
            lines.append(f"    ⚠ {issue}")
    else:
        lines.append("  ✓ ALL CHECKS PASSED")

    lines.append("=" * 60)
    return "\n".join(lines)


async def run_health_check(duration: int) -> None:
    """Run feeds for `duration` seconds, then generate and save report."""
    load_env()
    validate_config()

    log.info(f"Starting health check ({duration}s)...")
    aggregator = Aggregator()

    aggregator_task = asyncio.create_task(aggregator.start())
    start = time.time()

    # Wait for duration or Ctrl+C
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    try:
        await asyncio.wait_for(shutdown.wait(), timeout=duration)
        log.info("Health check interrupted")
    except asyncio.TimeoutError:
        pass  # Normal completion

    elapsed = time.time() - start
    await aggregator.stop()
    aggregator_task.cancel()
    try:
        await aggregator_task
    except asyncio.CancelledError:
        pass

    # Generate report
    report = generate_report(aggregator, elapsed, start)

    # Save to file
    out_path = Path(config.HEALTH_CHECK_OUTPUT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)

    # Also print
    print("\n" + report)
    log.info(f"Report saved to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Project13 Health Check")
    parser.add_argument("--duration", type=int, default=config.HEALTH_CHECK_DURATION,
                        help=f"Check duration in seconds (default: {config.HEALTH_CHECK_DURATION})")
    args = parser.parse_args()

    try:
        asyncio.run(run_health_check(args.duration))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
