"""Isolated redeem worker — reads queue, checks resolution, submits tx, writes results.

This worker NEVER imports PositionManager, OrderManager, or RiskManager.
It NEVER touches in-memory trading state.  It communicates only through
durable JSONL files on disk.
"""

from __future__ import annotations

import time
from typing import Optional

from execution.redeem_queue import RedeemQueue, RedeemQueueItem
from execution.redeem_result import (
    RedeemResult,
    RedeemResultLog,
    TERMINAL_STATUSES,
)
from execution.redeem_resolution import check_market_resolved

# Matches config.py values but hardcoded here to avoid coupling.
# These can be overridden via constructor.
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 30.0


class RedeemWorker:
    """Process redeem queue items and write results.

    Does NOT import or call PositionManager, OrderManager, or RiskManager.
    """

    def __init__(
        self,
        queue: RedeemQueue,
        results: RedeemResultLog,
        redeemer=None,
        clob_client=None,
        dry_run: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ):
        self._queue = queue
        self._results = results
        self._redeemer = redeemer       # OnchainRedeemer instance or None
        self._clob_client = clob_client
        self._dry_run = dry_run
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

    def run_once(self) -> dict:
        """Process all pending queue items once.  Returns summary dict."""
        summary = {
            "processed": 0,
            "not_resolved": 0,
            "wins": 0,
            "losses": 0,
            "tx_submitted": 0,
            "tx_confirmed": 0,
            "tx_failed": 0,
            "failed_manual": 0,
            "skipped_terminal": 0,
            "dry_run": self._dry_run,
        }

        items = self._queue.load_all()
        latest_results = self._results.get_latest_by_queue_id()

        for item in items:
            latest = latest_results.get(item.queue_id)

            # Skip terminal items
            if latest and latest.is_terminal:
                summary["skipped_terminal"] += 1
                continue

            # Check retry-based cooldown
            if latest and latest.status == "TX_FAILED":
                retry_count = self._results.count_retries(item.queue_id)
                if retry_count >= self._max_retries:
                    self._write_failed_manual(item, retry_count)
                    summary["failed_manual"] += 1
                    summary["processed"] += 1
                    continue
                backoff = self._retry_backoff * (2 ** min(retry_count - 1, 4))
                if latest.result_written_at and (time.time() - latest.result_written_at) < backoff:
                    continue  # Still in cooldown

            self._process_item(item, summary)
            summary["processed"] += 1

        return summary

    def run_loop(self, interval: float = 30.0) -> None:
        """Run processing loop until interrupted."""
        print(f"[REDEEM-WORKER] Starting loop (interval={interval}s, dry_run={self._dry_run})")
        try:
            while True:
                summary = self.run_once()
                processed = summary["processed"]
                if processed > 0:
                    print(
                        f"[REDEEM-WORKER] Cycle complete: "
                        f"processed={processed} "
                        f"wins={summary['wins']} losses={summary['losses']} "
                        f"tx_confirmed={summary['tx_confirmed']} "
                        f"tx_failed={summary['tx_failed']} "
                        f"not_resolved={summary['not_resolved']}"
                    )
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[REDEEM-WORKER] Stopped by user.")

    def _process_item(self, item: RedeemQueueItem, summary: dict) -> None:
        """Process a single queue item through the redeem pipeline."""
        checked_at = time.time()

        # Step 1: Check resolution
        resolved_info = check_market_resolved(
            condition_id=item.condition_id,
            market_id=item.market_id,
            clob_client=self._clob_client,
        )

        if resolved_info is None or not resolved_info.get("resolved"):
            self._write_not_resolved(item, checked_at, resolved_info)
            summary["not_resolved"] += 1
            return

        winning_token = resolved_info.get("winning_token_id", "")
        resolution_source = resolved_info.get("resolution_source", "")

        if not winning_token:
            self._write_resolved_no_winner(item, checked_at, resolution_source)
            summary["not_resolved"] += 1
            return

        # Step 2: Determine win/loss
        won = (item.token_id == winning_token)

        if not won:
            self._write_loss(item, checked_at, winning_token, resolution_source)
            summary["losses"] += 1
            return

        summary["wins"] += 1

        # Step 3: Handle win
        if self._dry_run:
            self._write_dry_run_win(item, checked_at, winning_token, resolution_source)
            return

        # Step 4: Submit on-chain redemption
        if self._redeemer is None or not self._redeemer.is_ready:
            self._write_redeemer_unavailable(item, checked_at, winning_token, resolution_source)
            return

        summary["tx_submitted"] += 1
        submitted_at = time.time()

        try:
            result = self._redeemer.redeem(item.condition_id)
        except Exception as e:
            result = {"success": False, "tx_hash": None, "error": str(e), "gas_used": None}

        if result["success"]:
            self._write_win(item, checked_at, submitted_at, winning_token,
                            resolution_source, result)
            summary["tx_confirmed"] += 1
        else:
            retry_count = self._results.count_retries(item.queue_id) + 1
            if retry_count >= self._max_retries:
                self._write_failed_manual(item, retry_count, error=result.get("error"))
                summary["failed_manual"] += 1
            else:
                self._write_tx_failed(item, checked_at, submitted_at, winning_token,
                                      resolution_source, result, retry_count)
                summary["tx_failed"] += 1

    # ------------------------------------------------------------------
    # Result writers
    # ------------------------------------------------------------------

    def _base_result(self, item: RedeemQueueItem) -> RedeemResult:
        """Create a RedeemResult pre-filled from a queue item."""
        return RedeemResult(
            queue_id=item.queue_id,
            position_id=item.position_id,
            condition_id=item.condition_id,
            token_id=item.token_id,
            market_id=item.market_id,
            direction=item.direction,
            market_type=item.market_type,
            entry_price=item.entry_price,
            num_shares=item.num_shares,
        )

    def _write_not_resolved(self, item: RedeemQueueItem, checked_at: float,
                            resolved_info: Optional[dict]) -> None:
        r = self._base_result(item)
        r.status = "NOT_RESOLVED"
        r.outcome = "UNKNOWN"
        r.checked_at = checked_at
        r.resolution_source = (resolved_info or {}).get("resolution_source", "none")
        self._results.append(r)

    def _write_resolved_no_winner(self, item: RedeemQueueItem, checked_at: float,
                                  resolution_source: str) -> None:
        r = self._base_result(item)
        r.status = "RESOLVED_NO_WINNER"
        r.outcome = "UNKNOWN"
        r.checked_at = checked_at
        r.resolution_source = resolution_source
        self._results.append(r)

    def _write_loss(self, item: RedeemQueueItem, checked_at: float,
                    winning_token: str, resolution_source: str) -> None:
        r = self._base_result(item)
        r.status = "CLOSED_LOSS"
        r.outcome = "LOSS"
        r.winning_token_id = winning_token
        r.checked_at = checked_at
        r.resolution_source = resolution_source
        r.terminal_reason = "position lost"
        self._results.append(r)

    def _write_dry_run_win(self, item: RedeemQueueItem, checked_at: float,
                           winning_token: str, resolution_source: str) -> None:
        r = self._base_result(item)
        r.status = "DRY_RUN_WIN"
        r.outcome = "WIN"
        r.winning_token_id = winning_token
        r.checked_at = checked_at
        r.resolution_source = resolution_source
        r.terminal_reason = "dry run — tx not submitted"
        self._results.append(r)

    def _write_redeemer_unavailable(self, item: RedeemQueueItem, checked_at: float,
                                    winning_token: str, resolution_source: str) -> None:
        r = self._base_result(item)
        r.status = "SKIPPED_REDEEMER_UNAVAILABLE"
        r.outcome = "WIN"
        r.winning_token_id = winning_token
        r.checked_at = checked_at
        r.resolution_source = resolution_source
        r.error = "OnchainRedeemer not ready"
        self._results.append(r)

    def _write_win(self, item: RedeemQueueItem, checked_at: float, submitted_at: float,
                   winning_token: str, resolution_source: str, tx_result: dict) -> None:
        r = self._base_result(item)
        r.status = "CLOSED_WIN"
        r.outcome = "WIN"
        r.winning_token_id = winning_token
        r.checked_at = checked_at
        r.submitted_at = submitted_at
        r.confirmed_at = time.time()
        r.resolution_source = resolution_source
        r.redeem_attempted = True
        r.redeem_success = True
        r.tx_hash = tx_result.get("tx_hash", "")
        r.gas_used = tx_result.get("gas_used")
        r.terminal_reason = "redeemed on-chain"
        self._results.append(r)

    def _write_tx_failed(self, item: RedeemQueueItem, checked_at: float, submitted_at: float,
                         winning_token: str, resolution_source: str,
                         tx_result: dict, retry_count: int) -> None:
        r = self._base_result(item)
        r.status = "TX_FAILED"
        r.outcome = "WIN"
        r.winning_token_id = winning_token
        r.checked_at = checked_at
        r.submitted_at = submitted_at
        r.resolution_source = resolution_source
        r.redeem_attempted = True
        r.redeem_success = False
        r.tx_hash = tx_result.get("tx_hash", "")
        r.error = tx_result.get("error", "unknown")
        r.retry_count = retry_count
        self._results.append(r)

    def _write_failed_manual(self, item: RedeemQueueItem, retry_count: int,
                             error: Optional[str] = None) -> None:
        r = self._base_result(item)
        r.status = "FAILED_MANUAL"
        r.outcome = "WIN"
        r.retry_count = retry_count
        r.error = error or f"max retries ({self._max_retries}) exhausted"
        r.terminal_reason = "manual action required"
        self._results.append(r)
