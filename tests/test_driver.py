"""Tests for how the driver types and opens pages and apps (avo/driver.py).

    uv run python -m unittest discover tests
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from avo.agent import as_action
from avo.driver import MacDriver
from avo.llm import describe


class TypeTest(unittest.TestCase):
    def typed(self, text: str) -> list:
        """What _type does for text: ("paste", line) and ("keys", [...]) in order."""
        done = []

        def run(cmd, input=None, **_):
            if cmd == ["pbcopy"]:
                done.append(("copy", input.decode()))
            return mock.Mock(stdout=b"the user's clipboard")

        with mock.patch("avo.driver.subprocess.run", run), mock.patch(
            "avo.driver.chord", lambda keys: done.append(("keys", keys))
        ), mock.patch("avo.driver.time.sleep"):
            MacDriver._type(text)
        return done

    def test_newline_is_enter(self):
        # 10/31: "metacareers.com/jobs\n" pasted into the address bar lost its newline.
        self.assertEqual(
            self.typed("metacareers.com/jobs\n"),
            [
                ("copy", "metacareers.com/jobs"),
                ("keys", ["command", "v"]),
                ("keys", ["enter"]),
                ("copy", "the user's clipboard"),
            ],
        )

    def test_plain_text_is_one_paste(self):
        self.assertEqual(
            self.typed("LAKEVIEW CAFE"),
            [("copy", "LAKEVIEW CAFE"), ("keys", ["command", "v"]), ("copy", "the user's clipboard")],
        )

    def test_two_lines(self):
        keys = [k for k in self.typed("a\nb") if k[0] == "keys"]
        self.assertEqual(keys, [("keys", ["command", "v"]), ("keys", ["enter"]), ("keys", ["command", "v"])])


class OpenTest(unittest.TestCase):
    def opened(self, action: dict, work_app: str | None = "com.google.Chrome", returncode: int = 0):
        driver = MacDriver(fresh_threshold=None)
        driver.work_app = work_app
        cmds = []

        def run(cmd, **_):
            cmds.append(cmd)
            return SimpleNamespace(returncode=returncode, stderr="Unable to find application named 'Calculater'")

        with mock.patch("avo.driver.subprocess.run", run):
            driver.execute([action])
        return cmds, driver.failures

    def test_open_url_in_the_browser_in_use(self):
        cmds, failures = self.opened({"type": "open_url", "url": "https://www.metacareers.com/jobs"})
        self.assertEqual(cmds, [["open", "-b", "com.google.Chrome", "https://www.metacareers.com/jobs"]])
        self.assertEqual(failures, [])

    def test_bare_domain_gets_https_and_default_browser(self):
        cmds, _ = self.opened({"type": "open_url", "url": "example.com/a"}, work_app="com.apple.finder")
        self.assertEqual(cmds, [["open", "https://example.com/a"]])

    def test_not_a_web_address(self):
        cmds, failures = self.opened({"type": "open_url", "url": "x-apple.systempreferences:com.apple.Security"})
        self.assertEqual(cmds, [])
        self.assertEqual(len(failures), 1)

    def test_open_app_and_a_wrong_name(self):
        cmds, failures = self.opened({"type": "open_app", "name": "Calculater"}, returncode=1)
        self.assertEqual(cmds, [["open", "-a", "Calculater"]])
        self.assertIn("no app called 'Calculater'", failures[0])

    def test_function_calls_become_actions(self):
        self.assertEqual(
            as_action({"name": "open_url", "arguments": '{"url": "https://example.com"}'}),
            {"type": "open_url", "url": "https://example.com"},
        )
        self.assertIsNone(as_action({"name": "run_shell", "arguments": '{"cmd": "ls"}'}))
        self.assertEqual(describe([{"type": "open_app", "name": "Notes"}]), 'open_app("Notes")')


if __name__ == "__main__":
    unittest.main()
