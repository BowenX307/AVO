"""The agent loop end to end, offline: a scripted model, a fake screen and driver.

    uv run python -m unittest discover tests
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from avo import agent
from avo.overlay import Overlay


def png(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1280, 832), color).save(buf, "PNG")
    return buf.getvalue()


class Item(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


def reply(*items, text=""):
    return SimpleNamespace(id="r", output=list(items), output_text=text, usage=None)


def fn(name, **args):
    return Item(type="function_call", name=name, arguments=json.dumps(args), call_id="f1")


def computer(*actions):
    return Item(type="computer_call", call_id="c1", actions=list(actions), pending_safety_checks=[])


class FakeDriver:
    """Each batch turns the screen a new colour, unless it is one that does nothing."""

    def __init__(self, *_, **__):
        self.geometry = SimpleNamespace(shot_size=(1280, 832))
        self.screen = png()
        self.ran = []
        self.fresh_checks, self.failures = [], []
        self.settle_s, self.settle_area, self.work_app = 0.1, None, None
        FakeDriver.last = self

    def screenshot(self):
        return self.screen

    def settled_screenshot(self):
        return self.screen

    def execute(self, actions, seen=None):
        self.ran.append(actions)
        if not any(a.get("keys") == ["cmd", "L"] for a in actions):  # cmd+L into nowhere
            self.screen = png((len(self.ran) * 40 % 256, 90, 90))

    def to_screen(self, x, y):
        return x, y

    def to_shot(self, x, y):
        return x, y


class LoopBase(unittest.TestCase):
    def run_with(self, replies, plan="never", driver=None, task="Open Meta careers", **kw):
        script = iter(replies)
        inputs = []

        def create(**kw):
            inputs.append(kw)
            return next(script)

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with tempfile.TemporaryDirectory() as d, mock.patch.multiple(
            agent,
            RUNS_DIR=Path(d),
            make_client=lambda: client,
            MacDriver=driver or FakeDriver,
            overlay_windows=lambda: [],
            frontmost_app=lambda: "com.google.Chrome",
            focus=lambda bundle: None,
            screen_text=lambda app: "",
            highlight=lambda *a, **k: None,
        ), mock.patch("avo.agent.Overlay", lambda: Overlay(url=None)):
            result = agent.run(
                task, gate=False, snap_to_controls=False, human_guard=False, plan=plan, **kw
            )
            run_dir = next(Path(d).glob("2*"))
            summary = json.loads((run_dir / "summary.json").read_text())
            self.inputs = inputs
        return result, summary, FakeDriver.last


class LoopTest(LoopBase):
    def test_open_url_then_done(self):
        result, summary, driver = self.run_with([
            reply(fn("open_url", url="https://www.metacareers.com/jobs"), text="Opening Meta Careers."),
            reply(text="Meta Careers is open."),
        ])
        self.assertEqual(result, "Meta Careers is open.")
        self.assertEqual(driver.ran, [[{"type": "open_url", "url": "https://www.metacareers.com/jobs"}]])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["api"]["calls"], 2)

    def test_a_dead_batch_isnt_run_again_and_a_second_stuck_stops(self):
        # 10/31: cmd+L pressed 19 times; "View all results" clicked 3 more times at 0 px each.
        cmd_l = computer({"type": "keypress", "keys": ["cmd", "L"]})
        why = reply(text="The page doesn't seem to have an address bar to focus.")
        result, summary, driver = self.run_with([reply(cmd_l)] * 6 + [why])
        self.assertEqual(result, "stopped: The page doesn't seem to have an address bar to focus.")  # says why
        self.assertEqual(len(driver.ran), 1)  # tried once; the five asks after it were refused
        self.assertEqual(summary["counts"]["stuck"], 2)  # the first: another way; the second: stop
        self.assertIn("NOT executed again", self.inputs[2]["input"][0]["content"][0]["text"])

    def test_the_first_stuck_makes_a_fresh_plan_without_asking(self):
        cmd_l = computer({"type": "keypress", "keys": ["cmd", "L"]})
        result, summary, _ = self.run_with(
            [plan_reply(start_kind="here", start_target=""), reply(cmd_l), reply(cmd_l), reply(cmd_l),
             plan_reply(start_kind="here", start_target="", steps=["Click the search field first"]), reply(text="Done.")],
            plan="auto",
        )
        self.assertEqual(result, "Done.")
        self.assertEqual(summary["counts"]["replans"], 1)
        self.assertEqual(summary["api"]["by_purpose"]["replan"]["calls"], 1)
        self.assertIn("made a fresh plan", self.inputs[-1]["input"][0]["content"][0]["text"])

    def test_the_step_limit_says_where_it_stopped(self):
        click = lambda y: reply(computer({"type": "click", "x": 5, "y": y}), text=f"Looking for the 2FA setting {y}")
        result, _, _ = self.run_with([click(10), click(60), click(110)], max_steps=2)
        self.assertEqual(result, "stopped: step limit (2) reached; last step: Looking for the 2FA setting 60")

    def test_leaving_the_demo_page_stops_the_run(self):
        # 10/31: a scripted run whose tab was lost opened the real Mail app.
        with mock.patch.object(agent, "front_title", lambda app: "Inbox - Mail"):
            result, _, driver = self.run_with(
                [reply(computer({"type": "click", "x": 5, "y": 5}))], stay_in=("com.google.Chrome", "Campus Mail")
            )
        self.assertTrue(result.startswith("stopped: left Campus Mail"))
        self.assertEqual(driver.ran, [])

    def test_unknown_function_is_reported_not_run(self):
        result, _, driver = self.run_with([reply(fn("run_shell", cmd="ls")), reply(text="ok")])
        self.assertEqual(result, "ok")
        self.assertEqual(driver.ran, [[]])


def plan_reply(**over):
    raw = {
        "start_kind": "url", "start_target": "https://www.metacareers.com/jobs",
        "steps": ["Search for Software Engineer Intern", "Open the listing"],
        "done_when": "The SWE intern listing is open.", "question": "", "options": [],
    }
    return reply(text=json.dumps(raw | over))


class PlanLoopTest(LoopBase):
    def test_plan_opens_the_start_then_guides_each_turn(self):
        result, summary, driver = self.run_with(
            [plan_reply(), reply(computer({"type": "click", "x": 5, "y": 5})), reply(text="Found it.")], plan="auto"
        )
        self.assertEqual(result, "Found it.")
        self.assertEqual(driver.ran[0], [{"type": "open_url", "url": "https://www.metacareers.com/jobs"}])
        self.assertEqual(summary["api"]["by_purpose"]["plan"]["calls"], 1)
        first_turn = self.inputs[1]["input"][0]["content"][0]["text"]
        self.assertIn("Done when: The SWE intern listing is open.", first_turn)
        self.assertIn("0. (AVO, before your first turn) open_url(https://www.metacareers.com/jobs)", first_turn)
        self.assertNotIn("Begin at", first_turn)  # already opened
        self.assertIn("AVO's plan", self.inputs[2]["input"][0]["content"][0]["text"])  # and on later turns

    def test_here_opens_nothing(self):
        _, _, driver = self.run_with([plan_reply(start_kind="here", start_target=""), reply(text="Done.")], plan="auto")
        self.assertEqual(driver.ran, [])

    def test_question_without_the_overlay_goes_on(self):
        result, summary, _ = self.run_with(
            [plan_reply(question="Which Alex?", options=["Alex Chen", "Alex Rivera"]), reply(text="Done.")], plan="auto"
        )
        self.assertEqual(result, "Done.")
        self.assertEqual(summary["counts"]["asked"], 0)

    def test_a_failing_planner_doesnt_stop_the_task(self):
        result, _, driver = self.run_with([reply(text="not json"), reply(text="Done.")], plan="auto")
        self.assertEqual(result, "Done.")
        self.assertEqual(driver.ran, [])

    def test_the_cost_limit_stops_the_run(self):
        usage = SimpleNamespace(model_dump=lambda: {"input_tokens": 300_000, "output_tokens": 0,
                                                     "input_tokens_details": {"cached_tokens": 0}})
        pricey = SimpleNamespace(id="r", output=[computer({"type": "click", "x": 5, "y": 5})], output_text="", usage=usage)
        result, _, driver = self.run_with([pricey, pricey, pricey], max_usd=0.5)
        self.assertTrue(result.startswith("stopped: cost limit reached"))  # $0.375 a turn: stops after two
        self.assertEqual(len(driver.ran), 1)

    def test_no_key_says_so(self):
        def no_key():
            raise agent.NoKey("MODEL_API_KEY is not set")

        with mock.patch.object(agent, "make_client", no_key):
            self.assertEqual(agent.run("x", overlay=Overlay(url=None)), "stopped: MODEL_API_KEY is not set")

    def test_never(self):
        result, summary, _ = self.run_with([reply(text="Done.")], plan="never")
        self.assertNotIn("plan", summary["api"]["by_purpose"])

    def test_moving_while_it_plans_pauses_instead_of_crashing(self):
        # 10/31: the user moved the mouse during planning; opening the start raised Interrupted.
        class MovedDuringPlan(FakeDriver):
            def execute(self, actions, seen=None):
                if not self.ran:
                    self.ran.append("interrupted")
                    raise agent.Interrupted("human", 0)
                super().execute(actions, seen)

        result, _, _ = self.run_with([plan_reply(), reply(text="Done.")], plan="auto", driver=MovedDuringPlan)
        self.assertEqual(result, "Done.")
        first_turn = self.inputs[1]["input"][0]["content"][0]["text"]
        self.assertIn("Begin at: https://www.metacareers.com/jobs", first_turn)  # the model opens it instead
        self.assertIn("paused before opening the start", first_turn)


class BoxTest(LoopBase):
    STRAY = {"A": (606, 159, 641, 351)}  # 10/31: drawn by accident over the mail page

    def test_a_box_the_task_doesnt_mention_is_ignored_and_the_plan_opens_the_start(self):
        _, summary, driver = self.run_with(
            [plan_reply(), reply(text="Done.")], plan="auto", regions=self.STRAY,
            task="Help me to find a hotel near UIUC on 11/10 near 200 dollar",
        )
        self.assertIn("boxes_ignored", summary["settings"])
        self.assertEqual(driver.ran[0], [{"type": "open_url", "url": "https://www.metacareers.com/jobs"}])

    def test_a_box_the_task_points_at_is_kept_and_planned_around(self):
        _, summary, driver = self.run_with(
            [plan_reply(question="Which?", options=["x", "y"]), reply(text="Done.")], plan="auto",
            regions={"A": (100, 100, 300, 160), "B": (400, 100, 600, 300)}, task="Assign B to the person A names.",
        )
        self.assertNotIn("boxes_ignored", summary["settings"])
        self.assertEqual(summary["api"]["by_purpose"]["plan"]["calls"], 1)  # planned, even with boxes
        self.assertEqual(driver.ran, [])  # but it starts here, and asks nothing up front
        self.assertIn("drew boxes", self.inputs[0]["input"][0]["content"][0]["text"])

    def test_which_tasks_mean_their_boxes(self):
        box = {"A": (0, 0, 100, 40)}
        self.assertEqual(agent.boxes_meant("Move this to the budget card", box), box)
        self.assertEqual(agent.boxes_meant("Book a study room next Friday", box), {})
        self.assertEqual(agent.boxes_meant("Copy A into B", {"A": (0, 0, 100, 6)}), {})  # a sliver: a click


class PersonalTest(unittest.TestCase):
    def test_mail_the_task_doesnt_mention(self):
        self.assertTrue(agent.personal_unasked("find a hotel near UIUC", "com.google.Chrome", "Campus Mail — Inbox - Google Chrome"))
        self.assertTrue(agent.personal_unasked("find a hotel near UIUC", "com.apple.mail", ""))
        self.assertFalse(agent.personal_unasked("Reply to Professor Smith", "com.google.Chrome", "Campus Mail — Inbox"))
        self.assertFalse(agent.personal_unasked("find a hotel near UIUC", "com.google.Chrome", "Hotels near UIUC - Google"))


class OnScreenTest(unittest.TestCase):
    def test_a_question_about_options_on_screen_is_left_to_the_gate(self):
        page = "Campus Mail Prof. Anna Smith (Math 241) Our 2:00 meeting Prof. David Smith (Physics 212) Lab section"
        with mock.patch.object(agent, "screen_text", lambda app: page):
            self.assertTrue(agent.options_on_screen(["Prof. Anna Smith (Math 241)", "Prof. David Smith (Physics 212)"], "x"))
            self.assertFalse(agent.options_on_screen(["Email Alex", "Message Alex in chat"], "x"))
        with mock.patch.object(agent, "screen_text", lambda app: ""):  # no accessibility text: ask as usual
            self.assertFalse(agent.options_on_screen(["Prof. Anna Smith (Math 241)"], "x"))


class ReplanTest(unittest.TestCase):
    def test_a_replan_is_wonders_and_metered_apart(self):
        from avo.planner import REPLAN_MODEL, make_plan

        sent = []
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: sent.append(kw) or plan_reply()))
        make_plan(client, "Open Meta careers", png(), past=["1. clicked the search field"], model=REPLAN_MODEL)
        make_plan(client, "Open Meta careers", png())
        self.assertEqual([k["model"] for k in sent], ["muse-wonder-1.0", "muse-spark-1.3"])
        self.assertEqual([k["text"]["format"]["name"] for k in sent], ["replan", "plan"])
        self.assertIn("got stuck", sent[0]["input"][0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
