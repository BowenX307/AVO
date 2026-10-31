"""Tests for when the gate waits for the model review (avo/gate.py).

    uv run python -m unittest discover tests
"""

import time
import unittest
from unittest import mock

from avo import gate

CLICK = [{"type": "click", "x": 100, "y": 100}]
OK_REVIEW = {"element_under_click": "Submit button", "hits_intended_target": True, "moves_task_forward": True,
             "ambiguous": False, "candidates": []}
AMBIGUOUS = OK_REVIEW | {"ambiguous": True, "candidates": [
    {"label": "Alex Chen", "x": 100, "y": 100}, {"label": "Alex Rivera", "x": 100, "y": 130}]}


class Seg:
    def __init__(self, conf, on=True):
        self.confidence, self.on, self.box = conf, on, (80, 90, 120, 110)

    def contains(self, x, y):
        return self.on


def assess(sam_conf, rev=OK_REVIEW, slow=0.4, **kw):
    def review(*_a, **_k):
        time.sleep(slow)
        return rev

    with mock.patch.object(gate, "review", review), mock.patch.object(
        gate, "segment", lambda *_a, **_k: ([Seg(sam_conf)], "")
    ):
        return gate.assess(None, "task", [], [], b"", CLICK, "note", (1280, 832), **kw)


class GateSkipTest(unittest.TestCase):
    def test_confident_sam_doesnt_wait_for_the_review(self):
        a = assess(0.92)
        self.assertLess(a.seconds, 0.3)
        self.assertFalse(a.flagged)
        self.assertIsNotNone(a.late_review)
        self.assertEqual(a.late_review.result(timeout=2), OK_REVIEW)  # still lands, for the log
        self.assertTrue(a.to_log()["review_skipped"])

    def test_unsure_sam_waits_and_the_review_decides(self):
        a = assess(0.71, rev=AMBIGUOUS)  # the "which Alex?" clicks scored 0.59-0.71
        self.assertGreaterEqual(a.seconds, 0.4)
        self.assertTrue(a.flagged)
        self.assertEqual(len(a.candidates), 2)
        self.assertIsNone(a.late_review)

    def test_boxed_tasks_always_wait(self):
        a = assess(0.95, rev=AMBIGUOUS, skip_ok=False)
        self.assertTrue(a.flagged)
        self.assertIsNone(a.late_review)

    def test_a_repeat_always_waits(self):
        with mock.patch.object(gate, "repeats", lambda *_: True):
            a = assess(0.95)
        self.assertTrue(a.flagged)
        self.assertIsNone(a.late_review)


if __name__ == "__main__":
    unittest.main()
