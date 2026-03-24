"""Near-resolution sniping strategy with net EV filter and Kelly sizing."""

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
    """Evaluate near-resolution sniping opportunity using net EV."""
    if time_remaining > config.SNIPER_MAX_TIME:
        return None
    if time_remaining < config.SNIPER_MIN_TIME:
        return None
    if spread > config.SNIPER_MAX_SPREAD:
        return None
    if strike_price <= 0:
        return None
    if volatility is None or volatility <= 0:
        return None

    if (
        config.SNIPER_MAX_PRICE_SOURCE_GAP_USD > 0
        and price_source_gap is not None
        and price_source_gap > config.SNIPER_MAX_PRICE_SOURCE_GAP_USD
    ):
        return None

    # Cap z-score blow-ups when rolling vol is tiny (same path as model, extra guard for sniper)
    effective_vol = max(volatility, config.MIN_MODEL_VOLATILITY_FLOOR_USD)

    probs = probability_model.calculate_probability(
        spot_price, strike_price, effective_vol, time_remaining
    )
    prob_up = probs["prob_up"]
    prob_down = probs["prob_down"]
    z_score = probs["z_score"]

    # Determine likely outcome
    if prob_up >= config.SNIPER_MIN_PROBABILITY:
        direction = "UP"
        model_prob = prob_up
        market_prob = market_yes_price
    elif prob_down >= config.SNIPER_MIN_PROBABILITY:
        direction = "DOWN"
        model_prob = prob_down
        market_prob = market_no_price
    else:
        return None

    if market_prob > config.SNIPER_MAX_ENTRY_PRICE:
        return None

    edge = probability_model.calculate_edge(model_prob, market_prob)
    if edge <= 0:
        return None

    ev = probability_model.calculate_ev(model_prob, market_prob, spread_cost=spread)
    min_net = max(config.MIN_NET_EV, config.SNIPER_MIN_NET_EV)
    if ev["net_ev"] <= min_net:
        return None

    confidence = probability_model.classify_confidence(edge, time_remaining)
    kelly_size = probability_model.calculate_kelly_size(model_prob, market_prob)
    # Sniper can size larger but still capped
    kelly_size = min(kelly_size * 1.5, config.SNIPER_MAX_SIZE)

    return TradeSignal(
        market_type=market_type,
        market_id=market_id,
        strategy="sniper",
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
            "entry_price": market_prob,
            "distance_to_strike": spot_price - strike_price,
            "ev_per_dollar": ev["ev_per_dollar"],
            "effective_volatility": effective_vol,
            "rolling_volatility": volatility,
            "min_net_ev_applied": min_net,
        },
    )
