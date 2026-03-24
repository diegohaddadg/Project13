"""Offline replay runner — processes a recorded tape through the live signal/risk/execution stack.

Reuses existing logic directly:
- strategies.signal_engine.SignalEngine
- risk.risk_manager.RiskManager
- execution.order_manager.OrderManager (paper mode)
- execution.position_manager.PositionManager
- execution.fill_tracker (simplified for replay)

Outputs go to data/replay_* paths to avoid overwriting live logs.

Realism gaps (documented):
- Replay uses taped feed_healthy state if present; otherwise assumes healthy.
- Position resolution uses paper timeout logic based on elapsed tape time,
  not real wall-clock. In fast mode, timeouts fire immediately when the
  tape-time gap exceeds the market's window duration.
- Signal cooldown and execution dedup use tape timestamps in fast mode.
- Volatility is taken from the tape snapshot, not recalculated from raw ticks.
"""

from __future__ import annotations

import json
import time as _time
from pathlib import Path
from typing import Optional

from models.market_state import MarketState
from models.trade_signal import TradeSignal
from strategies.signal_engine import SignalEngine
from risk.risk_manager import RiskManager
from risk.kill_switch import KillSwitch
from risk.exposure_tracker import ExposureTracker
from risk.performance_analytics import PerformanceAnalytics
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from utils.logger import get_logger
import config

log = get_logger("replay")

# Patch trade log path for replay
_ORIG_TRADE_LOG = config.TRADE_LOG_PATH


def _rebuild_market_state(d: Optional[dict]) -> Optional[MarketState]:
    """Reconstruct a MarketState from a tape dict."""
    if d is None:
        return None
    return MarketState(
        market_id=d.get("market_id", ""),
        condition_id=d.get("condition_id", ""),
        market_type=d.get("market_type", ""),
        strike_price=d.get("strike_price", 0),
        yes_price=d.get("yes_price", 0),
        no_price=d.get("no_price", 0),
        spread=d.get("spread", 0),
        time_remaining_seconds=d.get("time_remaining_seconds", 0),
        gamma_end_remaining_seconds=d.get("gamma_end_remaining_seconds", 0),
        is_active=d.get("is_active", True),
        up_token_id=d.get("up_token_id", "replay_up"),
        down_token_id=d.get("down_token_id", "replay_dn"),
        slug=d.get("slug", ""),
        question=d.get("question", ""),
        window_started=d.get("window_started", False),
        is_signalable=d.get("is_signalable", False),
        time_to_window_seconds=d.get("time_to_window_seconds", 0),
        timing_source=d.get("timing_source", ""),
    )


class _ReplayHealthStub:
    """Minimal health monitor stub for replay — always healthy unless tape says otherwise."""

    def run_health_check(self):
        return {
            "binance_ok": True, "coinbase_ok": True, "any_feed_ok": True,
            "polymarket_ok": True, "latency_ok": True,
            "feed_latency_ms": 10, "polymarket_age_s": 1,
            "volatility_available": True, "warming_up": False,
        }

    def is_system_healthy(self):
        return True

    def get_warnings(self):
        return []


class _ReplayFillTracker:
    """Simplified fill tracker for replay — resolves by tape-time elapsed vs window duration."""

    def __init__(self, pm: PositionManager):
        self._pm = pm

    def check_resolutions(self, tape_ts: float, spot: Optional[float]) -> list:
        """Check positions against tape timestamp."""
        closed = []
        timeouts = {"btc-5min": 300, "btc-15min": 900}

        for pos in list(self._pm.get_open_positions()):
            timeout = timeouts.get(pos.market_type, 600)
            elapsed = tape_ts - pos.entry_timestamp
            if elapsed >= timeout:
                strike = pos.metadata.get("strike", 0)
                if spot and strike > 0:
                    won = (pos.direction == "UP" and spot >= strike) or \
                          (pos.direction == "DOWN" and spot < strike)
                    resolution = 1.0 if won else 0.0
                else:
                    resolution = 0.0
                resolved = self._pm.close_position(pos.position_id, resolution)
                if resolved:
                    closed.append(resolved)
        return closed


def run_replay(
    tape_path: str,
    trade_log_path: str = "data/replay_trade_log.jsonl",
    trace_path: str = "data/replay_signal_execution_trace.jsonl",
    mode: str = "fast",
    sleep_scale: float = 1.0,
) -> dict:
    """Run offline replay on a recorded tape.

    Args:
        tape_path: Path to live_tape.jsonl
        trade_log_path: Where to write replay trade log
        trace_path: Where to write replay signal traces
        mode: "fast" (no delays) or "realtime" (replay at original speed)
        sleep_scale: Speed multiplier for realtime mode (1.0 = real speed)

    Returns:
        Summary dict with replay statistics.
    """
    tape = Path(tape_path)
    if not tape.exists():
        log.error(f"Tape not found: {tape_path}")
        return {"error": f"tape not found: {tape_path}"}

    # Redirect trade log for replay
    config.TRADE_LOG_PATH = trade_log_path
    # Clear replay outputs
    Path(trade_log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(trace_path).parent.mkdir(parents=True, exist_ok=True)
    for p in [trade_log_path, trace_path]:
        Path(p).write_text("")

    # Initialize components (same as live)
    pm = PositionManager()
    om = OrderManager(pm)
    engine = SignalEngine()
    ks = KillSwitch()
    exp = ExposureTracker(pm)
    analytics = PerformanceAnalytics()
    health = _ReplayHealthStub()
    rm = RiskManager(pm, ks, exp, analytics, health)
    ft = _ReplayFillTracker(pm)

    # Load tape
    records = []
    for line in tape.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    log.info(f"Replay: {len(records)} tape records from {tape_path}")
    log.info(f"Mode: {mode}, trade_log: {trade_log_path}")

    stats = {
        "tape_records": len(records),
        "signals_generated": 0,
        "signals_approved": 0,
        "fills": 0,
        "resolutions": 0,
        "total_pnl": 0.0,
    }

    prev_ts = records[0]["ts"] if records else 0

    for i, rec in enumerate(records):
        ts = rec.get("ts", 0)

        # Realtime delay
        if mode == "realtime" and i > 0:
            delay = (ts - prev_ts) * sleep_scale
            if delay > 0:
                _time.sleep(min(delay, 2.0))  # Cap at 2s per step
        prev_ts = ts

        # Rebuild signal input
        m5 = _rebuild_market_state(rec.get("market_state_5m"))
        m15 = _rebuild_market_state(rec.get("market_state_15m"))

        signal_input = {
            "spot_price": rec.get("spot_price"),
            "spot_source": rec.get("spot_source"),
            "volatility": rec.get("volatility"),
            "vol_source": rec.get("vol_source"),
            "market_state_5m": m5,
            "market_state_15m": m15,
            "timestamp": ts,
            "feed_healthy": rec.get("feed_healthy", True),
            "price_source_gap": rec.get("price_source_gap"),
        }

        # Process through signal engine
        signals = engine.process_snapshot(signal_input)
        stats["signals_generated"] += len(signals)

        # Process each signal through risk → execution
        for sig in signals:
            portfolio_state = {
                "current_capital": pm.get_total_equity(),
                "volatility": rec.get("volatility"),
                "feed_healthy": rec.get("feed_healthy", True),
            }
            risk_result = rm.evaluate_signal(sig, portfolio_state)

            trace = {
                "ts": ts,
                "source": "replay",
                "signal_id": sig.signal_id,
                "direction": sig.direction,
                "market_type": sig.market_type,
                "edge": sig.edge,
                "net_ev": sig.net_ev,
                "kelly_size": sig.recommended_size_pct,
                "risk_decision": risk_result["decision"],
                "risk_reason": risk_result["reason"],
            }

            if risk_result["decision"] in ("APPROVE", "REDUCE"):
                stats["signals_approved"] += 1
                approved = risk_result["adjusted_signal"]
                # Use the matching market state as snapshot
                snapshot = m5 if approved.market_type == "btc-5min" else m15
                order = om.execute_signal(approved, snapshot)
                trace["order_status"] = order.status if order else "rejected_by_order_mgr"
                if order and order.status == "FILLED":
                    stats["fills"] += 1
                    trace["fill_price"] = order.fill_price
                    trace["size_usdc"] = order.size_usdc
                    engine.record_trade(approved.market_type)
                    # Patch position timestamp to tape time for correct resolution
                    for pos in pm.get_open_positions():
                        if pos.order_id == order.order_id:
                            pos.entry_timestamp = ts
                            break
            else:
                trace["order_status"] = "not_submitted"

            with open(trace_path, "a") as f:
                f.write(json.dumps(trace, default=str) + "\n")

        # Check resolutions based on tape time
        spot = rec.get("spot_price")
        closed = ft.check_resolutions(ts, spot)
        for pos in closed:
            rm.record_trade_result(pos)
            stats["resolutions"] += 1
            # Sync PnL to order
            if pos.order_id:
                om.sync_order_pnl_from_position(pos.order_id, pos.pnl or 0)

    # Final summary
    perf = analytics.get_summary()
    stats["total_pnl"] = perf["total_pnl"]
    stats["win_rate"] = perf["win_rate"]
    stats["total_trades"] = perf["total_trades"]
    stats["final_equity"] = pm.get_total_equity()

    # Restore original trade log path
    config.TRADE_LOG_PATH = _ORIG_TRADE_LOG

    log.info(f"Replay complete: {stats['fills']} fills, {stats['resolutions']} resolutions, "
             f"PnL=${stats['total_pnl']:.2f}, equity=${stats['final_equity']:.2f}")

    return stats
