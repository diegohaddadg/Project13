"""Probability model for BTC up/down prediction markets.

Mathematical core: estimates the probability that BTC finishes above or below
a strike price within a given time window.

Model: Z-Score (Normal CDF)
------
Assumes short-term BTC price movement is approximately normally distributed.

    z = (spot_price - strike_price) / (volatility_per_second * sqrt(time_remaining_seconds))
    prob_up = norm.cdf(z)
    prob_down = 1 - prob_up

Volatility Calibration
----------------------
The volatility input from Phase 1 (aggregator.get_volatility()) is:

    rolling_std = np.std(recent prices)

The deque is chosen to match model spot: Coinbase prices when Coinbase is non-stale,
otherwise Binance — not a mixed stream.

This is the standard deviation of raw prices over the last ~300 ticks. At typical
Binance tick rates of 20-35 ticks/sec, this window spans approximately 10-15 seconds.

To convert to per-second volatility for the z-score formula:

    vol_per_second = rolling_std / sqrt(window_duration_seconds)

Where window_duration_seconds is estimated from config.VOLATILITY_WINDOW_SECONDS
(default: 12 seconds — calibrated to 300 ticks at ~25 ticks/sec).

Worked example:
    rolling_std = $12.50 over 300 ticks spanning ~12 seconds
    vol_per_second = $12.50 / sqrt(12) ≈ $3.61
    With spot=$68,100, strike=$68,000, time_remaining=120s:
    z = ($68,100 - $68,000) / ($3.61 * sqrt(120))
    z = $100 / ($3.61 * 10.95) = $100 / $39.53 ≈ 2.53
    prob_up = norm.cdf(2.53) ≈ 0.994

This is a pure computation module — no state, no side effects, no API calls.
"""

from __future__ import annotations

import math
from typing import Optional

from scipy.stats import norm

import config


def normalize_volatility(rolling_std: float) -> float:
    """Convert rolling window price std to per-second volatility.

    Args:
        rolling_std: Standard deviation of prices from the rolling window
                     (aggregator.get_volatility()).

    Returns:
        Per-second volatility estimate suitable for the z-score formula.
    """
    if rolling_std <= 0:
        return 0.0
    return rolling_std / math.sqrt(config.VOLATILITY_WINDOW_SECONDS)


def calculate_probability(
    spot_price: float,
    strike_price: float,
    volatility: float,
    time_remaining_seconds: float,
) -> dict:
    """Calculate probability of BTC finishing above/below strike.

    Args:
        spot_price: Current BTC spot price.
        strike_price: Market strike/reference price.
        volatility: Rolling window standard deviation (raw, from aggregator).
                    Will be normalized to per-second internally.
        time_remaining_seconds: Seconds until market resolution.

    Returns:
        dict with keys: prob_up, prob_down, z_score
    """
    # Edge case: market already resolved or no time left
    if time_remaining_seconds <= 0:
        if spot_price >= strike_price:
            return {"prob_up": 1.0, "prob_down": 0.0, "z_score": float("inf")}
        else:
            return {"prob_up": 0.0, "prob_down": 1.0, "z_score": float("-inf")}

    # Edge case: zero or negative volatility — deterministic based on current position
    vol_per_sec = normalize_volatility(volatility)
    if vol_per_sec <= 0:
        if spot_price > strike_price:
            return {"prob_up": 1.0, "prob_down": 0.0, "z_score": float("inf")}
        elif spot_price < strike_price:
            return {"prob_up": 0.0, "prob_down": 1.0, "z_score": float("-inf")}
        else:
            return {"prob_up": 0.5, "prob_down": 0.5, "z_score": 0.0}

    # Standard z-score calculation
    denominator = vol_per_sec * math.sqrt(time_remaining_seconds)
    z = (spot_price - strike_price) / denominator

    prob_up = float(norm.cdf(z))
    prob_down = 1.0 - prob_up

    # Clip to [0, 1] for safety (should already be, but defensive)
    prob_up = max(0.0, min(1.0, prob_up))
    prob_down = max(0.0, min(1.0, prob_down))

    return {"prob_up": prob_up, "prob_down": prob_down, "z_score": z}


def calculate_edge(model_prob: float, market_prob: float) -> float:
    """Calculate edge as difference between model and market probability.

    Positive edge means model sees higher probability than market prices reflect.
    """
    return model_prob - market_prob


def classify_confidence(edge: float, time_remaining: float) -> str:
    """Classify signal confidence based on edge magnitude and time remaining.

    Thresholds come from config.py:
    - HIGH: edge > CONFIDENCE_HIGH_EDGE and time > CONFIDENCE_HIGH_MIN_TIME
    - MEDIUM: edge > CONFIDENCE_MEDIUM_EDGE and time > CONFIDENCE_MEDIUM_MIN_TIME
    - LOW: otherwise
    """
    abs_edge = abs(edge)

    if (abs_edge >= config.CONFIDENCE_HIGH_EDGE
            and time_remaining >= config.CONFIDENCE_HIGH_MIN_TIME):
        return "HIGH"

    if (abs_edge >= config.CONFIDENCE_MEDIUM_EDGE
            and time_remaining >= config.CONFIDENCE_MEDIUM_MIN_TIME):
        return "MEDIUM"

    return "LOW"


def calculate_ev(
    model_prob: float,
    market_price: float,
    fees: float = None,
    slippage: float = None,
    spread_cost: float = 0.0,
) -> dict:
    """Calculate expected value accounting for real trading costs.

    Args:
        model_prob: Our model's estimated probability of this outcome.
        market_price: Current market price (cost to enter this position).
        fees: Trading fee as fraction (default: config.ESTIMATED_FEE_PCT).
        slippage: Estimated slippage (default: config.ESTIMATED_SLIPPAGE_PCT).
        spread_cost: Current bid-ask spread from market data.

    Returns:
        dict with gross_ev, estimated_costs, net_ev, ev_per_dollar
    """
    if fees is None:
        fees = config.ESTIMATED_FEE_PCT
    if slippage is None:
        slippage = config.ESTIMATED_SLIPPAGE_PCT

    # Binary outcome: win pays 1.0, lose pays 0.0
    payout = 1.0 - market_price
    gross_ev = (model_prob * payout) - ((1.0 - model_prob) * market_price)

    estimated_costs = fees + slippage + (spread_cost / 2.0)
    net_ev = gross_ev - estimated_costs
    ev_per_dollar = net_ev / max(market_price, 1e-9)

    return {
        "gross_ev": gross_ev,
        "estimated_costs": estimated_costs,
        "net_ev": net_ev,
        "ev_per_dollar": ev_per_dollar,
    }


def calculate_kelly_size(
    model_prob: float,
    market_price: float,
    kelly_fraction: float = None,
) -> float:
    """Calculate fractional Kelly position size for binary outcome.

    Args:
        model_prob: Our model's estimated probability.
        market_price: Cost per share (market price of the outcome token).
        kelly_fraction: Fraction of full Kelly to use (default: config.KELLY_FRACTION).

    Returns:
        Suggested position size as fraction of capital (0.0 to KELLY_MAX_SIZE_PCT).
    """
    if kelly_fraction is None:
        kelly_fraction = config.KELLY_FRACTION

    if market_price <= 0 or market_price >= 1.0 or model_prob <= 0:
        return 0.0

    # Kelly for binary: f* = (p * b - q) / b
    # where b = odds = payout/cost = (1-price)/price, p = model_prob, q = 1-p
    odds = (1.0 - market_price) / market_price
    if odds <= 0:
        return 0.0

    kelly_pct = (model_prob * odds - (1.0 - model_prob)) / odds
    suggested = max(kelly_pct, 0.0) * kelly_fraction

    return min(suggested, config.KELLY_MAX_SIZE_PCT)


def recommend_size(confidence: str, strategy: str) -> float:
    """Return recommended position size as fraction of capital.

    Sizes come from config.py tiers. Sniper strategy has its own max.
    """
    if strategy == "sniper":
        if confidence == "HIGH":
            return config.SNIPER_MAX_SIZE
        elif confidence == "MEDIUM":
            return config.SIZE_TIER_MEDIUM
        else:
            return config.SIZE_TIER_LOW

    # latency_arb and others
    if confidence == "HIGH":
        return config.SIZE_TIER_HIGH
    elif confidence == "MEDIUM":
        return config.SIZE_TIER_MEDIUM
    else:
        return config.SIZE_TIER_LOW
