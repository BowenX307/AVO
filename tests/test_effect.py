"""Tests for noticing actions that change nothing on screen (avo/effect.py).

    uv run python -m unittest discover tests
"""

import unittest
from unittest import mock

from PIL import Image, ImageDraw

from avo.driver import MacDriver
from avo.effect import MIN_CHANGED_PX, EffectWatch, changed_pixels, plain, still

W, H = 1280, 832
CMD_L = [{"type": "keypress", "keys": ["cmd", "L"]}, {"type": "wait"}]
TYPE_URL = [{"type": "type", "text": "metacareers.com/jobs\n"}]


def screen(*boxes, color=(30, 30, 30)) -> Image.Image:
    """A white screen with dark boxes (l, t, r, b) drawn on it."""
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    for box in boxes:
        draw.rectangle(box, fill=color)
    return img


BLANK = screen()


class ChangedPixelsTest(unittest.TestCase):
    def test_same_screen(self):
        self.assertEqual(changed_pixels(BLANK, screen()), 0)

    def test_caret_blink_is_not_a_change(self):
        caret = screen((600, 400, 600, 417))  # 1 x 18 px
        self.assertLess(changed_pixels(BLANK, caret), MIN_CHANGED_PX)

    def test_a_few_typed_letters_are(self):
        letters = screen(*[(600 + 9 * i, 400, 605 + 9 * i, 412) for i in range(3)])
        self.assertGreaterEqual(changed_pixels(BLANK, letters), MIN_CHANGED_PX)

    def test_menu_bar_clock_is_left_out(self):
        clock = screen((1150, 4, 1270, 18))
        self.assertEqual(changed_pixels(BLANK, clock), 0)

    def test_faint_tint_is_not_a_change(self):
        hover = screen((500, 300, 700, 340), color=(240, 240, 240))
        self.assertEqual(changed_pixels(BLANK, hover), 0)

    def test_png_bytes(self):
        import io

        def png(img):
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()

        self.assertEqual(changed_pixels(png(BLANK), png(screen())), 0)
        self.assertGreater(changed_pixels(png(BLANK), png(screen((0, 100, 99, 199)))), 9000)


class EffectWatchTest(unittest.TestCase):
    def test_cmd_l_into_nowhere_is_stuck_on_the_third(self):
        # 10/31: cmd+L went to AVO's own overlay, again and again.
        watch = EffectWatch()
        self.assertEqual([watch.record(CMD_L, BLANK, BLANK) for _ in range(3)], ["none", "none", "stuck"])

    def test_a_change_clears_the_streak(self):
        watch = EffectWatch()
        watch.record(CMD_L, BLANK, BLANK)
        watch.record(TYPE_URL, BLANK, BLANK)
        self.assertEqual(watch.record(CMD_L, BLANK, screen((140, 80, 1090, 200))), "changed")
        self.assertEqual(watch.record(TYPE_URL, BLANK, BLANK), "none")

    def test_waiting_neither_counts_nor_clears(self):
        watch = EffectWatch()
        watch.record(CMD_L, BLANK, BLANK)
        watch.record(TYPE_URL, BLANK, BLANK)
        self.assertEqual(watch.record([{"type": "wait"}], BLANK, BLANK), "passive")
        self.assertEqual(watch.record(CMD_L, BLANK, BLANK), "stuck")

    def test_reset(self):
        watch = EffectWatch()
        watch.record(CMD_L, BLANK, BLANK)
        watch.record(CMD_L, BLANK, BLANK)
        watch.reset()
        self.assertEqual(watch.record(CMD_L, BLANK, BLANK), "none")


class SettleTest(unittest.TestCase):
    def test_still(self):
        self.assertTrue(still(BLANK, screen()))
        self.assertTrue(still(BLANK, screen((600, 400, 601, 417))))  # caret blink
        self.assertFalse(still(BLANK, screen((40, 60, 50, 70))))  # tab spinner, a 10 px square
        self.assertFalse(still(BLANK, screen((0, 100, 1279, 800))))  # the new page paints

    def frames(self, *imgs):
        """A driver whose screenshots are imgs in turn (the last one repeats); no waiting."""
        driver = MacDriver()
        seq = list(imgs)
        driver.grab = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
        return driver

    @mock.patch("avo.driver.SETTLE_MIN", 0)
    @mock.patch("avo.driver.SETTLE_POLL", 0)
    def test_settle_waits_out_a_loading_page(self):
        spin = [screen((40, 60, 50, 70 - i)) for i in range(4)]  # the spinner turns: every frame differs
        page = screen((0, 100, 1279, 800))
        driver = self.frames(*spin, page, page)
        self.assertIs(driver.settle(), page)

    @mock.patch("avo.driver.SETTLE_MIN", 0)
    @mock.patch("avo.driver.SETTLE_POLL", 0.01)
    def test_settle_gives_up_on_a_screen_that_never_stops(self):
        frames = [screen((40, 60, 50, 60 + i % 2 * 9)) for i in range(1000)]
        driver = self.frames(*frames)
        driver.settle(longest=0.1)
        self.assertLess(driver.settle_s, 0.3)

    def test_trailing_waits_are_skipped(self):
        driver = MacDriver(fresh_threshold=None)
        ran = []
        driver._execute_one = lambda a, i: ran.append(a["type"])
        driver.execute([{"type": "keypress", "keys": ["cmd", "L"]}, {"type": "wait"}, {"type": "screenshot"}])
        self.assertEqual(ran, ["keypress"])
        driver.execute([{"type": "wait"}])
        self.assertEqual(ran, ["keypress"])


class PlainTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(plain(CMD_L), "pressing ⌘L")
        self.assertEqual(plain([{"type": "keypress", "keys": ["Escape"]}]), "pressing Escape")
        self.assertEqual(plain(TYPE_URL), "typing")
        self.assertEqual(plain([{"type": "click", "x": 1, "y": 2}, {"type": "type", "text": "x"}]), "typing")
        self.assertEqual(plain([{"type": "click", "x": 1, "y": 2}]), "clicking there")
        self.assertEqual(plain([{"type": "scroll", "x": 1, "y": 2, "scroll_y": 6}]), "scrolling")

class DeadRepeatTest(unittest.TestCase):
    def test_the_same_click_as_one_that_did_nothing(self):
        from avo.confidence import repeats_dead

        dead = [[{"type": "click", "x": 501, "y": 695}]]
        self.assertTrue(repeats_dead(dead, [{"type": "click", "x": 501, "y": 699}]))  # 4 px off: the same
        self.assertFalse(repeats_dead(dead, [{"type": "click", "x": 501, "y": 760}]))
        self.assertFalse(repeats_dead(dead, [{"type": "wait"}]))  # waiting is never refused
        self.assertFalse(repeats_dead([], [{"type": "click", "x": 501, "y": 695}]))

    def test_a_drag_to_somewhere_else_is_not_the_same(self):
        # 11/01: a zero-length drag did nothing; the real drag to the folder was then refused.
        from avo.confidence import repeats_dead

        def drag(*pts):
            return [{"type": "drag", "path": [{"x": x, "y": y} for x, y in pts]}]

        dead = [drag((1215, 214), (1215, 214))]
        self.assertFalse(repeats_dead(dead, drag((1220, 220), (1122, 215))))
        self.assertTrue(repeats_dead(dead, drag((1218, 216), (1212, 210))))



class DesktopAreaTest(unittest.TestCase):
    def test_on_the_desktop_the_whole_screen_is_watched(self):
        # 11/01: the file moved into its folder, but only the Terminal window in front was watched.
        from avo import driver as drv

        d = drv.MacDriver.__new__(drv.MacDriver)
        d.geometry, d.work_app = object(), "com.apple.finder"
        with mock.patch.object(drv, "has_window", lambda b: False), \
                mock.patch.object(drv, "work_area", lambda: (0, 0, 100, 100)):
            self.assertIsNone(d.work_box())
            self.assertTrue(d.on_desktop())
        d.work_app = "com.google.Chrome"
        self.assertFalse(d.on_desktop())


if __name__ == "__main__":
    unittest.main()
