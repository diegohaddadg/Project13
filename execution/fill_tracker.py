"""Fill tracker — resolves paper positions from real Polymarket market outcomes.

Paper positions are settled using the actual Polymarket market resolution (UP or
DOWN winner) via the Gamma API, NOT from local spot-vs-strike approximation.
Positions remain open until the real market resolves.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from models.position import Position

if TYPE_CHECKING:
    from execution.order_manager import OrderManager
from models.market_state import MarketState
from execution.position_manager import PositionManager
from feeds.aggregator import Aggregator
from utils.logger import get_logger
import config

log = get_logger("fill_tracker")

# Maximum time to wait for resolution before force-closing as a loss.
# Guards against positions stuck forever if Gamma API never returns resolution.
RESOLUTION_SAFETY_TIMEOUT_S = 1200  # 20 minutes


class FillTracker:
    """Monitors open positions and closes them when the real market resolves."""

    def __init__(
        self,
        position_manager: PositionManager,
        aggregator: Aggregator,
        order_manager: Optional["OrderManager"] = None,
    ):
        self._pm = position_manager
        self._agg = aggregator
        self._om = order_manager

    def check_resolutions(
        self,
        market_state_5m: Optional[MarketState],
        market_state_15m: Optional[MarketState],
    ) -> list[Position]:
        """Check if any open positions' real markets have resolved."""
        closed = []
        for pos in list(self._pm.get_open_positions()):
            if pos.market_type not in ("btc-5min", "btc-15min"):
                continue

            resolution = self._check_real_market_resolution(pos)
            if resolution is not None:
                resolved_pos = self._pm.close_position(pos.position_id, resolution)
                if resolved_pos:
                    closed.append(resolved_pos)
                    self._log_resolution(resolved_pos)
                    if (
                        self._om
                        and resolved_pos.order_id
                        and resolved_pos.pnl is not None
                    ):
                        self._om.sync_order_pnl_from_position(
                            resolved_pos.order_id, resolved_pos.pnl
                        )

        return closed

    # --- Real market resolution ---

    def _check_real_market_resolution(self, pos: Position) -> Optional[float]:
        """Query Polymarket for real market outcome.

        Returns:
            1.0 if position's side won
            0.0 if position's side lost
            None if market not yet resolved (position stays open)
        """
        market_id = pos.market_id
        condition_id = pos.metadata.get("condition_id", "")
        token_id = pos.metadata.get("token_id", "")

        if not market_id:
            log.warning(
                f"[PAPER_RESOLVE] skip reason=missing_market_id "
                f"pos={pos.position_id}"
            )
            return None

        # Safety timeout: if held far too long, close as loss with warning
        hold_time = pos.hold_duration_seconds()
        if hold_time > RESOLUTION_SAFETY_TIMEOUT_S:
            log.warning(
                f"[PAPER_RESOLVE] safety_timeout pos={pos.position_id} "
                f"market_id={market_id} held={hold_time:.0f}s "
                f"max={RESOLUTION_SAFETY_TIMEOUT_S}s — closing as LOSS"
            )
            return 0.0

        # Query the Gamma API for market resolution
        resolved_info = self._query_market_resolution(market_id, condition_id)

        if resolved_info is None:
            # API failure — don't resolve, will retry next cycle
            return None

        if not resolved_info["resolved"]:
            # Market not yet resolved — position stays open
            return None

        winning_token = resolved_info.get("winning_token_id", "")

        if not winning_token:
            log.warning(
                f"[PAPER_RESOLVE] warning=resolved_but_no_winner "
                f"market_id={market_id} pos={pos.position_id} "
                f"— cannot determine winner, skipping resolution"
            )
            return None

        # Determine win/loss from real market outcome
        if token_id and winning_token:
            won = (token_id == winning_token)
        else:
            # Fallback: match direction to winning side via clobTokenIds mapping
            won = self._match_direction_to_winner(
                pos.direction, winning_token, resolved_info
            )

        resolution_price = 1.0 if won else 0.0
        resolved_side = resolved_info.get("resolved_direction", "UNKNOWN")

        log.warning(
            f"[PAPER_RESOLVE] source=real_market "
            f"market_id={market_id} "
            f"resolved_side={resolved_side} "
            f"bought_side={pos.direction} "
            f"win={won} "
            f"pnl={(resolution_price - pos.entry_price) * pos.num_shares:+.2f} "
            f"pos={pos.position_id} "
            f"token_match={'token_id' if token_id else 'direction'}"
        )

        return resolution_price

    def _query_market_resolution(
        self, market_id: str, condition_id: str
    ) -> Optional[dict]:
        """Query Gamma API for market resolution status.

        Returns dict with keys: resolved, winning_token_id, resolved_direction
        or None on API failure.
        """
        try:
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Project13/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log.info(
                f"[PAPER_RESOLVE] api_error market_id={market_id} error={e}"
            )
            return None

        if not isinstance(data, dict):
            return None

        # Parse resolved status
        raw_closed = data.get("closed")
        raw_active = data.get("active")
        raw_end = data.get("endDate", "")

        resolved = _to_bool(raw_closed) or _to_bool(data.get("resolved"))

        # Heuristic: active=false + endDate in the past → resolved
        if not resolved and not _to_bool(raw_active) and raw_end:
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
                if end_dt < datetime.now(timezone.utc):
                    resolved = True
            except Exception:
                pass

        if not resolved:
            return {"resolved": False, "winning_token_id": "", "resolved_direction": ""}

        # Determine winning token
        winning_token_id = ""
        resolved_direction = ""

        # Method 1: tokens list with winner field
        tokens = data.get("tokens", [])
        for t in tokens:
            if isinstance(t, dict) and _safe_float(t.get("winner", 0)) == 1.0:
                winning_token_id = t.get("token_id", "")
                break

        # Method 2: outcomePrices + clobTokenIds
        if not winning_token_id:
            winning_token_id = _extract_winner_from_gamma(data)

        # Determine resolved direction from clobTokenIds mapping
        # clobTokenIds[0] = UP token, clobTokenIds[1] = DOWN token
        if winning_token_id:
            clob_ids = data.get("clobTokenIds")
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            if isinstance(clob_ids, list) and len(clob_ids) >= 2:
                if str(clob_ids[0]).strip() == winning_token_id:
                    resolved_direction = "UP"
                elif str(clob_ids[1]).strip() == winning_token_id:
                    resolved_direction = "DOWN"

        return {
            "resolved": True,
            "winning_token_id": winning_token_id,
            "resolved_direction": resolved_direction,
        }

    def _match_direction_to_winner(
        self, direction: str, winning_token: str, info: dict
    ) -> bool:
        """Fallback: match position direction to winner when token_id not available."""
        resolved_dir = info.get("resolved_direction", "")
        if resolved_dir and direction:
            return direction == resolved_dir
        # Cannot determine — conservative: treat as loss
        log.warning(
            f"[PAPER_RESOLVE] warning=cannot_match_direction_to_winner "
            f"direction={direction} resolved_direction={resolved_dir}"
        )
        return False

    # --- Logging ---

    def _log_resolution(self, pos: Position) -> None:
        """Log resolved position to audit file."""
        try:
            entry = {
                "timestamp": time.time(),
                "position_id": pos.position_id,
                "market_type": pos.market_type,
                "market_id": pos.market_id,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "num_shares": pos.num_shares,
                "pnl": pos.pnl,
                "resolution_price": pos.resolution_price,
                "hold_seconds": pos.hold_duration_seconds(),
                "status": pos.status,
                "source": "real_market",
                "strike_source": pos.metadata.get("strike_source", ""),
                "strategy": pos.metadata.get("strategy", ""),
            }
            path = Path("logs/execution_consistency_audit.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(f"RESOLVED: {json.dumps(entry)}\n")
        except Exception:
            pass


# --- Helpers (shared logic with live_reconciler) ---


def _safe_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val) if val is not None else False


def _extract_winner_from_gamma(resp: dict) -> str:
    """Extract winning token from Gamma API using clobTokenIds + outcomePrices."""
    clob_token_ids = resp.get("clobTokenIds")
    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except Exception:
            clob_token_ids = []
    if not isinstance(clob_token_ids, list) or len(clob_token_ids) < 2:
        return ""

    outcome_prices = resp.get("outcomePrices")
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = []
    if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
        try:
            p0 = float(outcome_prices[0])
            p1 = float(outcome_prices[1])
            if p0 > 0.9:
                return str(clob_token_ids[0]).strip()
            elif p1 > 0.9:
                return str(clob_token_ids[1]).strip()
        except (ValueError, TypeError):
            pass

    return ""
