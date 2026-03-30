"""Apply terminal redeem results to bot state on startup.

Reads data/redeem_results.jsonl once, applies eligible terminal results
to PositionManager / OrderManager / RiskManager, and records applied
result_ids to data/redeem_applied.jsonl to prevent double-apply.

This module is called exactly once during bot startup, before the main loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from utils.logger import get_logger

if TYPE_CHECKING:
    from execution.position_manager import PositionManager
    from execution.order_manager import OrderManager
    from risk.risk_manager import RiskManager

log = get_logger("redeem_startup")

# Terminal statuses that should be applied to PM/OM/RM accounting.
_APPLY_STATUSES = frozenset({"CLOSED_WIN", "CLOSED_LOSS", "CLOSED_EXTERNAL"})

# Terminal statuses that are logged but NOT applied to accounting.
_LOG_ONLY_STATUSES = frozenset({"DRY_RUN_WIN", "DRY_RUN_LOSS", "FAILED_MANUAL"})

_RESULTS_PATH = "data/redeem_results.jsonl"
_APPLIED_PATH = "data/redeem_applied.jsonl"


def apply_startup_results(
    pm: "PositionManager",
    om: "OrderManager",
    rm: "RiskManager",
    results_path: str = _RESULTS_PATH,
    applied_path: str = _APPLIED_PATH,
) -> dict:
    """Scan terminal results and apply to bot state.  Returns summary dict."""
    summary = {
        "results_scanned": 0,
        "applied_win": 0,
        "applied_loss": 0,
        "skipped_already_applied": 0,
        "skipped_dry_run": 0,
        "skipped_failed_manual": 0,
        "skipped_non_terminal": 0,
        "skipped_position_not_found": 0,
        "skipped_malformed": 0,
        "errors": 0,
    }

    rpath = Path(results_path)
    if not rpath.exists():
        log.info("[REDEEM-STARTUP] No results file found — nothing to apply")
        return summary

    # Load already-applied result_ids
    applied_ids = _load_applied_ids(applied_path)

    # Parse results
    results = []
    with open(rpath) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                log.warning(f"[REDEEM-STARTUP] Malformed line {lineno} — skipped")
                summary["skipped_malformed"] += 1
                continue
            if not isinstance(d, dict):
                summary["skipped_malformed"] += 1
                continue
            results.append(d)

    summary["results_scanned"] = len(results)

    if not results:
        log.info("[REDEEM-STARTUP] Results file empty — nothing to apply")
        return summary

    # Get latest result per queue_id (same logic as RedeemResultLog)
    latest: dict[str, dict] = {}
    for r in results:
        qid = r.get("queue_id", "")
        if qid:
            latest[qid] = r

    for qid, r in latest.items():
        result_id = r.get("result_id", "")
        status = r.get("status", "")
        position_id = r.get("position_id", "")

        # Skip already applied
        if result_id in applied_ids:
            summary["skipped_already_applied"] += 1
            continue

        # Skip non-terminal / non-actionable
        if status in _LOG_ONLY_STATUSES:
            if status == "FAILED_MANUAL":
                log.warning(
                    f"[REDEEM-STARTUP] FAILED_MANUAL result_id={result_id} "
                    f"pos={position_id} — requires manual action, not applied"
                )
                summary["skipped_failed_manual"] += 1
            else:
                log.info(
                    f"[REDEEM-STARTUP] {status} result_id={result_id} "
                    f"pos={position_id} — dry run, not applied"
                )
                summary["skipped_dry_run"] += 1
            continue

        if status not in _APPLY_STATUSES:
            summary["skipped_non_terminal"] += 1
            continue

        # Apply to PM/OM/RM
        try:
            applied = _apply_result(r, pm, om, rm, summary)
            if applied:
                _record_applied(applied_path, result_id)
                applied_ids.add(result_id)
        except Exception as e:
            log.error(
                f"[REDEEM-STARTUP] Error applying result_id={result_id} "
                f"pos={position_id}: {e}"
            )
            summary["errors"] += 1

    log.warning(
        f"[REDEEM-STARTUP] Complete: scanned={summary['results_scanned']} "
        f"applied_win={summary['applied_win']} applied_loss={summary['applied_loss']} "
        f"skipped_already={summary['skipped_already_applied']} "
        f"skipped_dry_run={summary['skipped_dry_run']} "
        f"skipped_manual={summary['skipped_failed_manual']} "
        f"not_found={summary['skipped_position_not_found']} "
        f"errors={summary['errors']}"
    )
    return summary


def _apply_result(
    r: dict,
    pm: "PositionManager",
    om: "OrderManager",
    rm: "RiskManager",
    summary: dict,
) -> bool:
    """Apply a single terminal result to PM/OM/RM.  Returns True if applied."""
    status = r.get("status", "")
    position_id = r.get("position_id", "")
    result_id = r.get("result_id", "")

    # Determine resolution price
    if status in ("CLOSED_WIN", "CLOSED_EXTERNAL"):
        resolution_price = 1.0
        summary_key = "applied_win"
    elif status == "CLOSED_LOSS":
        resolution_price = 0.0
        summary_key = "applied_loss"
    else:
        return False

    # Find position in PM open positions
    pos = None
    for p in pm.get_open_positions():
        if p.position_id == position_id:
            pos = p
            break

    if pos is None:
        log.info(
            f"[REDEEM-STARTUP] Position {position_id} not in open positions "
            f"— may already be closed, skipping result_id={result_id}"
        )
        summary["skipped_position_not_found"] += 1
        return False

    # Close position
    resolved_pos = pm.close_position(position_id, resolution_price)
    if resolved_pos is None:
        log.warning(
            f"[REDEEM-STARTUP] close_position returned None for {position_id}"
        )
        summary["skipped_position_not_found"] += 1
        return False

    # Mark metadata
    resolved_pos.metadata["redeemed"] = True
    resolved_pos.metadata["redeem_startup_applied"] = True
    resolved_pos.metadata["redeem_result_id"] = result_id
    if r.get("tx_hash"):
        resolved_pos.metadata["redeem_tx_hash"] = r["tx_hash"]

    # Sync order PnL
    if resolved_pos.order_id and resolved_pos.pnl is not None:
        om.sync_order_pnl_from_position(resolved_pos.order_id, resolved_pos.pnl)

    # Record trade result for risk manager
    rm.record_trade_result(resolved_pos)

    summary[summary_key] += 1

    pnl_str = f" PnL={resolved_pos.pnl:+.2f}" if resolved_pos.pnl is not None else ""
    log.warning(
        f"[REDEEM-STARTUP] Applied {status}: pos={position_id} "
        f"{pos.direction} {pos.market_type} "
        f"{pos.num_shares:.1f}sh{pnl_str} result_id={result_id} "
        f"capital_after=${pm.get_available_capital():.2f}"
    )
    return True


def _load_applied_ids(applied_path: str) -> set[str]:
    """Load the set of already-applied result_ids."""
    p = Path(applied_path)
    if not p.exists():
        return set()
    ids = set()
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                rid = d.get("result_id", "")
                if rid:
                    ids.add(rid)
            except (json.JSONDecodeError, ValueError):
                continue
    return ids


def _record_applied(applied_path: str, result_id: str) -> None:
    """Append a result_id to the applied ledger."""
    import time
    p = Path(applied_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"result_id": result_id, "applied_at": time.time()}
    with open(p, "a") as f:
        f.write(json.dumps(entry) + "\n")
