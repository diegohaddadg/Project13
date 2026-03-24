"""Kill switch — emergency stop system.

Triggers halt all new trading immediately. Recovery requires manual reset
after system health is verified.

Every activation is logged with full context. Automatic recovery is NOT allowed.
"""

from __future__ import annotations

import time

from utils.logger import get_logger
import config

log = get_logger("kill_switch")


class KillSwitch:
    """Emergency trading halt with manual-only recovery."""

    def __init__(self):
        self._active: bool = config.KILL_SWITCH_ACTIVE
        self._trigger_reason: str = ""
        self._trigger_time: float = 0.0

        if self._active:
            self._trigger_reason = "Activated via config on startup"
            self._trigger_time = time.time()
            log.warning("Kill switch ACTIVE on startup (config.KILL_SWITCH_ACTIVE=True)")

    def is_active(self) -> bool:
        return self._active

    @property
    def trigger_reason(self) -> str:
        return self._trigger_reason

    @property
    def trigger_time(self) -> float:
        return self._trigger_time

    def activate(self, reason: str) -> None:
        """Activate the kill switch. Stops all new trading immediately."""
        if self._active:
            return  # Already active
        self._active = True
        self._trigger_reason = reason
        self._trigger_time = time.time()
        log.error(f"KILL SWITCH ACTIVATED: {reason}")

    def deactivate(self) -> None:
        """Manually deactivate the kill switch.

        This should only be called after verifying system health.
        Trading will not automatically resume — other checks still apply.
        """
        if not self._active:
            return
        self._active = False
        log.warning(
            f"Kill switch DEACTIVATED (was active for "
            f"{time.time() - self._trigger_time:.0f}s, reason: {self._trigger_reason})"
        )
        self._trigger_reason = ""
        self._trigger_time = 0.0

    def check_triggers(
        self,
        drawdown_breached: bool = False,
        daily_limit_hit: bool = False,
        feeds_healthy: bool = True,
        polymarket_healthy: bool = True,
        latency_ok: bool = True,
    ) -> bool:
        """Check all kill switch triggers. Activates if any fire.

        Returns True if kill switch is now active.
        """
        if drawdown_breached:
            self.activate("Max drawdown threshold breached")
        elif daily_limit_hit:
            self.activate("Daily loss limit reached")
        elif not feeds_healthy:
            self.activate("All spot price feeds are unhealthy/stale")
        elif not polymarket_healthy:
            self.activate("Polymarket API unreachable for extended period")
        elif not latency_ok:
            self.activate("Abnormal feed latency persisting beyond threshold")

        return self._active

    def get_status(self) -> dict:
        return {
            "active": self._active,
            "reason": self._trigger_reason,
            "trigger_time": self._trigger_time,
            "duration_seconds": time.time() - self._trigger_time if self._active else 0,
        }
