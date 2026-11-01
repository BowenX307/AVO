"""Tests for when the gate asks (avo/gate.py).

    uv run python -m unittest discover tests
"""

import time
import unittest
from unittest import mock

from avo import gate

CLICK = [{"type": "click", "x": 100, "y": 100}]
OK_REVIEW = {"element_under_click": "Submit button", "hits_intended_target": True, "moves_task_forward": True,
             "ambiguous": False, "candidates": []}
MISS = OK_REVIEW | {"element_under_click": "gap between fields", "hits_intended_target": False,
                    "moves_task_forward": False}
NO_PROGRESS = OK_REVIEW | {"moves_task_forward": False}
AMBIGUOUS = OK_REVIEW | {"ambiguous": True, "candidates": [
    {"label": "Alex Chen", "x": 100, "y": 100}, {"label": "Alex Rivera", "x": 100, "y": 130}]}


class Seg:
    def __init__(self, conf, on=True):
        self.confidence, self.on, self.box = conf, on, (80, 90, 120, 110)

    def contains(self, x, y):
        return self.on


def assess(sam_conf, rev=OK_REVIEW, slow=0.2, place=None, repeat=False):
    def review(*_a, **_k):
        time.sleep(slow)
        if rev is None:
            raise TimeoutError("review timed out")
        return rev

    with mock.patch.object(gate, "review", review), mock.patch.object(
        gate, "segment", lambda *_a, **_k: ([Seg(sam_conf)], "")
    ), mock.patch.object(gate, "repeats", lambda *_: repeat):
        return gate.assess(None, "task", [], [], b"", CLICK, "note", (1280, 832), where=lambda x, y: place)


class GateTest(unittest.TestCase):
    def test_clear_and_not_ambiguous_goes(self):
        a = assess(0.92, rev=MISS)  # SAM sure: the review's "miss" doesn't get a say
        self.assertFalse(a.flagged)
        self.assertTrue(a.clear)

    def test_waits_for_the_review_every_time(self):
        # The "which one?" question comes from the review, so it is never skipped.
        a = assess(0.95, rev=AMBIGUOUS, slow=0.3)
        self.assertGreaterEqual(a.seconds, 0.3)
        self.assertTrue(a.flagged)
        self.assertEqual(len(a.candidates), 2)

    def test_ambiguous_asks_even_on_a_clear_target(self):
        a = assess(0.71, rev=AMBIGUOUS)  # the "which Alex?" clicks scored 0.59-0.71
        self.assertTrue(a.flagged)
        self.assertEqual([c["label"] for c in a.candidates], ["Alex Chen", "Alex Rivera"])

    def test_unsure_sam_and_the_review_says_hit(self):
        self.assertFalse(assess(0.6).flagged)

    def test_unsure_sam_and_the_review_says_miss(self):
        a = assess(0.6, rev=MISS)
        self.assertTrue(a.flagged)
        self.assertIn("gap between fields", a.reasons[0])

    def test_unsure_sam_and_no_review_asks(self):
        self.assertTrue(assess(0.6, rev=None).flagged)

    def test_a_gap_overrides_a_sure_sam(self):
        # 10/29: a click between the date and category fields scored 0.949 in SAM.
        self.assertTrue(assess(0.95, rev=MISS, place="gap").flagged)

    def test_a_control_under_the_point_overrides_an_unsure_sam(self):
        # 10/31: "gap above search field" was on the search field.
        self.assertFalse(assess(0.69, rev=MISS, place="on").flagged)

    def test_no_progress_and_repeats_dont_ask_by_themselves(self):
        a = assess(0.6, rev=NO_PROGRESS, repeat=True)
        self.assertFalse(a.flagged)
        self.assertTrue(a.to_log()["repeat"])


class RingTest(unittest.TestCase):
    def test_rings_come_from_sam_when_accessibility_finds_nothing(self):
        # 10/31, Edge: no control found, so both rings were small fixed boxes at the model's guess.
        from types import SimpleNamespace

        asked = []
        overlay = SimpleNamespace(ask=lambda **f: asked.append(f) or {"choice": "pick", "index": 0})
        driver = SimpleNamespace(to_screen=lambda x, y: (x, y), to_shot=lambda x, y: (x, y),
                                 geometry=SimpleNamespace(shot_size=(1280, 832)))
        seg = Seg(0.9)
        seg.box = (400, 420, 820, 446)  # the listing's title line
        a = gate.Assessment(flagged=True, target=(688, 361), plain=["Your task fits more than one of these."], candidates=[
            {"label": "Software Engineer Intern, Systems and Infrastructure (PhD)", "x": 540, "y": 433},
            {"label": "Software Engineer Intern, Machine Learning (PhD)", "x": 512, "y": 563}])
        with mock.patch.object(gate, "_locate", lambda *a: None), mock.patch.object(gate, "clickable_at", lambda *a: None), \
                mock.patch.object(gate, "segment", lambda *a, **k: ([seg], "")), mock.patch.object(gate.time, "sleep"):
            choice, actions, _ = gate._ask_which(overlay, driver, a, CLICK, b"png", client=object())
        rings = asked[0]["candidates"]
        self.assertEqual(rings[0]["box"], [400, 420, 820, 446])
        self.assertEqual(rings[0]["ring"], "sam")
        self.assertEqual((choice, actions[0]["x"], actions[0]["y"]), ("pick", 610, 433))  # the middle of the SAM box


if __name__ == "__main__":
    unittest.main()
