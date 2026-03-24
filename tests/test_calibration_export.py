"""Tests for scripts/calibration_export.py (import by path)."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load_calib_module():
    root = Path(__file__).resolve().parent.parent
    path = root / "scripts" / "calibration_export.py"
    spec = importlib.util.spec_from_file_location("calibration_export", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


cal = _load_calib_module()


class TestCalibrationExport(unittest.TestCase):

    def test_dedupe_order_last_wins(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text(
                json.dumps({"order_id": "a", "status": "FILLED", "pnl": None}) + "\n"
                + json.dumps(
                    {
                        "order_id": "a",
                        "status": "FILLED",
                        "pnl": -1.0,
                        "signal_id": "s1",
                        "metadata": {"strategy": "latency_arb", "model_probability": 0.55},
                    }
                )
                + "\n"
            )
            m = cal.dedupe_orders(p)
            self.assertEqual(len(m), 1)
            self.assertEqual(m["a"]["pnl"], -1.0)

    def test_resolved_joins_trace(self):
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            trade = tdir / "trade.jsonl"
            trade.write_text(
                json.dumps(
                    {
                        "order_id": "o1",
                        "signal_id": "sig1",
                        "status": "FILLED",
                        "pnl": 2.0,
                        "fill_price": 0.5,
                        "size_usdc": 10.0,
                        "num_shares": 20.0,
                        "market_type": "btc-5min",
                        "direction": "UP",
                        "metadata": {
                            "strategy": "sniper",
                            "model_probability": 0.7,
                            "market_probability": 0.6,
                            "edge": 0.1,
                            "condition_id": "0xabc",
                        },
                    }
                )
                + "\n"
            )
            trace = tdir / "trace.jsonl"
            trace.write_text(
                json.dumps(
                    {
                        "signal_id": "sig1",
                        "net_ev": 0.05,
                        "kelly_size": 0.07,
                    }
                )
                + "\n"
            )
            orders = cal.dedupe_orders(trade)
            resolved = cal.resolved_filled_orders(orders)
            traces = cal.dedupe_signal_traces(trace)
            rows = cal.build_rows(resolved, traces)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["order_id"], "o1")
            self.assertEqual(r["net_ev"], 0.05)
            self.assertEqual(r["kelly_size"], 0.07)
            self.assertEqual(r["strategy"], "sniper")
            self.assertAlmostEqual(float(r["pnl"]), 2.0)

    def test_summary_counts(self):
        rows = [
            {
                "strategy": "a",
                "market_type": "btc-5min",
                "model_probability": 0.55,
                "pnl": 1.0,
            },
            {
                "strategy": "a",
                "market_type": "btc-15min",
                "model_probability": 0.85,
                "pnl": -2.0,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "s.txt"
            text = cal.write_summary(rows, sp)
            self.assertIn("Resolved trades: 2", text)
            self.assertIn("Total PnL", text)
            self.assertTrue(sp.read_text())


if __name__ == "__main__":
    unittest.main()
