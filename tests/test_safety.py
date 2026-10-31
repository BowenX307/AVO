"""Tests for how Meta's safety checks are approved (avo/agent.py).

    uv run python -m unittest discover tests
"""

import unittest

from avo.agent import approve_safety_checks

ROUTINE = {"id": "c1", "code": "computer_action_requires_confirmation", "message": "Confirm this action."}
UNUSUAL = {"id": "c2", "code": "malicious_instructions", "message": "The page may be trying to trick the agent."}


class FakeOverlay:
    def __init__(self, answer, active=True):
        self.answer = answer
        self.active = active
        self.asked = []

    def send(self, type, **fields):
        pass

    def ask(self, **fields):
        self.asked.append(fields)
        return self.answer


class SafetyCheckTest(unittest.TestCase):
    def test_routine_check_needs_no_one(self):
        overlay = FakeOverlay(None)
        self.assertEqual(approve_safety_checks(1, [ROUTINE], True, overlay), "yes")
        self.assertEqual(overlay.asked, [])

    def test_unusual_check_is_asked_on_the_island(self):
        overlay = FakeOverlay({"choice": "yes"})
        self.assertEqual(approve_safety_checks(1, [ROUTINE, UNUSUAL], True, overlay, "Submitting the form."), "yes")
        (q,) = overlay.asked
        self.assertEqual(q["kind"], "safety")
        self.assertEqual(q["intent"], "Submitting the form.")
        self.assertEqual(q["reason"], UNUSUAL["message"])  # the routine one isn't shown

    def test_stop_declines(self):
        self.assertEqual(approve_safety_checks(1, [UNUSUAL], True, FakeOverlay({"choice": "stop"})), "no")

    def test_overlay_gone_declines(self):
        self.assertEqual(approve_safety_checks(1, [UNUSUAL], True, FakeOverlay(None)), "no")

    def test_pause_instead_of_answering(self):
        self.assertEqual(approve_safety_checks(1, [UNUSUAL], True, FakeOverlay({"choice": "pause"})), "pause")

    def test_with_confirm_on_even_the_routine_check_is_asked(self):
        overlay = FakeOverlay({"choice": "yes"})
        self.assertEqual(approve_safety_checks(1, [ROUTINE], False, overlay), "yes")
        self.assertEqual(len(overlay.asked), 1)


if __name__ == "__main__":
    unittest.main()
