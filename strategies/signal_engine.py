"""Signal engine orchestrator.

Consumes structured input snapshots from the aggregator, runs all enabled
strategies, and produces filtered/ranked/deduplicated trade signals.

The signal engine itself maintains only lightweight cooldown state for emission
suppression. Strategy evaluation is fully stateless and deterministic.

Deduplication rule:
    If multiple strategies produce signals for the same market in the same cycle:
    - Keep the one with the highest edge
    - If edges are within 0.01 of each other, prefer:
      1. sniper (when near resolution)
      2. latency_arb (otherwise)

Signal cooldown:
    The same signal (same market_id + direction + strategy) is suppressed for
    SIGNAL_COOLDOWN_SECONDS after emission. This prevents spamming the dashboard
    and downstream systems with identical repeated signals. It does NOT affect
    the underlying strategy math.
"""

from __future__ import annotations

import time
from typing import Optional

from models.trade_signal import TradeSignal
from models.market_state import MarketState
from strategies import latency_arb, sniper, market_maker
from strategies import latency_arb_v2
from utils.logger import get_logger
import config

log = get_logger("signal_engine")

# Strategy priority for tie-breaking (lower = higher priority near resolution)
STRATEGY_PRIORITY = {"sniper": 0, "latency_arb": 1, "market_maker": 2}


class SignalEngine:
    """Orchestrates strategy evaluation and signal filtering."""

    def __init__(self):
        self._cooldowns: dict[tuple[str, str, str], float] = {}
        self._signal_history: list[TradeSignal] = []
        # Diagnostics: per-market reasoning for why signals did/didn't fire
        self.diagnostics: dict[str, dict] = {}
        # Missed window tracking
        self._window_tracker: dict[str, dict] = {}  # condition_id -> tracking state
        # Strategy competition: populated each cycle by _deduplicate()
        self.last_competition: dict[str, dict] = {}
        # Momentum snapshot for diagnostics
        self._last_momentum: dict = {}
        # Strike fallback edge rejections (for analytics)
        self.approx_edge_rejections: int = 0

    @property
    def signal_history(self) -> list[TradeSignal]:
        return list(self._signal_history)

    def get_active_strategies(self) -> list[str]:
        """Return list of currently enabled strategy names."""
        return list(config.ENABLED_STRATEGIES)

    def process_snapshot(self, signal_input: dict) -> list[TradeSignal]:
        """Run all enabled strategies against the current data snapshot.

        Args:
            signal_input: dict from aggregator.get_signal_input() containing:
                spot_price, volatility, market_state_5m, market_state_15m

        Returns:
            List of actionable, deduplicated, cooldown-filtered TradeSignals,
            sorted by descending edge.
        """
        self._last_momentum = signal_input.get("momentum") or {}
        spot_price = signal_input.get("spot_price")
        volatility = signal_input.get("volatility")
        if spot_price is None or volatility is None:
            return []

        # Data quality check: block signals if price sources diverge too much
        gap = signal_input.get("price_source_gap")
        gap_blocked = gap is not None and gap > config.PRICE_SOURCE_DIVERGENCE_FAIL_USD

        all_signals: list[TradeSignal] = []

        for key in ("market_state_5m", "market_state_15m"):
            state: Optional[MarketState] = signal_input.get(key)
            if state is None:
                continue

            diag = self._compute_diagnostics(spot_price, volatility, state)

            # Add data quality info to diagnostics
            if gap is not None:
                diag["price_source_gap"] = gap
                if gap > config.PRICE_SOURCE_DIVERGENCE_WARN_USD:
                    diag["reasons"].insert(0, f"USDT/USD basis ${gap:.0f} elevated (warn>{config.PRICE_SOURCE_DIVERGENCE_WARN_USD})")
            if gap_blocked:
                diag["reasons"].insert(0, f"BLOCKED: price gap ${gap:.0f} > ${config.PRICE_SOURCE_DIVERGENCE_FAIL_USD} — possible feed error")

            # Check Polymarket data staleness
            mkt_age = time.time() - state.timestamp
            if mkt_age > config.MARKET_DATA_STALE_WARN_SECONDS:
                diag["reasons"].insert(0, f"market data stale ({mkt_age:.0f}s)")

            self.diagnostics[state.market_type] = diag
            self._track_window(state, diag)

            if not state.is_active or state.strike_price <= 0 or not state.is_signalable:
                continue
            if gap_blocked:
                continue  # Don't signal on divergent price sources

            momentum = signal_input.get("momentum")
            open_positions = signal_input.get("open_positions")
            market_signals = self._evaluate_market(
                spot_price=spot_price,
                volatility=volatility,
                state=state,
                price_source_gap=gap,
                momentum=momentum,
                open_positions=open_positions,
            )
            all_signals.extend(market_signals)

        # Filter to actionable only (exclude data_only signals from execution)
        actionable = [
            s for s in all_signals
            if s.is_actionable() and not s.metadata.get("data_only")
        ]

        # Apply approximate-strike edge buffer gate (condition C)
        actionable = self._apply_approx_strike_gate(actionable, signal_input)
        # Log data-only candidates to history for visibility
        for s in all_signals:
            if s.metadata.get("data_only") and s.is_actionable():
                self._signal_history.insert(0, s)

        # Deduplicate: one signal per market
        deduped = self._deduplicate(actionable)

        # Apply cooldown
        filtered = self._apply_cooldown(deduped)

        # Sort by descending edge
        filtered.sort(key=lambda s: s.edge, reverse=True)

        # Record in history
        for sig in filtered:
            self._signal_history.insert(0, sig)
        # Keep history bounded
        self._signal_history = self._signal_history[:50]

        return filtered

    def _evaluate_market(
        self,
        spot_price: float,
        volatility: float,
        state: MarketState,
        price_source_gap: Optional[float] = None,
        momentum: Optional[dict] = None,
        open_positions: Optional[list[dict]] = None,
    ) -> list[TradeSignal]:
        """Run all enabled strategies for a single market."""
        signals = []
        kwargs = dict(
            spot_price=spot_price,
            strike_price=state.strike_price,
            volatility=volatility,
            time_remaining=state.time_remaining_seconds,
            market_yes_price=state.yes_price,
            market_no_price=state.no_price,
            spread=state.spread,
            market_type=state.market_type,
            market_id=state.market_id,
            price_source_gap=price_source_gap,
            momentum=momentum,
            market_age_ms=(time.time() - state.timestamp) * 1000 if state.timestamp else 0,
        )

        # Tag all signals from this market with strike source
        _strike_source = state.strike_source
        _strike_status = state.strike_status

        if "latency_arb" in config.ENABLED_STRATEGIES:
            sig = latency_arb.evaluate(**kwargs)
            if sig and config.LATENCY_ARB_V2_ENABLED:
                v2_result = latency_arb_v2.refine(sig, open_positions or [])
                sig = v2_result.get("signal")
                if sig:
                    sig.metadata["v2_decision"] = v2_result["decision"]
                    sig.metadata["v2_reason"] = v2_result["reason"]
                    sig.metadata["v2_diagnostics"] = v2_result.get("v2_diagnostics", {})
            if sig:
                signals.append(sig)

        if "sniper" in config.ENABLED_STRATEGIES:
            sig = sniper.evaluate(**kwargs)
            if sig:
                signals.append(sig)

        if "market_maker" in config.ENABLED_STRATEGIES:
            mm_kw = {k: v for k, v in kwargs.items() if k != "price_source_gap"}
            sig = market_maker.evaluate(**mm_kw)
            if sig:
                signals.append(sig)

        for sig in signals:
            sig.metadata["strike_source"] = _strike_source
            sig.metadata["strike_status"] = _strike_status

        return signals

    def _deduplicate(self, signals: list[TradeSignal]) -> list[TradeSignal]:
        """Keep at most one signal per market. Prefer highest edge with tie-break.

        Also populates self.last_competition with per-market competition details.
        """
        best_per_market: dict[str, TradeSignal] = {}
        # Collect all contenders per market for competition logging
        contenders_per_market: dict[str, list[TradeSignal]] = {}

        for sig in signals:
            key = sig.market_type
            contenders_per_market.setdefault(key, []).append(sig)
            existing = best_per_market.get(key)

            if existing is None:
                best_per_market[key] = sig
                continue

            if abs(sig.edge - existing.edge) < 0.01:
                sig_priority = STRATEGY_PRIORITY.get(sig.strategy, 99)
                existing_priority = STRATEGY_PRIORITY.get(existing.strategy, 99)
                if sig_priority < existing_priority:
                    best_per_market[key] = sig
            elif sig.edge > existing.edge:
                best_per_market[key] = sig

        # Build competition record
        self.last_competition = {}
        for mkt, contenders in contenders_per_market.items():
            winner = best_per_market.get(mkt)
            losers = [s for s in contenders if s is not winner]

            if len(contenders) <= 1:
                reason = "solo"
                tie_break = False
            elif winner and losers:
                loser = losers[0]
                edge_gap = winner.edge - loser.edge
                if abs(edge_gap) < 0.01:
                    reason = "tie_break_priority"
                    tie_break = True
                else:
                    reason = "edge_higher"
                    tie_break = False
            else:
                reason = "unknown"
                tie_break = False

            def _sig_summary(s):
                return {
                    "strategy": s.strategy, "signal_id": s.signal_id,
                    "edge": s.edge, "net_ev": s.net_ev,
                    "model_probability": s.model_probability,
                    "market_probability": s.market_probability,
                    "confidence": s.confidence,
                }

            rec = {
                "market_type": mkt,
                "contenders": [_sig_summary(s) for s in contenders],
                "num_contenders": len(contenders),
                "winner_strategy": winner.strategy if winner else None,
                "winner_edge": winner.edge if winner else None,
                "reason": reason,
                "tie_break": tie_break,
                "edge_gap": (winner.edge - losers[0].edge) if winner and losers else 0,
            }
            if losers:
                rec["loser_strategy"] = losers[0].strategy
                rec["loser_edge"] = losers[0].edge

            self.last_competition[mkt] = rec

            # Log competition to file when multiple contenders
            if len(contenders) > 1:
                self._log_competition(rec)

        return list(best_per_market.values())

    def _log_competition(self, rec: dict) -> None:
        """Write competition record to JSONL with run_id."""
        import json as _json
        from pathlib import Path as _Path
        try:
            rec_with_ts = {"ts": time.time(), "run_id": getattr(self, "run_id", ""), **rec}
            p = _Path("logs/strategy_competition_trace.jsonl")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(_json.dumps(rec_with_ts, default=str) + "\n")
        except Exception:
            pass

    def _apply_approx_strike_gate(
        self, signals: list[TradeSignal], signal_input: dict
    ) -> list[TradeSignal]:
        """Filter signals from approx_fallback markets unless edge is strong enough.

        When a market's strike_status is "approx_fallback", signals must meet
        a higher edge and net_ev threshold (STRIKE_APPROX_EDGE_MULTIPLIER times
        the normal minimum) to compensate for strike uncertainty.
        """
        if not config.STRIKE_ALLOW_APPROX_FALLBACK:
            return signals

        multiplier = config.STRIKE_APPROX_EDGE_MULTIPLIER
        min_edge = config.MIN_ACTIONABLE_EDGE * multiplier
        min_ev = config.MIN_NET_EV * multiplier

        # Identify which market types are in approx_fallback
        approx_markets: set[str] = set()
        for key in ("market_state_5m", "market_state_15m"):
            state: MarketState | None = signal_input.get(key)
            if state and state.strike_status == "approx_fallback":
                approx_markets.add(state.market_type)

        if not approx_markets:
            return signals

        filtered = []
        for sig in signals:
            if sig.market_type not in approx_markets:
                filtered.append(sig)
                continue

            edge_ok = sig.edge >= min_edge
            ev_ok = sig.net_ev >= min_ev
            if edge_ok or ev_ok:
                sig.metadata["strike_approx_gated"] = True
                sig.metadata["strike_source"] = "spot_approx_early"
                filtered.append(sig)
                log.info(
                    f"[STRIKE] approx_edge_gate_passed market={sig.market_type} "
                    f"edge={sig.edge:.4f}>={min_edge:.4f} "
                    f"net_ev={sig.net_ev:.4f}>={min_ev:.4f} "
                    f"strategy={sig.strategy}"
                )
            else:
                self.approx_edge_rejections += 1
                log.warning(
                    f"[STRIKE] fallback_rejected market={sig.market_type} "
                    f"reason=weak_edge edge={sig.edge:.4f}<{min_edge:.4f} "
                    f"net_ev={sig.net_ev:.4f}<{min_ev:.4f} "
                    f"strategy={sig.strategy}"
                )

        return filtered

    def _apply_cooldown(self, signals: list[TradeSignal]) -> list[TradeSignal]:
        """Suppress signals that were recently emitted."""
        now = time.time()
        filtered = []

        for sig in signals:
            cooldown_key = (sig.market_id, sig.direction, sig.strategy)
            last_emit = self._cooldowns.get(cooldown_key, 0.0)

            if now - last_emit >= config.SIGNAL_COOLDOWN_SECONDS:
                self._cooldowns[cooldown_key] = now
                filtered.append(sig)

        cutoff = now - config.SIGNAL_COOLDOWN_SECONDS * 10
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > cutoff}

        return filtered

    def _compute_diagnostics(self, spot: float, vol: float, state: MarketState) -> dict:
        """Compute full diagnostic for a market — always, even when no signal fires."""
        from strategies import probability_model

        diag: dict = {
            "market_type": state.market_type,
            "market_id": state.market_id,
            "condition_id": state.condition_id,
            "spot": spot,
            "strike": state.strike_price,
            "distance": spot - state.strike_price if state.strike_price > 0 else None,
            "time_remaining": state.time_remaining_seconds,
            "time_to_window": state.time_to_window_seconds,
            "window_started": state.window_started,
            "volatility": vol,
            "market_up": state.yes_price,
            "market_down": state.no_price,
            "spread": state.spread,
            "reasons": [],
        }

        if state.strike_price <= 0 or vol is None or vol <= 0:
            diag["reasons"].append("missing strike or volatility")
            return diag

        probs = probability_model.calculate_probability(
            spot, state.strike_price, vol, state.time_remaining_seconds)
        diag["z_score"] = probs["z_score"]
        diag["model_up"] = probs["prob_up"]
        diag["model_down"] = probs["prob_down"]

        edge_up = probs["prob_up"] - state.yes_price
        edge_dn = probs["prob_down"] - state.no_price
        diag["edge_up"] = edge_up
        diag["edge_down"] = edge_dn
        diag["best_edge"] = max(edge_up, edge_dn)
        diag["best_direction"] = "UP" if edge_up > edge_dn else "DOWN"

        # Calculate EV for both directions
        ev_up = probability_model.calculate_ev(
            probs["prob_up"], state.yes_price, spread_cost=state.spread)
        ev_dn = probability_model.calculate_ev(
            probs["prob_down"], state.no_price, spread_cost=state.spread)
        best_ev = ev_up if ev_up["net_ev"] > ev_dn["net_ev"] else ev_dn
        diag["gross_ev"] = best_ev["gross_ev"]
        diag["net_ev"] = best_ev["net_ev"]
        diag["net_ev_up"] = ev_up["net_ev"]
        diag["net_ev_down"] = ev_dn["net_ev"]
        diag["estimated_costs"] = best_ev["estimated_costs"]
        diag["ev_per_dollar"] = best_ev["ev_per_dollar"]

        # Kelly sizing for both directions
        kelly_up = probability_model.calculate_kelly_size(probs["prob_up"], state.yes_price)
        kelly_dn = probability_model.calculate_kelly_size(probs["prob_down"], state.no_price)
        diag["kelly_size"] = max(kelly_up, kelly_dn)
        diag["kelly_up"] = kelly_up
        diag["kelly_down"] = kelly_dn

        # Model-market disagreement
        best_model = probs["prob_up"] if edge_up > edge_dn else probs["prob_down"]
        best_mkt_price = state.yes_price if edge_up > edge_dn else state.no_price
        disagreement = abs(best_model - best_mkt_price)
        fragile = (
            best_model >= config.FRAGILE_CERTAINTY_MODEL_PROB
            and best_mkt_price <= config.FRAGILE_CERTAINTY_MAX_MARKET_PROB
        )
        diag["disagreement"] = disagreement
        diag["fragile_certainty"] = fragile

        # Price move from strike
        price_move = abs(spot - state.strike_price) if state.strike_price > 0 else 0

        # 15min data-only flag
        data_only_15m = (
            state.market_type == "btc-15min" and not config.LATENCY_ARB_15MIN_ENABLED
        )
        diag["data_only_15m"] = data_only_15m

        # Momentum fields
        mom = self._last_momentum
        diag["move_5s"] = mom.get("move_5s")
        diag["move_10s"] = mom.get("move_10s")
        diag["move_30s"] = mom.get("move_30s")
        diag["abs_move_5s"] = mom.get("abs_move_5s")
        diag["abs_move_10s"] = mom.get("abs_move_10s")
        diag["abs_move_30s"] = mom.get("abs_move_30s")

        from strategies.latency_arb import _check_urgency, _check_freshness, _classify_phase, _simulate_phase_rules
        urgency_pass = _check_urgency(mom)
        diag["urgency_pass"] = urgency_pass

        freshness_pass, freshest_window = _check_freshness(mom)
        diag["freshness_pass"] = freshness_pass
        diag["freshest_window"] = freshest_window

        # Lag proxy
        mkt_age = (time.time() - state.timestamp) * 1000 if state.timestamp else 0
        diag["market_age_ms"] = mkt_age
        lag_proxy_pass = mkt_age >= config.LATENCY_ARB_MIN_MARKET_AGE_MS if mkt_age > 0 else True
        diag["lag_proxy_pass"] = lag_proxy_pass

        # Proto latency gate: urgency + lag + disagreement
        proto_gate = urgency_pass and lag_proxy_pass and disagreement >= config.LATENCY_ARB_MIN_DISAGREEMENT
        diag["proto_latency_gate"] = proto_gate

        # Quality filter reasons
        reasons = []
        if not urgency_pass:
            reasons.append(f"insufficient short-term move (5s={mom.get('abs_move_5s')}, 10s={mom.get('abs_move_10s')}, 30s={mom.get('abs_move_30s')})")
        if not lag_proxy_pass:
            reasons.append(f"lag proxy weak: market_age {mkt_age:.0f}ms < min {config.LATENCY_ARB_MIN_MARKET_AGE_MS}ms")
        if disagreement < config.LATENCY_ARB_MIN_DISAGREEMENT:
            reasons.append(f"disagreement {disagreement:.3f} < min {config.LATENCY_ARB_MIN_DISAGREEMENT} for proto gate")
        if not freshness_pass:
            reasons.append(f"move too old: only in {freshest_window} window, need 5s or 10s")

        # Market phase simulation
        market_phase = _classify_phase(state.time_remaining_seconds)
        phase_would_pass, phase_reject_reason = _simulate_phase_rules(
            market_phase, disagreement, freshness_pass, freshest_window, urgency_pass)
        diag["market_phase"] = market_phase
        diag["phase_would_pass"] = phase_would_pass
        diag["phase_reject_reason"] = phase_reject_reason
        if not phase_would_pass:
            reasons.append(f"phase would block: {phase_reject_reason}")
        if state.time_remaining_seconds < config.LATENCY_ARB_MIN_TIME:
            reasons.append(f"time {state.time_remaining_seconds:.0f}s < min {config.LATENCY_ARB_MIN_TIME}s")
        if state.spread > config.LATENCY_ARB_MAX_SPREAD:
            reasons.append(f"spread {state.spread:.3f} > max {config.LATENCY_ARB_MAX_SPREAD}")
        if price_move < config.LATENCY_ARB_MIN_PRICE_MOVE:
            reasons.append(f"price move ${price_move:.1f} < min ${config.LATENCY_ARB_MIN_PRICE_MOVE}")
        _min_ev = max(config.MIN_NET_EV, config.SHORT_MARKET_MIN_NET_EV)
        if best_ev["net_ev"] < _min_ev:
            reasons.append(f"net EV {best_ev['net_ev']:.4f} < min {_min_ev:.4f}")
        best = max(edge_up, edge_dn)
        if best < config.MIN_ACTIONABLE_EDGE:
            reasons.append(f"edge {best:.3f} < min actionable {config.MIN_ACTIONABLE_EDGE}")
        if disagreement >= config.DISAGREEMENT_HARD_REJECT:
            reasons.append(f"disagreement {disagreement:.2f} >= hard reject {config.DISAGREEMENT_HARD_REJECT}")
        elif fragile:
            reasons.append(f"fragile certainty (model={best_model:.2f} mkt={best_mkt_price:.2f})")
        if data_only_15m:
            reasons.append("15min latency_arb: DATA ONLY (paused)")

        if not reasons:
            reasons.append("signal would fire")

        diag["reasons"] = reasons
        return diag

    def _track_window(self, state: MarketState, diag: dict) -> None:
        """Track window lifecycle for missed-window logging."""
        import json
        from pathlib import Path

        cid = state.condition_id
        if not cid:
            return

        tracker = self._window_tracker.get(cid)

        # If window just started, init tracker
        if state.window_started and tracker is None:
            self._window_tracker[cid] = {
                "market_id": state.market_id,
                "market_type": state.market_type,
                "condition_id": cid,
                "question": state.question,
                "strike": state.strike_price,
                "window_start": time.time(),
                "peak_edge": 0.0,
                "traded": False,
                "reasons_seen": set(),
            }
        elif state.window_started and tracker is not None:
            # Update peak edge during window
            best = diag.get("best_edge", 0.0)
            if best and best > tracker["peak_edge"]:
                tracker["peak_edge"] = best
            if diag.get("reasons"):
                for r in diag["reasons"]:
                    tracker["reasons_seen"].add(r)

        # If window ended (market cycled away or resolved), log missed window
        if tracker is not None and not state.window_started and state.condition_id != cid:
            pass  # Market transitioned — handled below

        # Check for completed windows by scanning tracker for stale entries
        now = time.time()
        stale = []
        for tid, t in self._window_tracker.items():
            # If the active market has a different condition_id, this window ended
            if tid != state.condition_id and now - t["window_start"] > 30:
                if not t["traded"]:
                    self._log_missed_window(t)
                stale.append(tid)
        for tid in stale:
            del self._window_tracker[tid]

    def record_trade(self, market_type: str) -> None:
        """Called when a trade executes, to mark the current window as traded."""
        for t in self._window_tracker.values():
            if t["market_type"] == market_type:
                t["traded"] = True

    def _log_missed_window(self, tracker: dict) -> None:
        """Log a missed window to JSONL."""
        import json
        from pathlib import Path

        entry = {
            "timestamp": time.time(),
            "market_id": tracker["market_id"],
            "market_type": tracker["market_type"],
            "condition_id": tracker["condition_id"],
            "question": tracker["question"],
            "strike": tracker["strike"],
            "window_start": tracker["window_start"],
            "window_duration_s": time.time() - tracker["window_start"],
            "peak_edge": tracker["peak_edge"],
            "min_threshold": config.LATENCY_ARB_MIN_EDGE,
            "closest_approach": tracker["peak_edge"],
            "reasons": list(tracker.get("reasons_seen", set()))[:5],
        }
        try:
            path = Path(config.MISSED_WINDOW_LOG_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            log.info(f"Missed window logged: {tracker['market_type']} peak_edge={tracker['peak_edge']:.4f}")
        except Exception as e:
            log.error(f"Failed to log missed window: {e}")
