"""Tests for execution/redeem_startup.py — startup result application."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from models.order import Order
from execution.position_manager import PositionManager
from execution.redeem_startup import apply_startup_results


def _make_order(**overrides) -> Order:
    defaults = dict(
        order_id="ord-001",
        signal_id="sig-001",
        market_id="mkt-001",
        market_type="btc-5min",
        direction="UP",
        side="BUY",
        token_id="tok_winner",
        price=0.55,
        size_usdc=27.50,
        num_shares=50.0,
        order_type="LIMIT",
        status="FILLED",
        execution_mode="live",
        metadata={"condition_id": "0x" + "ab" * 32},
    )
    defaults.update(overrides)
    return Order(**defaults)


def _write_result(path, result_dict):
    """Append a result dict to a JSONL file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(result_dict) + "\n")


def _make_result(status, position_id="", queue_id="qid-001", **overrides):
    """Create a result dict."""
    r = {
        "result_id": f"rr-{status.lower()[:6]}",
        "queue_id": queue_id,
        "position_id": position_id,
        "condition_id": "0x" + "ab" * 32,
        "token_id": "tok_winner",
        "market_id": "mkt-001",
        "direction": "UP",
        "market_type": "btc-5min",
        "entry_price": 0.55,
        "num_shares": 50.0,
        "outcome": "WIN" if "WIN" in status else "LOSS",
        "winning_token_id": "tok_winner",
        "resolution_source": "gamma_by_id",
        "redeem_attempted": status == "CLOSED_WIN",
        "redeem_success": status == "CLOSED_WIN",
        "tx_hash": "0xtx123" if status == "CLOSED_WIN" else "",
        "gas_used": 150000 if status == "CLOSED_WIN" else None,
        "status": status,
        "retry_count": 0,
        "error": None,
        "terminal_reason": None,
        "result_written_at": time.time(),
    }
    r.update(overrides)
    return r


class _MockOM:
    """Minimal mock for OrderManager."""

    def __init__(self):
        self.synced = []

    def sync_order_pnl_from_position(self, order_id, pnl):
        self.synced.append((order_id, pnl))


class _MockRM:
    """Minimal mock for RiskManager."""

    def __init__(self):
        self.recorded = []

    def record_trade_result(self, pos):
        self.recorded.append(pos)


class TestApplyStartupResults(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._results_path = os.path.join(self._tmpdir, "results.jsonl")
        self._applied_path = os.path.join(self._tmpdir, "applied.jsonl")

    def tearDown(self):
        for f in [self._results_path, self._applied_path]:
            if os.path.exists(f):
                os.unlink(f)
        os.rmdir(self._tmpdir)

    def _apply(self, pm, om=None, rm=None):
        return apply_startup_results(
            pm,
            om or _MockOM(),
            rm or _MockRM(),
            results_path=self._results_path,
            applied_path=self._applied_path,
        )

    def test_missing_results_file(self):
        """No results file → nothing applied, no error."""
        pm = PositionManager()
        summary = self._apply(pm)
        self.assertEqual(summary["results_scanned"], 0)

    def test_closed_win_applied(self):
        """CLOSED_WIN closes position with resolution_price=1.0."""
        pm = PositionManager()
        om = _MockOM()
        rm = _MockRM()

        order = _make_order()
        pos = pm.open_position(order)
        pos.metadata["execution_mode"] = "live"

        result = _make_result("CLOSED_WIN", position_id=pos.position_id)
        _write_result(self._results_path, result)

        summary = apply_startup_results(
            pm, om, rm,
            results_path=self._results_path,
            applied_path=self._applied_path,
        )

        self.assertEqual(summary["applied_win"], 1)
        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertEqual(len(closed), 1)
        self.assertGreater(closed[0].pnl, 0)
        self.assertTrue(closed[0].metadata.get("redeemed"))
        self.assertTrue(closed[0].metadata.get("redeem_startup_applied"))
        self.assertEqual(closed[0].metadata.get("redeem_tx_hash"), "0xtx123")
        self.assertEqual(len(om.synced), 1)
        self.assertEqual(len(rm.recorded), 1)

    def test_closed_loss_applied(self):
        """CLOSED_LOSS closes position with resolution_price=0.0."""
        pm = PositionManager()
        om = _MockOM()
        rm = _MockRM()

        order = _make_order()
        pos = pm.open_position(order)

        result = _make_result("CLOSED_LOSS", position_id=pos.position_id)
        _write_result(self._results_path, result)

        summary = apply_startup_results(
            pm, om, rm,
            results_path=self._results_path,
            applied_path=self._applied_path,
        )

        self.assertEqual(summary["applied_loss"], 1)
        self.assertEqual(pm.count_open_positions(), 0)
        closed = pm.get_closed_positions()
        self.assertLess(closed[0].pnl, 0)

    def test_closed_external_applied(self):
        """CLOSED_EXTERNAL is treated like a win."""
        pm = PositionManager()
        order = _make_order()
        pos = pm.open_position(order)

        result = _make_result("CLOSED_EXTERNAL", position_id=pos.position_id)
        _write_result(self._results_path, result)

        summary = self._apply(pm)
        self.assertEqual(summary["applied_win"], 1)
        self.assertEqual(pm.count_open_positions(), 0)

    def test_dry_run_win_not_applied(self):
        """DRY_RUN_WIN must NOT close the position."""
        pm = PositionManager()
        order = _make_order()
        pos = pm.open_position(order)

        result = _make_result("DRY_RUN_WIN", position_id=pos.position_id)
        _write_result(self._results_path, result)

        summary = self._apply(pm)
        self.assertEqual(summary["skipped_dry_run"], 1)
        self.assertEqual(summary["applied_win"], 0)
        # Position must still be open
        self.assertEqual(pm.count_open_positions(), 1)

    def test_failed_manual_not_applied(self):
        """FAILED_MANUAL is logged but not applied."""
        pm = PositionManager()
        order = _make_order()
        pos = pm.open_position(order)

        result = _make_result("FAILED_MANUAL", position_id=pos.position_id)
        _write_result(self._results_path, result)

        summary = self._apply(pm)
        self.assertEqual(summary["skipped_failed_manual"], 1)
        self.assertEqual(pm.count_open_positions(), 1)

    def test_non_terminal_skipped(self):
        """Non-terminal statuses like TX_FAILED are skipped."""
        pm = PositionManager()
        order = _make_order()
        pos = pm.open_position(order)

        result = _make_result("TX_FAILED", position_id=pos.position_id)
        _write_result(self._results_path, result)

        summary = self._apply(pm)
        self.assertEqual(summary["skipped_non_terminal"], 1)
        self.assertEqual(pm.count_open_positions(), 1)

    def test_double_apply_prevented(self):
        """Same result is not applied twice across restarts."""
        pm = PositionManager()
        om = _MockOM()
        rm = _MockRM()

        order = _make_order()
        pos = pm.open_position(order)

        result = _make_result("CLOSED_WIN", position_id=pos.position_id)
        _write_result(self._results_path, result)

        # First apply
        summary1 = apply_startup_results(
            pm, om, rm,
            results_path=self._results_path,
            applied_path=self._applied_path,
        )
        self.assertEqual(summary1["applied_win"], 1)

        # Simulate restart — new PM with a new position (same id won't exist)
        # But the applied ledger should prevent re-application
        pm2 = PositionManager()
        order2 = _make_order()
        pos2 = pm2.open_position(order2)

        summary2 = apply_startup_results(
            pm2, _MockOM(), _MockRM(),
            results_path=self._results_path,
            applied_path=self._applied_path,
        )
        self.assertEqual(summary2["skipped_already_applied"], 1)
        self.assertEqual(summary2["applied_win"], 0)
        # Position still open because result was already applied
        self.assertEqual(pm2.count_open_positions(), 1)

    def test_position_not_found_skipped(self):
        """If position_id is not in open positions, skip gracefully."""
        pm = PositionManager()  # No positions

        result = _make_result("CLOSED_WIN", position_id="nonexistent-pos")
        _write_result(self._results_path, result)

        summary = self._apply(pm)
        self.assertEqual(summary["skipped_position_not_found"], 1)
        self.assertEqual(summary["applied_win"], 0)

    def test_malformed_line_skipped(self):
        """Malformed JSONL lines are skipped without crashing."""
        pm = PositionManager()
        order = _make_order()
        pos = pm.open_position(order)

        os.makedirs(os.path.dirname(self._results_path), exist_ok=True)
        with open(self._results_path, "w") as f:
            f.write("NOT VALID JSON\n")
            f.write(json.dumps(_make_result("CLOSED_WIN", position_id=pos.position_id)) + "\n")

        summary = self._apply(pm)
        self.assertEqual(summary["skipped_malformed"], 1)
        self.assertEqual(summary["applied_win"], 1)

    def test_latest_result_per_queue_id(self):
        """Multiple results for same queue_id — only latest is considered."""
        pm = PositionManager()
        order = _make_order()
        pos = pm.open_position(order)

        # First: TX_FAILED, then: CLOSED_WIN
        r1 = _make_result("TX_FAILED", position_id=pos.position_id,
                          result_id="rr-fail1")
        r2 = _make_result("CLOSED_WIN", position_id=pos.position_id,
                          result_id="rr-win1")
        _write_result(self._results_path, r1)
        _write_result(self._results_path, r2)

        summary = self._apply(pm)
        # Latest is CLOSED_WIN → applied
        self.assertEqual(summary["applied_win"], 1)
        self.assertEqual(pm.count_open_positions(), 0)


if __name__ == "__main__":
    unittest.main()
