"""Tests for how the driver types (avo/driver.py).

    uv run python -m unittest discover tests
"""

import unittest
from unittest import mock

from avo.driver import MacDriver


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


if __name__ == "__main__":
    unittest.main()
