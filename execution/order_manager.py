"""Order manager — central execution coordinator.

Receives TradeSignals, validates them, constructs Orders, routes to paper
or live execution, tracks lifecycle, and persists trade history.

Token direction mapping (BTC up/down markets):
- Signal direction "UP" → buy the Up token (MarketState.up_token_id)
- Signal direction "DOWN" → buy the Down token (MarketState.down_token_id)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from models.trade_signal import TradeSignal
from models.order import Order
from models.market_state import MarketState
from execution.paper_trader import PaperTrader
from execution.live_trader import LiveTrader
from execution.position_manager import PositionManager
from utils.logger import get_logger
import config

log = get_logger("order_mgr")


class OrderManager:
    """Central execution coordinator."""

    def __init__(self, position_manager: PositionManager):
        self._pm = position_manager
        self._paper_trader = PaperTrader()
        self._live_trader = LiveTrader()
        self._order_history: list[Order] = []
        self._rejected_count: int = 0
        self._rejection_reasons: dict[str, int] = {}  # category -> count
        self._recent_rejections: list[dict] = []  # last N rejections with detail
        self._last_reject_reason: str = ""  # exposed for trace logging

        # Dedup tracking: (market_id, direction, strategy) -> last_execute_time
        self._dedup: dict[tuple[str, str, str], float] = {}

        # Last sizing computation detail (for dashboard observability)
        self._last_sizing_detail: dict = {}

        log.warning("[ORDER_MGR] PATCH_ACTIVE rejection_logging_v1")

        self._load_trade_log()

        # Live reconciler (initialized after live trader succeeds)
        self._live_reconciler = None

        # Initialize live trader if in live mode
        if config.EXECUTION_MODE == "live":
            if self._live_trader.initialize():
                self._init_live_reconciler()
            else:
                log.error(
                    "[LIVE] Live trader initialization FAILED — "
                    "live orders will be rejected until credentials are fixed"
                )

    def _init_live_reconciler(self) -> None:
        """Initialize the live reconciliation layer."""
        if not config.LIVE_RECONCILIATION_ENABLED:
            log.info("[LIVE] Reconciliation disabled by config")
            return
        try:
            from execution.live_reconciler import LiveReconciler
            self._live_reconciler = LiveReconciler(
                clob_client=self._live_trader.clob_client,
                position_manager=self._pm,
                order_manager=self,
            )
            self._live_trader.set_reconciler(self._live_reconciler)
            log.warning("[LIVE] Reconciler initialized — running startup sync")
            self._live_reconciler.startup_sync()
        except Exception as e:
            log.error(f"[LIVE] Reconciler init failed: {e}")

    @property
    def live_reconciler(self):
        """Expose reconciler for main loop integration."""
        return self._live_reconciler

    # --- Public API ---

    def execute_signal(
        self,
        signal: TradeSignal,
        market_snapshot: Optional[MarketState] = None,
    ) -> Optional[Order]:
        """Validate a signal and execute it if eligible.

        Returns the resulting Order, or None if rejected.
        """
        # Pre-trade validation
        rejection = self._validate(signal, market_snapshot)
        if rejection:
            self._last_reject_reason = rejection
            self._record_rejection(rejection, signal)
            return None

        # Construct order
        order = self._build_order(signal, market_snapshot)
        if order is None:
            self._last_reject_reason = "build_order_failed"
            self._rejected_count += 1
            return None

        # Route to executor
        if config.EXECUTION_MODE == "paper":
            order = self._paper_trader.execute(order, market_snapshot)
        elif config.EXECUTION_MODE == "live":
            order = self._live_trader.execute(order, market_snapshot)
        else:
            order.status = "REJECTED"
            order.metadata["rejection_reason"] = f"Unknown execution mode: {config.EXECUTION_MODE}"
            log.error(f"Unknown execution mode: {config.EXECUTION_MODE}")

        # Track
        self._order_history.append(order)
        self._record_dedup(signal)

        # Persist
        self._append_trade_log(order)

        # Open position if filled
        if order.status == "FILLED":
            pos = self._pm.open_position(order)
            self._log_lifecycle(order, pos)

        return order

    def get_open_orders(self) -> list[Order]:
        return [o for o in self._order_history if not o.is_complete()]

    def get_order_history(self) -> list[Order]:
        return list(self._order_history)

    def get_recent_fills(self, n: int = 5) -> list[Order]:
        fills = [o for o in self._order_history if o.status == "FILLED"]
        return fills[-n:]

    def sync_order_pnl_from_position(self, order_id: str, pnl: float) -> None:
        """When a position resolves, mirror PnL onto the order record (dashboard + trade log)."""
        for order in self._order_history:
            if order.order_id == order_id and order.status == "FILLED":
                order.pnl = pnl
                self._append_trade_log(order)
                return

    def _restore_open_positions_from_orders(self) -> None:
        """Recreate open positions for FILLED orders with no PnL (matches restored capital)."""
        seen: set[str] = set()
        for p in self._pm.get_open_positions():
            if p.order_id:
                seen.add(p.order_id)
        for o in self._order_history:
            if o.status != "FILLED" or o.pnl is not None:
                continue
            if o.order_id in seen:
                continue
            self._pm.restore_open_position_from_order(o)

    def _expire_stale_live_orders(self) -> None:
        """Mark LIVE orders from previous sessions as EXPIRED.

        On restart, LIVE orders in the trade log are from past sessions.
        Their market windows have long since resolved. Keeping them as LIVE
        blocks new entries via the max-concurrent count. Mark them EXPIRED
        so they don't pollute the active order count.
        """
        expired_count = 0
        for o in self._order_history:
            if o.status == "LIVE" and o.execution_mode == "live":
                o.status = "EXPIRED"
                o.metadata["expired_reason"] = "stale_from_previous_session"
                self._append_trade_log(o)
                expired_count += 1
        if expired_count > 0:
            log.warning(
                f"[ORDER_MGR] Expired {expired_count} stale LIVE orders from previous session"
            )

    @property
    def rejected_count(self) -> int:
        return self._rejected_count

    @property
    def rejection_breakdown(self) -> dict:
        return dict(self._rejection_reasons)

    @property
    def recent_rejections(self) -> list:
        return list(self._recent_rejections[-20:])

    def _record_rejection(self, reason: str, signal: TradeSignal) -> None:
        """Record a rejection with categorization."""
        self._rejected_count += 1

        # Categorize
        if "duplicate" in reason.lower() or "dedup" in reason.lower():
            cat = "duplicate_cooldown"
        elif "max position" in reason.lower() or "max entries" in reason.lower():
            cat = "max_entries"
        elif "max concurrent" in reason.lower():
            cat = "max_concurrent"
        elif "capital" in reason.lower():
            cat = "insufficient_capital"
        elif "exposure" in reason.lower():
            cat = "exposure_limit"
        elif "stale" in reason.lower() or "old" in reason.lower():
            cat = "stale_signal"
        elif "not active" in reason.lower() or "resolved" in reason.lower():
            cat = "market_invalid"
        elif "kill switch" in reason.lower():
            cat = "kill_switch"
        elif "risk" in reason.lower():
            cat = "risk_reject"
        elif "trading is disabled" in reason.lower():
            cat = "trading_disabled"
        elif "token" in reason.lower():
            cat = "token_mapping"
        elif "time remaining" in reason.lower():
            cat = "insufficient_time"
        else:
            cat = "other"

        self._rejection_reasons[cat] = self._rejection_reasons.get(cat, 0) + 1

        self._recent_rejections.append({
            "timestamp": time.time(),
            "market_type": signal.market_type,
            "direction": signal.direction,
            "strategy": signal.strategy,
            "reason": reason,
            "category": cat,
        })
        # Keep bounded
        if len(self._recent_rejections) > 100:
            self._recent_rejections = self._recent_rejections[-50:]

        log.warning(
            f"[ORDER_MGR] REJECT category={cat} reason={reason} "
            f"signal_id={signal.signal_id} strategy={signal.strategy} "
            f"direction={signal.direction} market_type={signal.market_type}"
        )

    def cancel_order(self, order_id: str) -> bool:
        for order in self._order_history:
            if order.order_id == order_id and not order.is_complete():
                order.status = "CANCELLED"
                self._append_trade_log(order)
                log.info(f"Order {order_id} cancelled")
                return True
        return False

    # --- Pre-trade validation ---

    def _validate(
        self, signal: TradeSignal, snapshot: Optional[MarketState]
    ) -> Optional[str]:
        """Run all pre-trade checks. Returns rejection reason or None if OK."""
        sig_tag = f"sig={signal.signal_id} {signal.direction} {signal.market_type}"

        if not config.TRADING_ENABLED:
            return "Trading is disabled"

        if config.EXECUTION_MODE not in ("paper", "live"):
            return f"Invalid execution mode: {config.EXECUTION_MODE}"

        # Signal freshness
        age = time.time() - signal.timestamp
        if age > config.MAX_SIGNAL_AGE_SECONDS:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=stale_signal age={age:.1f}s max={config.MAX_SIGNAL_AGE_SECONDS}s")
            return f"Signal too old: {age:.1f}s (max {config.MAX_SIGNAL_AGE_SECONDS}s)"

        if not signal.is_actionable():
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=not_actionable edge={signal.edge:.3f} net_ev={signal.net_ev:.4f} conf={signal.confidence}")
            return "Signal is not actionable"

        # Time remaining
        if signal.time_remaining < config.MIN_EXECUTION_TIME_REMAINING:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=insufficient_time remaining={signal.time_remaining:.0f}s min={config.MIN_EXECUTION_TIME_REMAINING}s")
            return f"Insufficient time remaining: {signal.time_remaining:.0f}s"

        # Market snapshot validation
        if snapshot is None:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=no_snapshot")
            return "No market snapshot available"

        if not snapshot.is_active:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=market_inactive market_id={signal.market_id}")
            return "Market is not active"

        # Token mapping
        if signal.direction == "UP" and not snapshot.up_token_id:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=no_up_token")
            return "No Up token_id available — cannot map direction to token"
        if signal.direction == "DOWN" and not snapshot.down_token_id:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=no_down_token")
            return "No Down token_id available — cannot map direction to token"

        # Position limits — controlled multi-entry
        market_positions = self._pm.count_positions_for_market(signal.market_id)
        live_pending = 0
        if config.EXECUTION_MODE == "live":
            live_pending = sum(
                1 for o in self._order_history
                if o.status == "LIVE"
                and o.execution_mode == "live"
                and o.market_id == signal.market_id
            )
        total_market_entries = market_positions + live_pending
        if total_market_entries >= config.MAX_ENTRIES_PER_WINDOW:
            log.warning(
                f"[ORDER_MGR] REJECT {sig_tag} reason=max_entries_per_window "
                f"filled={market_positions} pending={live_pending} total={total_market_entries} "
                f"cap={config.MAX_ENTRIES_PER_WINDOW} market_id={signal.market_id}"
            )
            return f"Max entries per window reached ({total_market_entries}/{config.MAX_ENTRIES_PER_WINDOW})"

        # Direction conflict: reject if there's already an open position in the
        # opposite direction for the same market. Contradictory bets (UP + DOWN
        # in the same window) are always net-negative due to spread and fees.
        opposite = "DOWN" if signal.direction == "UP" else "UP"
        for p in self._pm.get_open_positions():
            if p.market_id == signal.market_id and p.direction == opposite:
                log.warning(
                    f"[ORDER_MGR] REJECT {sig_tag} reason=direction_conflict "
                    f"existing={opposite} market_id={signal.market_id}"
                )
                return f"Direction conflict: already have {opposite} in this market"

        total_open = self._pm.count_open_positions() + (
            sum(1 for o in self._order_history
                if o.status == "LIVE" and o.execution_mode == "live")
            if config.EXECUTION_MODE == "live" else 0
        )
        if total_open >= config.MAX_CONCURRENT_POSITIONS:
            log.warning(
                f"[ORDER_MGR] REJECT {sig_tag} reason=max_concurrent "
                f"current_open={total_open} limit={config.MAX_CONCURRENT_POSITIONS}"
            )
            return f"Max concurrent positions reached ({config.MAX_CONCURRENT_POSITIONS})"

        # Capital check
        size_usdc = self._calculate_size_usdc(signal)
        if not self._pm.has_sufficient_capital(size_usdc):
            log.warning(
                f"[ORDER_MGR] REJECT {sig_tag} reason=insufficient_capital "
                f"need=${size_usdc:.2f} have=${self._pm.get_available_capital():.2f}"
            )
            return (
                f"Insufficient capital: need ${size_usdc:.2f}, "
                f"have ${self._pm.get_available_capital():.2f}"
            )

        if size_usdc > config.MAX_ORDER_SIZE_CEILING_USDC:
            log.warning(f"[ORDER_MGR] REJECT {sig_tag} reason=ceiling_exceeded size=${size_usdc:.2f} ceiling=${config.MAX_ORDER_SIZE_CEILING_USDC:.2f}")
            return f"Order size ${size_usdc:.2f} exceeds ceiling ${config.MAX_ORDER_SIZE_CEILING_USDC:.2f}"

        # Duplicate suppression
        dedup_key = (signal.market_id, signal.direction, signal.strategy)
        last_exec = self._dedup.get(dedup_key, 0.0)
        dedup_age = time.time() - last_exec
        if dedup_age < config.EXECUTION_DEDUP_SECONDS:
            log.warning(
                f"[ORDER_MGR] REJECT {sig_tag} reason=execution_dedup "
                f"age={dedup_age:.1f}s threshold={config.EXECUTION_DEDUP_SECONDS}s "
                f"market_id={signal.market_id}"
            )
            return "Duplicate signal suppressed (execution dedup)"

        return None  # All checks passed

    # --- Order construction ---

    def _build_order(
        self, signal: TradeSignal, snapshot: Optional[MarketState]
    ) -> Optional[Order]:
        """Build an Order from a validated signal."""
        if snapshot is None:
            return None

        # Token mapping
        if signal.direction == "UP":
            token_id = snapshot.up_token_id
            price = snapshot.yes_price
        elif signal.direction == "DOWN":
            token_id = snapshot.down_token_id
            price = snapshot.no_price
        else:
            log.error(f"Invalid direction: {signal.direction}")
            return None

        if not token_id:
            log.error(f"Cannot resolve token_id for direction={signal.direction}")
            return None

        if price <= 0 or price >= 1.0:
            log.warning(f"Unusual price {price} for {signal.direction} — proceeding")

        size_usdc = self._calculate_size_usdc(signal)
        num_shares = round(size_usdc / price, 2) if price > 0 else 0

        return Order(
            signal_id=signal.signal_id,
            market_id=signal.market_id,
            market_type=signal.market_type,
            direction=signal.direction,
            side="BUY",
            token_id=token_id,
            price=price,
            size_usdc=size_usdc,
            num_shares=num_shares,
            order_type="LIMIT",
            status="PENDING",
            execution_mode=config.EXECUTION_MODE,
            metadata={
                "strategy": signal.strategy,
                "edge": signal.edge,
                "confidence": signal.confidence,
                "model_probability": signal.model_probability,
                "market_probability": signal.market_probability,
                "condition_id": snapshot.condition_id,
                "strike": snapshot.strike_price,
                "strike_source": signal.metadata.get("strike_source", ""),
                "strike_status": signal.metadata.get("strike_status", ""),
                # Lag proxy fields
                "decision_ts": time.time(),
                "market_snapshot_ts": snapshot.timestamp,
                "market_age_ms": (time.time() - snapshot.timestamp) * 1000,
                "price_move_from_strike": abs(signal.spot_price - snapshot.strike_price),
                # Sizing detail
                "sizing_binding_cap": self._last_sizing_detail.get("binding_cap", ""),
                "sizing_equity": self._last_sizing_detail.get("equity", 0),
            },
        )

    def _calculate_size_usdc(self, signal: TradeSignal) -> float:
        """Calculate USDC size using risk equity base.

        Flow:
          1. kelly_suggested = risk_equity * recommended_size_pct
          2. pct_cap = risk_equity * MAX_ORDER_SIZE_PCT
          3. Apply ceiling, floor, and free-capital clamp
        """
        equity = self._pm.get_risk_equity()
        free_capital = self._pm.get_available_capital()

        # Step 1: Kelly-suggested size against total equity
        kelly_suggested = equity * signal.recommended_size_pct

        # Step 2: Percentage cap against total equity
        pct_cap = equity * config.MAX_ORDER_SIZE_PCT

        # Step 3: Take the lesser of Kelly and pct cap
        size = min(kelly_suggested, pct_cap)

        # Step 4: Apply absolute ceiling
        size = min(size, config.MAX_ORDER_SIZE_CEILING_USDC)

        # Step 5: Apply floor (only if equity can support it)
        if equity >= config.MAX_ORDER_SIZE_FLOOR_USDC:
            size = max(size, config.MAX_ORDER_SIZE_FLOOR_USDC)

        # Step 6: Final availability clamp — never exceed free capital
        size = min(size, free_capital)

        # Record which cap was binding (for observability)
        self._last_sizing_detail = {
            "equity": equity,
            "free_capital": free_capital,
            "kelly_pct": signal.recommended_size_pct,
            "kelly_suggested": kelly_suggested,
            "pct_cap": pct_cap,
            "ceiling": config.MAX_ORDER_SIZE_CEILING_USDC,
            "floor": config.MAX_ORDER_SIZE_FLOOR_USDC,
            "final_size": size,
            "binding_cap": self._identify_binding_cap(
                kelly_suggested, pct_cap, size, free_capital, equity),
        }

        return size

    @staticmethod
    def _identify_binding_cap(
        kelly: float, pct_cap: float, final: float, free_capital: float, equity: float
    ) -> str:
        """Identify which cap determined the final order size."""
        if final <= 0:
            return "zero"
        if final >= free_capital - 0.01:
            return "free_capital"
        if final >= config.MAX_ORDER_SIZE_CEILING_USDC - 0.01:
            return "ceiling"
        if final <= config.MAX_ORDER_SIZE_FLOOR_USDC + 0.01 and equity >= config.MAX_ORDER_SIZE_FLOOR_USDC:
            return "floor"
        if abs(final - pct_cap) < 0.01:
            return "pct_cap"
        if abs(final - kelly) < 0.01:
            return "kelly"
        return "kelly"

    def _record_dedup(self, signal: TradeSignal) -> None:
        key = (signal.market_id, signal.direction, signal.strategy)
        self._dedup[key] = time.time()
        # Prune old entries
        cutoff = time.time() - config.EXECUTION_DEDUP_SECONDS * 10
        self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}

    def _log_lifecycle(self, order, position) -> None:
        """Log fill-to-position lifecycle event."""
        try:
            entry = {
                "event": "fill_to_position",
                "ts": time.time(),
                "order_id": order.order_id,
                "position_id": position.position_id if position else None,
                "market_id": order.market_id,
                "market_type": order.market_type,
                "direction": order.direction,
                "fill_price": order.fill_price,
                "size_usdc": order.size_usdc,
                "num_shares": order.num_shares,
                "open_positions_count": self._pm.count_open_positions(),
                "available_capital": self._pm.get_available_capital(),
            }
            p = Path("logs/fill_to_position_trace.jsonl")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # --- Trade log persistence ---

    def _append_trade_log(self, order: Order) -> None:
        """Append an order event to the JSONL trade log."""
        try:
            path = Path(config.TRADE_LOG_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(order.to_dict()) + "\n")
        except Exception as e:
            log.error(f"Failed to write trade log: {e}")

    @staticmethod
    def _order_from_trade_dict(data: dict) -> Order:
        return Order(
            order_id=data.get("order_id", ""),
            signal_id=data.get("signal_id", ""),
            timestamp=data.get("timestamp", 0),
            market_id=data.get("market_id", ""),
            market_type=data.get("market_type", ""),
            direction=data.get("direction", ""),
            side=data.get("side", "BUY"),
            token_id=data.get("token_id", ""),
            price=data.get("price", 0),
            size_usdc=data.get("size_usdc", 0),
            num_shares=data.get("num_shares", 0),
            order_type=data.get("order_type", "LIMIT"),
            status=data.get("status", ""),
            fill_price=data.get("fill_price"),
            fill_timestamp=data.get("fill_timestamp"),
            pnl=data.get("pnl"),
            execution_mode=data.get("execution_mode", "paper"),
            metadata=data.get("metadata", {}),
        )

    def _load_trade_log(self) -> None:
        """Load trade history from JSONL log on startup.

        The log is append-only: each fill and each PnL sync re-serializes the same
        order_id on a new line. Loading every line double-counts size_usdc and
        (pnl+size_usdc) in the capital formula — we keep the last row per order_id.
        """
        path = Path(config.TRADE_LOG_PATH)
        if not path.exists():
            log.info("No existing trade log found — starting fresh")
            log.warning(
                f"[PAPER] fresh_session_reset "
                f"starting_equity={config.STARTING_CAPITAL_USDC} "
                f"backup=check logs/backup_* for previous session"
            )
            return

        raw_rows: list[dict] = []
        errors = 0
        for line_num, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw_rows.append(json.loads(line))
            except (json.JSONDecodeError, TypeError) as e:
                errors += 1
                log.warning(f"Trade log line {line_num} corrupt: {e}")

        # Last JSONL line wins per order_id (canonical order state)
        by_order_id: dict[str, dict] = {}
        for i, data in enumerate(raw_rows):
            oid = str(data.get("order_id") or "").strip()
            key = oid if oid else f"__missing_{i}__"
            by_order_id[key] = data

        deduped_rows = sorted(
            by_order_id.values(),
            key=lambda d: float(d.get("timestamp") or 0),
        )
        for data in deduped_rows:
            self._order_history.append(self._order_from_trade_dict(data))

        loaded = len(self._order_history)
        jsonl_lines = len(raw_rows)
        restored_capital: Optional[float] = None

        if loaded > 0:
            if jsonl_lines > loaded:
                log.info(
                    f"Loaded {loaded} canonical orders from {jsonl_lines} JSONL lines "
                    f"(deduped by order_id)"
                )
            else:
                log.info(f"Loaded {loaded} orders from trade log")

            # Restore capital from filled orders (deduped list only)
            filled = [o for o in self._order_history if o.status == "FILLED"]
            total_spent = sum(o.size_usdc for o in filled)
            total_payouts = sum(
                (o.pnl + o.size_usdc) for o in filled if o.pnl is not None
            )
            restored_capital = config.STARTING_CAPITAL_USDC - total_spent + total_payouts
            self._pm.set_capital(restored_capital)
            log.info(
                f"Restored capital: ${restored_capital:.2f} "
                f"(spent=${total_spent:.2f}, payouts=${total_payouts:.2f})"
            )
            self._restore_open_positions_from_orders()
            self._expire_stale_live_orders()

        # region agent log
        try:
            _dbg = {
                "sessionId": "16560d",
                "hypothesisId": "AC1",
                "location": "order_manager.py:_load_trade_log",
                "message": "trade log dedup capital restore",
                "data": {
                    "jsonl_lines": jsonl_lines,
                    "canonical_orders": loaded,
                    "restored_capital": round(restored_capital, 4)
                    if restored_capital is not None
                    else None,
                },
                "timestamp": int(time.time() * 1000),
            }
            with open(
                "/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log", "a"
            ) as _df:
                _df.write(json.dumps(_dbg) + "\n")
        except Exception:
            pass
        # endregion

        if errors > 0:
            log.warning(f"Trade log had {errors} corrupt line(s) — skipped")
