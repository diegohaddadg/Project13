"""Latency arbitrage strategy with net EV filter, Kelly sizing, and quality guards."""

from __future__ import annotations

from typing import Optional

from models.trade_signal import TradeSignal
from strategies import probability_model
import config


def evaluate(
    spot_price: float,
    strike_price: float,
    volatility: float,
    time_remaining: float,
    market_yes_price: float,
    market_no_price: float,
    spread: float,
    market_type: str,
    market_id: str,
    price_source_gap: Optional[float] = None,
    momentum: Optional[dict] = None,
    market_age_ms: float = 0,
) -> Optional[TradeSignal]:
    """Evaluate latency arbitrage opportunity using proto-latency gate.

    Requires urgency + lag proxy + disagreement before proceeding to EV check.
    """
    # Guard: insufficient time
    if time_remaining < config.LATENCY_ARB_MIN_TIME:
        return None
    # Guard: spread too wide
    if spread > config.LATENCY_ARB_MAX_SPREAD:
        return None
    # Guard: invalid inputs
    if strike_price <= 0 or volatility <= 0:
        return None

    # Momentum / urgency filter
    mom = momentum or {}
    urgency_pass = _check_urgency(mom)
    if config.LATENCY_ARB_REQUIRE_URGENCY and not urgency_pass:
        return None

    # Lag proxy filter — market snapshot must be old enough to plausibly be behind
    lag_pass = market_age_ms >= config.LATENCY_ARB_MIN_MARKET_AGE_MS if market_age_ms > 0 else True

    # Guard: price move from strike too small
    price_move = abs(spot_price - strike_price)
    if price_move < config.LATENCY_ARB_MIN_PRICE_MOVE:
        return None

    probs = probability_model.calculate_probability(
        spot_price, strike_price, volatility, time_remaining)
    prob_up = probs["prob_up"]
    prob_down = probs["prob_down"]
    z_score = probs["z_score"]

    edge_up = probability_model.calculate_edge(prob_up, market_yes_price)
    edge_down = probability_model.calculate_edge(prob_down, market_no_price)

    ev_up = probability_model.calculate_ev(prob_up, market_yes_price, spread_cost=spread)
    ev_down = probability_model.calculate_ev(prob_down, market_no_price, spread_cost=spread)

    min_net = max(config.MIN_NET_EV, config.SHORT_MARKET_MIN_NET_EV)

    # Select direction with better net EV
    if ev_up["net_ev"] > ev_down["net_ev"] and ev_up["net_ev"] > min_net:
        direction = "UP"
        edge = edge_up
        model_prob = prob_up
        market_prob = market_yes_price
        ev = ev_up
        kelly_size = probability_model.calculate_kelly_size(prob_up, market_yes_price)
    elif ev_down["net_ev"] > min_net:
        direction = "DOWN"
        edge = edge_down
        model_prob = prob_down
        market_prob = market_no_price
        ev = ev_down
        kelly_size = probability_model.calculate_kelly_size(prob_down, market_no_price)
    else:
        return None

    # Freshness check — move must be visible in a recent window
    freshness_pass, freshest_window = _check_freshness(mom)

    # Proto latency gate v2: urgency + lag + disagreement + freshness
    disagreement = abs(model_prob - market_prob)
    proto_gate = True
    gate_reason = None
    if config.LATENCY_ARB_REQUIRE_URGENCY:
        if not urgency_pass:
            proto_gate = False
            gate_reason = "insufficient urgency"
        elif not lag_pass:
            proto_gate = False
            gate_reason = f"lag proxy weak: market_age {market_age_ms:.0f}ms < {config.LATENCY_ARB_MIN_MARKET_AGE_MS}ms"
        elif disagreement < config.LATENCY_ARB_MIN_DISAGREEMENT:
            proto_gate = False
            gate_reason = f"disagreement {disagreement:.3f} < {config.LATENCY_ARB_MIN_DISAGREEMENT}"
        elif config.LATENCY_ARB_REQUIRE_FRESH_MOVE and not freshness_pass:
            proto_gate = False
            gate_reason = f"move too old: only in {freshest_window} window, need 5s or 10s"

    if not proto_gate:
        return None

    # Market phase classification + simulated phase rules
    market_phase = _classify_phase(time_remaining)
    phase_would_pass, phase_reject_reason = _simulate_phase_rules(
        market_phase, disagreement, freshness_pass, freshest_window, urgency_pass)

    # In enforce mode, block on phase failure
    if config.LATENCY_ARB_PHASE_MODE == "enforce" and not phase_would_pass:
        return None

    confidence = probability_model.classify_confidence(edge, time_remaining)

    # 15min data-only flag
    data_only = (market_type == "btc-15min" and not config.LATENCY_ARB_15MIN_ENABLED)

    return TradeSignal(
        market_type=market_type,
        market_id=market_id,
        strategy="latency_arb",
        direction=direction,
        model_probability=model_prob,
        market_probability=market_prob,
        edge=edge,
        gross_ev=ev["gross_ev"],
        net_ev=ev["net_ev"],
        estimated_costs=ev["estimated_costs"],
        confidence=confidence,
        recommended_size_pct=kelly_size,
        strike_price=strike_price,
        spot_price=spot_price,
        time_remaining=time_remaining,
        metadata={
            "z_score": z_score,
            "prob_up": prob_up,
            "prob_down": prob_down,
            "market_yes": market_yes_price,
            "market_no": market_no_price,
            "spread": spread,
            "ev_per_dollar": ev["ev_per_dollar"],
            "kelly_raw": kelly_size / config.KELLY_FRACTION if config.KELLY_FRACTION > 0 else 0,
            "price_move_from_strike": price_move,
            "data_only": data_only,
            "paused_reason": "15min latency_arb paused — insufficient evidence" if data_only else None,
            "move_5s": mom.get("move_5s"),
            "move_10s": mom.get("move_10s"),
            "move_30s": mom.get("move_30s"),
            "abs_move_5s": mom.get("abs_move_5s"),
            "abs_move_10s": mom.get("abs_move_10s"),
            "abs_move_30s": mom.get("abs_move_30s"),
            "urgency_pass": urgency_pass,
            "market_age_ms": market_age_ms,
            "lag_proxy_pass": lag_pass,
            "disagreement": disagreement,
            "proto_latency_gate": proto_gate,
            "freshness_pass": freshness_pass,
            "freshest_window": freshest_window,
            "market_phase": market_phase,
            "phase_mode": config.LATENCY_ARB_PHASE_MODE,
            "phase_would_pass": phase_would_pass,
            "phase_reject_reason": phase_reject_reason,
        },
    )


def _classify_phase(time_remaining: float) -> str:
    """Classify BTC 5m market phase by time remaining."""
    if time_remaining > config.LATENCY_ARB_EARLY_PHASE_MIN_SECONDS:
        return "early"
    if time_remaining > config.LATENCY_ARB_LATE_PHASE_MAX_SECONDS:
        return "mid"
    return "late"


def _simulate_phase_rules(
    phase: str,
    disagreement: float,
    freshness_pass: bool,
    freshest_window: str,
    urgency_pass: bool,
) -> tuple[bool, str]:
    """Simulate phase-aware strictness rules. Returns (would_pass, reason).

    Early: require stronger disagreement + urgency
    Mid: baseline (always passes if proto gate passed)
    Late: require very fresh move (5s window)
    """
    if phase == "early":
        if disagreement < config.LATENCY_ARB_EARLY_MIN_DISAGREEMENT:
            return (False, f"early phase: disagreement {disagreement:.3f} < {config.LATENCY_ARB_EARLY_MIN_DISAGREEMENT}")
        if not urgency_pass:
            return (False, "early phase: urgency too weak")
        return (True, None)

    if phase == "mid":
        return (True, None)

    if phase == "late":
        if config.LATENCY_ARB_LATE_REQUIRE_FRESH_5S and freshest_window != "5s":
            return (False, f"late phase: need 5s freshness, got {freshest_window}")
        return (True, None)

    return (True, None)


def _check_freshness(mom: dict) -> tuple[bool, str]:
    """Check if the qualifying move is recent enough (visible in 5s or 10s).

    Returns (fresh, freshest_window):
    - (True, "5s") — move visible in 5-second window (very fresh)
    - (True, "10s") — move visible in 10-second window (fresh)
    - (False, "30s_only") — move only in 30s window (stale, may be arbitraged)
    - (True, "no_data") — no momentum data available, don't block
    """
    m5 = mom.get("abs_move_5s")
    m10 = mom.get("abs_move_10s")
    m30 = mom.get("abs_move_30s")

    # If no data at all, don't block
    if m5 is None and m10 is None and m30 is None:
        return (True, "no_data")

    # Check short windows first (freshest)
    if m5 is not None and m5 >= config.LATENCY_ARB_MIN_ABS_MOVE_5S:
        return (True, "5s")
    if m10 is not None and m10 >= config.LATENCY_ARB_MIN_ABS_MOVE_10S:
        return (True, "10s")

    # Only 30s qualifies — move is stale
    if m30 is not None and m30 >= config.LATENCY_ARB_MIN_ABS_MOVE_30S:
        return (False, "30s_only")

    # Nothing qualifies at all
    return (False, "none")


def _check_urgency(mom: dict) -> bool:
    """Check if at least one momentum window exceeds its threshold.

    Returns True if any short-horizon move is large enough to suggest
    real urgency rather than noise. Returns True if momentum data is
    unavailable (don't block on missing data).
    """
    checks = [
        (mom.get("abs_move_5s"), config.LATENCY_ARB_MIN_ABS_MOVE_5S),
        (mom.get("abs_move_10s"), config.LATENCY_ARB_MIN_ABS_MOVE_10S),
        (mom.get("abs_move_30s"), config.LATENCY_ARB_MIN_ABS_MOVE_30S),
    ]
    # If all momentum values are None, don't block (insufficient history)
    available = [(val, thresh) for val, thresh in checks if val is not None]
    if not available:
        return True  # No data to filter on
    # Require at least one window to pass
    return any(val >= thresh for val, thresh in available)
