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


class LoopTest(unittest.TestCase):
    def run_with(self, replies, **kw):
        script = iter(replies)
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: next(script)))
        with tempfile.TemporaryDirectory() as d, mock.patch.multiple(
            agent,
            RUNS_DIR=Path(d),
            make_client=lambda: client,
            MacDriver=FakeDriver,
            overlay_windows=lambda: [],
            frontmost_app=lambda: "com.google.Chrome",
            focus=lambda bundle: None,
        ), mock.patch("avo.agent.Overlay", lambda: Overlay(url=None)):
            result = agent.run("Open Meta careers", gate=False, snap_to_controls=False, human_guard=False, **kw)
            summary = json.loads(next(Path(d).glob("*/summary.json")).read_text())
        return result, summary, FakeDriver.last

    def test_open_url_then_done(self):
        result, summary, driver = self.run_with([
            reply(fn("open_url", url="https://www.metacareers.com/jobs"), text="Opening Meta Careers."),
            reply(text="Meta Careers is open."),
        ])
        self.assertEqual(result, "Meta Careers is open.")
        self.assertEqual(driver.ran, [[{"type": "open_url", "url": "https://www.metacareers.com/jobs"}]])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["api"]["calls"], 2)

    def test_stuck_without_the_overlay_stops(self):
        cmd_l = computer({"type": "keypress", "keys": ["cmd", "L"]})
        result, summary, driver = self.run_with([reply(cmd_l)] * 5)
        self.assertTrue(result.startswith("stopped: stuck"))
        self.assertEqual(len(driver.ran), 3)
        self.assertEqual(summary["counts"]["stuck"], 1)
        self.assertEqual(summary["counts"]["no_effect"], 3)

    def test_unknown_function_is_reported_not_run(self):
        result, _, driver = self.run_with([reply(fn("run_shell", cmd="ls")), reply(text="ok")])
        self.assertEqual(result, "ok")
        self.assertEqual(driver.ran, [[]])


if __name__ == "__main__":
    unittest.main()
