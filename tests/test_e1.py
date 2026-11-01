"""Tests for the E1 timer server (scripts/e1.py).

    uv run python -m unittest discover tests
"""

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import e1  # noqa: E402

URL = f"http://127.0.0.1:{e1.PORT}"


def post(path, body):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(URL + path, data=data, method="POST")) as r:
        return json.loads(r.read() or b"null")


class E1ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(e1, "RESULTS", self.tmp / "results.json"),
            mock.patch.object(e1, "AUDIO", self.tmp / "audio"),
            mock.patch.object(e1, "NOTES", self.tmp / "e1-notes.txt"),
            mock.patch.object(e1, "ARCHIVE", self.tmp / "e1-archive"),
            mock.patch.object(e1.subprocess, "run"),  # don't open Chrome or Finder
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = e1.serve(e1.State())
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_a_demo_task_is_checked_and_timed_by_the_page(self):
        post("/e1/start", {"task": "mail-reply", "tool": "voice-control"})
        post("/e1/audio", b"\0\0" * 2400)
        post("/report", {"sent": [{"to": "david.smith@physics.example.edu", "body": "I'll be ten minutes late"}]})
        row = post("/e1/end", {"outcome": "done", "mistakes": 1})["row"]
        self.assertTrue(row["ok"])
        self.assertEqual(row["timed_by"], "page")
        self.assertEqual(row["mistakes"], 1)
        self.assertEqual((e1.AUDIO / row["audio"]).stat().st_size, 4800)
        self.assertEqual(json.loads(e1.RESULTS.read_text())[0]["task"], "mail-reply")

    def test_the_wrong_professor_fails(self):
        post("/e1/start", {"task": "mail-reply", "tool": "avo"})
        post("/report", {"sent": [{"to": "anna.smith@math.example.edu", "body": "late"}]})
        self.assertFalse(post("/e1/end", {"outcome": "done"})["row"]["ok"])

    def test_the_finder_task_makes_its_files_and_checks_the_disk(self):
        post("/e1/start", {"task": "finder-move", "tool": "avo"})
        self.assertTrue(e1.NOTES.exists() and e1.ARCHIVE.is_dir())
        e1.NOTES.rename(e1.ARCHIVE / e1.NOTES.name)
        row = post("/e1/end", {"outcome": "done"})["row"]
        self.assertTrue(row["ok"])
        self.assertFalse(e1.ARCHIVE.exists())  # cleaned up afterwards

    def test_the_wikipedia_answer_and_the_manual_check(self):
        post("/e1/start", {"task": "wiki-year", "tool": "avo"})
        self.assertTrue(post("/e1/end", {"outcome": "done", "answer": "1937"})["row"]["ok"])
        post("/e1/start", {"task": "meta-jobs", "tool": "avo"})
        self.assertFalse(post("/e1/end", {"outcome": "done", "worked": False})["row"]["ok"])
        post("/e1/start", {"task": "meta-jobs", "tool": "voice-control"})
        self.assertFalse(post("/e1/end", {"outcome": "gave_up", "worked": True})["row"]["ok"])  # gave up is never ok


if __name__ == "__main__":
    unittest.main()
