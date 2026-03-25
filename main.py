"""Project13 — Real-time BTC trading system with risk management and web dashboard."""

from __future__ import annotations

import asyncio
import signal
import time
from typing import Optional

from feeds.aggregator import Aggregator
from models.market_state import MarketState
from models.trade_signal import TradeSignal
from models.order import Order
from strategies.signal_engine import SignalEngine
from strategies import probability_model
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from execution.fill_tracker import FillTracker
from risk.risk_manager import RiskManager
from risk.kill_switch import KillSwitch
from risk.exposure_tracker import ExposureTracker
from risk.performance_analytics import PerformanceAnalytics
from risk.health_monitor import HealthMonitor
from utils.config_loader import load_env, validate_config
from utils.logger import get_logger
import config

log = get_logger("main")

import json as _json
import uuid as _uuid
from pathlib import Path as _Path

_RUN_ID = str(_uuid.uuid4())[:8]
_RUN_STARTED = time.time()

_TRACE_PATH = _Path("logs/signal_execution_trace.jsonl")
_COMPETITION_TRACE_PATH = _Path("logs/strategy_competition_trace.jsonl")


def _rotate_logs() -> None:
    """Archive previous run's trace files so each run starts fresh."""
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in [_TRACE_PATH, _COMPETITION_TRACE_PATH]:
        if p.exists() and p.stat().st_size > 0:
            archive = p.parent / f"{p.stem}_{ts}{p.suffix}"
            p.rename(archive)


def _log_trace(trace: dict) -> None:
    """Append signal execution trace to JSONL with run_id."""
    try:
        trace["run_id"] = _RUN_ID
        trace["run_started_at"] = _RUN_STARTED
        _TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRACE_PATH, "a") as f:
            f.write(_json.dumps(trace, default=str) + "\n")
    except Exception:
        pass

# Terminal dashboard
CLEAR_LINE = "\033[2K"
DASHBOARD_LINES = 55
C, G, Y, R, D, B, M, RST = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m",
    "\033[90m", "\033[1m", "\033[95m", "\033[0m",
)


def _cl(ms):
    if ms is None: return "---"
    s = f"{ms:.0f}ms"
    if ms < 100: return f"{G}{s}{RST}"
    return f"{Y}{s}{RST}" if ms < 300 else f"{R}{s}{RST}"


def _fl(tick, feed, label):
    if tick is None:
        return f"  {R}● {label:<8} ---{' ':>24}NO DATA{RST}"
    p = f"${tick.price:,.2f}"
    h = f"{Y}STALE{RST}" if tick.is_stale else f"{G}OK{RST}"
    l = _cl(tick.age_ms()) if label == "Binance" else _cl(tick.staleness_ms())
    rt = f"{feed.tick_rate:.1f}/s" if feed else "---"
    c = G if not tick.is_stale else Y
    return f"  {c}●{RST} {label:<8} {p:<16} {h:<15} {l:<14} {rt}"


def _ft(s):
    if s <= 0: return f"{R}RESOLVED{RST}"
    if s <= 20: return f"{R}{s:.0f}s{RST}"
    if s <= 60: return f"{Y}{s:.0f}s{RST}"
    if s <= 3600: return f"{G}{int(s//60)}m {int(s%60):02d}s{RST}"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return f"{G}{h}h {m:02d}m{RST}"


def _ce(e):
    s = f"{e:+.3f}"
    if abs(e) >= config.CONFIDENCE_HIGH_EDGE: return f"{G}{s}{RST}"
    if abs(e) >= config.CONFIDENCE_MEDIUM_EDGE: return f"{Y}{s}{RST}"
    return f"{D}{s}{RST}"


def _cp(p):
    s = f"${p:+.2f}"
    return f"{G}{s}{RST}" if p > 0 else f"{R}{s}{RST}" if p < 0 else f"{D}{s}{RST}"


def _mkt_section(state, label, spot, vol, sig):
    lines = [f"  {B}{M}{label}{RST}"]
    if state is None:
        lines.append(f"    {D}Searching for active market...{RST}")
        return lines
    uc = G if state.yes_price > 0.5 else Y if state.yes_price > 0.3 else R
    dc = G if state.no_price > 0.5 else Y if state.no_price > 0.3 else R
    lines.append(f"    Mkt: Up={uc}{state.yes_price:.3f}{RST} Down={dc}{state.no_price:.3f}{RST} Spread={state.spread:.3f}")
    if spot and vol and state.strike_price > 0:
        pr = probability_model.calculate_probability(spot, state.strike_price, vol, state.time_remaining_seconds)
        lines.append(f"    Mdl: Up={G}{pr['prob_up']:.3f}{RST} Down={G}{pr['prob_down']:.3f}{RST} z={pr['z_score']:+.2f}")
        lines.append(f"    Edge: Up={_ce(pr['prob_up']-state.yes_price)} Down={_ce(pr['prob_down']-state.no_price)}")
    else:
        lines.append(f"    {D}Model: awaiting data...{RST}")
    sk = f"${state.strike_price:,.2f}" if state.strike_price > 0 else "N/A"
    sig_tag = f" {G}[LIVE]{RST}" if state.is_signalable else ""
    lines.append(f"    Strike: {sk}  |  Resolves: {_ft(state.time_remaining_seconds)}{sig_tag}")
    if sig:
        dc2 = G if sig.direction == "UP" else R
        lines.append(f"    {B}Signal: {dc2}{sig.direction}{RST} [{sig.strategy}] edge={_ce(sig.edge)} size={sig.recommended_size_pct:.0%}")
    else:
        lines.append(f"    {D}Signal: none{RST}")
    return lines


def _exec_section(om, pm):
    mode = config.EXECUTION_MODE.upper()
    mc = Y if mode == "PAPER" else R
    en = f"{G}ON{RST}" if config.TRADING_ENABLED else f"{R}OFF{RST}"
    cap = pm.get_available_capital()
    lines = [f"  {B}{C}EXECUTION{RST} Mode:{mc}{mode}{RST} Trading:{en} Cap:{B}${cap:.2f}{RST} Open:{pm.count_open_positions()} Rej:{om.rejected_count}"]
    for p in pm.get_open_positions()[:2]:
        dc = G if p.direction == "UP" else R
        lines.append(f"    {dc}●{RST} {p.direction} {p.market_type} {p.num_shares:.0f}sh @{p.entry_price:.3f} ({p.hold_duration_seconds():.0f}s)")
    for o in reversed(om.get_recent_fills(2)):
        age = time.time() - (o.fill_timestamp or o.timestamp)
        pnl = _cp(o.pnl) if o.pnl is not None else f"{D}pend{RST}"
        dc = G if o.direction == "UP" else R
        lines.append(f"    {D}{age:.0f}s{RST} {dc}{o.direction}{RST} {o.market_type} ${o.size_usdc:.1f}@{o.fill_price:.3f} {pnl}")
    return lines


def _risk_section(rm):
    rs = rm.get_risk_status()
    ks = rs["kill_switch"]
    ks_str = f"{R}{B}ACTIVE: {ks['reason']}{RST}" if ks["active"] else f"{G}OK{RST}"
    dd = rs["drawdown_pct"]
    dd_c = R if dd >= rs["drawdown_limit"] * 0.8 else Y if dd > 0.05 else G
    dp = rs["daily_pnl"]
    dp_c = R if dp <= -rs["daily_limit"] * 0.8 else Y if dp < 0 else G
    cl = rs["consecutive_losses"]
    cl_c = R if cl >= rs["max_consecutive"] else Y if cl >= 2 else G
    cool = f" {R}COOLDOWN {rs['cooldown_remaining_s']:.0f}s{RST}" if rs["cooldown_remaining_s"] > 0 else ""
    exp = rs["exposure_pct"]
    exp_c = R if exp >= rs["exposure_limit"] * 0.8 else Y if exp > 0.2 else G
    return [
        f"  {B}{C}RISK{RST} KillSwitch:{ks_str}",
        f"    DD:{dd_c}{dd:.1%}{RST}/{rs['drawdown_limit']:.0%} "
        f"DayPnL:{dp_c}${dp:+.2f}{RST}/-${rs['daily_limit']:.0f}({rs.get('daily_loss_limit_pct', 0):.0%}eq) "
        f"Losses:{cl_c}{cl}{RST}/{rs['max_consecutive']}{cool} "
        f"Exp:{exp_c}{exp:.0%}{RST}/{rs['exposure_limit']:.0%} "
        f"RiskRej:{rs['risk_rejections']}",
    ]


def _perf_section(analytics, capital):
    s = analytics.get_summary()
    if s["total_trades"] == 0:
        return [f"  {B}{C}PERF{RST} {D}No completed trades yet{RST}"]
    wr = s["win_rate"]
    wc = G if wr >= 0.5 else Y if wr >= 0.3 else R
    pc = G if s["total_pnl"] > 0 else R
    pf = s["profit_factor"]
    pfc = G if pf > 1.5 else Y if pf > 1.0 else R
    return [
        f"  {B}{C}PERF{RST} Trades:{s['total_trades']} WR:{wc}{wr:.0%}{RST} PnL:{pc}${s['total_pnl']:+.2f}{RST} PF:{pfc}{pf:.2f}{RST} Sharpe:{s['sharpe_ratio']:.2f} MaxDD:{s['max_drawdown']:.1%}",
        f"    Best:{_cp(s['best_trade'])} Worst:{_cp(s['worst_trade'])} Avg:{_cp(s['avg_pnl'])} HWM:${s['high_water_mark']:.2f}",
    ]


def _health_section(hm):
    warnings = hm.get_warnings()
    if not warnings:
        return [f"  {B}{C}HEALTH{RST} {G}All systems healthy{RST}"]
    lines = [f"  {B}{C}HEALTH{RST} {Y}{len(warnings)} warning(s){RST}"]
    for w in warnings[:3]:
        lines.append(f"    {Y}! {w}{RST}")
    return lines


async def terminal_dashboard(agg, engine, om, pm, ft, rm, analytics, hm, state_adapter, tape_recorder=None):
    """Terminal fallback dashboard + trading logic loop."""
    print("\n" * DASHBOARD_LINES, end="", flush=True)
    last_res_check = 0.0
    last_report = time.time()

    while True:
        tick = agg.get_current_price()
        vol = agg.get_volatility()
        spot = tick.price if tick else None
        ps = f"${tick.price:,.2f}" if tick else "---"

        # Record price for web dashboard sparkline
        if spot and state_adapter:
            state_adapter.record_price(spot, time.time())

        src = agg.current_active_source
        sd = f"{G}{B}▶ BINANCE{RST}" if src == "binance" else f"{Y}{B}▶ COINBASE{RST}" if src == "coinbase" else f"{R}▶ NONE{RST}"
        vs = f"${vol:,.2f}" if vol is not None else "collecting..."

        signals = []
        if not agg.warming_up:
            si = agg.get_signal_input()
            # Inject lightweight position context for v2 overlap/conflict awareness
            if config.LATENCY_ARB_V2_ENABLED:
                si["open_positions"] = [
                    {"market_id": p.market_id, "market_type": p.market_type,
                     "direction": p.direction}
                    for p in pm.get_open_positions()
                ]
            # Record to tape if enabled
            if tape_recorder:
                tape_recorder.record(si)
            signals = engine.process_snapshot(si)

            for sig in signals:
                portfolio_state = {
                    "current_capital": pm.get_total_equity(),
                    "volatility": vol,
                    "feed_healthy": si.get("feed_healthy", False),
                }
                risk_result = rm.evaluate_signal(sig, portfolio_state)

                # Trace every signal through the pipeline
                trace = {
                    "ts": time.time(),
                    "signal_id": sig.signal_id,
                    "strategy": sig.strategy,
                    "direction": sig.direction,
                    "market_type": sig.market_type,
                    "edge": sig.edge,
                    "net_ev": sig.net_ev,
                    "kelly_size": sig.recommended_size_pct,
                    "model_probability": sig.model_probability,
                    "market_probability": sig.market_probability,
                    "price_move_from_strike": abs(sig.spot_price - sig.strike_price) if sig.strike_price > 0 else 0,
                    "move_5s": sig.metadata.get("move_5s"),
                    "move_10s": sig.metadata.get("move_10s"),
                    "move_30s": sig.metadata.get("move_30s"),
                    "urgency_pass": sig.metadata.get("urgency_pass"),
                    "lag_proxy_pass": sig.metadata.get("lag_proxy_pass"),
                    "proto_latency_gate": sig.metadata.get("proto_latency_gate"),
                    "freshness_pass": sig.metadata.get("freshness_pass"),
                    "freshest_window": sig.metadata.get("freshest_window"),
                    "market_phase": sig.metadata.get("market_phase"),
                    "phase_would_pass": sig.metadata.get("phase_would_pass"),
                    "market_age_ms_signal": sig.metadata.get("market_age_ms"),
                    "disagreement": sig.metadata.get("disagreement"),
                    "risk_decision": risk_result["decision"],
                    "risk_reason": risk_result["reason"],
                }

                if risk_result["decision"] in ("APPROVE", "REDUCE"):
                    approved_sig = risk_result["adjusted_signal"]
                    snapshot = agg.get_current_market(approved_sig.market_type)
                    order = om.execute_signal(approved_sig, snapshot)
                    trace["order_status"] = order.status if order else "rejected_by_order_mgr"
                    if order and order.status == "FILLED":
                        engine.record_trade(approved_sig.market_type)
                        trace["fill_price"] = order.fill_price
                        trace["size_usdc"] = order.size_usdc
                        trace["execution_path"] = order.metadata.get("execution_path", "unknown")
                        trace["fee_mode"] = order.metadata.get("fee_mode", "unknown")
                        trace["market_age_ms"] = order.metadata.get("market_age_ms")
                else:
                    trace["order_status"] = "not_submitted"

                _log_trace(trace)

            now = time.time()
            if now - last_res_check >= config.RESOLUTION_POLL_INTERVAL_SECONDS:
                last_res_check = now
                m5 = agg.get_current_market("btc-5min")
                m15 = agg.get_current_market("btc-15min")
                closed = ft.check_resolutions(m5, m15)
                for pos in closed:
                    # Performance counts via RiskManager only (avoid double-counting analytics)
                    rm.record_trade_result(pos)

            if now - last_report >= config.PERFORMANCE_REPORT_INTERVAL_MINUTES * 60:
                last_report = now
                analytics.save_report(pm.get_available_capital())

            if agg.polymarket_feed and agg.polymarket_feed.poll_count > 0:
                hm.record_polymarket_success()

        s5 = next((s for s in signals if s.market_type == "btc-5min"), None)
        s15 = next((s for s in signals if s.market_type == "btc-15min"), None)

        if agg.warming_up:
            status = f"  {Y}{B}⏳ WARMING UP — metrics suppressed{RST}"
        else:
            pe = f" | PM:{agg.polymarket_feed.api_errors}err" if agg.polymarket_feed else ""
            web = f" | Web::{config.DASHBOARD_PORT}" if config.DASHBOARD_ENABLED else ""
            status = f"  Failovers:{agg.failover_events} | Stale:{agg.stale_events}{pe}{web}"

        m5 = agg.get_current_market("btc-5min")
        m15 = agg.get_current_market("btc-15min")

        lines = [
            f"{D}{'─'*72}{RST}",
            f"{B}{C}  PROJECT13 — TRADING SYSTEM{RST}",
            f"{D}{'─'*72}{RST}",
            f"  Price: {B}{ps}{RST}  Source: {sd}  Freshness: {_cl(agg.get_tick_age_ms())}  Vol: {vs}",
            f"{D}{'─'*72}{RST}",
            f"  {'Feed':<10}{'Price':<16} {'Health':<15} {'Latency':<14} {'Rate'}",
            _fl(agg.latest_binance_tick, agg.binance_feed, "Binance"),
            _fl(agg.latest_coinbase_tick, agg.coinbase_feed, "Coinbase"),
            f"{D}{'─'*72}{RST}",
        ]
        lines.extend(_mkt_section(m5, "BTC 5-MIN", spot, vol, s5))
        lines.extend(_mkt_section(m15, "BTC 15-MIN", spot, vol, s15))
        lines.append(f"{D}{'─'*72}{RST}")
        lines.extend(_exec_section(om, pm))
        lines.append(f"{D}{'─'*72}{RST}")
        lines.extend(_risk_section(rm))
        lines.append(f"{D}{'─'*72}{RST}")
        lines.extend(_perf_section(analytics, pm.get_available_capital()))
        lines.append(f"{D}{'─'*72}{RST}")
        lines.extend(_health_section(hm))
        lines.extend([f"{D}{'─'*72}{RST}", status, f"{D}{'─'*72}{RST}"])

        while len(lines) < DASHBOARD_LINES:
            lines.append("")

        out = f"\033[{DASHBOARD_LINES}A"
        for line in lines[:DASHBOARD_LINES]:
            out += f"{CLEAR_LINE}{line}\n"
        print(out, end="", flush=True)
        await asyncio.sleep(config.DASHBOARD_REFRESH_INTERVAL)


async def run():
    """Main entry point."""
    load_env()
    validate_config()
    _rotate_logs()
    log.info(f"Starting Project13... (run_id={_RUN_ID})")
    log.info(f"Execution mode: {config.EXECUTION_MODE}")
    log.info(f"Trading enabled: {config.TRADING_ENABLED}")
    log.info(f"Starting capital: ${config.STARTING_CAPITAL_USDC:.2f}")
    log.info(f"Max drawdown: {config.MAX_DRAWDOWN_PCT:.0%} (of HWM)")
    ex = config.DAILY_LOSS_LIMIT_PCT * config.STARTING_CAPITAL_USDC
    log.info(
        f"Daily loss limit: {config.DAILY_LOSS_LIMIT_PCT:.0%} of equity "
        f"(≈ ${ex:.2f} at ${config.STARTING_CAPITAL_USDC:.0f} baseline)"
    )
    if config.TESTING_MODE:
        log.warning("TESTING MODE ACTIVE — lower thresholds, paper-only")

    agg = Aggregator()
    engine = SignalEngine()
    engine.run_id = _RUN_ID
    pm = PositionManager()
    om = OrderManager(pm)
    ft = FillTracker(pm, agg, om)
    ks = KillSwitch()
    exp = ExposureTracker(pm)
    analytics = PerformanceAnalytics()
    hm = HealthMonitor(agg)
    rm = RiskManager(pm, ks, exp, analytics, hm)
    rm.set_session_start_equity(pm.get_total_equity())

    log.info(f"Signal engine — strategies: {engine.get_active_strategies()}")

    # Tape recorder for replay
    tape_recorder = None
    if config.REPLAY_TAPE_ENABLED:
        from replay.tape_recorder import TapeRecorder
        tape_recorder = TapeRecorder(
            path=config.REPLAY_TAPE_PATH,
            every_n=config.REPLAY_TAPE_EVERY_N_TICKS,
        )
        log.info(f"Tape recording enabled: {config.REPLAY_TAPE_PATH} (every {config.REPLAY_TAPE_EVERY_N_TICKS} ticks)")
    log.info(f"Kill switch: {'ACTIVE' if ks.is_active() else 'inactive'}")

    # State adapter for web dashboard
    state_adapter = None
    web_tasks = []

    if config.DASHBOARD_ENABLED:
        try:
            from dashboard.state_adapter import StateAdapter
            from dashboard.ws_bridge import WebSocketBridge
            from dashboard.server import start_dashboard_server

            state_adapter = StateAdapter(agg, engine, om, pm, rm, ks, analytics, hm)
            ws_bridge = WebSocketBridge(state_adapter)

            web_tasks.append(asyncio.create_task(
                start_dashboard_server(state_adapter, ks, ws_bridge)))
            web_tasks.append(asyncio.create_task(
                ws_bridge.start_broadcasting()))

            log.info(f"Web dashboard enabled on port {config.DASHBOARD_PORT}")
        except Exception as e:
            log.error(f"Dashboard failed to start (bot continues): {e}")
            state_adapter = None
    else:
        log.info("Web dashboard disabled")

    shutdown_event = asyncio.Event()

    def handle_signal():
        log.info("Shutdown signal received — cleaning up...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    agg_task = asyncio.create_task(agg.start())
    term_task = asyncio.create_task(
        terminal_dashboard(agg, engine, om, pm, ft, rm, analytics, hm, state_adapter, tape_recorder)
    )

    await shutdown_event.wait()

    analytics.save_report(pm.get_available_capital())

    term_task.cancel()
    for t in web_tasks:
        t.cancel()
    await agg.stop()
    agg_task.cancel()

    try:
        await asyncio.gather(agg_task, term_task, *web_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass

    log.info("Project13 shut down cleanly — all connections closed")


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
