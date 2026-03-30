"""Durable append-only result log for redeem outcomes.

Multiple result lines per queue_id are expected (one per attempt).
The worker uses the latest result line per queue_id to determine
current state.  Terminal statuses mean "stop processing."
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Terminal statuses — worker stops processing these queue items.
TERMINAL_STATUSES = frozenset({
    "CLOSED_WIN",
    "CLOSED_LOSS",
    "CLOSED_EXTERNAL",
    "FAILED_MANUAL",
    "DRY_RUN_WIN",
    "DRY_RUN_LOSS",
})

# Non-terminal — worker will re-process on next cycle.
NON_TERMINAL_STATUSES = frozenset({
    "NOT_RESOLVED",
    "RESOLVED_NO_WINNER",
    "TX_FAILED",
    "SKIPPED_REDEEMER_UNAVAILABLE",
})


@dataclass
class RedeemResult:
    """A single result entry appended to the result log."""

    result_id: str = field(default_factory=lambda: f"rr-{uuid.uuid4().hex[:8]}")
    queue_id: str = ""
    position_id: str = ""
    condition_id: str = ""
    token_id: str = ""
    market_id: str = ""
    direction: str = ""
    market_type: str = ""
    entry_price: float = 0.0
    num_shares: float = 0.0

    outcome: str = ""              # "WIN", "LOSS", "UNKNOWN"
    winning_token_id: str = ""
    resolution_source: str = ""    # "gamma_by_id", "gamma_by_id_list", "clob", "none"

    redeem_attempted: bool = False
    redeem_success: bool = False
    tx_hash: str = ""
    gas_used: Optional[int] = None

    status: str = ""               # One of TERMINAL_STATUSES or NON_TERMINAL_STATUSES
    retry_count: int = 0
    error: Optional[str] = None
    terminal_reason: Optional[str] = None

    checked_at: Optional[float] = None
    submitted_at: Optional[float] = None
    confirmed_at: Optional[float] = None
    result_written_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _parse_result_line(line: str) -> Optional[RedeemResult]:
    """Parse a single JSONL line into a RedeemResult, or None on failure."""
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return RedeemResult(
        result_id=d.get("result_id", f"rr-{uuid.uuid4().hex[:8]}"),
        queue_id=d.get("queue_id", ""),
        position_id=d.get("position_id", ""),
        condition_id=d.get("condition_id", ""),
        token_id=d.get("token_id", ""),
        market_id=d.get("market_id", ""),
        direction=d.get("direction", ""),
        market_type=d.get("market_type", ""),
        entry_price=float(d.get("entry_price", 0.0)),
        num_shares=float(d.get("num_shares", 0.0)),
        outcome=d.get("outcome", ""),
        winning_token_id=d.get("winning_token_id", ""),
        resolution_source=d.get("resolution_source", ""),
        redeem_attempted=bool(d.get("redeem_attempted", False)),
        redeem_success=bool(d.get("redeem_success", False)),
        tx_hash=d.get("tx_hash", ""),
        gas_used=d.get("gas_used"),
        status=d.get("status", ""),
        retry_count=int(d.get("retry_count", 0)),
        error=d.get("error"),
        terminal_reason=d.get("terminal_reason"),
        checked_at=d.get("checked_at"),
        submitted_at=d.get("submitted_at"),
        confirmed_at=d.get("confirmed_at"),
        result_written_at=float(d.get("result_written_at", 0.0)),
    )


class RedeemResultLog:
    """Append-only JSONL result log for redeem outcomes."""

    def __init__(self, path: str = "data/redeem_results.jsonl"):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, result: RedeemResult) -> None:
        """Append a result entry to the log."""
        result.result_written_at = time.time()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(result.to_dict(), default=str) + "\n")

    def load_all(self) -> list[RedeemResult]:
        """Load all valid result entries.  Malformed lines are skipped."""
        if not self._path.exists():
            return []
        results: list[RedeemResult] = []
        with open(self._path) as f:
            for lineno, line in enumerate(f, 1):
                parsed = _parse_result_line(line)
                if parsed is None:
                    if line.strip():
                        import sys
                        print(
                            f"[WARN] redeem_results: skipping malformed line {lineno} "
                            f"in {self._path}",
                            file=sys.stderr,
                        )
                    continue
                results.append(parsed)
        return results

    def get_latest_by_queue_id(self) -> dict[str, RedeemResult]:
        """Return a dict of queue_id -> latest RedeemResult.

        "Latest" is the last line in the file for each queue_id.
        """
        all_results = self.load_all()
        latest: dict[str, RedeemResult] = {}
        for r in all_results:
            if r.queue_id:
                latest[r.queue_id] = r
        return latest

    def count_retries(self, queue_id: str) -> int:
        """Count how many TX_FAILED results exist for a queue_id."""
        count = 0
        for r in self.load_all():
            if r.queue_id == queue_id and r.status == "TX_FAILED":
                count += 1
        return count
