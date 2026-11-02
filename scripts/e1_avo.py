"""E1's AVO column, run by script through the same timer server as the Voice Control column.

    uv run scripts/e1_avo.py                 # all 8 tasks
    uv run scripts/e1_avo.py wiki-year       # some of them

Start `uv run scripts/e1.py` first (the timer server), turn macOS Voice Control
off, and don't touch the computer while it runs. For each task it presses
Start on the server (which opens the start page, or makes the Finder files),
runs AVO with the task text standing in for the spoken task, then presses Done
with what happened: the page's own report, the disk, AVO's answer (Wikipedia),
or Chrome's address and title (Meta Careers). AVO's questions are answered by
avo/scripted.py from the words the task names (David, Rivera, corrected...).

Recorded as tool "avo" with "by": "script", and "said" = the task plus every
answer (AVO's own count), since there's no voice to record. Differences from a
person speaking: no time to say the task (a few seconds), and the script
answers at once. docs/eval.md says so next to the results.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request

from avo.agent import run
from avo.scripted import ScriptedUser

SERVER = "http://127.0.0.1:8777"
LIMIT_S = 300  # the same 5-minute cap as for Voice Control
SETTLE = 2.5  # seconds for the start page to load

# What the person means, for the script's answers, and the window AVO must stay in.
SETUP = {
    "mail-reply": (["David", "Physics"], "com.google.Chrome", "Campus Mail"),
    "room-book": (["13", "November 13", "Nov 13"], "com.google.Chrome", "Library Study Rooms"),
    "course-download": (["corrected"], "com.google.Chrome", "PHYS 212"),
    "expense-form": (["Meals"], "com.google.Chrome", "Expense Report"),
    "board-assign": (["Rivera"], "com.google.Chrome", "Northwind"),
    "wiki-year": ([], "com.google.Chrome", "Wikipedia"),
    "meta-jobs": (["software engineer", "intern"], "com.google.Chrome", "Meta"),
    "finder-move": (["e1-archive"], "com.apple.finder", ""),
}


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"null")


def chrome_page() -> str:
    """The front Chrome tab's address and title."""
    script = 'tell application "Google Chrome" to return (URL of active tab of front window) & " | " & (title of active tab of front window)'
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()


def one(spec: dict) -> dict:
    means, app, title = SETUP[spec["id"]]
    post("/e1/start", {"task": spec["id"], "tool": "avo"})
    time.sleep(SETTLE)
    t0 = time.time()
    user = ScriptedUser(means, show=True)
    result = run(
        spec["task"], app=app, overlay=user, human_guard=False, tools=False,
        stay_in=(app, title) if title else None, voice=True,
    )
    t1 = time.time()
    time.sleep(1.0)  # the page's last report
    done = not result.startswith(("stopped", "crashed")) and t1 - t0 <= LIMIT_S
    body = {"outcome": "done" if done else "gave_up", "t1": t1, "mistakes": 0, "by": "script", "note": f"by script: {result[:200]}"}
    if "answer" in spec["check"]:
        body["answer"] = (re.search(r"\b1[89]\d\d\b", result) or [""])[0]
    if "manual" in spec["check"]:
        page = chrome_page()
        body["worked"] = bool(re.search(r"intern", page, re.I)) and bool(re.search(r"software", page, re.I))
        body["note"] += f" · page: {page[:160]}"
    row = post("/e1/end", body)["row"]
    print(f"{spec['id']:16} {'ok' if row['ok'] else 'NO':3} {row['seconds']:6.1f}s  {row['check']}  | {result[:90]}")
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*")
    args = parser.parse_args()
    specs = json.loads(urllib.request.urlopen(SERVER + "/e1/state").read())["tasks"]
    if args.ids:
        specs = [s for s in specs if s["id"] in args.ids]
    print("Taking over the screen; fling the mouse into a corner to abort.")
    for spec in specs:
        one(spec)


if __name__ == "__main__":
    main()
