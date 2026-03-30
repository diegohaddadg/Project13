"""Tests for execution/redeem_queue.py — append-only JSONL queue with dedup."""

import json
import os
import tempfile
import unittest

from execution.redeem_queue import RedeemQueue, RedeemQueueItem


def _make_item(**overrides) -> RedeemQueueItem:
    defaults = dict(
        position_id="pos-001",
        order_id="ord-001",
        market_id="12345",
        condition_id="0x" + "ab" * 32,
        token_id="tok-" + "1" * 40,
        direction="UP",
        market_type="btc-5min",
        entry_price=0.42,
        num_shares=10.0,
    )
    defaults.update(overrides)
    return RedeemQueueItem(**defaults)


class TestRedeemQueueItem(unittest.TestCase):

    def test_valid_item_passes_validation(self):
        item = _make_item()
        self.assertIsNone(item.validate())

    def test_missing_position_id(self):
        item = _make_item(position_id="")
        self.assertIn("missing position_id", item.validate())

    def test_missing_condition_id(self):
        item = _make_item(condition_id="")
        self.assertIn("missing condition_id", item.validate())

    def test_invalid_hex_condition_id(self):
        item = _make_item(condition_id="0xNOTHEX" + "0" * 56)
        err = item.validate()
        self.assertIsNotNone(err)
        self.assertIn("hex", err.lower())

    def test_wrong_length_condition_id(self):
        item = _make_item(condition_id="0x" + "ab" * 16)  # 32 hex chars, not 64
        err = item.validate()
        self.assertIsNotNone(err)
        self.assertIn("64", err)

    def test_missing_token_id(self):
        item = _make_item(token_id="")
        self.assertIn("missing token_id", item.validate())

    def test_to_dict_roundtrip(self):
        item = _make_item()
        d = item.to_dict()
        self.assertEqual(d["position_id"], "pos-001")
        self.assertEqual(d["entry_price"], 0.42)


class TestRedeemQueue(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "queue.jsonl")
        self.queue = RedeemQueue(self._path)

    def tearDown(self):
        if os.path.exists(self._path):
            os.unlink(self._path)
        os.rmdir(self._tmpdir)

    def test_enqueue_and_load(self):
        item = _make_item()
        ok, msg = self.queue.enqueue(item)
        self.assertTrue(ok, msg)

        loaded = self.queue.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].position_id, "pos-001")
        self.assertEqual(loaded[0].condition_id, item.condition_id)

    def test_dedup_rejects_same_position_and_condition(self):
        item1 = _make_item()
        ok1, _ = self.queue.enqueue(item1)
        self.assertTrue(ok1)

        item2 = _make_item()  # Same position_id + condition_id
        ok2, msg2 = self.queue.enqueue(item2)
        self.assertFalse(ok2)
        self.assertIn("duplicate", msg2)

    def test_different_position_id_allowed(self):
        item1 = _make_item(position_id="pos-001")
        item2 = _make_item(position_id="pos-002")

        ok1, _ = self.queue.enqueue(item1)
        ok2, _ = self.queue.enqueue(item2)
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(len(self.queue.load_all()), 2)

    def test_validation_failure_rejected(self):
        item = _make_item(condition_id="")
        ok, msg = self.queue.enqueue(item)
        self.assertFalse(ok)
        self.assertIn("validation failed", msg)

    def test_malformed_lines_skipped(self):
        # Write some garbage followed by a valid item
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            f.write("NOT VALID JSON\n")
            f.write("{}\n")  # Valid JSON but empty — will parse with defaults
            valid = _make_item().to_dict()
            f.write(json.dumps(valid) + "\n")

        loaded = self.queue.load_all()
        # Line 1: malformed JSON → skipped
        # Line 2: empty dict → parses but has empty fields
        # Line 3: valid item
        self.assertEqual(len(loaded), 2)  # lines 2 and 3

    def test_empty_file(self):
        loaded = self.queue.load_all()
        self.assertEqual(loaded, [])

    def test_append_only(self):
        """Verify file grows by appending, not rewriting."""
        item1 = _make_item(position_id="pos-001")
        item2 = _make_item(position_id="pos-002")

        self.queue.enqueue(item1)
        size_after_1 = os.path.getsize(self._path)

        self.queue.enqueue(item2)
        size_after_2 = os.path.getsize(self._path)

        self.assertGreater(size_after_2, size_after_1)

        # Read raw lines — should be exactly 2
        with open(self._path) as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
