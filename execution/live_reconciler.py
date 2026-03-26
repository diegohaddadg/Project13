"""Live exchange reconciliation — syncs Polymarket CLOB truth into local state.

Responsibilities:
- Poll exchange for order status (LIVE → FILLED / CANCELLED)
- Create local positions from confirmed fills
- Detect resolved winning positions eligible for redemption
- Execute auto-redemption of winning CTF tokens
- Log every reconciliation step clearly

EXECUTION_MODE=live only. Paper mode is never touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from models.order import Order
from models.position import Position
from execution.position_manager import PositionManager
from utils.logger import get_logger
import config

if TYPE_CHECKING:
    from execution.order_manager import OrderManager

log = get_logger("live_recon")

# CLOB order statuses
_CLOB_FILLED = "MATCHED"
_CLOB_LIVE = "LIVE"
_CLOB_CANCELLED = "CANCELLED"

_RECON_LOG_PATH = "logs/live_reconciliation.jsonl"


class LiveReconciler:
    """Reconciles local state against Polymarket exchange truth.

    Only active when EXECUTION_MODE == "live" and LIVE_RECONCILIATION_ENABLED.
    """

    def __init__(
        self,
        clob_client,
        position_manager: PositionManager,
        order_manager: "OrderManager",
    ):
        self._client = clob_client
        self._pm = position_manager
        self._om = order_manager

        # Tracking
        self._last_reconcile_ts: float = 0.0
        self._reconcile_count: int = 0
        self._fills_detected: int = 0
        self._cancels_detected: int = 0
        self._errors: int = 0
        self._last_error: str = ""
        self._stale: bool = True  # True until first successful reconciliation

        # Redemption tracking
        self._pending_redemptions: dict[str, dict] = {}  # position_id -> info
        self._redeemed_count: int = 0
        self._redeem_failures: int = 0
        self._redeem_cooldowns: dict[str, float] = {}  # position_id -> next_retry_ts

    # ------------------------------------------------------------------
    # Main reconciliation entry point
    # ------------------------------------------------------------------

    def reconcile(self) -> dict:
        """Run one reconciliation cycle. Returns summary dict."""
        if config.EXECUTION_MODE != "live":
            return {"skipped": True, "reason": "not_live_mode"}
        if not config.LIVE_RECONCILIATION_ENABLED:
            return {"skipped": True, "reason": "reconciliation_disabled"}
        if self._client is None:
            return {"skipped": True, "reason": "no_clob_client"}

        self._reconcile_count += 1
        summary = {
            "cycle": self._reconcile_count,
            "ts": time.time(),
            "orders_checked": 0,
            "fills_this_cycle": 0,
            "cancels_this_cycle": 0,
            "errors_this_cycle": 0,
            "redemptions_this_cycle": 0,
        }

        try:
            # Phase 1: Reconcile LIVE orders → detect fills / cancels
            self._reconcile_live_orders(summary)

            # Phase 2: Detect and execute redemptions
            if config.LIVE_AUTO_REDEEM_ENABLED:
                self._check_and_redeem(summary)

            self._stale = False
            self._last_reconcile_ts = time.time()

        except Exception as e:
            self._errors += 1
            self._last_error = str(e)
            summary["errors_this_cycle"] += 1
            log.error(f"[RECON] Reconciliation cycle {self._reconcile_count} failed: {e}")

        self._log_reconciliation(summary)
        return summary

    # ------------------------------------------------------------------
    # Phase 1: Order fill reconciliation
    # ------------------------------------------------------------------

    def _reconcile_live_orders(self, summary: dict) -> None:
        """Check all LIVE orders against exchange and update local state."""
        live_orders = [
            o for o in self._om.get_order_history()
            if o.status == "LIVE" and o.execution_mode == "live"
        ]

        if not live_orders:
            return

        for order in live_orders:
            exchange_id = order.metadata.get("exchange_order_id", "")
            if not exchange_id:
                continue

            summary["orders_checked"] += 1

            try:
                exchange_data = self._client.get_order(exchange_id)
            except Exception as e:
                self._errors += 1
                self._last_error = f"get_order({exchange_id[:16]}): {e}"
                summary["errors_this_cycle"] += 1
                log.warning(f"[RECON] Failed to fetch order {exchange_id[:16]}: {e}")
                continue

            if exchange_data is None:
                continue

            self._process_order_update(order, exchange_data, summary)

    def _process_order_update(self, order: Order, exchange_data: dict, summary: dict) -> None:
        """Process an exchange order response and update local state."""
        exchange_id = order.metadata.get("exchange_order_id", "")

        # Parse exchange status
        if isinstance(exchange_data, dict):
            clob_status = exchange_data.get("status", "")
            size_matched = _safe_float(exchange_data.get("size_matched", 0))
            original_size = _safe_float(exchange_data.get("original_size", order.num_shares))
            avg_price = _safe_float(exchange_data.get("associate_trades_avg_price",
                        exchange_data.get("price", order.price)))
        else:
            return

        if clob_status == _CLOB_FILLED:
            self._handle_fill(order, exchange_data, size_matched, avg_price, summary)
        elif clob_status == _CLOB_CANCELLED:
            self._handle_cancel(order, exchange_data, summary)
        elif clob_status == _CLOB_LIVE and size_matched > 0:
            # Partial fill — order still on book but some shares matched
            self._handle_partial_fill(order, exchange_data, size_matched, original_size, avg_price, summary)

    def _handle_fill(self, order: Order, exchange_data: dict, size_matched: float,
                     avg_price: float, summary: dict) -> None:
        """Handle a fully filled order from exchange."""
        exchange_id = order.metadata.get("exchange_order_id", "")

        fill_price = avg_price if avg_price > 0 else order.price
        fill_shares = size_matched if size_matched > 0 else order.num_shares

        order.status = "FILLED"
        order.fill_price = fill_price
        order.fill_timestamp = time.time()
        order.num_shares = fill_shares
        order.size_usdc = fill_price * fill_shares
        order.metadata["recon_fill_ts"] = time.time()
        order.metadata["recon_exchange_data"] = _truncate_dict(exchange_data)

        # Create local position
        pos = self._pm.open_position(order)

        # Store exchange info on position for redemption tracking
        if pos:
            pos.metadata["exchange_order_id"] = exchange_id
            pos.metadata["token_id"] = order.token_id
            pos.metadata["condition_id"] = order.metadata.get("condition_id", "")

        # Persist to trade log
        self._om._append_trade_log(order)
        self._om._log_lifecycle(order, pos)

        self._fills_detected += 1
        summary["fills_this_cycle"] += 1

        log.warning(
            f"[RECON] FILL DETECTED: {order.direction} {order.market_type} "
            f"${order.size_usdc:.2f} @{fill_price:.3f} ({fill_shares:.1f}sh) "
            f"exchange_id={exchange_id[:24]}"
        )

    def _handle_partial_fill(self, order: Order, exchange_data: dict,
                             size_matched: float, original_size: float,
                             avg_price: float, summary: dict) -> None:
        """Handle a partially filled order — log but wait for full fill."""
        pct = (size_matched / original_size * 100) if original_size > 0 else 0
        order.metadata["partial_fill_size"] = size_matched
        order.metadata["partial_fill_pct"] = round(pct, 1)
        order.metadata["partial_fill_price"] = avg_price

        log.info(
            f"[RECON] PARTIAL FILL: {order.direction} {order.market_type} "
            f"{size_matched:.1f}/{original_size:.1f} shares ({pct:.0f}%) "
            f"@{avg_price:.3f}"
        )

    def _handle_cancel(self, order: Order, exchange_data: dict, summary: dict) -> None:
        """Handle a cancelled order from exchange."""
        exchange_id = order.metadata.get("exchange_order_id", "")

        order.status = "CANCELLED"
        order.metadata["recon_cancel_ts"] = time.time()
        order.metadata["recon_exchange_data"] = _truncate_dict(exchange_data)

        # Persist
        self._om._append_trade_log(order)

        self._cancels_detected += 1
        summary["cancels_this_cycle"] += 1

        log.warning(
            f"[RECON] CANCEL DETECTED: {order.direction} {order.market_type} "
            f"${order.size_usdc:.2f} exchange_id={exchange_id[:24]}"
        )

    # ------------------------------------------------------------------
    # Phase 2: Redemption
    # ------------------------------------------------------------------

    def _check_and_redeem(self, summary: dict) -> None:
        """Check for resolved winning positions and attempt redemption."""
        for pos in list(self._pm.get_open_positions()):
            if pos.metadata.get("execution_mode") != "live":
                continue
            if pos.metadata.get("redeemed"):
                continue

            condition_id = pos.metadata.get("condition_id", "")
            if not condition_id:
                continue

            # Check cooldown
            cooldown_until = self._redeem_cooldowns.get(pos.position_id, 0)
            if time.time() < cooldown_until:
                continue

            # Check if market is resolved on exchange
            resolved_info = self._check_market_resolved(condition_id)
            if resolved_info is None:
                continue

            is_resolved = resolved_info.get("resolved", False)
            if not is_resolved:
                continue

            # Determine if this position won
            winning_token = resolved_info.get("winning_token_id", "")
            pos_token = pos.metadata.get("token_id", "")

            if not winning_token or not pos_token:
                continue

            won = (pos_token == winning_token)

            if won:
                self._attempt_redemption(pos, condition_id, summary)
            else:
                # Loss — close position locally with resolution_price=0
                self._close_losing_position(pos, summary)

    def _check_market_resolved(self, condition_id: str) -> Optional[dict]:
        """Check if a market has resolved via the CLOB API."""
        try:
            # Try CLOB /markets endpoint with condition_id
            resp = self._client.get_market(condition_id=condition_id)
            if resp is None:
                return None

            if isinstance(resp, dict):
                resolved = resp.get("closed", False) or resp.get("resolved", False)
                # Determine winning token from resolution data
                winning_token_id = ""
                if resolved:
                    tokens = resp.get("tokens", [])
                    for t in tokens:
                        if isinstance(t, dict) and _safe_float(t.get("winner", 0)) == 1.0:
                            winning_token_id = t.get("token_id", "")
                            break
                return {
                    "resolved": resolved,
                    "winning_token_id": winning_token_id,
                    "raw": _truncate_dict(resp),
                }
            return None
        except Exception as e:
            log.debug(f"[RECON] Market resolution check failed for {condition_id[:16]}: {e}")
            return None

    def _attempt_redemption(self, pos: Position, condition_id: str, summary: dict) -> None:
        """Attempt to redeem a winning resolved position."""
        retry_count = pos.metadata.get("redeem_retry_count", 0)
        if retry_count >= config.LIVE_REDEEM_MAX_RETRIES:
            log.warning(
                f"[RECON] REDEEM MAX RETRIES for {pos.position_id}: "
                f"tried {retry_count} times — giving up"
            )
            return

        log.warning(
            f"[RECON] REDEEMING: {pos.direction} {pos.market_type} "
            f"{pos.num_shares:.1f}sh @{pos.entry_price:.3f} "
            f"condition={condition_id[:16]}..."
        )

        try:
            # Use py-clob-client's redeem method if available,
            # otherwise fall back to direct contract call
            success = self._execute_redemption(condition_id, pos)

            if success:
                # Close position locally as a win
                resolved_pos = self._pm.close_position(pos.position_id, 1.0)
                if resolved_pos:
                    resolved_pos.metadata["redeemed"] = True
                    resolved_pos.metadata["redeem_ts"] = time.time()

                    # Sync PnL to order
                    if resolved_pos.order_id and resolved_pos.pnl is not None:
                        self._om.sync_order_pnl_from_position(
                            resolved_pos.order_id, resolved_pos.pnl
                        )

                self._redeemed_count += 1
                summary["redemptions_this_cycle"] += 1

                log.warning(
                    f"[RECON] REDEEMED: {pos.direction} {pos.market_type} "
                    f"PnL={resolved_pos.pnl:+.2f}" if resolved_pos and resolved_pos.pnl is not None
                    else f"[RECON] REDEEMED: {pos.direction} {pos.market_type}"
                )
            else:
                self._schedule_retry(pos)

        except Exception as e:
            self._redeem_failures += 1
            self._last_error = f"redemption failed: {e}"
            self._schedule_retry(pos)
            log.error(f"[RECON] REDEEM FAILED: {pos.position_id} — {e}")

    def _execute_redemption(self, condition_id: str, pos: Position) -> bool:
        """Execute the actual redemption call.

        Tries py-clob-client methods first. If those don't exist or fail,
        logs the situation clearly and returns False.
        """
        # Attempt 1: Try the CLOB client's redeem method (if it exists in newer versions)
        if hasattr(self._client, 'redeem'):
            try:
                resp = self._client.redeem(condition_id=condition_id)
                if resp:
                    log.info(f"[RECON] Redemption via client.redeem() succeeded: {_truncate_dict(resp)}")
                    return True
            except Exception as e:
                log.warning(f"[RECON] client.redeem() failed: {e}")

        # Attempt 2: Try merge_positions (some client versions)
        if hasattr(self._client, 'merge_positions'):
            try:
                token_id = pos.metadata.get("token_id", "")
                resp = self._client.merge_positions(
                    condition_id=condition_id,
                    token_id=token_id,
                )
                if resp:
                    log.info(f"[RECON] Redemption via merge_positions() succeeded")
                    return True
            except Exception as e:
                log.warning(f"[RECON] merge_positions() failed: {e}")

        # Attempt 3: Direct CLOB API call to redeem endpoint
        try:
            # POST /redeem with condition_id
            if hasattr(self._client, '_post'):
                resp = self._client._post(
                    f"{self._client.host}/redeem",
                    json={"conditionId": condition_id}
                )
                if resp and isinstance(resp, dict) and resp.get("success"):
                    log.info(f"[RECON] Redemption via /redeem endpoint succeeded")
                    return True
        except Exception as e:
            log.warning(f"[RECON] /redeem endpoint failed: {e}")

        log.warning(
            f"[RECON] All redemption methods failed for {condition_id[:16]}. "
            f"Manual redemption may be required via Polymarket UI."
        )
        return False

    def _close_losing_position(self, pos: Position, summary: dict) -> None:
        """Close a losing resolved position locally."""
        resolved_pos = self._pm.close_position(pos.position_id, 0.0)
        if resolved_pos:
            resolved_pos.metadata["recon_loss_detected"] = True
            if resolved_pos.order_id and resolved_pos.pnl is not None:
                self._om.sync_order_pnl_from_position(
                    resolved_pos.order_id, resolved_pos.pnl
                )
            log.warning(
                f"[RECON] LOSS DETECTED: {pos.direction} {pos.market_type} "
                f"PnL={resolved_pos.pnl:+.2f}"
            )

    def _schedule_retry(self, pos: Position) -> None:
        """Schedule a redemption retry with exponential backoff."""
        retry_count = pos.metadata.get("redeem_retry_count", 0) + 1
        pos.metadata["redeem_retry_count"] = retry_count
        backoff = config.LIVE_REDEEM_RETRY_BACKOFF_SECONDS * (2 ** min(retry_count - 1, 4))
        self._redeem_cooldowns[pos.position_id] = time.time() + backoff
        self._redeem_failures += 1
        log.info(f"[RECON] Retry #{retry_count} for {pos.position_id} in {backoff:.0f}s")

    # ------------------------------------------------------------------
    # Startup sync
    # ------------------------------------------------------------------

    def startup_sync(self) -> dict:
        """Query exchange at startup and log current state.

        Does NOT overwrite local state — just reports what the exchange sees.
        """
        if self._client is None:
            return {"error": "no_clob_client"}

        result = {
            "ts": time.time(),
            "open_orders": 0,
            "filled_untracked": 0,
            "errors": [],
        }

        log.warning("[RECON] ===== STARTUP SYNC =====")

        # Check local LIVE orders against exchange
        live_orders = [
            o for o in self._om.get_order_history()
            if o.status == "LIVE" and o.execution_mode == "live"
        ]
        log.warning(f"[RECON] Local LIVE orders pending reconciliation: {len(live_orders)}")

        for order in live_orders:
            exchange_id = order.metadata.get("exchange_order_id", "")
            if not exchange_id:
                continue
            try:
                data = self._client.get_order(exchange_id)
                if data and isinstance(data, dict):
                    status = data.get("status", "UNKNOWN")
                    matched = _safe_float(data.get("size_matched", 0))
                    log.warning(
                        f"[RECON]   {order.order_id}: exchange_status={status} "
                        f"matched={matched:.1f}/{order.num_shares:.1f}"
                    )
                    if status == _CLOB_FILLED:
                        result["filled_untracked"] += 1
            except Exception as e:
                result["errors"].append(str(e))
                log.warning(f"[RECON]   {order.order_id}: fetch failed — {e}")

        # Log position state
        open_pos = self._pm.get_open_positions()
        live_positions = [p for p in open_pos if p.metadata.get("execution_mode") == "live"]
        log.warning(f"[RECON] Open positions (live): {len(live_positions)}")
        log.warning(f"[RECON] Open positions (total): {len(open_pos)}")
        log.warning(f"[RECON] Available capital: ${self._pm.get_available_capital():.2f}")
        log.warning(f"[RECON] Total equity: ${self._pm.get_total_equity():.2f}")
        log.warning(f"[RECON] Realized PnL: ${self._pm.get_total_pnl():.2f}")

        # Run immediate reconciliation of any pending LIVE orders
        if live_orders:
            log.warning("[RECON] Running immediate reconciliation of pending orders...")
            self.reconcile()

        log.warning("[RECON] ===== STARTUP SYNC COMPLETE =====")
        return result

    # ------------------------------------------------------------------
    # Live exposure query (used by live entry gate)
    # ------------------------------------------------------------------

    def get_live_market_exposure(self, market_id: str) -> dict:
        """Count all live exposure for a market window using exchange-backed state.

        Counts both:
        - LIVE orders (accepted, pending fill)
        - FILLED positions (open, not yet resolved)

        Returns dict with entry counts and side distribution.
        """
        # Count LIVE (pending) orders for this market
        live_orders_for_market = []
        for o in self._om.get_order_history():
            if (o.status == "LIVE"
                    and o.execution_mode == "live"
                    and o.market_id == market_id):
                live_orders_for_market.append(o)

        # Count open FILLED positions for this market
        open_positions_for_market = []
        for p in self._pm.get_open_positions():
            if (p.market_id == market_id
                    and p.metadata.get("execution_mode") == "live"):
                open_positions_for_market.append(p)

        total_entries = len(live_orders_for_market) + len(open_positions_for_market)

        # Side distribution
        up_count = (
            sum(1 for o in live_orders_for_market if o.direction == "UP")
            + sum(1 for p in open_positions_for_market if p.direction == "UP")
        )
        down_count = (
            sum(1 for o in live_orders_for_market if o.direction == "DOWN")
            + sum(1 for p in open_positions_for_market if p.direction == "DOWN")
        )

        # Deployed capital
        pending_capital = sum(o.size_usdc for o in live_orders_for_market)
        deployed_capital = sum(
            p.entry_price * p.num_shares for p in open_positions_for_market
        )

        # Directions with active exposure
        active_directions = set()
        for o in live_orders_for_market:
            active_directions.add(o.direction)
        for p in open_positions_for_market:
            active_directions.add(p.direction)

        return {
            "market_id": market_id,
            "total_entries": total_entries,
            "pending_orders": len(live_orders_for_market),
            "open_positions": len(open_positions_for_market),
            "up_count": up_count,
            "down_count": down_count,
            "pending_capital": pending_capital,
            "deployed_capital": deployed_capital,
            "total_capital": pending_capital + deployed_capital,
            "active_directions": active_directions,
            "stale": self._stale,
            "last_reconcile_age_s": (
                round(time.time() - self._last_reconcile_ts, 1)
                if self._last_reconcile_ts > 0 else None
            ),
        }

    # ------------------------------------------------------------------
    # Status / dashboard
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return reconciliation status for dashboard."""
        return {
            "enabled": config.LIVE_RECONCILIATION_ENABLED,
            "auto_redeem_enabled": config.LIVE_AUTO_REDEEM_ENABLED,
            "reconcile_count": self._reconcile_count,
            "last_reconcile_ts": self._last_reconcile_ts,
            "last_reconcile_age_s": round(time.time() - self._last_reconcile_ts, 1) if self._last_reconcile_ts > 0 else None,
            "stale": self._stale,
            "fills_detected": self._fills_detected,
            "cancels_detected": self._cancels_detected,
            "redeemed_count": self._redeemed_count,
            "redeem_failures": self._redeem_failures,
            "pending_redemptions": len(self._pending_redemptions),
            "errors": self._errors,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_reconciliation(self, summary: dict) -> None:
        """Append reconciliation cycle to JSONL log."""
        try:
            path = Path(_RECON_LOG_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(summary, default=str) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val) -> float:
    """Safely convert a value to float."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _truncate_dict(d: dict, max_keys: int = 10) -> dict:
    """Truncate a dict for safe logging."""
    if not isinstance(d, dict):
        return {}
    out = {}
    for i, (k, v) in enumerate(d.items()):
        if i >= max_keys:
            out["_truncated"] = True
            break
        if isinstance(v, str) and len(v) > 100:
            out[k] = v[:100] + "..."
        else:
            out[k] = v
    return out
