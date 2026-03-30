"""Tests for scripts/enqueue_redeem.py — trade log path resolution."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.enqueue_redeem import _resolve_trade_log


class TestResolveTradeLog(unittest.TestCase):

    def test_explicit_path_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b"{}\n")
            path = f.name
        try:
            result = _resolve_trade_log(path)
            self.assertEqual(result, path)
        finally:
            os.unlink(path)

    def test_explicit_path_missing(self):
        result = _resolve_trade_log("/nonexistent/trade_log.jsonl")
        self.assertIsNone(result)

    def test_default_search_finds_logs_dir(self):
        """When no explicit path, finds logs/trade_log.jsonl first."""
        # This test relies on the actual repo having logs/trade_log.jsonl
        # which exists in this repo.
        result = _resolve_trade_log(None)
        self.assertIsNotNone(result)
        self.assertIn("trade_log.jsonl", result)

    def test_default_search_none_found(self):
        """When default paths don't exist, returns None."""
        import scripts.enqueue_redeem as mod
        original = mod._DEFAULT_TRADE_LOG_SEARCH
        mod._DEFAULT_TRADE_LOG_SEARCH = [
            "/nonexistent_a/trade_log.jsonl",
            "/nonexistent_b/trade_log.jsonl",
        ]
        try:
            result = _resolve_trade_log(None)
            self.assertIsNone(result)
        finally:
            mod._DEFAULT_TRADE_LOG_SEARCH = original


if __name__ == "__main__":
    unittest.main()
