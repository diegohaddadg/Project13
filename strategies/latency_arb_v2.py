"""Latency arbitrage v2 refinement layer.

Applies price-quality gating, adaptive disagreement, and conflict-aware
overlap penalties on top of the existing v1 latency_arb signal.

This module is a **post-filter**: v1 produces a candidate signal, then v2
decides APPROVE / REDUCE / REJECT based on entry-price quality, disagreement
relative to price, and open-position conflict state.

When LATENCY_ARB_V2_ENABLED is False, this module is never called and v1
behavior is unchanged.
"""

from __future__ import annotations

from copy import copy
from typing import Optional

from models.trade_signal import TradeSignal
import config


# ---------------------------------------------------------------------------
# Price-quality zone classification
# ---------------------------------------------------------------------------

def _classify_price_zone(entry_price: float) -> str:
    """Classify the purchased-side price into quality zones.

    Zone A: <= 0.52  — favorable (near midpoint, cheap)
    Zone B: 0.52-0.62 — acceptable but not ideal
    Zone C: 0.62-0.72 — expensive
    Zone D: > 0.72   — extremely expensive
    """
    if entry_price <= config.V2_PRICE_ZONE_A_MAX:
        return "A"
    if entry_price <= config.V2_PRICE_ZONE_B_MAX:
        return "B"
    if entry_price <= config.V2_PRICE_ZONE_C_MAX:
        return "C"
    return "D"


def _price_quality_score(entry_price: float) -> float:
    """Continuous 0-1 score: 1.0 = best (cheap), 0.0 = worst (expensive).

    Linear interpolation from 0.35 (score=1) to 0.80 (score=0).
    """
    low = config.V2_PRICE_SCORE_BEST
    high = config.V2_PRICE_SCORE_WORST
    if entry_price <= low:
        return 1.0
    if entry_price >= high:
        return 0.0
    return (high - entry_price) / (high - low)


# ---------------------------------------------------------------------------
# Adaptive disagreement thresholds
# ---------------------------------------------------------------------------

def _adaptive_min_disagreement(zone: str) -> float:
    """Minimum disagreement required by price zone.

    Favorable prices tolerate weaker disagreement.
    Expensive prices demand stronger disagreement.
    """
    thresholds = {
        "A": config.V2_DISAGREE_MIN_ZONE_A,
        "B": config.V2_DISAGREE_MIN_ZONE_B,
        "C": config.V2_DISAGREE_MIN_ZONE_C,
        "D": config.V2_DISAGREE_MIN_ZONE_D,
    }
    return thresholds.get(zone, config.LATENCY_ARB_MIN_DISAGREEMENT)


# ---------------------------------------------------------------------------
# Overlap / conflict scoring
# ---------------------------------------------------------------------------

def _compute_overlap_penalty(
    signal: TradeSignal,
    open_positions: list[dict],
) -> dict:
    """Compute overlap/conflict penalty for a new entry.

    open_positions: list of dicts with keys:
        market_id, market_type, direction

    Returns dict with:
        open_count: int — total open positions
        same_market: int — same market_id
        same_direction: int — same market + same direction
        opposite_direction: int — same market + opposite direction
        penalty: float — 0.0 (no penalty) to 1.0 (full penalty)
        reason: str
    """
    if not open_positions:
        return {
            "open_count": 0, "same_market": 0,
            "same_direction": 0, "opposite_direction": 0,
            "penalty": 0.0, "reason": "no_overlap",
        }

    open_count = len(open_positions)
    same_market = sum(
        1 for p in open_positions if p.get("market_id") == signal.market_id
    )
    same_direction = sum(
        1 for p in open_positions
        if p.get("market_id") == signal.market_id
        and p.get("direction") == signal.direction
    )
    opposite_direction = same_market - same_direction

    penalty = 0.0
    reasons = []

    # Opposite-direction conflict: significant penalty
    if opposite_direction > 0:
        penalty += config.V2_CONFLICT_OPPOSITE_PENALTY * opposite_direction
        reasons.append(f"opposite_direction={opposite_direction}")

    # High concurrency: incremental penalty per open position beyond threshold
    if open_count > config.V2_OVERLAP_HIGH_THRESHOLD:
        excess = open_count - config.V2_OVERLAP_HIGH_THRESHOLD
        penalty += config.V2_OVERLAP_PER_EXCESS_PENALTY * excess
        reasons.append(f"high_concurrency={open_count}")

    # Same-market stacking: mild penalty
    if same_direction > 0:
        penalty += config.V2_OVERLAP_SAME_DIR_PENALTY * same_direction
        reasons.append(f"same_dir_stack={same_direction}")

    penalty = min(penalty, 1.0)
    reason = "; ".join(reasons) if reasons else "no_overlap"

    return {
        "open_count": open_count,
        "same_market": same_market,
        "same_direction": same_direction,
        "opposite_direction": opposite_direction,
        "penalty": penalty,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Composite quality score
# ---------------------------------------------------------------------------

def _composite_quality(
    price_score: float,
    disagreement: float,
    min_disagreement: float,
    urgency_pass: bool,
    freshness_pass: bool,
    overlap_penalty: float,
    net_ev: float,
    direction: str = "UP",
) -> float:
    """Compute composite quality score 0-1 for the entry.

    Higher is better. Used to decide approve/reduce/reject.
    v2.1: DOWN trades receive a mild quality penalty to reflect
    observed weaker performance on bearish entries.
    """
    # Disagreement surplus: how much above the adaptive minimum
    disagree_surplus = max(0.0, disagreement - min_disagreement)
    disagree_score = min(1.0, disagree_surplus / config.V2_DISAGREE_SURPLUS_NORMALIZER)

    urgency_score = 1.0 if urgency_pass else config.V2_URGENCY_FAIL_SCORE
    freshness_score = 1.0 if freshness_pass else config.V2_FRESHNESS_FAIL_SCORE

    # EV contribution (normalized)
    ev_score = min(1.0, net_ev / config.V2_EV_NORMALIZER)

    # Weighted composite
    raw = (
        config.V2_WEIGHT_PRICE * price_score
        + config.V2_WEIGHT_DISAGREEMENT * disagree_score
        + config.V2_WEIGHT_URGENCY * urgency_score
        + config.V2_WEIGHT_FRESHNESS * freshness_score
        + config.V2_WEIGHT_EV * ev_score
    )

    # v2.1: directional penalty for DOWN trades
    if direction == "DOWN":
        raw = max(0.0, raw - config.V2_1_DOWN_QUALITY_PENALTY)

    # Apply overlap penalty as a multiplier
    adjusted = raw * (1.0 - overlap_penalty)

    return max(0.0, min(1.0, adjusted))


# ---------------------------------------------------------------------------
# Main refinement entry point
# ---------------------------------------------------------------------------

def refine(
    signal: TradeSignal,
    open_positions: Optional[list[dict]] = None,
) -> dict:
    """Apply v2 refinement to a v1 latency_arb signal.

    Args:
        signal: The v1 TradeSignal candidate.
        open_positions: List of dicts describing open positions. Each dict
            should have: market_id, market_type, direction.

    Returns:
        dict with:
            decision: "APPROVE" | "REDUCE" | "REJECT"
            signal: TradeSignal (possibly with adjusted size) or None
            reason: str — human-readable explanation
            v2_diagnostics: dict — compact diagnostics for tracing
    """
    if signal.strategy != "latency_arb":
        return {"decision": "APPROVE", "signal": signal, "reason": "not_latency_arb",
                "v2_diagnostics": {}}

    # --- Extract entry price for the purchased side ---
    entry_price = signal.market_probability  # market price of the side being bought
    disagreement = abs(signal.model_probability - signal.market_probability)

    # --- Price zone & score ---
    zone = _classify_price_zone(entry_price)
    price_score = _price_quality_score(entry_price)

    # --- Adaptive disagreement ---
    min_disagree = _adaptive_min_disagreement(zone)
    is_down = signal.direction == "DOWN"

    # v2.1: bump disagreement requirement for DOWN in expensive zones
    if is_down:
        down_bumps = {
            "B": config.V2_1_DOWN_DISAGREE_BUMP_ZONE_B,
            "C": config.V2_1_DOWN_DISAGREE_BUMP_ZONE_C,
            "D": config.V2_1_DOWN_DISAGREE_BUMP_ZONE_D,
        }
        min_disagree += down_bumps.get(zone, 0.0)

    # --- Overlap / conflict ---
    overlap = _compute_overlap_penalty(signal, open_positions or [])

    # --- Supporting factors from v1 metadata ---
    urgency_pass = signal.metadata.get("urgency_pass", True)
    freshness_pass = signal.metadata.get("freshness_pass", True)

    # --- Composite quality ---
    quality = _composite_quality(
        price_score=price_score,
        disagreement=disagreement,
        min_disagreement=min_disagree,
        urgency_pass=urgency_pass,
        freshness_pass=freshness_pass,
        overlap_penalty=overlap["penalty"],
        net_ev=signal.net_ev,
        direction=signal.direction,
    )

    # --- Build diagnostics (compact) ---
    diag = {
        "entry_price": entry_price,
        "price_zone": zone,
        "price_score": round(price_score, 3),
        "disagreement": round(disagreement, 4),
        "min_disagreement": round(min_disagree, 4),
        "overlap_penalty": round(overlap["penalty"], 3),
        "overlap_reason": overlap["reason"],
        "open_count": overlap["open_count"],
        "composite_quality": round(quality, 3),
        "direction": signal.direction,
        "v2_1_down_active": is_down,
    }

    # --- Decision logic ---

    # Zone D: default reject unless exceptional quality
    if zone == "D":
        if quality >= config.V2_ZONE_D_EXCEPTIONAL_THRESHOLD:
            # Exceptional override — still reduce significantly
            size_mult = config.V2_ZONE_D_EXCEPTIONAL_SIZE_MULT
            return _reduce_result(signal, size_mult, diag,
                                  f"zone_D_exceptional: quality={quality:.3f}")
        return _reject_result(diag, f"zone_D_reject: entry={entry_price:.3f} quality={quality:.3f}")

    # Zone C: require strong quality, otherwise reduce or reject
    if zone == "C":
        # v2.1: DOWN gets higher quality thresholds in zone C
        c_min_q = config.V2_ZONE_C_MIN_QUALITY + (config.V2_1_DOWN_QUALITY_BUMP_ZONE_C if is_down else 0.0)
        c_full_q = config.V2_ZONE_C_FULL_QUALITY + (config.V2_1_DOWN_QUALITY_BUMP_ZONE_C_FULL if is_down else 0.0)

        if disagreement < min_disagree:
            return _reject_result(diag,
                                  f"zone_C_weak_disagree: {disagreement:.3f} < {min_disagree:.3f}")
        if quality < c_min_q:
            return _reject_result(diag,
                                  f"zone_C_low_quality: {quality:.3f} < {c_min_q:.3f}")
        if quality < c_full_q:
            size_mult = config.V2_ZONE_C_REDUCED_SIZE_MULT
            # v2.1: DOWN gets additional size reduction for borderline zone C
            if is_down:
                size_mult *= config.V2_1_DOWN_REDUCE_SIZE_MULT
            return _reduce_result(signal, size_mult, diag,
                                  f"zone_C_reduce: quality={quality:.3f}")
        # Strong enough for full size
        diag["decision_path"] = "zone_C_approve"
        return _approve_result(signal, diag, "zone_C_approve")

    # Zone B: moderate scrutiny
    if zone == "B":
        # v2.1: DOWN gets higher quality threshold in zone B
        b_min_q = config.V2_ZONE_B_MIN_QUALITY + (config.V2_1_DOWN_QUALITY_BUMP_ZONE_B if is_down else 0.0)

        if disagreement < min_disagree:
            # Weak disagreement at acceptable price — reduce rather than reject
            size_mult = config.V2_ZONE_B_WEAK_DISAGREE_SIZE_MULT
            # v2.1: DOWN gets additional size reduction for weak disagreement
            if is_down:
                size_mult *= config.V2_1_DOWN_REDUCE_SIZE_MULT
            return _reduce_result(signal, size_mult, diag,
                                  f"zone_B_weak_disagree: {disagreement:.3f} < {min_disagree:.3f}")
        if quality < b_min_q:
            size_mult = config.V2_ZONE_B_LOW_QUALITY_SIZE_MULT
            if is_down:
                size_mult *= config.V2_1_DOWN_REDUCE_SIZE_MULT
            return _reduce_result(signal, size_mult, diag,
                                  f"zone_B_low_quality: {quality:.3f}")
        diag["decision_path"] = "zone_B_approve"
        return _approve_result(signal, diag, "zone_B_approve")

    # Zone A: favorable — most permissive
    if disagreement < min_disagree:
        # Even at good price, very weak disagreement gets a mild size cut
        # v2.1: DOWN gets a harsher cut than UP at zone A weak disagree
        size_mult = (config.V2_1_DOWN_ZONE_A_WEAK_DISAGREE_SIZE_MULT if is_down
                     else config.V2_ZONE_A_WEAK_DISAGREE_SIZE_MULT)
        return _reduce_result(signal, size_mult, diag,
                              f"zone_A_weak_disagree: {disagreement:.3f} < {min_disagree:.3f}")

    # High overlap penalty can still reduce even zone A
    if overlap["penalty"] > config.V2_OVERLAP_REDUCE_THRESHOLD:
        size_mult = max(config.V2_ZONE_A_OVERLAP_MIN_SIZE_MULT, 1.0 - overlap["penalty"])
        return _reduce_result(signal, size_mult, diag,
                              f"zone_A_overlap_reduce: penalty={overlap['penalty']:.3f}")

    diag["decision_path"] = "zone_A_approve"
    return _approve_result(signal, diag, "zone_A_approve")


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _approve_result(signal: TradeSignal, diag: dict, reason: str) -> dict:
    diag["decision"] = "APPROVE"
    diag["decision_path"] = reason
    return {"decision": "APPROVE", "signal": signal, "reason": reason,
            "v2_diagnostics": diag}


def _reduce_result(
    signal: TradeSignal, size_mult: float, diag: dict, reason: str
) -> dict:
    adjusted = copy(signal)
    adjusted.metadata = dict(signal.metadata)
    adjusted.recommended_size_pct = signal.recommended_size_pct * size_mult
    adjusted.metadata["v2_size_mult"] = size_mult
    adjusted.metadata["v2_reason"] = reason
    diag["decision"] = "REDUCE"
    diag["decision_path"] = reason
    diag["size_mult"] = size_mult
    return {"decision": "REDUCE", "signal": adjusted, "reason": reason,
            "v2_diagnostics": diag}


def _reject_result(diag: dict, reason: str) -> dict:
    diag["decision"] = "REJECT"
    diag["decision_path"] = reason
    return {"decision": "REJECT", "signal": None, "reason": reason,
            "v2_diagnostics": diag}
