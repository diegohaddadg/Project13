"""Live tape recorder — captures the bot's signal input at each evaluation cycle.

Writes one JSONL line per snapshot to data/live_tape.jsonl.
Each line contains enough information to reconstruct signal evaluation offline.

No secrets are recorded. Market state is serialized compactly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from models.market_state import MarketState
from utils.logger import get_logger
import config

log = get_logger("tape_recorder")


def _serialize_market(state: Optional[MarketState]) -> Optional[dict]:
    """Compact serialization of MarketState for tape."""
    if state is None:
        return None
    return {
        "market_id": state.market_id,
        "condition_id": state.condition_id,
        "market_type": state.market_type,
        "strike_price": state.strike_price,
        "yes_price": state.yes_price,
        "no_price": state.no_price,
        "spread": state.spread,
        "time_remaining_seconds": state.time_remaining_seconds,
        "gamma_end_remaining_seconds": state.gamma_end_remaining_seconds,
        "time_to_window_seconds": state.time_to_window_seconds,
        "window_started": state.window_started,
        "is_signalable": state.is_signalable,
        "is_active": state.is_active,
        "timing_source": state.timing_source,
        "slug": state.slug,
        "question": state.question,
        "up_token_id": state.up_token_id[:20] if state.up_token_id else "",
        "down_token_id": state.down_token_id[:20] if state.down_token_id else "",
    }


class TapeRecorder:
    """Records signal_input snapshots to JSONL tape file."""

    def __init__(self, path: str = None, every_n: int = 1):
        self._path = Path(path or config.REPLAY_TAPE_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._every_n = max(1, every_n)
        self._tick_count = 0
        self._records = 0

    def record(self, signal_input: dict) -> None:
        """Record a signal_input snapshot if due this tick."""
        self._tick_count += 1
        if self._tick_count % self._every_n != 0:
            return

        try:
            entry = {
                "ts": signal_input.get("timestamp", time.time()),
                "spot_price": signal_input.get("spot_price"),
                "spot_source": signal_input.get("spot_source"),
                "volatility": signal_input.get("volatility"),
                "vol_source": signal_input.get("vol_source"),
                "price_source_gap": signal_input.get("price_source_gap"),
                "feed_healthy": signal_input.get("feed_healthy"),
                "market_state_5m": _serialize_market(signal_input.get("market_state_5m")),
                "market_state_15m": _serialize_market(signal_input.get("market_state_15m")),
            }

            with open(self._path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            self._records += 1

        except Exception as e:
            log.error(f"Tape write failed: {e}")

    @property
    def records_written(self) -> int:
        return self._records
