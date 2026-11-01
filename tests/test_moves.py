"""Tests for AVO's own check of a drag (avo/moves.py).

    uv run python -m unittest discover tests
"""

import unittest
from unittest import mock

from avo import moves

ICONS = {"20261016.pdf": (1368, 208, 64, 64), "math": (1261, 200, 64, 64), "CS341": (1156, 74, 64, 64)}


def drag(*pts):
    return {"type": "drag", "path": [{"x": x, "y": y} for x, y in pts]}


def same(x, y):
    return x, y


class ZeroLengthTest(unittest.TestCase):
    def test_a_drag_to_the_same_point_goes_nowhere(self):
        # 11/01: the model's first drag, twice: (1214,217) -> (1214,217).
        self.assertTrue(moves.zero_length([drag((1214, 217), (1214, 217))]))
        self.assertTrue(moves.zero_length([drag((1214, 217), (1216, 219))]))
        self.assertFalse(moves.zero_length([drag((1211, 214), (1111, 215))]))
        self.assertFalse(moves.zero_length([{"type": "click", "x": 1, "y": 2}]))


class CoverTest(unittest.TestCase):
    def test_the_effect_area_takes_in_both_ends_of_a_drag(self):
        area = (400, 100, 1000, 700)  # the Terminal window in front
        self.assertEqual(moves.cover(area, [drag((1211, 214), (1111, 215))]), (400, 100, 1271, 700))
        self.assertEqual(moves.cover(area, [{"type": "click", "x": 1, "y": 2}]), area)
        self.assertIsNone(moves.cover(None, [drag((1, 2), (300, 400))]))


class DesktopMoveTest(unittest.TestCase):
    def check(self, icons_after):
        with mock.patch.object(moves, "desktop_icons", lambda: ICONS):
            drags = moves.before([drag((1400, 240), (1290, 230))], same)
        self.assertEqual((drags[0].item, drags[0].target, drags[0].desktop), ("20261016.pdf", "math", True))
        with mock.patch.object(moves, "desktop_icons", lambda: icons_after):
            return moves.after(drags)

    def test_the_file_gone_and_the_folder_still_there_is_a_move(self):
        after = {k: v for k, v in ICONS.items() if k != "20261016.pdf"}
        (m,) = self.check(after)
        self.assertEqual((m.item, m.target), ("20261016.pdf", "math"))
        self.assertIn("now in “math”", m.note())

    def test_the_file_still_on_the_desktop_is_not(self):
        self.assertEqual(self.check(ICONS), [])

    def test_the_label_under_an_icon_counts_as_the_icon(self):
        with mock.patch.object(moves, "desktop_icons", lambda: ICONS):
            (d,) = moves.before([drag((1400, 290), (1290, 230))], same)  # on the file's name, below its image
        self.assertEqual(d.item, "20261016.pdf")


class HitTestMoveTest(unittest.TestCase):
    def test_off_the_desktop_the_hit_test_names_the_ends(self):
        before = {(10, 10): "Draft card", (500, 10): "Done"}
        with mock.patch.object(moves, "desktop_icons", lambda: {}), mock.patch.object(moves, "item_at", lambda x, y: before[(x, y)]):
            drags = moves.before([drag((10, 10), (500, 10))], same)
        after = {(10, 10): "Review card", (500, 10): "Done"}
        with mock.patch.object(moves, "item_at", lambda x, y: after[(x, y)]):
            self.assertEqual([(m.item, m.target) for m in moves.after(drags)], [("Draft card", "Done")])
        dropped_over = {(10, 10): "", (500, 10): "Draft card"}  # moved, but not into anything
        with mock.patch.object(moves, "item_at", lambda x, y: dropped_over[(x, y)]):
            self.assertEqual(moves.after(drags), [])


if __name__ == "__main__":
    unittest.main()
