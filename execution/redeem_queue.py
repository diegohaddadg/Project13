"""Durable append-only redeem queue backed by JSONL.

Queue items are write-once.  Status tracking lives in the result log,
not here.  Dedup is enforced by (position_id, condition_id) pair.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RedeemQueueItem:
    """A single redeem candidate written to the queue file."""

    queue_id: str = field(default_factory=lambda: f"rq-{uuid.uuid4().hex[:8]}")
    position_id: str = ""
    order_id: str = ""
    market_id: str = ""
    condition_id: str = ""
    token_id: str = ""
    direction: str = ""
    market_type: str = ""
    entry_price: float = 0.0
    num_shares: float = 0.0
    enqueued_at: float = field(default_factory=time.time)
    source: str = "manual"

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> Optional[str]:
        """Return an error string if invalid, else None."""
        if not self.position_id:
            return "missing position_id"
        if not self.condition_id:
            return "missing condition_id"
        cid = self.condition_id
        if cid.startswith("0x") or cid.startswith("0X"):
            cid = cid[2:]
        if len(cid) != 64:
            return f"condition_id hex length {len(cid)} != 64"
        try:
            int(cid, 16)
        except ValueError:
            return f"condition_id not valid hex: {cid[:20]}..."
        if not self.token_id:
            return "missing token_id"
        return None


def _parse_queue_line(line: str) -> Optional[RedeemQueueItem]:
    """Parse a single JSONL line into a RedeemQueueItem, or None on failure."""
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return RedeemQueueItem(
        queue_id=d.get("queue_id", f"rq-{uuid.uuid4().hex[:8]}"),
        position_id=d.get("position_id", ""),
        order_id=d.get("order_id", ""),
        market_id=d.get("market_id", ""),
        condition_id=d.get("condition_id", ""),
        token_id=d.get("token_id", ""),
        direction=d.get("direction", ""),
        market_type=d.get("market_type", ""),
        entry_price=float(d.get("entry_price", 0.0)),
        num_shares=float(d.get("num_shares", 0.0)),
        enqueued_at=float(d.get("enqueued_at", 0.0)),
        source=d.get("source", "manual"),
    )


class RedeemQueue:
    """Append-only JSONL queue for redeem candidates."""

    def __init__(self, path: str = "data/redeem_queue.jsonl"):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def enqueue(self, item: RedeemQueueItem) -> tuple[bool, str]:
        """Append item to queue.  Returns (success, message).

        Rejects if validation fails or if (position_id, condition_id)
        already exists in the queue.
        """
        err = item.validate()
        if err:
            return False, f"validation failed: {err}"

        # Dedup check
        existing = self.load_all()
        for ex in existing:
            if (ex.position_id == item.position_id
                    and ex.condition_id == item.condition_id):
                return False, (
                    f"duplicate: position_id={item.position_id} "
                    f"condition_id={item.condition_id[:16]}... "
                    f"already queued as {ex.queue_id}"
                )

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(item.to_dict(), default=str) + "\n")
        return True, f"enqueued {item.queue_id}"

    def load_all(self) -> list[RedeemQueueItem]:
        """Load all valid queue items.  Malformed lines are skipped."""
        if not self._path.exists():
            return []
        items: list[RedeemQueueItem] = []
        with open(self._path) as f:
            for lineno, line in enumerate(f, 1):
                parsed = _parse_queue_line(line)
                if parsed is None:
                    if line.strip():
                        import sys
                        print(
                            f"[WARN] redeem_queue: skipping malformed line {lineno} "
                            f"in {self._path}",
                            file=sys.stderr,
                        )
                    continue
                items.append(parsed)
        return items
