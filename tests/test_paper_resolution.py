"""Tests for paper position resolution using real market outcomes."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch, MagicMock

from models.position import Position
from execution.fill_tracker import FillTracker, _to_bool, _extract_winner_from_gamma
from execution.position_manager import PositionManager
import config


def _make_position(
    direction="UP",
    market_id="12345",
    market_type="btc-5min",
    entry_price=0.55,
    num_shares=14.0,
    token_id="tok_up_abc",
    condition_id="0xcond123",
) -> Position:
    return Position(
        market_id=market_id,
        market_type=market_type,
        direction=direction,
        entry_price=entry_price,
        num_shares=num_shares,
        entry_timestamp=time.time(),
        status="OPEN",
        metadata={
            "execution_mode": "paper",
            "strategy": "latency_arb",
            "condition_id": condition_id,
            "token_id": token_id,
        },
    )


def _gamma_response(
    closed=True,
    active=False,
    clob_token_ids=None,
    outcome_prices=None,
    tokens=None,
):
    """Build a mock Gamma API response."""
    resp = {"id": "12345", "closed": closed, "active": active}
    if clob_token_ids is not None:
        resp["clobTokenIds"] = clob_token_ids
    if outcome_prices is not None:
        resp["outcomePrices"] = outcome_prices
    if tokens is not None:
        resp["tokens"] = tokens
    return resp


class TestPaperResolutionRealMarket(unittest.TestCase):
    """Paper positions must resolve from real Polymarket market outcomes."""

    def setUp(self):
        self._pm = PositionManager()
        self._agg = MagicMock()
        self._ft = FillTracker(self._pm, self._agg)

    def _add_position(self, **kwargs) -> Position:
        pos = _make_position(**kwargs)
        self._pm._open_positions.append(pos)
        return pos

    # --- Core resolution tests ---

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_bought_up_market_resolves_up_profitable(self, mock_urlopen):
        """UP position + market resolves UP = WIN."""
        pos = self._add_position(
            direction="UP", token_id="tok_up", entry_price=0.55
        )

        resp_data = _gamma_response(
            closed=True,
            tokens=[
                {"token_id": "tok_up", "winner": 1.0},
                {"token_id": "tok_down", "winner": 0.0},
            ],
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].resolution_price, 1.0)
        self.assertGreater(closed[0].pnl, 0)
        expected_pnl = (1.0 - 0.55) * 14.0
        self.assertAlmostEqual(closed[0].pnl, expected_pnl, places=2)

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_bought_up_market_resolves_down_full_loss(self, mock_urlopen):
        """UP position + market resolves DOWN = LOSS."""
        pos = self._add_position(
            direction="UP", token_id="tok_up", entry_price=0.55
        )

        resp_data = _gamma_response(
            closed=True,
            tokens=[
                {"token_id": "tok_up", "winner": 0.0},
                {"token_id": "tok_down", "winner": 1.0},
            ],
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].resolution_price, 0.0)
        self.assertLess(closed[0].pnl, 0)

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_bought_down_market_resolves_down_profitable(self, mock_urlopen):
        """DOWN position + market resolves DOWN = WIN."""
        pos = self._add_position(
            direction="DOWN", token_id="tok_down", entry_price=0.45
        )

        resp_data = _gamma_response(
            closed=True,
            tokens=[
                {"token_id": "tok_up", "winner": 0.0},
                {"token_id": "tok_down", "winner": 1.0},
            ],
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].resolution_price, 1.0)
        self.assertGreater(closed[0].pnl, 0)
        expected_pnl = (1.0 - 0.45) * 14.0
        self.assertAlmostEqual(closed[0].pnl, expected_pnl, places=2)

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_bought_down_market_resolves_up_full_loss(self, mock_urlopen):
        """DOWN position + market resolves UP = LOSS."""
        pos = self._add_position(
            direction="DOWN", token_id="tok_down", entry_price=0.45
        )

        resp_data = _gamma_response(
            closed=True,
            tokens=[
                {"token_id": "tok_up", "winner": 1.0},
                {"token_id": "tok_down", "winner": 0.0},
            ],
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].resolution_price, 0.0)
        self.assertLess(closed[0].pnl, 0)

    # --- Unresolved market tests ---

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_unresolved_market_position_stays_open(self, mock_urlopen):
        """Market not yet resolved → position remains open."""
        pos = self._add_position(direction="UP")

        resp_data = _gamma_response(closed=False, active=True)
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 0)
        self.assertEqual(len(self._pm.get_open_positions()), 1)

    # --- Ambiguous/missing data tests ---

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_api_failure_position_stays_open(self, mock_urlopen):
        """API error → position remains open, not resolved."""
        pos = self._add_position(direction="UP")
        mock_urlopen.side_effect = Exception("Connection timeout")

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 0)
        self.assertEqual(len(self._pm.get_open_positions()), 1)

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_resolved_but_no_winner_position_stays_open(self, mock_urlopen):
        """Market resolved but winner unknown → position stays open."""
        pos = self._add_position(direction="UP")

        # Resolved but no tokens, no outcomePrices
        resp_data = _gamma_response(closed=True)
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 0)
        self.assertEqual(len(self._pm.get_open_positions()), 1)

    # --- Fallback direction matching via outcomePrices ---

    @patch("execution.fill_tracker.urllib.request.urlopen")
    def test_outcome_prices_fallback_up_wins(self, mock_urlopen):
        """When tokens list absent, use outcomePrices to determine winner."""
        pos = self._add_position(
            direction="UP", token_id="",  # no token_id
        )

        resp_data = _gamma_response(
            closed=True,
            clob_token_ids='["tok_up", "tok_down"]',
            outcome_prices='["1.0", "0.0"]',  # UP wins
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = __import__("json").dumps(resp_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        closed = self._ft.check_resolutions(None, None)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].resolution_price, 1.0)


class TestHelpers(unittest.TestCase):

    def test_to_bool_variants(self):
        self.assertTrue(_to_bool(True))
        self.assertTrue(_to_bool("true"))
        self.assertTrue(_to_bool("1"))
        self.assertFalse(_to_bool(False))
        self.assertFalse(_to_bool("false"))
        self.assertFalse(_to_bool(None))

    def test_extract_winner_up(self):
        resp = {
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.99", "0.01"]',
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "tok_up")

    def test_extract_winner_down(self):
        resp = {
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.01", "0.99"]',
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "tok_down")

    def test_extract_winner_no_clear_winner(self):
        resp = {
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.50", "0.50"]',
        }
        self.assertEqual(_extract_winner_from_gamma(resp), "")


if __name__ == "__main__":
    unittest.main()
