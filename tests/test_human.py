"""Tests for noticing the user (avo/human.py) and for how a batch stops (avo/driver.py).

No real input: a fake AVO "posts" marked events and a fake user unmarked ones,
straight into the rules; the driver runs against fake screenshots.

    uv run python -m unittest discover tests
"""

import io
import time
import unittest
from unittest import mock

import Quartz
from PIL import Image, ImageDraw

from avo.driver import Interrupted, MacDriver, ScreenChanged, region_diff
from avo.human import JITTER_PT, MOVE_WINDOW_S, SETTLE_S, HumanGuard, Input, Rules, event_clock

OPTION = Quartz.kCGEventFlagMaskAlternate
COMMAND = Quartz.kCGEventFlagMaskCommand
SPACE = 49


class FakeOverlay:
    active = True

    def __init__(self):
        self.sent = []

    def send(self, type, **fields):
        self.sent.append((type, fields))


_pointer = [0.0, 0.0]


def move(dx, dy=0, avo=False):
    """A pointer move by (dx, dy) from wherever the last one left it."""
    _pointer[0] += dx
    _pointer[1] += dy
    return Input("move", x=_pointer[0], y=_pointer[1], dx=dx, dy=dy, avo=avo)


class RulesTest(unittest.TestCase):
    def test_jitter_is_ignored(self):
        r = Rules()
        self.assertIsNone(r.judge(move(8), 0.0))
        self.assertIsNone(r.judge(move(8), 0.1))  # 16 pt so far

    def test_travel_past_the_jitter_limit_counts(self):
        r = Rules()
        r.judge(move(12), 0.0)
        self.assertEqual(r.judge(move(JITTER_PT - 12), 0.1), "mouse")

    def test_nudges_far_apart_in_time_dont_add_up(self):
        r = Rules()
        r.judge(move(15), 0.0)
        self.assertIsNone(r.judge(move(15), MOVE_WINDOW_S + 0.1))

    def test_avo_events_are_ignored(self):
        r = Rules()
        self.assertIsNone(r.judge(move(500, avo=True), 0.0))
        self.assertIsNone(r.judge(move(10), 0.1))  # measured from where AVO left the pointer, not 510 pt
        self.assertIsNone(r.judge(Input("click", 100, 100, avo=True), 0.0))
        self.assertIsNone(r.judge(Input("key", keycode=9, avo=True), 0.0))

    def test_user_click_scroll_and_key_count(self):
        r = Rules()
        self.assertEqual(r.judge(Input("click", 100, 100), 0.0), "mouse")
        self.assertEqual(r.judge(Input("scroll", 100, 100), 0.0), "mouse")
        self.assertEqual(r.judge(Input("key", keycode=0), 0.0), "keyboard")

    def test_clicks_answer_a_question(self):
        r = Rules(asking=True)
        self.assertIsNone(r.judge(Input("click", 300, 300), 0.0))
        self.assertIsNone(r.judge(move(200), 0.0))

    def test_an_answer_reaching_the_tap_late_still_answers(self):
        r = Rules(asked=[(1_000, 2_000)])  # a question was up from t=1000 to t=2000 (event clock)
        self.assertIsNone(r.judge(Input("click", 300, 300, ts=1_500), 0.0))
        self.assertEqual(r.judge(Input("click", 300, 300, ts=2_500), 0.0), "mouse")

    def test_a_real_question_span_catches_a_late_answer(self):
        # Until 10/31 the span was read off a clock that always said 0, so this was "mouse".
        guard = HumanGuard(FakeOverlay(), use_tap=False)
        with guard.asking():
            clicked = event_clock()
        self.assertIsNone(guard.rules.judge(Input("click", 300, 300, ts=clicked), 0.0))
        self.assertEqual(guard.rules.judge(Input("click", 300, 300, ts=event_clock()), 0.0), "mouse")

    def test_event_clock_runs_on_uptime_nanoseconds(self):
        a = event_clock()
        time.sleep(0.02)
        self.assertAlmostEqual((event_clock() - a) / 1e9, 0.02, delta=0.015)
        self.assertGreater(a, 0)

    def test_events_posted_by_avo_count_as_avo_even_unmarked(self):
        from avo.human import OWN_PID

        r = Rules()
        self.assertIsNone(r.judge(Input("click", 100, 100, pid=OWN_PID), 0.0))
        self.assertEqual(r.judge(Input("click", 100, 100, pid=OWN_PID + 1), 0.0), "mouse")

    def test_moving_away_right_after_answering_doesnt_count(self):
        r = Rules()
        r.reset(10.0)  # a question just closed
        self.assertIsNone(r.judge(move(200), 10.0 + SETTLE_S / 2))
        self.assertEqual(r.judge(Input("click", 5, 5), 10.0 + SETTLE_S / 2), "mouse")
        self.assertEqual(r.judge(move(200), 10.0 + SETTLE_S + 1), "mouse")

    def test_option_space_is_the_pause_key(self):
        r = Rules()
        self.assertIsNone(r.judge(Input("flags", flags=OPTION), 0.0))  # Option goes down first
        self.assertIsNone(r.judge(Input("key", keycode=SPACE, flags=OPTION), 0.0))

    def test_other_space_chords_count(self):
        r = Rules()
        self.assertEqual(r.judge(Input("key", keycode=SPACE), 0.0), "keyboard")
        self.assertEqual(r.judge(Input("key", keycode=SPACE, flags=COMMAND), 0.0), "keyboard")  # Spotlight

    def test_events_on_the_overlay_are_ignored(self):
        r = Rules(on_overlay=lambda x, y: y > 900)  # the island at the bottom
        self.assertIsNone(r.judge(Input("click", 700, 920), 0.0))
        self.assertIsNone(r.judge(Input("scroll", 700, 920), 0.0))
        self.assertEqual(r.judge(Input("click", 700, 500), 0.0), "mouse")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.overlay = FakeOverlay()
        self.guard = HumanGuard(self.overlay)
        self.guard._running = True  # as after start(), without the tap or the app thread
        self.guard.rules.on_overlay = lambda x, y: False

    def test_trips_once_and_tells_the_overlay(self):
        self.guard.feed(Input("click", 10, 10), now=1.0)
        self.guard.feed(Input("key", keycode=0), now=1.2)
        self.assertTrue(self.guard.tripped.is_set())
        self.assertEqual(len(self.guard.interruptions), 1)
        self.assertEqual(self.guard.interruptions[0].reason, "mouse")
        self.assertEqual([t for t, _ in self.overlay.sent], ["interrupt"])
        self.assertEqual(self.guard.last_human, 1.2)  # still active: pushes back auto-resume

    def test_resume_clears_and_records_how_long(self):
        self.guard.feed(Input("key", keycode=0))
        self.guard._running = False  # no lsappinfo in the test
        self.guard.resume()
        self.assertFalse(self.guard.tripped.is_set())
        self.assertIsNotNone(self.guard.interruptions[0].paused_s)

    def test_avo_clicks_while_paused_are_counted(self):
        self.guard.feed(Input("key", keycode=0))
        self.guard.feed(Input("click", 50, 50, avo=True))
        self.assertEqual(self.guard.interruptions[0].avo_clicks_while_paused, 1)

    def test_disabled_guard_never_trips(self):
        guard = HumanGuard(self.overlay, enabled=False)
        guard.start()
        guard.feed(Input("click", 10, 10))
        self.assertFalse(guard.tripped.is_set())

    def test_front_app_switch_counts_unless_avo_did_it(self):
        self.guard._expected_app = "com.google.Chrome"
        self.guard._overlay_apps = {"com.github.Electron"}
        self.guard.check_front("com.github.Electron")  # AVO's own overlay
        self.assertFalse(self.guard.tripped.is_set())
        with mock.patch("avo.human.frontmost_app", return_value="com.apple.finder"):
            with self.guard.acting():
                self.guard.check_front("com.apple.finder")  # AVO switched apps itself
            self.assertFalse(self.guard.tripped.is_set())
            self.assertEqual(self.guard._expected_app, "com.apple.finder")
        self.guard.check_front("com.apple.Notes")
        self.assertTrue(self.guard.tripped.is_set())
        self.assertEqual(self.guard.interruptions[0].reason, "app")


def screen(button_at=(100, 100)) -> Image.Image:
    img = Image.new("RGB", (640, 400), "white")
    x, y = button_at
    ImageDraw.Draw(img).rectangle((x - 30, y - 12, x + 30, y + 12), fill="black")
    return img


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class DriverTest(unittest.TestCase):
    """The driver's batch loop, with a recording stand-in for real input."""

    def make(self, now: Image.Image, interrupt=lambda: None):
        driver = MacDriver(interrupt=interrupt)
        driver.grab = lambda: now
        driver.ran = []
        driver._execute_one = lambda a, done=0: driver.ran.append(a)
        return driver

    def test_region_diff(self):
        self.assertEqual(region_diff(screen(), screen(), 100, 100), 0.0)
        self.assertGreater(region_diff(screen(), screen((100, 300)), 100, 100), 0.08)
        blue, brown = screen(), screen()  # same brightness, different button
        ImageDraw.Draw(blue).rectangle((70, 88, 130, 112), fill=(37, 99, 235))
        ImageDraw.Draw(brown).rectangle((70, 88, 130, 112), fill=(180, 83, 9))
        self.assertGreater(region_diff(blue, brown, 100, 100), 0.08)
        elsewhere = screen()
        ImageDraw.Draw(elsewhere).rectangle((450, 250, 600, 350), fill="red")  # a change far from the target
        self.assertEqual(region_diff(screen(), elsewhere, 100, 100), 0.0)

    def test_unchanged_screen_runs_everything(self):
        d = self.make(screen())
        batch = [{"type": "click", "x": 100, "y": 100}, {"type": "type", "text": "hi"}]
        d.execute(batch, seen=png(screen()))
        self.assertEqual(d.ran, batch)
        self.assertFalse(d.fresh_checks[0]["skipped"])

    def test_moved_target_is_not_clicked(self):
        d = self.make(screen((100, 300)))  # the button moved down after the model looked
        with self.assertRaises(ScreenChanged) as cm:
            d.execute([{"type": "click", "x": 100, "y": 100}], seen=png(screen()))
        self.assertEqual(d.ran, [])
        self.assertEqual(cm.exception.done, 0)
        self.assertTrue(cm.exception.png)

    def test_change_is_checked_before_each_pointer_action(self):
        d = self.make(screen((100, 300)))
        batch = [
            {"type": "keypress", "keys": ["tab"]},  # no target: runs
            {"type": "click", "x": 500, "y": 50},  # far from the change: runs
            {"type": "click", "x": 100, "y": 100},  # where the button was: skipped
            {"type": "type", "text": "x"},
        ]
        with self.assertRaises(ScreenChanged) as cm:
            d.execute(batch, seen=png(screen()))
        self.assertEqual(cm.exception.done, 2)
        self.assertEqual(d.ran, batch[:2])

    def test_freshness_off(self):
        d = self.make(screen((100, 300)))
        d.fresh_threshold = None
        d.execute([{"type": "click", "x": 100, "y": 100}], seen=png(screen()))
        self.assertEqual(len(d.ran), 1)

    def test_interrupt_stops_the_batch(self):
        calls = {"n": 0}

        def interrupt():  # the user moves the mouse once the first action has run
            calls["n"] += 1
            return "human" if d.ran else None

        d = self.make(screen(), interrupt)
        with self.assertRaises(Interrupted) as cm:
            d.execute([{"type": "keypress", "keys": ["tab"]}, {"type": "keypress", "keys": ["tab"]}])
        self.assertEqual((cm.exception.reason, cm.exception.done), ("human", 1))
        self.assertEqual(len(d.ran), 1)


if __name__ == "__main__":
    unittest.main()
