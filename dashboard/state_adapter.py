"""Read-only state adapter for the monitoring dashboard.

Centralizes all state collection from bot components into normalized
JSON-serializable snapshots. All methods are read-only — no mutations.

This is the single interface between the dashboard and trading engine internals.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Any
from collections import deque

from feeds.aggregator import Aggregator
from strategies.signal_engine import SignalEngine
from strategies import probability_model
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from risk.risk_manager import RiskManager
from risk.kill_switch import KillSwitch
from risk.performance_analytics import PerformanceAnalytics
from risk.health_monitor import HealthMonitor
import config


class StateAdapter:
    """Read-only adapter that gathers normalized dashboard data."""

    def __init__(
        self,
        aggregator: Aggregator,
        signal_engine: SignalEngine,
        order_manager: OrderManager,
        position_manager: PositionManager,
        risk_manager: RiskManager,
        kill_switch: KillSwitch,
        analytics: PerformanceAnalytics,
        health_monitor: HealthMonitor,
    ):
        self._agg = aggregator
        self._engine = signal_engine
        self._om = order_manager
        self._pm = position_manager
        self._rm = risk_manager
        self._ks = kill_switch
        self._analytics = analytics
        self._hm = health_monitor
        self._start_time = time.time()
        self._price_history: deque[dict] = deque(maxlen=120)
        self._last_truth_trace_ts: float = 0.0
        self._last_market_timing_trace_ts: float = 0.0

    def record_price(self, price: float, ts: float) -> None:
        """Record a price point for sparkline history."""
        self._price_history.append({"t": ts, "p": price})

    def get_status_snapshot(self) -> dict:
        ks_on = self._ks.is_active()
        # kill_switch_active == emergency halt engaged (blocks new trades via risk layer)
        return {
            "execution_mode": config.EXECUTION_MODE,
            "trading_enabled": config.TRADING_ENABLED,
            "testing_mode": config.TESTING_MODE,
            "kill_switch_active": ks_on,
            "kill_switch_blocks_trading": ks_on,
            "kill_switch_reason": self._ks.trigger_reason,
            "system_healthy": self._hm.is_system_healthy(),
            "uptime_seconds": time.time() - self._start_time,
            "warming_up": self._agg.warming_up,
        }

    def get_price_snapshot(self) -> dict:
        tick = self._agg.get_current_price()
        vol = self._agg.get_volatility()
        b = self._agg.latest_binance_tick
        c = self._agg.latest_coinbase_tick
        model_spot = self._agg.get_model_spot_price()
        gap = self._agg.get_price_source_gap()
        model_source = "Coinbase USD" if (c and not c.is_stale) else "Binance USDT"
        gap_detail = self._agg.get_price_gap_detail()

        return {
            "price": tick.price if tick else None,
            "source": self._agg.current_active_source,
            "latency_ms": tick.age_ms() if tick else None,
            "model_spot": model_spot,
            "model_source": model_source,
            "price_source_gap": gap,
            "price_gap_detail": gap_detail,
            "binance": {
                "ok": b is not None and not b.is_stale,
                "price": b.price if b else None,
                "latency_ms": b.age_ms() if b else None,
                "tick_rate": self._agg.binance_feed.tick_rate if self._agg.binance_feed else 0,
                "last_error": self._agg.binance_feed.last_error if self._agg.binance_feed else "",
                "reconnect_count": self._agg.binance_feed.reconnect_count if self._agg.binance_feed else 0,
            },
            "coinbase": {
                "ok": c is not None and not c.is_stale,
                "price": c.price if c else None,
                "latency_ms": c.staleness_ms() if c else None,
                "tick_rate": self._agg.coinbase_feed.tick_rate if self._agg.coinbase_feed else 0,
            },
            "volatility": vol,
            "sparkline": list(self._price_history),
        }

    def _market_dict(self, state) -> Optional[dict]:
        if state is None:
            return None
        tick = self._agg.get_current_price()
        vol = self._agg.get_volatility()
        model = None
        if tick and vol and state.strike_price > 0:
            model = probability_model.calculate_probability(
                tick.price, state.strike_price, vol, state.time_remaining_seconds)

        # Determine market lifecycle phase + window progress
        if state.time_remaining_seconds <= 0:
            phase = "RESOLVED"
        elif state.window_started:
            phase = "ACTIVE_WINDOW"
        else:
            phase = "TRADING"

        # Window progress: how far through the observation window we are
        window_progress = 0.0
        window_duration = 300.0 if "5min" in state.market_type else 900.0
        window_phase = "EARLY"
        if state.window_started and state.time_remaining_seconds > 0:
            elapsed = window_duration - min(state.time_remaining_seconds, window_duration)
            window_progress = max(0.0, min(1.0, elapsed / window_duration))
            if state.time_remaining_seconds <= 30:
                window_phase = "SNIPER"
            elif window_progress >= 0.6:
                window_phase = "LATE"

        # Diagnostics must match this MarketState row (same condition_id). Otherwise the
        # dashboard can mix a new Polymarket poll (timing/strike) with stale edges from
        # the previous window until the next process_snapshot cycle.
        diag = self._engine.diagnostics.get(state.market_type, {})
        if diag and diag.get("condition_id") != state.condition_id:
            diag = None

        gamma_end = getattr(state, "gamma_end_remaining_seconds", 0.0) or 0.0
        timing_src = getattr(state, "timing_source", "") or ""
        return {
            "market_id": state.market_id,
            "condition_id": state.condition_id,
            "market_type": state.market_type,
            "yes_price": state.yes_price,
            "no_price": state.no_price,
            "spread": state.spread,
            "strike_price": state.strike_price,
            "time_remaining": state.time_remaining_seconds,
            "gamma_end_remaining": gamma_end,
            "timing_display_source": timing_src or "observation_window",
            "timing_source": timing_src,
            "live_tradable_countdown_s": state.time_remaining_seconds,
            "time_to_window": state.time_to_window_seconds,
            "window_started": state.window_started,
            "is_signalable": state.is_signalable,
            "phase": phase,
            "question": state.question,
            "is_active": state.is_active,
            "price_source": "CLOB midpoint",
            "updated_ago_s": time.time() - state.timestamp,
            "model": model,
            "window_progress": window_progress,
            "window_phase": window_phase,
            "window_duration": window_duration,
            "diagnostics": {
                "spot": diag.get("spot"),
                "strike": diag.get("strike"),
                "distance": diag.get("distance"),
                "z_score": diag.get("z_score"),
                "model_up": diag.get("model_up"),
                "model_down": diag.get("model_down"),
                "edge_up": diag.get("edge_up"),
                "edge_down": diag.get("edge_down"),
                "best_direction": diag.get("best_direction"),
                "best_edge": diag.get("best_edge"),
                "gross_ev": diag.get("gross_ev"),
                "net_ev": diag.get("net_ev"),
                "estimated_costs": diag.get("estimated_costs"),
                "ev_per_dollar": diag.get("ev_per_dollar"),
                "kelly_size": diag.get("kelly_size"),
                "disagreement": diag.get("disagreement"),
                "fragile_certainty": diag.get("fragile_certainty"),
                "data_only_15m": diag.get("data_only_15m"),
                "move_5s": diag.get("move_5s"),
                "move_10s": diag.get("move_10s"),
                "move_30s": diag.get("move_30s"),
                "urgency_pass": diag.get("urgency_pass"),
                "market_age_ms": diag.get("market_age_ms"),
                "lag_proxy_pass": diag.get("lag_proxy_pass"),
                "proto_latency_gate": diag.get("proto_latency_gate"),
                "freshness_pass": diag.get("freshness_pass"),
                "freshest_window": diag.get("freshest_window"),
                "market_phase": diag.get("market_phase"),
                "phase_would_pass": diag.get("phase_would_pass"),
                "reasons": diag.get("reasons", []),
            } if diag else None,
        }

    def get_market_snapshot(self) -> dict:
        m5 = self._agg.get_current_market("btc-5min")
        m15 = self._agg.get_current_market("btc-15min")
        return {
            "btc_5min": self._market_dict(m5),
            "btc_15min": self._market_dict(m15),
        }

    def get_signal_snapshot(self) -> dict:
        history = self._engine.signal_history[:20]

        # Determine why signals may be absent
        m5 = self._agg.get_current_market("btc-5min")
        m15 = self._agg.get_current_market("btc-15min")
        any_signalable = (m5 and m5.is_signalable) or (m15 and m15.is_signalable)

        if len(history) > 0:
            idle_reason = None
        elif any_signalable:
            idle_reason = "Evaluating live markets — edge below threshold"
        elif m5 or m15:
            idle_reason = "Markets found but missing valid data"
        else:
            idle_reason = "No active markets discovered yet"

        return {
            "active_strategies": self._engine.get_active_strategies(),
            "idle_reason": idle_reason,
            "any_signalable": any_signalable,
            "recent_signals": [
                {
                    "signal_id": s.signal_id,
                    "timestamp": s.timestamp,
                    "market_type": s.market_type,
                    "strategy": s.strategy,
                    "direction": s.direction,
                    "edge": s.edge,
                    "confidence": s.confidence,
                    "model_probability": s.model_probability,
                    "market_probability": s.market_probability,
                    "recommended_size_pct": s.recommended_size_pct,
                }
                for s in history
            ],
            "last_competition": self._engine.last_competition,
        }

    def get_positions_snapshot(self) -> dict:
        open_pos = self._pm.get_open_positions()
        closed = self._pm.get_closed_positions()[-20:]
        total_equity = self._pm.get_total_equity()
        now = time.time()
        open_by_order = {p.order_id: p for p in open_pos if p.order_id}

        # Recent fills: execution log; linked_open ties each row to an active position
        recent_orders = self._om.get_recent_fills(10)
        recent_fills = []
        for o in recent_orders:
            linked = open_by_order.get(o.order_id)
            fts = o.fill_timestamp or o.timestamp
            recent_fills.append({
                "order_id": o.order_id,
                "timestamp": fts,
                "fill_age_seconds": max(0.0, now - fts),
                "market_type": o.market_type,
                "direction": o.direction,
                "size_usdc": o.size_usdc,
                "fill_price": o.fill_price,
                "pnl": o.pnl,
                "strategy": o.metadata.get("strategy", ""),
                "position_id": linked.position_id if linked else None,
                "linked_open": linked is not None,
            })

        deployed = sum(p.entry_price * p.num_shares for p in open_pos)
        realized = self._pm.get_total_pnl()

        # region agent log
        try:
            _dbg = {
                "sessionId": "16560d",
                "runId": "truth-pre",
                "hypothesisId": "H1",
                "location": "state_adapter.py:get_positions_snapshot",
                "message": "positions vs fills",
                "data": {
                    "open_n": len(open_pos),
                    "fills_n": len(recent_fills),
                    "deployed": round(deployed, 4),
                    "free": round(self._pm.get_available_capital(), 4),
                },
                "timestamp": int(now * 1000),
            }
            Path("/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log").open("a").write(
                json.dumps(_dbg) + "\n"
            )
        except Exception:
            pass
        # endregion

        return {
            "starting_capital_usdc": config.STARTING_CAPITAL_USDC,
            "available_capital": self._pm.get_available_capital(),
            "total_equity": total_equity,
            "deployed_capital_usdc": deployed,
            "realized_pnl_usdc": realized,
            "accounting_identity": {
                "free_capital": (
                    "available cash: on each fill subtract order.size_usdc; on resolution add "
                    "resolution_price * num_shares. Equals available_capital."
                ),
                "deployed_capital_usdc": (
                    "sum(entry_price * num_shares) for OPEN positions (cost basis, not MTM)."
                ),
                "total_equity": "total_equity = free_capital + deployed_capital_usdc.",
                "realized_pnl_usdc": "sum of pnl on closed positions; equity drift = starting + realized when flat.",
            },
            "open_positions_count": len(open_pos),
            "recent_fills_count": len(recent_fills),
            "resolved_count": len(closed),
            "open_positions": [
                {
                    "position_id": p.position_id,
                    "order_id": p.order_id,
                    "market_id": p.market_id,
                    "market_type": p.market_type,
                    "direction": p.direction,
                    "entry_price": p.entry_price,
                    "num_shares": p.num_shares,
                    "cost_basis": p.entry_price * p.num_shares,
                    "entry_timestamp": p.entry_timestamp,
                    "hold_seconds": p.hold_duration_seconds(),
                    "strategy": p.metadata.get("strategy", ""),
                }
                for p in open_pos
            ],
            "recent_closed": [
                {
                    "position_id": p.position_id,
                    "market_type": p.market_type,
                    "direction": p.direction,
                    "entry_price": p.entry_price,
                    "num_shares": p.num_shares,
                    "pnl": p.pnl,
                    "resolution_price": p.resolution_price,
                }
                for p in reversed(closed)
            ],
            "recent_fills": recent_fills,
            "rejected_count": self._om.rejected_count,
            "rejection_breakdown": self._om.rejection_breakdown,
            "recent_rejections": self._om.recent_rejections[-10:],
            "sizing": {
                "max_order_pct": config.MAX_ORDER_SIZE_PCT,
                "computed_max_order_usdc": total_equity * config.MAX_ORDER_SIZE_PCT,
                "floor_usdc": config.MAX_ORDER_SIZE_FLOOR_USDC,
                "ceiling_usdc": config.MAX_ORDER_SIZE_CEILING_USDC,
                "last_sizing_detail": self._om._last_sizing_detail,
            },
        }

    def get_performance_snapshot(self) -> dict:
        s = self._analytics.get_summary()
        breakdown = self._analytics.get_strategy_breakdown()
        deployed = sum(
            p.entry_price * p.num_shares for p in self._pm.get_open_positions()
        )
        return {
            **s,
            "strategy_breakdown": breakdown,
            "total_equity": self._pm.get_total_equity(),
            "available_capital": self._pm.get_available_capital(),
            "deployed_capital_usdc": deployed,
            "open_count": self._pm.count_open_positions(),
        }

    def get_risk_snapshot(self) -> dict:
        return self._rm.get_risk_status()

    def get_health_snapshot(self) -> dict:
        check = self._hm.run_health_check()
        warnings = self._hm.get_warnings()
        return {**check, "warnings": warnings}

    @staticmethod
    def _fmt_time(s: float) -> str:
        if s < 60: return f"{s:.0f}s"
        if s < 3600: return f"{int(s//60)}m {int(s%60)}s"
        return f"{int(s//3600)}h {int((s%3600)//60):02d}m"

    def get_full_snapshot(self) -> dict:
        """Full state for WebSocket broadcast."""
        snap = {
            "ts": time.time(),
            "status": self.get_status_snapshot(),
            "prices": self.get_price_snapshot(),
            "markets": self.get_market_snapshot(),
            "signals": self.get_signal_snapshot(),
            "positions": self.get_positions_snapshot(),
            "performance": self.get_performance_snapshot(),
            "risk": self.get_risk_snapshot(),
            "health": self.get_health_snapshot(),
        }
        self._maybe_append_dashboard_truth_trace(snap)
        self._maybe_append_market_timing_truth_trace(snap)
        return snap

    def _maybe_append_dashboard_truth_trace(self, snap: dict) -> None:
        """Throttled JSONL + one-line report for dashboard vs engine consistency audits."""
        now = time.time()
        if now - self._last_truth_trace_ts < 2.0:
            return
        self._last_truth_trace_ts = now
        try:
            pos = snap["positions"]
            risk = snap["risk"]
            st = snap["status"]
            health = snap["health"]
            m5 = snap["markets"].get("btc_5min") or {}
            m15 = snap["markets"].get("btc_15min") or {}
            row = {
                "timestamp": now,
                "total_equity": pos.get("total_equity"),
                "free_capital": pos.get("available_capital"),
                "deployed_capital_usdc": pos.get("deployed_capital_usdc"),
                "open_positions_count": pos.get("open_positions_count"),
                "recent_fills_count": pos.get("recent_fills_count"),
                "resolved_count": pos.get("resolved_count"),
                "exposure_pct": risk.get("exposure_pct"),
                "kill_switch_active": st.get("kill_switch_active"),
                "kill_switch_blocks_trading": st.get("kill_switch_blocks_trading"),
                "system_healthy": st.get("system_healthy"),
                "open_positions": [],
                "recent_fills": [],
                "market_cards": [],
            }
            for p in pos.get("open_positions") or []:
                row["open_positions"].append({
                    "position_id": p.get("position_id"),
                    "market_type": p.get("market_type"),
                    "direction": p.get("direction"),
                    "position_open_ts": p.get("entry_timestamp"),
                    "displayed_hold_seconds": p.get("hold_seconds"),
                })
            for f in pos.get("recent_fills") or []:
                row["recent_fills"].append({
                    "fill_id": f.get("order_id"),
                    "fill_ts": f.get("timestamp"),
                    "linked_open": f.get("linked_open"),
                })
            for label, m in (("btc_5min", m5), ("btc_15min", m15)):
                if not m:
                    row["market_cards"].append({
                        "label": label,
                        "displayed_time_remaining": None,
                        "raw_backend_timing_value": None,
                        "source_field": "time_remaining",
                    })
                    continue
                row["market_cards"].append({
                    "label": label,
                    "displayed_time_remaining": m.get("time_remaining"),
                    "raw_backend_timing_value": m.get("time_remaining"),
                    "gamma_end_remaining": m.get("gamma_end_remaining"),
                    "timing_source": m.get("timing_source"),
                    "source_field": "time_remaining (slug_period | gamma_*)",
                })
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            trace_path = log_dir / "dashboard_truth_trace.jsonl"
            with trace_path.open("a") as tf:
                tf.write(json.dumps(row, default=str) + "\n")
            report_path = log_dir / "dashboard_truth_report.txt"
            with report_path.open("a") as rf:
                rf.write(
                    f"{now:.3f} equity={pos.get('total_equity'):.2f} "
                    f"free={pos.get('available_capital'):.2f} "
                    f"open={pos.get('open_positions_count')} "
                    f"fills={pos.get('recent_fills_count')} "
                    f"resolved={pos.get('resolved_count')} "
                    f"exp={risk.get('exposure_pct')} "
                    f"ks={st.get('kill_switch_active')} "
                    f"health={st.get('system_healthy')}\n"
                )
        except Exception:
            pass

    def _maybe_append_market_timing_truth_trace(self, snap: dict) -> None:
        """Throttled JSONL for market card timing audit (slug vs Gamma fields)."""
        now = time.time()
        if now - self._last_market_timing_trace_ts < 2.0:
            return
        self._last_market_timing_trace_ts = now
        try:
            pos = snap.get("positions") or {}
            risk = snap.get("risk") or {}
            markets = snap.get("markets") or {}
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            trace_path = log_dir / "market_timing_truth_trace.jsonl"
            report_path = log_dir / "market_timing_truth_report.txt"
            active_ids = [
                p.get("position_id")
                for p in (pos.get("open_positions") or [])
                if p.get("position_id")
            ]
            for key in ("btc_5min", "btc_15min"):
                m = markets.get(key) or {}
                if not m:
                    continue
                row = {
                    "timestamp": now,
                    "market_id": m.get("market_id"),
                    "market_type": m.get("market_type"),
                    "displayed_observation_start_sec": m.get("time_to_window"),
                    "displayed_window_end_sec": m.get("time_remaining"),
                    "raw_backend_time_remaining_s": m.get("time_remaining"),
                    "raw_backend_time_to_window_s": m.get("time_to_window"),
                    "timing_source": m.get("timing_source"),
                    "intended_live_countdown_s": m.get("live_tradable_countdown_s"),
                    "window_started": m.get("window_started"),
                    "gamma_end_remaining_s": m.get("gamma_end_remaining"),
                    "refers_to_current_live_slug_period": m.get("timing_source") == "slug_period",
                    "open_positions_count": pos.get("open_positions_count"),
                    "active_position_ids": active_ids,
                    "daily_pnl": risk.get("daily_pnl"),
                    "daily_loss_limit": risk.get("daily_limit"),
                }
                with trace_path.open("a") as tf:
                    tf.write(json.dumps(row, default=str) + "\n")
            m5 = markets.get("btc_5min") or {}
            m15 = markets.get("btc_15min") or {}
            with report_path.open("a") as rf:
                rf.write(
                    f"{now:.3f} 5m_src={m5.get('timing_source')} "
                    f"5m_rem={m5.get('time_remaining')} "
                    f"15m_src={m15.get('timing_source')} "
                    f"15m_rem={m15.get('time_remaining')} "
                    f"open={pos.get('open_positions_count')} "
                    f"daily_pnl={risk.get('daily_pnl')}\n"
                )
        except Exception:
            pass
