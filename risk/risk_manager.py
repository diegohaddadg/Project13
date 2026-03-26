"""Risk manager — central risk decision layer.

Sits between signal engine and execution engine. Every signal passes through
the risk manager before execution. Decisions are APPROVE, REDUCE, or REJECT.

Every risk decision is logged with an explicit reason.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional
from copy import copy

from models.trade_signal import TradeSignal
from models.position import Position
from execution.position_manager import PositionManager
from risk.kill_switch import KillSwitch
from risk.exposure_tracker import ExposureTracker
from risk.performance_analytics import PerformanceAnalytics
from risk.health_monitor import HealthMonitor
from utils.logger import get_logger
import config

log = get_logger("risk_mgr")


class RiskManager:
    """Central risk decision layer."""

    def __init__(
        self,
        position_manager: PositionManager,
        kill_switch: KillSwitch,
        exposure_tracker: ExposureTracker,
        analytics: PerformanceAnalytics,
        health_monitor: HealthMonitor,
    ):
        self._pm = position_manager
        self._ks = kill_switch
        self._exposure = exposure_tracker
        self._analytics = analytics
        self._health = health_monitor

        # State tracking
        self._consecutive_losses: int = 0
        self._cooldown_until: float = 0.0
        self._drawdown_cooldown_until: float = 0.0  # separate cooldown for drawdown
        self._daily_pnl: float = 0.0
        self._daily_reset_date: str = ""
        self._risk_rejections: int = 0
        self._session_start_equity: float = 0.0  # set once by set_session_start_equity()

        self._reset_daily_if_needed()

    def _paper_warn_only(self) -> bool:
        """True when paper mode should warn on risk breaches but not hard-block."""
        return config.EXECUTION_MODE == "paper" and config.PAPER_RISK_WARN_ONLY

    def set_session_start_equity(self, equity: float) -> None:
        """Capture session-start equity. Daily loss cap is fixed to this for the session."""
        self._session_start_equity = equity
        log.info(f"Session-start equity set: ${equity:.2f} → daily cap ${self.daily_loss_limit_usd():.2f}")

    def daily_loss_limit_usd(self) -> float:
        """Fixed daily loss cap = session_start_equity × DAILY_LOSS_LIMIT_PCT."""
        return max(0.0, self._session_start_equity) * float(config.DAILY_LOSS_LIMIT_PCT)

    @staticmethod
    def min_net_ev_threshold(signal: TradeSignal) -> float:
        """Same floor the strategy used (or stricter); keeps risk aligned with signal economics."""
        if signal.strategy == "sniper":
            return max(config.MIN_NET_EV, config.SNIPER_MIN_NET_EV)
        if signal.strategy == "latency_arb":
            return max(config.MIN_NET_EV, config.SHORT_MARKET_MIN_NET_EV)
        return config.MIN_NET_EV

    def evaluate_signal(
        self, signal: TradeSignal, portfolio_state: dict
    ) -> dict:
        """Evaluate a signal against all risk rules.

        Args:
            signal: The trade signal to evaluate.
            portfolio_state: Dict with current_capital, volatility, feed_healthy, etc.

        Returns:
            dict with:
                decision: "APPROVE" | "REDUCE" | "REJECT"
                adjusted_signal: TradeSignal or None
                reason: str
        """
        current_capital = portfolio_state.get("current_capital", 0)
        volatility = portfolio_state.get("volatility")
        daily_loss_cap_usd = self.daily_loss_limit_usd()

        # Update analytics HWM
        self._analytics.update_hwm(current_capital)

        # A. Kill switch
        if self._ks.is_active():
            return self._reject(f"Kill switch active: {self._ks.trigger_reason}")

        # Run kill switch trigger checks — only for sustained critical failures
        # Do NOT kill-switch on transient latency spikes
        health = self._health.run_health_check()
        drawdown = self._analytics.get_current_drawdown(current_capital)
        pw = self._paper_warn_only()

        self._ks.check_triggers(
            drawdown_breached=False,  # drawdown now uses cooldown, not kill switch
            daily_limit_hit=(self._daily_pnl <= -daily_loss_cap_usd) and not pw,
            feeds_healthy=health["any_feed_ok"],
            polymarket_healthy=True,
            latency_ok=True,
        )
        if self._ks.is_active():
            return self._reject(f"Kill switch just triggered: {self._ks.trigger_reason}")

        # B. Max drawdown — cooldown-based (not permanent kill switch)
        now = time.time()
        if drawdown >= config.MAX_DRAWDOWN_PCT:
            if pw:
                log.warning(f"[PAPER WARN] Drawdown {drawdown:.1%} exceeds max {config.MAX_DRAWDOWN_PCT:.1%} — continuing for data collection")
            else:
                if self._drawdown_cooldown_until == 0.0:
                    # First breach — start cooldown
                    self._drawdown_cooldown_until = now + config.DRAWDOWN_COOLDOWN_SECONDS
                    log.warning(
                        f"[RISK] Drawdown {drawdown:.1%} exceeds max {config.MAX_DRAWDOWN_PCT:.1%} "
                        f"— entering {config.DRAWDOWN_COOLDOWN_SECONDS:.0f}s cooldown"
                    )
                remaining = self._drawdown_cooldown_until - now
                if remaining > 0:
                    return self._reject(
                        f"Drawdown cooldown: {drawdown:.1%} exceeds max {config.MAX_DRAWDOWN_PCT:.1%} "
                        f"({remaining:.0f}s remaining)"
                    )
                else:
                    # Cooldown expired but still in drawdown — reset cooldown for another cycle
                    log.warning(
                        f"[RISK] Drawdown cooldown expired but still at {drawdown:.1%} "
                        f"— restarting {config.DRAWDOWN_COOLDOWN_SECONDS:.0f}s cooldown"
                    )
                    self._drawdown_cooldown_until = now + config.DRAWDOWN_COOLDOWN_SECONDS
                    return self._reject(
                        f"Drawdown cooldown restarted: {drawdown:.1%} still exceeds max {config.MAX_DRAWDOWN_PCT:.1%}"
                    )
        elif self._drawdown_cooldown_until > 0.0:
            # Was in drawdown cooldown but drawdown has recovered
            if now >= self._drawdown_cooldown_until:
                log.warning(
                    f"[RISK] Drawdown recovered to {drawdown:.1%} after cooldown — resuming trading"
                )
                self._drawdown_cooldown_until = 0.0
            else:
                # Still in cooldown window even though drawdown recovered
                remaining = self._drawdown_cooldown_until - now
                return self._reject(
                    f"Drawdown cooldown active ({remaining:.0f}s remaining), "
                    f"current drawdown {drawdown:.1%}"
                )

        # C. Daily loss limit
        self._reset_daily_if_needed()
        if self._daily_pnl <= -daily_loss_cap_usd:
            if pw:
                log.warning(f"[PAPER WARN] Daily loss ${self._daily_pnl:.2f} exceeds limit -${daily_loss_cap_usd:.2f} — continuing for data collection")
            else:
                return self._reject(
                    f"Daily loss ${self._daily_pnl:.2f} exceeds limit "
                    f"-${daily_loss_cap_usd:.2f} ({config.DAILY_LOSS_LIMIT_PCT:.0%} of ${current_capital:.2f} equity)"
                )

        # D. Consecutive loss cooldown
        if time.time() < self._cooldown_until:
            remaining = self._cooldown_until - time.time()
            if pw:
                log.warning(f"[PAPER WARN] Cooldown active ({remaining:.0f}s) — continuing for data collection")
            else:
                return self._reject(
                    f"Cooldown active ({remaining:.0f}s remaining after "
                    f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losses)"
                )

        # E. Exposure limits
        size_usdc = current_capital * signal.recommended_size_pct
        if self._exposure.would_exceed_limits(size_usdc, signal.market_id):
            # Try reducing size
            reduced_pct = signal.recommended_size_pct * 0.5
            reduced_size = current_capital * reduced_pct
            if not self._exposure.would_exceed_limits(reduced_size, signal.market_id):
                return self._reduce(signal, reduced_pct, "Exposure limit — size reduced by 50%")
            return self._reject("Exposure limits would be exceeded even at reduced size")

        # F. Volatility circuit breaker
        if volatility is not None and config.VOLATILITY_CIRCUIT_BREAKER > 0:
            if volatility > config.VOLATILITY_CIRCUIT_BREAKER:
                return self._reject(
                    f"Volatility ${volatility:.2f} exceeds circuit breaker "
                    f"${config.VOLATILITY_CIRCUIT_BREAKER:.2f}"
                )

        # G. Latency guard (same economics as strategies: gate on strategy net_ev below)
        if not health["latency_ok"]:
            return self._reject(
                f"Feed latency {health.get('feed_latency_ms', 0):.0f}ms exceeds "
                f"max {config.MAX_ACCEPTABLE_LATENCY_MS}ms"
            )

        # H. Minimum net EV
        min_net = RiskManager.min_net_ev_threshold(signal)
        if signal.net_ev < min_net:
            return self._reject(
                f"net_ev {signal.net_ev:.4f} below minimum {min_net:.4f} for {signal.strategy}"
            )

        # I. Model-market disagreement guard
        disagreement = abs(signal.model_probability - signal.market_probability)
        if disagreement >= config.DISAGREEMENT_HARD_REJECT:
            return self._reject(
                f"rejected: model-market disagreement {disagreement:.2f} >= hard limit "
                f"{config.DISAGREEMENT_HARD_REJECT}"
            )

        # J. Fragile certainty check
        fragile = (
            signal.model_probability >= config.FRAGILE_CERTAINTY_MODEL_PROB
            and signal.market_probability <= config.FRAGILE_CERTAINTY_MAX_MARKET_PROB
        )

        # Apply size adjustments for disagreement / fragile certainty
        adjusted = signal
        reason_notes = []

        if disagreement >= config.DISAGREEMENT_SOFT_CAP:
            adjusted = copy(signal)
            adjusted.metadata = dict(signal.metadata)
            adjusted.recommended_size_pct = min(
                adjusted.recommended_size_pct, config.DISAGREEMENT_REDUCED_SIZE_PCT
            )
            reason_notes.append(
                f"size capped to {config.DISAGREEMENT_REDUCED_SIZE_PCT:.0%}: "
                f"disagreement {disagreement:.2f}"
            )

        if fragile:
            if adjusted is signal:
                adjusted = copy(signal)
                adjusted.metadata = dict(signal.metadata)
            adjusted.recommended_size_pct *= config.FRAGILE_CERTAINTY_SIZE_MULTIPLIER
            adjusted.metadata["fragile_certainty"] = True
            reason_notes.append(
                f"fragile certainty: model={signal.model_probability:.2f} "
                f"mkt={signal.market_probability:.2f}"
            )

        if reason_notes:
            for note in reason_notes:
                log.info(f"[RISK] SIZE ADJUSTED: {note}")
            return {
                "decision": "REDUCE",
                "adjusted_signal": adjusted,
                "reason": "; ".join(reason_notes),
            }

        return {"decision": "APPROVE", "adjusted_signal": signal, "reason": "All risk checks passed"}

    def record_trade_result(self, position: Position) -> None:
        """Update risk state after a position closes."""
        if position.pnl is None:
            return

        self._analytics.update(position)

        # Daily PnL
        self._daily_pnl += position.pnl

        # Consecutive losses
        if position.pnl > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
                self._cooldown_until = time.time() + config.COOLDOWN_MINUTES * 60
                log.warning(
                    f"Cooldown activated: {config.MAX_CONSECUTIVE_LOSSES} consecutive losses. "
                    f"Pausing for {config.COOLDOWN_MINUTES} minutes"
                )

    def get_risk_status(self) -> dict:
        """Return current risk state for dashboard display.

        Drawdown uses total equity (same as evaluate_signal / main.py portfolio_state).
        """
        self._reset_daily_if_needed()
        equity = self._pm.get_total_equity()
        free_capital = self._pm.get_available_capital()
        drawdown = self._analytics.get_current_drawdown(equity)
        cooldown_remaining = max(0, self._cooldown_until - time.time())

        daily_limit_usd = self.daily_loss_limit_usd()
        daily_loss_pct = float(config.DAILY_LOSS_LIMIT_PCT)
        dd_limit = float(config.MAX_DRAWDOWN_PCT)
        exp_limit = float(config.MAX_TOTAL_EXPOSURE_PCT)
        exposure_pct = self._exposure.get_exposure_pct()
        hwm = self._analytics._high_water_mark
        drawdown_usd = max(0.0, hwm - equity)
        drawdown_max_loss_usd = dd_limit * hwm if hwm > 0 else 0.0

        daily_headroom_usdc = daily_limit_usd + self._daily_pnl
        dd_headroom_pct = max(0.0, dd_limit - drawdown)
        exp_headroom_pct = max(0.0, exp_limit - exposure_pct)

        dd_cooldown_remaining = max(0, self._drawdown_cooldown_until - time.time())

        blockers: list[str] = []
        if self._ks.is_active():
            blockers.append(f"Kill switch: {self._ks.trigger_reason or 'active'}")
        if self._daily_pnl <= -daily_limit_usd:
            blockers.append(
                f"Daily loss limit reached ({daily_loss_pct:.0%} of equity ≈ ${daily_limit_usd:.2f})"
            )
        if dd_cooldown_remaining > 0:
            blockers.append(f"Max drawdown cooldown (~{dd_cooldown_remaining:.0f}s remaining)")
        elif drawdown >= dd_limit:
            blockers.append("Max drawdown")
        if cooldown_remaining > 0:
            blockers.append(
                f"Loss cooldown (~{cooldown_remaining:.0f}s after {config.MAX_CONSECUTIVE_LOSSES} consecutive losses)"
            )

        trading_allowed = len(blockers) == 0

        return {
            "kill_switch": self._ks.get_status(),
            "drawdown_pct": drawdown,
            "drawdown_limit": dd_limit,
            "drawdown_usd": drawdown_usd,
            "drawdown_max_loss_usd": drawdown_max_loss_usd,
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit_pct": daily_loss_pct,
            "daily_limit": daily_limit_usd,
            "daily_limit_usd": daily_limit_usd,
            "session_start_equity": self._session_start_equity,
            "consecutive_losses": self._consecutive_losses,
            "max_consecutive": config.MAX_CONSECUTIVE_LOSSES,
            "cooldown_remaining_s": cooldown_remaining,
            "drawdown_cooldown_remaining_s": dd_cooldown_remaining,
            "exposure_pct": exposure_pct,
            "exposure_limit": exp_limit,
            "risk_rejections": self._risk_rejections,
            "hwm": self._analytics._high_water_mark,
            "total_equity": equity,
            "free_capital": free_capital,
            "trading_allowed": trading_allowed,
            "paper_warn_only": self._paper_warn_only(),
            "trading_blockers": blockers,
            "limits_headroom": {
                "daily_headroom_usdc": daily_headroom_usdc,
                "drawdown_headroom_pct": dd_headroom_pct,
                "exposure_headroom_pct": exp_headroom_pct,
            },
            "per_signal_note": (
                "Signals can still be rejected for latency, EV, volatility, or per-order exposure."
            ),
        }

    def _reject(self, reason: str) -> dict:
        self._risk_rejections += 1
        log.info(f"[RISK] REJECT: {reason}")
        return {"decision": "REJECT", "adjusted_signal": None, "reason": reason}

    def _reduce(self, signal: TradeSignal, new_size_pct: float, reason: str) -> dict:
        adjusted = copy(signal)
        adjusted.recommended_size_pct = new_size_pct
        adjusted.metadata = dict(signal.metadata)
        adjusted.metadata["risk_reduction"] = reason
        log.info(
            f"[RISK] REDUCE: {reason} "
            f"(size {signal.recommended_size_pct:.0%} → {new_size_pct:.0%})"
        )
        return {"decision": "REDUCE", "adjusted_signal": adjusted, "reason": reason}

    def _reset_daily_if_needed(self) -> None:
        """Reset daily PnL at configured UTC hour."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if today != self._daily_reset_date and now.hour >= config.DAILY_LOSS_RESET_HOUR_UTC:
            self._daily_pnl = 0.0
            self._daily_reset_date = today
