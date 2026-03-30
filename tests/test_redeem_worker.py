"""Tests for execution/redeem_worker.py — isolated redeem worker logic.

All tests use mocked resolution and mocked redeemer.
No PositionManager, OrderManager, or RiskManager is imported or used.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from execution.redeem_queue import RedeemQueue, RedeemQueueItem
from execution.redeem_result import RedeemResultLog, TERMINAL_STATUSES
from execution.redeem_worker import RedeemWorker


def _make_item(**overrides) -> RedeemQueueItem:
    defaults = dict(
        position_id="pos-001",
        order_id="ord-001",
        market_id="12345",
        condition_id="0x" + "ab" * 32,
        token_id="tok_up",
        direction="UP",
        market_type="btc-5min",
        entry_price=0.42,
        num_shares=10.0,
    )
    defaults.update(overrides)
    return RedeemQueueItem(**defaults)


class _WorkerTestBase(unittest.TestCase):
    """Base class that sets up temp queue/result files and a worker."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._queue_path = os.path.join(self._tmpdir, "queue.jsonl")
        self._result_path = os.path.join(self._tmpdir, "results.jsonl")
        self.queue = RedeemQueue(self._queue_path)
        self.results = RedeemResultLog(self._result_path)

    def tearDown(self):
        for f in [self._queue_path, self._result_path]:
            if os.path.exists(f):
                os.unlink(f)
        os.rmdir(self._tmpdir)

    def _make_worker(self, redeemer=None, dry_run=True, max_retries=5):
        return RedeemWorker(
            queue=self.queue,
            results=self.results,
            redeemer=redeemer,
            clob_client=None,
            dry_run=dry_run,
            max_retries=max_retries,
        )


class TestNotResolvedPath(_WorkerTestBase):
    """Market not resolved → NOT_RESOLVED result."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_not_resolved_writes_result(self, mock_check):
        mock_check.return_value = {"resolved": False, "resolution_source": "gamma_by_id"}

        item = _make_item()
        self.queue.enqueue(item)

        worker = self._make_worker()
        summary = worker.run_once()

        self.assertEqual(summary["not_resolved"], 1)
        self.assertEqual(summary["processed"], 1)

        results = self.results.load_all()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "NOT_RESOLVED")
        self.assertEqual(results[0].queue_id, item.queue_id)

    @patch("execution.redeem_worker.check_market_resolved")
    def test_api_returns_none(self, mock_check):
        mock_check.return_value = None

        self.queue.enqueue(_make_item())
        worker = self._make_worker()
        summary = worker.run_once()

        self.assertEqual(summary["not_resolved"], 1)
        results = self.results.load_all()
        self.assertEqual(results[0].status, "NOT_RESOLVED")


class TestLossPath(_WorkerTestBase):
    """Position lost → CLOSED_LOSS result (terminal)."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_loss_writes_terminal_result(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_down",  # Different from item's tok_up
            "resolution_source": "gamma_by_id",
        }

        item = _make_item(token_id="tok_up")
        self.queue.enqueue(item)

        worker = self._make_worker()
        summary = worker.run_once()

        self.assertEqual(summary["losses"], 1)
        results = self.results.load_all()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "CLOSED_LOSS")
        self.assertTrue(results[0].is_terminal)
        self.assertEqual(results[0].outcome, "LOSS")

    @patch("execution.redeem_worker.check_market_resolved")
    def test_loss_is_skipped_on_second_run(self, mock_check):
        """Terminal results are not reprocessed."""
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_down",
            "resolution_source": "gamma_by_id",
        }

        self.queue.enqueue(_make_item(token_id="tok_up"))
        worker = self._make_worker()

        summary1 = worker.run_once()
        self.assertEqual(summary1["losses"], 1)

        summary2 = worker.run_once()
        self.assertEqual(summary2["skipped_terminal"], 1)
        self.assertEqual(summary2["processed"], 0)


class TestDryRunWinPath(_WorkerTestBase):
    """Position won, dry-run → DRY_RUN_WIN result (terminal, no tx)."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_dry_run_win(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_up",
            "resolution_source": "gamma_by_id",
        }

        item = _make_item(token_id="tok_up")
        self.queue.enqueue(item)

        worker = self._make_worker(dry_run=True)
        summary = worker.run_once()

        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["dry_run"], True)

        results = self.results.load_all()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "DRY_RUN_WIN")
        self.assertTrue(results[0].is_terminal)
        self.assertFalse(results[0].redeem_attempted)
        self.assertEqual(results[0].outcome, "WIN")


class TestTxFailedRetryPath(_WorkerTestBase):
    """On-chain tx fails → TX_FAILED, retries, then FAILED_MANUAL."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_tx_failed_then_manual(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_up",
            "resolution_source": "gamma_by_id",
        }

        mock_redeemer = MagicMock()
        mock_redeemer.is_ready = True
        mock_redeemer.redeem.return_value = {
            "success": False,
            "tx_hash": "0xfailed",
            "error": "reverted",
            "gas_used": None,
        }

        item = _make_item(token_id="tok_up")
        self.queue.enqueue(item)

        worker = self._make_worker(redeemer=mock_redeemer, dry_run=False, max_retries=3)

        # Run 1: first failure
        summary1 = worker.run_once()
        self.assertEqual(summary1["tx_failed"], 1)
        results1 = self.results.load_all()
        self.assertEqual(results1[-1].status, "TX_FAILED")
        self.assertEqual(results1[-1].retry_count, 1)

        # Run 2: second failure (bypass cooldown by patching time)
        with patch("execution.redeem_result.time.time", return_value=9999990000.0):
            with patch("execution.redeem_worker.time.time", return_value=9999990000.0):
                summary2 = worker.run_once()
        self.assertEqual(summary2["tx_failed"], 1)
        results2 = self.results.load_all()
        self.assertEqual(results2[-1].retry_count, 2)

        # Run 3: third failure → FAILED_MANUAL (must be later than run 2 to pass cooldown)
        with patch("execution.redeem_result.time.time", return_value=9999999999.0):
            with patch("execution.redeem_worker.time.time", return_value=9999999999.0):
                summary3 = worker.run_once()
        self.assertEqual(summary3["failed_manual"], 1)
        results3 = self.results.load_all()
        self.assertEqual(results3[-1].status, "FAILED_MANUAL")
        self.assertTrue(results3[-1].is_terminal)

    @patch("execution.redeem_worker.check_market_resolved")
    def test_tx_success(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_up",
            "resolution_source": "gamma_by_id",
        }

        mock_redeemer = MagicMock()
        mock_redeemer.is_ready = True
        mock_redeemer.redeem.return_value = {
            "success": True,
            "tx_hash": "0xabc123",
            "error": None,
            "gas_used": 180000,
        }

        item = _make_item(token_id="tok_up")
        self.queue.enqueue(item)

        worker = self._make_worker(redeemer=mock_redeemer, dry_run=False)
        summary = worker.run_once()

        self.assertEqual(summary["tx_confirmed"], 1)
        results = self.results.load_all()
        self.assertEqual(results[-1].status, "CLOSED_WIN")
        self.assertTrue(results[-1].is_terminal)
        self.assertEqual(results[-1].tx_hash, "0xabc123")
        self.assertEqual(results[-1].gas_used, 180000)
        self.assertTrue(results[-1].redeem_attempted)
        self.assertTrue(results[-1].redeem_success)


class TestRedeemerUnavailable(_WorkerTestBase):
    """Redeemer not ready → SKIPPED_REDEEMER_UNAVAILABLE."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_redeemer_none(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_up",
            "resolution_source": "gamma_by_id",
        }

        self.queue.enqueue(_make_item(token_id="tok_up"))
        worker = self._make_worker(redeemer=None, dry_run=False)
        summary = worker.run_once()

        results = self.results.load_all()
        self.assertEqual(results[-1].status, "SKIPPED_REDEEMER_UNAVAILABLE")
        self.assertFalse(results[-1].is_terminal)

    @patch("execution.redeem_worker.check_market_resolved")
    def test_redeemer_not_ready(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "tok_up",
            "resolution_source": "gamma_by_id",
        }

        mock_redeemer = MagicMock()
        mock_redeemer.is_ready = False

        self.queue.enqueue(_make_item(token_id="tok_up"))
        worker = self._make_worker(redeemer=mock_redeemer, dry_run=False)
        worker.run_once()

        results = self.results.load_all()
        self.assertEqual(results[-1].status, "SKIPPED_REDEEMER_UNAVAILABLE")


class TestResolvedNoWinner(_WorkerTestBase):
    """Market resolved but winner unknown → RESOLVED_NO_WINNER."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_resolved_no_winner(self, mock_check):
        mock_check.return_value = {
            "resolved": True,
            "winning_token_id": "",
            "resolution_source": "gamma_by_id",
        }

        self.queue.enqueue(_make_item())
        worker = self._make_worker()
        summary = worker.run_once()

        self.assertEqual(summary["not_resolved"], 1)
        results = self.results.load_all()
        self.assertEqual(results[-1].status, "RESOLVED_NO_WINNER")


class TestMultipleItems(_WorkerTestBase):
    """Worker processes multiple items in one run."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_mixed_outcomes(self, mock_check):
        # Item 1: not resolved
        item1 = _make_item(position_id="pos-001", token_id="tok_up",
                           condition_id="0x" + "a1" * 32)
        # Item 2: loss
        item2 = _make_item(position_id="pos-002", token_id="tok_up",
                           condition_id="0x" + "b2" * 32)
        # Item 3: dry-run win
        item3 = _make_item(position_id="pos-003", token_id="tok_up",
                           condition_id="0x" + "c3" * 32)

        self.queue.enqueue(item1)
        self.queue.enqueue(item2)
        self.queue.enqueue(item3)

        def side_effect(condition_id, market_id="", clob_client=None):
            if "a1" in condition_id:
                return {"resolved": False, "resolution_source": "gamma_by_id"}
            elif "b2" in condition_id:
                return {"resolved": True, "winning_token_id": "tok_down",
                        "resolution_source": "gamma_by_id"}
            elif "c3" in condition_id:
                return {"resolved": True, "winning_token_id": "tok_up",
                        "resolution_source": "gamma_by_id"}
            return None

        mock_check.side_effect = side_effect

        worker = self._make_worker(dry_run=True)
        summary = worker.run_once()

        self.assertEqual(summary["processed"], 3)
        self.assertEqual(summary["not_resolved"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["wins"], 1)

        results = self.results.load_all()
        statuses = [r.status for r in results]
        self.assertIn("NOT_RESOLVED", statuses)
        self.assertIn("CLOSED_LOSS", statuses)
        self.assertIn("DRY_RUN_WIN", statuses)


class TestMalformedResultsHandled(_WorkerTestBase):
    """Worker handles malformed result lines gracefully."""

    @patch("execution.redeem_worker.check_market_resolved")
    def test_malformed_result_line_skipped(self, mock_check):
        mock_check.return_value = {"resolved": False, "resolution_source": "gamma_by_id"}

        item = _make_item()
        self.queue.enqueue(item)

        # Write garbage to results file
        os.makedirs(os.path.dirname(self._result_path), exist_ok=True)
        with open(self._result_path, "w") as f:
            f.write("THIS IS NOT JSON\n")

        worker = self._make_worker()
        # Should not crash
        summary = worker.run_once()
        self.assertEqual(summary["processed"], 1)


if __name__ == "__main__":
    unittest.main()
