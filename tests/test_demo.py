"""Tests for the scripted demo runs (scripts/run_demo.py, avo/scripted.py).

    uv run python -m unittest discover tests
"""

import json
import sys
import unittest
from unittest import mock
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_demo  # noqa: E402
from avo.scripted import ScriptedUser  # noqa: E402

SPECS = {s["id"]: s for s in json.loads((run_demo.ROOT / "eval" / "demos.json").read_text())}


class ScriptedUserTest(unittest.TestCase):
    def test_picks_the_option_the_user_means(self):
        user = ScriptedUser(SPECS["mail-smith"]["means"])
        cands = [{"label": "Prof. Anna Smith (Math 241)"}, {"label": "Prof. David Smith (Physics 212)"}]
        self.assertEqual(user.ask(candidates=cands), {"choice": "pick", "index": 1, "how": "script"})
        self.assertEqual(user.asked[0]["kind"], "which")

    def test_a_date_written_any_way(self):
        user = ScriptedUser(SPECS["room-friday"]["means"])  # "next Friday" = Nov 13
        self.assertEqual(user.pick(["Fri 6 Nov", "Fri 13 Nov"]), 1)
        self.assertEqual(user.pick(["Friday, November 13", "Friday, November 6"]), 0)

    def test_ok_to_click_points_at_the_nearest_control_when_off_one(self):
        from avo.snap import Target

        user = ScriptedUser([])
        button = Target("AXButton", "Book room", (100, 200, 120, 36))
        with mock.patch("avo.snap.clickable_at", lambda x, y: None), mock.patch("avo.snap.nearby", lambda x, y: [button]), \
                mock.patch("avo.snap.in_page", lambda x, y: True):
            self.assertEqual(user.ask(x=150, y=250), {"choice": "no", "x": 160.0, "y": 218.0, "how": "script"})
        # 10/31: the nearest control was an extension button in Chrome's toolbar. Never that.
        with mock.patch("avo.snap.clickable_at", lambda x, y: None), mock.patch("avo.snap.nearby", lambda x, y: [button]), \
                mock.patch("avo.snap.in_page", lambda x, y: False):
            self.assertEqual(user.ask(x=150, y=250)["choice"], "yes")
        with mock.patch("avo.snap.clickable_at", lambda x, y: button):
            self.assertEqual(user.ask(x=150, y=210)["choice"], "yes")

    def test_none_of_these(self):
        a = ScriptedUser(["corrected"]).ask(options=["Lecture 1 handout", "Lecture 2 handout"])
        self.assertEqual(a["choice"], "none")
        self.assertIn("corrected", a["said"])

    def test_yes_to_ok_and_send_silent_through_the_countdown(self):
        user = ScriptedUser([])
        self.assertEqual(user.ask(kind="confirm", title="Send — OK?")["choice"], "yes")
        self.assertEqual(user.ask(kind="countdown", box=[0, 0, 1, 1])["choice"], "go")
        self.assertEqual([q["kind"] for q in user.asked], ["confirm"])  # the countdown isn't a question


class CheckTest(unittest.TestCase):
    def test_mail(self):
        spec = SPECS["mail-smith"]
        ok, _ = run_demo.check(spec, {"sent": [{"to": "david.smith@physics.example.edu", "body": "I'll be ten minutes late."}]})
        self.assertTrue(ok)
        ok, what = run_demo.check(spec, {"sent": [{"to": "anna.smith@math.example.edu", "body": "late"}]})
        self.assertEqual((ok, what), (False, "sent to anna.smith@math.example.edu"))
        self.assertEqual(run_demo.check(spec, None), (False, "nothing sent"))

    def test_room(self):
        spec = SPECS["room-friday"]
        good = {"bookings": [{"room": "Room 204", "date": "2026-11-13", "time": "2:00 PM"}]}
        self.assertTrue(run_demo.check(spec, good)[0])
        self.assertFalse(run_demo.check(spec, {"bookings": [{"room": "Room 204", "date": "2026-11-06", "time": "2:00 PM"}]})[0])

    def test_course(self):
        spec = SPECS["course-week3"]
        self.assertTrue(run_demo.check(spec, {"downloads": ["w3-handout-corrected"]})[0])
        self.assertFalse(run_demo.check(spec, {"downloads": ["w3-handout", "w3-handout-corrected"]})[0])

    def test_expense_and_board(self):
        # E1's checks (eval/e1/tasks.json): subtotal + tax, no tip; the budget card to Rivera.
        e = {"id": "x", "check": {"submitted": {"merchant": "lakeview", "date": "2026-10-24", "category": "meals", "amount": "46.54"}}}
        good = {"submitted": {"merchant": "LAKEVIEW CAFÉ", "date": "2026-10-24", "category": "Meals", "amount": "$46.54"}}
        self.assertTrue(run_demo.check(e, good)[0])
        self.assertFalse(run_demo.check(e, {"submitted": {**good["submitted"], "amount": "54.54"}})[0])
        b = {"id": "y", "check": {"assigned": {"budget": "Alex Rivera"}}}
        self.assertTrue(run_demo.check(b, {"assigned": {"budget": "Alex Rivera", "review": "Alex Chen"}})[0])
        self.assertFalse(run_demo.check(b, {"assigned": {"budget": "Alex Chen"}})[0])


class ServerTest(unittest.TestCase):
    def test_pages_and_reports(self):
        reports = run_demo.Reports()
        server = run_demo.serve(reports)
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{run_demo.PORT}/demo/mail.html").read().decode()
            self.assertIn("Campus Mail", page)
            req = urllib.request.Request(f"http://127.0.0.1:{run_demo.PORT}/report", data=b'{"downloads": ["w3-ps"]}', method="POST")
            urllib.request.urlopen(req)
            self.assertEqual(reports.state, {"downloads": ["w3-ps"]})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
