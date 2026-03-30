"""Tests for execution/redeem_resolution.py — standalone resolution check."""

import unittest
from unittest.mock import patch, MagicMock
import json

from execution.redeem_resolution import (
    check_market_resolved,
    _extract_winner_from_gamma,
    _to_bool,
    _safe_float,
)


class TestToBool(unittest.TestCase):

    def test_true_values(self):
        for val in [True, 1, 1.0, "true", "True", "TRUE", "1", "yes"]:
            self.assertTrue(_to_bool(val), f"Expected True for {val!r}")

    def test_false_values(self):
        for val in [False, 0, 0.0, "false", "False", "0", "no", "", None]:
            self.assertFalse(_to_bool(val), f"Expected False for {val!r}")


class TestSafeFloat(unittest.TestCase):

    def test_normal(self):
        self.assertEqual(_safe_float(1.5), 1.5)
        self.assertEqual(_safe_float("3.14"), 3.14)

    def test_none(self):
        self.assertEqual(_safe_float(None), 0.0)

    def test_garbage(self):
        self.assertEqual(_safe_float("abc"), 0.0)


class TestExtractWinnerFromGamma(unittest.TestCase):

    def test_first_token_wins(self):
        resp = {
            "clobTokenIds": json.dumps(["token_UP", "token_DOWN"]),
            "outcomePrices": json.dumps(["1.0", "0.0"]),
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "token_UP")

    def test_second_token_wins(self):
        resp = {
            "clobTokenIds": json.dumps(["token_UP", "token_DOWN"]),
            "outcomePrices": json.dumps(["0.0", "1.0"]),
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "token_DOWN")

    def test_no_clear_winner(self):
        resp = {
            "clobTokenIds": json.dumps(["token_UP", "token_DOWN"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "")

    def test_missing_clob_token_ids(self):
        resp = {"outcomePrices": json.dumps(["1.0", "0.0"])}
        self.assertEqual(_extract_winner_from_gamma(resp), "")

    def test_clob_token_ids_as_list(self):
        resp = {
            "clobTokenIds": ["token_A", "token_B"],
            "outcomePrices": ["0.95", "0.05"],
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "token_A")


class TestCheckMarketResolved(unittest.TestCase):

    def _mock_gamma_response(self, data_dict):
        """Create a mock urllib response returning data_dict as JSON."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(data_dict).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("urllib.request.urlopen")
    def test_resolved_with_winner_gamma(self, mock_urlopen):
        gamma_data = {
            "id": "12345",
            "closed": True,
            "active": False,
            "clobTokenIds": json.dumps(["tok_up", "tok_down"]),
            "outcomePrices": json.dumps(["1.0", "0.0"]),
        }
        mock_urlopen.return_value = self._mock_gamma_response(gamma_data)

        result = check_market_resolved("0x" + "ab" * 32, market_id="12345")

        self.assertIsNotNone(result)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["winning_token_id"], "tok_up")
        self.assertEqual(result["resolution_source"], "gamma_by_id")

    @patch("urllib.request.urlopen")
    def test_not_resolved(self, mock_urlopen):
        gamma_data = {
            "id": "12345",
            "closed": False,
            "active": True,
        }
        mock_urlopen.return_value = self._mock_gamma_response(gamma_data)

        result = check_market_resolved("0x" + "ab" * 32, market_id="12345")

        self.assertIsNotNone(result)
        self.assertFalse(result["resolved"])

    def test_no_market_id_no_clob_returns_none(self):
        result = check_market_resolved("0x" + "ab" * 32, market_id="", clob_client=None)
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_resolved_with_tokens_list(self, mock_urlopen):
        """CLOB format: tokens list with winner field."""
        gamma_data = {
            "id": "12345",
            "closed": True,
            "tokens": [
                {"token_id": "tok_up", "winner": 1.0},
                {"token_id": "tok_down", "winner": 0.0},
            ],
        }
        mock_urlopen.return_value = self._mock_gamma_response(gamma_data)

        result = check_market_resolved("0x" + "ab" * 32, market_id="12345")

        self.assertTrue(result["resolved"])
        self.assertEqual(result["winning_token_id"], "tok_up")

    @patch("urllib.request.urlopen")
    def test_api_failure_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("timeout")

        result = check_market_resolved("0x" + "ab" * 32, market_id="12345")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
