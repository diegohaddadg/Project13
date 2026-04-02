"""Performance analytics — comprehensive trading metrics and reporting."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from models.position import Position
from utils.logger import get_logger
import config

log = get_logger("analytics")


class PerformanceAnalytics:
    """Tracks realized trading performance and generates reports.

    Uses PositionManager._closed_positions as the single source of truth
    for all realized-trade metrics (PnL, win rate, profit factor, etc.).
    Does NOT maintain its own independent closed-position list.
    """

    def __init__(self, position_manager=None):
        self._session_start: float = time.time()
        self._pm = position_manager  # set after construction if needed
        self._high_water_mark: float = config.STARTING_CAPITAL_USDC
        self._max_drawdown_observed: float = 0.0

    def set_position_manager(self, pm) -> None:
        """Late-bind the PositionManager (for existing construction order)."""
        self._pm = pm

    @property
    def _closed_positions(self) -> list:
        """Single source of truth: delegate to PositionManager."""
        if self._pm is not None:
            return self._pm.get_closed_positions()
        return []

    def reset_hwm(self, equity: float) -> None:
        """Reset high-water mark to current TRUE equity at session start."""
        self._high_water_mark = equity
        self._max_drawdown_observed = 0.0

    def update(self, closed_position: Position) -> None:
        """No-op: closed positions are tracked by PositionManager.

        Kept for API compatibility — callers (RiskManager.record_trade_result)
        still call this, but the position is already in pm._closed_positions
        via pm.close_position().
        """
        pass

    def update_hwm(self, current_capital: float) -> None:
        """Update high-water mark and drawdown tracking.

        current_capital MUST be true equity (pm.get_total_equity()),
        NOT risk_equity (which can be inflated by PAPER_LIKE_RISK_MODE).
        """
        if current_capital > self._high_water_mark:
            self._high_water_mark = current_capital
        if self._high_water_mark > 0:
            dd = (self._high_water_mark - current_capital) / self._high_water_mark
            if dd > self._max_drawdown_observed:
                self._max_drawdown_observed = dd

    def get_current_drawdown(self, current_capital: float) -> float:
        """Current drawdown from high-water mark as a fraction.

        current_capital MUST be true equity.
        """
        if self._high_water_mark <= 0:
            return 0.0
        return max(0.0, (self._high_water_mark - current_capital) / self._high_water_mark)

    def get_strike_source_breakdown(self) -> dict:
        """Break down trades by strike source (confirmed vs approximate)."""
        confirmed = [p for p in self._closed_positions if p.pnl is not None
                     and p.metadata.get("strike_source") not in ("spot_approx_early",)]
        approx = [p for p in self._closed_positions if p.pnl is not None
                   and p.metadata.get("strike_source") == "spot_approx_early"]

        def _stats(positions):
            pnls = [p.pnl for p in positions if p.pnl is not None]
            wins = [x for x in pnls if x > 0]
            return {
                "count": len(pnls),
                "win_rate": len(wins) / len(pnls) if pnls else 0.0,
                "total_pnl": sum(pnls),
            }

        return {
            "confirmed": _stats(confirmed),
            "approx_fallback": _stats(approx),
        }

    def get_summary(self) -> dict:
        """Full performance summary."""
        pnls = [p.pnl for p in self._closed_positions if p.pnl is not None]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl <= 0]

        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        # Must be finite: json.dumps(..., allow_nan=False) / browser JSON.parse reject Infinity.
        if gross_losses > 0:
            profit_factor = gross_wins / gross_losses
        elif gross_wins > 0:
            profit_factor = 1e6  # cap: "no losing trades yet" (was inf → WS JSON.parse crash)
        else:
            profit_factor = 0.0

        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        std_pnl = 0.0
        if len(pnls) > 1:
            mean = avg_pnl
            std_pnl = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1))
        sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0.0

        durations = [p.hold_duration_seconds() for p in self._closed_positions]
        avg_hold = sum(durations) / len(durations) if durations else 0.0

        return {
            "total_trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(pnls) if pnls else 0.0,
            "total_pnl": sum(pnls),
            "avg_pnl": avg_pnl,
            "gross_wins": gross_wins,
            "gross_losses": gross_losses,
            "profit_factor": profit_factor,
            "pnl_std": std_pnl,
            "sharpe_ratio": sharpe,
            "best_trade": max(pnls) if pnls else 0.0,
            "worst_trade": min(pnls) if pnls else 0.0,
            "max_drawdown": self._max_drawdown_observed,
            "high_water_mark": self._high_water_mark,
            "avg_hold_seconds": avg_hold,
            "session_duration_minutes": (time.time() - self._session_start) / 60,
        }

    def get_strategy_breakdown(self) -> dict:
        """PnL and win rate by strategy."""
        by_strat: dict[str, list[float]] = {}
        for p in self._closed_positions:
            if p.pnl is None:
                continue
            # Strategy info is in order metadata, but we only have position
            # Use market_type as a proxy grouping
            strat = p.metadata.get("strategy", p.market_type)
            by_strat.setdefault(strat, []).append(p.pnl)

        result = {}
        for strat, pnls in by_strat.items():
            wins = sum(1 for p in pnls if p > 0)
            result[strat] = {
                "trades": len(pnls),
                "total_pnl": sum(pnls),
                "win_rate": wins / len(pnls) if pnls else 0.0,
            }
        return result

    def generate_report(self, current_capital: float) -> str:
        """Generate a human-readable performance report."""
        s = self.get_summary()
        lines = [
            "=" * 60,
            "  PROJECT13 — PERFORMANCE REPORT",
            "=" * 60,
            f"  Timestamp:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"  Session:      {s['session_duration_minutes']:.1f} minutes",
            f"  Capital:      ${current_capital:.2f}",
            "",
            "  TRADES",
            f"    Total:        {s['total_trades']}",
            f"    Wins:         {s['wins']}",
            f"    Losses:       {s['losses']}",
            f"    Win Rate:     {s['win_rate']:.1%}",
            "",
            "  PnL",
            f"    Total:        ${s['total_pnl']:+.2f}",
            f"    Average:      ${s['avg_pnl']:+.2f}",
            f"    Best Trade:   ${s['best_trade']:+.2f}",
            f"    Worst Trade:  ${s['worst_trade']:+.2f}",
            f"    Gross Wins:   ${s['gross_wins']:.2f}",
            f"    Gross Losses: ${s['gross_losses']:.2f}",
            f"    Profit Factor:{s['profit_factor']:.2f}",
            "",
            "  RISK",
            f"    Sharpe:       {s['sharpe_ratio']:.2f}",
            f"    PnL StdDev:   ${s['pnl_std']:.2f}",
            f"    Max Drawdown: {s['max_drawdown']:.1%}",
            f"    HWM:          ${s['high_water_mark']:.2f}",
            "",
            f"  Avg Hold:       {s['avg_hold_seconds']:.0f}s",
            "=" * 60,
        ]
        return "\n".join(lines)

    def save_report(self, current_capital: float) -> None:
        """Save report with timestamp to avoid overwriting previous reports.

        Saves to: logs/performance_report_YYYYMMDD_HHMMSS.txt
        Also writes logs/performance_report_latest.txt for easy access.
        """
        try:
            report = self.generate_report(current_capital)
            base = Path(config.REPORT_OUTPUT_PATH)
            base.parent.mkdir(parents=True, exist_ok=True)

            # Timestamped report
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            stem = base.stem
            timestamped = base.parent / f"{stem}_{ts}{base.suffix}"
            timestamped.write_text(report)

            # Latest symlink/copy for convenience
            latest = base.parent / f"{stem}_latest{base.suffix}"
            latest.write_text(report)

            log.info(f"Performance report saved to {timestamped}")
        except Exception as e:
            log.error(f"Failed to save performance report: {e}")
