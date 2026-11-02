"""Tests for how a batch gets the user's OK (avo/confirm.py).

    uv run python -m unittest discover tests
"""

import unittest
from unittest import mock

from avo import confirm
from avo.snap import Target

ROUTINE = {"id": "c1", "code": "computer_action_requires_confirmation", "message": "Confirm this action."}
UNUSUAL = {"id": "c2", "code": "malicious_instructions", "message": "The page may be trying to trick the agent."}
CLICK = [{"type": "click", "x": 100, "y": 200}]
REPLY = Target("AXButton", "Reply", (80, 190, 60, 24))
SEND = Target("AXButton", "Send", (80, 190, 60, 24))


class FakeOverlay:
    def __init__(self, *answers, active=True):
        self.answers = list(answers)
        self.active = active
        self.asked = []

    def send(self, type, **fields):
        pass

    def ask(self, **fields):
        self.asked.append(fields)
        return self.answers.pop(0) if self.answers else None


def before(actions=CLICK, checks=(ROUTINE,), overlay=None, under=REPLY, **kw):
    with mock.patch.object(confirm, "clickable_at", lambda x, y: under), mock.patch.object(confirm, "focused", lambda: None):
        return confirm.before(1, actions, list(checks), overlay, lambda x, y: (x, y), **kw)


class ConfirmTest(unittest.TestCase):
    def test_routine_check_is_a_countdown_on_the_ringed_target(self):
        overlay = FakeOverlay({"choice": "go", "shown": True})
        c = before(overlay=overlay)
        self.assertEqual((c.method, c.choice, c.shown), ("countdown", "go", True))
        (q,) = overlay.asked
        self.assertEqual(q["kind"], "countdown")
        self.assertEqual(q["box"], [80, 190, 140, 214])  # the Reply button's frame
        self.assertEqual(q["text"], "Clicking “Reply”")

    def test_no_during_the_countdown_skips_the_batch(self):
        c = before(overlay=FakeOverlay({"choice": "no", "how": "voice", "said": "no, the physics one"}))
        self.assertEqual((c.choice, c.declined, c.how, c.said), ("no", True, "voice", "no, the physics one"))

    def test_a_countdown_that_never_showed_asks_for_a_yes(self):
        overlay = FakeOverlay({"choice": "go", "shown": False}, {"choice": "yes", "how": "click"})
        c = before(overlay=overlay)
        self.assertEqual([q["kind"] for q in overlay.asked], ["countdown", "confirm"])
        self.assertEqual((c.method, c.choice), ("yes", "go"))

    def test_nothing_to_ring_asks_for_a_yes(self):
        overlay = FakeOverlay({"choice": "yes"})
        c = before(actions=[{"type": "keypress", "keys": ["cmd", "l"]}], overlay=overlay, under=None)
        self.assertEqual(overlay.asked[0]["kind"], "confirm")
        self.assertEqual(c.method, "yes")

    def test_send_needs_a_yes_with_the_draft(self):
        overlay = FakeOverlay({"choice": "yes", "how": "voice"})
        with mock.patch("avo.helper.form_fields", lambda app: []):
            c = before(overlay=overlay, under=SEND, draft="Dear Professor Smith, I'll be ten minutes late.")
        (q,) = overlay.asked
        self.assertEqual(q["kind"], "confirm")
        self.assertEqual(q["title"], "Send — OK?")
        self.assertIn("ten minutes late", q["detail"])
        self.assertEqual((c.irreversible, c.choice, c.how), ("Send", "go", "voice"))

    def test_the_preview_shows_who_it_goes_to(self):
        overlay = FakeOverlay({"choice": "yes"})
        fields = [("To", "david.smith@physics.example.edu"), ("Subject", "Re: Lab section this afternoon"), ("Search", "x")]
        with mock.patch("avo.helper.form_fields", lambda app: fields):
            before(overlay=overlay, under=SEND, draft="I'll be ten minutes late.", app="com.google.Chrome")
        self.assertEqual(overlay.asked[0]["detail"], "To: david.smith@physics.example.edu\n"
                         "Subject: Re: Lab section this afternoon\n\nI'll be ten minutes late.")

    def test_send_needs_a_yes_even_without_a_check(self):
        c = before(checks=(), overlay=FakeOverlay({"choice": "no", "how": "voice"}), under=SEND)
        self.assertEqual((c.method, c.choice, c.declined), ("yes", "no", True))

    def test_the_reviewer_naming_a_submit_button_counts(self):
        c = before(overlay=FakeOverlay({"choice": "yes"}), under=None,
                   review={"element_under_click": "Submit order button"})
        self.assertEqual(c.irreversible, "Submit")  # the action, not the review's description

    def test_cmd_enter_is_a_send(self):
        c = before(actions=[{"type": "keypress", "keys": ["cmd", "Enter"]}], overlay=FakeOverlay({"choice": "yes"}))
        self.assertEqual(c.irreversible, "⌘Enter (send)")

    def test_an_answered_question_is_the_confirmation(self):
        overlay = FakeOverlay()
        c = before(overlay=overlay, answered=True)
        self.assertEqual((c.method, c.choice), ("answered", "go"))
        self.assertEqual(overlay.asked, [])

    def test_unusual_check_always_needs_a_yes(self):
        overlay = FakeOverlay({"choice": "yes"})
        c = before(checks=(ROUTINE, UNUSUAL), overlay=overlay, answered=True)
        (q,) = overlay.asked
        self.assertEqual((q["kind"], q["title"]), ("confirm", "Meta flagged this step"))
        self.assertEqual(q["reason"], UNUSUAL["message"])  # the routine one isn't shown
        self.assertEqual(c.codes, ["computer_action_requires_confirmation", "malicious_instructions"])

    def test_no_check_and_nothing_irreversible_goes_without_asking(self):
        overlay = FakeOverlay()
        self.assertEqual(before(checks=(), overlay=overlay).method, "none")
        self.assertEqual(overlay.asked, [])

    def test_overlay_gone_while_asking_stops(self):
        c = before(checks=(UNUSUAL,), overlay=FakeOverlay())
        self.assertEqual((c.choice, c.declined), ("stop", True))

    def test_without_the_overlay_it_asks_in_the_terminal_never_auto(self):
        with mock.patch.object(confirm, "_ask_terminal", lambda *a: False) as t:
            c = before(overlay=FakeOverlay(active=False))
        self.assertEqual((c.method, c.choice), ("terminal", "stop"))

    def test_pause_instead_of_answering(self):
        self.assertEqual(before(overlay=FakeOverlay({"choice": "pause"})).choice, "pause")


if __name__ == "__main__":
    unittest.main()
