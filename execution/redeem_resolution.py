"""Standalone market resolution check for the redeem worker.

This is a deliberate copy of the resolution logic from live_reconciler.py,
extracted as pure functions with no class state and no imports of PM/OM/RM.

Will be consolidated with live_reconciler.py in a later phase.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional


def check_market_resolved(
    condition_id: str,
    market_id: str = "",
    clob_client=None,
) -> Optional[dict]:
    """Check if a Polymarket market has resolved.

    Returns dict with keys:
        resolved: bool
        winning_token_id: str (may be empty if resolved but winner unknown)
        resolution_source: str
    Or None if all API sources fail.
    """
    resp = None
    source = "none"

    # Primary: Gamma /markets/{id}
    if market_id:
        try:
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Project13/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
                if isinstance(data, dict) and data.get("id"):
                    resp = data
                    source = "gamma_by_id"
        except Exception:
            pass

    # Fallback: Gamma list with id= param
    if resp is None and market_id:
        try:
            url = f"https://gamma-api.polymarket.com/markets?id={market_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Project13/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
                if isinstance(data, list) and len(data) == 1:
                    resp = data[0]
                    source = "gamma_by_id_list"
        except Exception:
            pass

    # Fallback: CLOB get_market
    if resp is None and clob_client is not None:
        try:
            resp = clob_client.get_market(condition_id)
            if resp is not None:
                source = "clob"
        except Exception:
            pass

    if resp is None or not isinstance(resp, dict):
        return None

    # Parse closed/resolved
    raw_closed = resp.get("closed")
    raw_resolved = resp.get("resolved")
    raw_active = resp.get("active")
    raw_end = resp.get("endDate", "")

    resolved = _to_bool(raw_closed) or _to_bool(raw_resolved)

    # Heuristic: active=false + endDate in the past
    if not resolved and not _to_bool(raw_active) and raw_end:
        try:
            from datetime import datetime, timezone
            end_dt = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            if end_dt < datetime.now(timezone.utc):
                resolved = True
        except Exception:
            pass

    # Determine winning token
    winning_token_id = ""
    if resolved:
        # Method 1: tokens list with winner field (CLOB format)
        tokens = resp.get("tokens", [])
        for t in tokens:
            if isinstance(t, dict) and _safe_float(t.get("winner", 0)) == 1.0:
                winning_token_id = t.get("token_id", "")
                break

        # Method 2: clobTokenIds + outcomePrices (Gamma format)
        if not winning_token_id:
            winning_token_id = _extract_winner_from_gamma(resp)

    return {
        "resolved": resolved,
        "winning_token_id": winning_token_id,
        "resolution_source": source,
    }


def _extract_winner_from_gamma(resp: dict) -> str:
    """Extract winning token from Gamma API response using clobTokenIds + outcomePrices."""
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


def _to_bool(val) -> bool:
    """Convert various truthy representations to bool."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return False


def _safe_float(val) -> float:
    """Safely convert a value to float."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
