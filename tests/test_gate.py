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


def assess(sam_conf, rev=OK_REVIEW, slow=0.2, place=None, repeat=False, near=None, seen=None):
    def review(*_a, **k):
        if seen is not None:
            seen.append(k.get("controls"))
        time.sleep(slow)
        if rev is None:
            raise TimeoutError("review timed out")
        return rev

    with mock.patch.object(gate, "review", review), mock.patch.object(
        gate, "segment", lambda *_a, **_k: ([Seg(sam_conf)], "")
    ), mock.patch.object(gate, "repeats", lambda *_: repeat):
        return gate.assess(None, "task", [], [], b"", CLICK, "note", (1280, 832), where=lambda x, y: place, near=near)


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

    def test_identical_options_arent_a_question(self):
        same = OK_REVIEW | {"ambiguous": True, "candidates": [
            {"label": "2:00 PM", "x": 100, "y": 100}, {"label": "2:00 PM", "x": 300, "y": 100}]}
        a = assess(0.92, rev=same)
        self.assertFalse(a.flagged)
        self.assertEqual(a.candidates, [])

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


class NamedFixTest(unittest.TestCase):
    """11/01: "Empty space above Reply button" with Reply right there asked anyway."""

    def setUp(self):
        from types import SimpleNamespace
        from avo.snap import Target

        self.driver = SimpleNamespace(to_screen=lambda x, y: (x, y))
        self.reply = Target("AXButton", "Reply", (600, 402, 60, 24))
        self.forward = Target("AXButton", "Forward", (670, 402, 70, 24))

    def fix(self, under, near, hits=False, **kw):
        a = gate.Assessment(flagged=True, target=(629, 396), review={"element_under_click": under, "hits_intended_target": hits}, **kw)
        with mock.patch.object(gate, "nearby", lambda x, y, r: near):
            return gate.named_fix(a, self.driver)

    def test_clicks_the_control_the_review_names(self):
        (x, y), t = self.fix("Empty space above Reply button", [self.reply, self.forward])
        self.assertEqual((t.label, x, y), ("Reply", 630, 414))

    def test_asks_when_nothing_or_two_match(self):
        self.assertIsNone(self.fix("Empty space above the toolbar", [self.reply, self.forward]))
        self.assertIsNone(self.fix("Gap between Reply and Forward buttons", [self.reply, self.forward]))
        self.assertIsNone(self.fix("Empty space", [self.reply]))

    def test_never_for_a_choice_or_a_hit(self):
        self.assertIsNone(self.fix("Reply button", [self.reply], hits=True))
        self.assertIsNone(self.fix("Empty space above Reply button", [self.reply], candidates=[{"label": "x"}, {"label": "y"}]))


class ChromeTabTest(unittest.TestCase):
    """11/01: a hit-test on a Chrome tab returns the empty strip over it, not the tab."""

    def test_a_tab_under_an_empty_strip_counts_as_a_control(self):
        from avo import snap

        tab = {"AXRole": "AXRadioButton", "AXTitle": "Campus Mail — Inbox", "frame": (154, 116, 192, 41)}
        group = {"AXRole": "AXTabGroup", "frame": (154, 116, 1060, 41), "AXChildren": [tab]}
        strip = {"AXRole": "AXGroup", "frame": (154, 116, 1060, 41)}
        toolbar = {"AXRole": "AXGroup", "frame": (40, 116, 1376, 41), "AXChildren": [strip, group]}
        strip["AXParent"] = toolbar

        def attr(el, name):
            return el.get(name)

        with mock.patch.object(snap.AX, "AXUIElementCopyElementAtPosition", lambda *a: (0, strip)), \
                mock.patch.object(snap, "_attr", attr), mock.patch.object(snap, "_frame", lambda el: el.get("frame")):
            self.assertIsNone(snap.clickable_at(215, 130))  # what the hit-test alone says
            t = snap.clickable_at(215, 130, deep=True)
            self.assertEqual((t.role, t.label), ("AXRadioButton", "Campus Mail — Inbox"))
            self.assertEqual(snap.location(215, 130), "on")
            self.assertIsNone(snap.snap(215, 130))  # not moved onto the address bar


class TableGapTest(unittest.TestCase):
    def test_an_empty_table_cell_is_blank_space(self):
        # 11/02, room demo: between two "2:00 PM" buttons the hit-test lands on the <td>.
        from avo import snap

        cell = {"AXRole": "AXCell"}
        with mock.patch.object(snap.AX, "AXUIElementCopyElementAtPosition", lambda *a: (0, cell)), \
                mock.patch.object(snap, "_attr", lambda el, name: el.get(name)):
            self.assertTrue(snap.on_blank(848, 323))
        labelled = {"AXRole": "AXCell", "AXTitle": "Room 204"}
        with mock.patch.object(snap.AX, "AXUIElementCopyElementAtPosition", lambda *a: (0, labelled)), \
                mock.patch.object(snap, "_attr", lambda el, name: el.get(name)):
            self.assertFalse(snap.on_blank(848, 323))  # a cell with text is content, not a gap


class MeantControlTest(unittest.TestCase):
    """11/02: the model kept aiming at the gap between two "2:00 PM" buttons."""

    SLOTS = [{"label": "Room 204, 2:00 PM", "x": 852, "y": 305}, {"label": "Room 210, 2:00 PM", "x": 852, "y": 352}]

    def test_a_stray_click_goes_to_the_control_the_review_names(self):
        seen = []
        a = assess(0.3, rev=MISS | {"meant_control": 1}, place="gap", near=lambda x, y: self.SLOTS, seen=seen)
        self.assertEqual(seen, [self.SLOTS])  # the review was shown the controls
        self.assertFalse(a.flagged)
        self.assertEqual(a.meant["label"], "Room 204, 2:00 PM")

    def test_no_pick_still_asks(self):
        a = assess(0.3, rev=MISS | {"meant_control": 0}, place="gap", near=lambda x, y: self.SLOTS)
        self.assertTrue(a.flagged)
        self.assertIsNone(a.meant)

    def test_on_a_control_nothing_is_listed(self):
        seen = []
        a = assess(0.9, rev=OK_REVIEW | {"meant_control": 0}, place="on", near=lambda x, y: self.SLOTS, seen=seen)
        self.assertEqual(seen, [None])
        self.assertIsNone(a.meant)

    def test_a_question_of_which_one_wins(self):
        a = assess(0.3, rev=AMBIGUOUS | {"meant_control": 1}, place="gap", near=lambda x, y: self.SLOTS)
        self.assertTrue(a.flagged)
        self.assertIsNone(a.meant)


if __name__ == "__main__":
    unittest.main()
