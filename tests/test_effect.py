"""Tests for noticing actions that change nothing on screen (avo/effect.py).

    uv run python -m unittest discover tests
"""

import unittest

from PIL import Image, ImageDraw

from avo.effect import MIN_CHANGED_PX, EffectWatch, changed_pixels, plain

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


class PlainTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(plain(CMD_L), "pressing ⌘L")
        self.assertEqual(plain([{"type": "keypress", "keys": ["Escape"]}]), "pressing Escape")
        self.assertEqual(plain(TYPE_URL), "typing")
        self.assertEqual(plain([{"type": "click", "x": 1, "y": 2}, {"type": "type", "text": "x"}]), "typing")
        self.assertEqual(plain([{"type": "click", "x": 1, "y": 2}]), "clicking there")
        self.assertEqual(plain([{"type": "scroll", "x": 1, "y": 2, "scroll_y": 6}]), "scrolling")


if __name__ == "__main__":
    unittest.main()
