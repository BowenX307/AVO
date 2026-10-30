"""Live checks for part A, run against the real screen (the overlay must be running).

    uv run python tests/live_a4.py moving    # look-again check: the Approve button moves
    uv run python tests/live_a4.py expense   # the user moves the mouse, later presses a key

"The user" is this script: it posts unmarked events, which the guard can't tell
from a person's. In `expense` it moves the mouse while AVO is running a batch,
clicks Resume on the island, then presses F19 (a key with no effect) during a
later batch and resumes again. Prints how fast each pause took hold.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import Quartz

from avo.desktop import overlay_windows

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = {
    "moving": ("demo/moving.html", "Approval Queue", "On the current page, open the receipt details, then approve the expense report."),
    "expense": (
        "demo/expense.html",
        "Expense Report",
        "Using the receipt and expense policy on the left, fill in the expense form on the right "
        "(merchant, date, category, amount) and submit it.",
    ),
}
F19 = 0x50


def front_title() -> str | None:
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
        if w.get("kCGWindowLayer") == 0 and w["kCGWindowBounds"]["Height"] > 300:
            return w.get("kCGWindowName")
    return None


def post(event) -> None:  # unmarked: looks like a person
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.01)


def where() -> tuple[float, float]:
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def person_moves_mouse() -> float:
    x, y = where()
    t = time.time()
    for i in range(1, 7):
        post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x + 10 * i, y + 4 * i), 0))
    return t


def person_presses_key() -> float:
    t = time.time()
    for down in (True, False):
        post(Quartz.CGEventCreateKeyboardEvent(None, F19, down))
    return t


def click_resume() -> None:
    island = next(w for w in overlay_windows() if w["title"] == "AVO island")
    l, t, r, b = island["box"]
    x, y = r - 7 - 38, (t + b) / 2  # Resume is the rightmost button
    post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0))
    post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft))
    post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft))


def main() -> None:
    name = sys.argv[1]
    page, title, task = SCENARIOS[name]
    subprocess.run(["open", "-a", "Google Chrome", (ROOT / page).as_uri()], check=True)
    time.sleep(2)
    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'])
    time.sleep(1.5)
    if front_title() != title:
        raise SystemExit(f"{title!r} is not in front ({front_title()!r}); not starting")
    if not any(w["title"] == "AVO island" for w in overlay_windows()):
        raise SystemExit("the overlay is not running")

    log_path = ROOT / "runs" / f"live-{name}.log"
    out = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", "scripts/run_task.py", task], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT
    )
    plan = ["mouse", "key"] if name == "expense" else []
    answered = 0
    acted: list[tuple[str, float]] = []
    seen_exec = 0
    while proc.poll() is None:
        text = log_path.read_text()
        execs = re.findall(r"^\[(\d+)\] executing", text, re.M)
        pauses = len(re.findall(r"paused \(you moved\)", text))
        asks = re.findall(r"^asking (which|ok): (.*)$", text, re.M)
        if len(asks) > answered:  # answer like a user who agrees: click AVO's guess / the ring
            kind, rest = asks[answered]
            answered += 1
            time.sleep(1.5)
            spot = re.search(r"\((-?\d+),(-?\d+)\)" + ("" if kind == "which" else "$"), rest)
            x, y = map(int, spot.groups())
            print(f"answering {kind}: click ({x},{y})", flush=True)
            post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft))
            post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft))
            continue
        if plan and len(execs) > seen_exec and len(acted) == pauses:
            seen_exec = len(execs)
            if int(execs[-1]) >= 2 or plan[0] == "key":  # leave step 1 alone
                time.sleep(0.15)  # mid-glide, before the click
                kind = plan.pop(0)
                acted.append((kind, person_moves_mouse() if kind == "mouse" else person_presses_key()))
                print(f"person: {kind} at {time.strftime('%H:%M:%S')}.{int(acted[-1][1] % 1 * 1000):03d}", flush=True)
        if acted and pauses == len(acted) and "resumed" not in text.split("paused (you moved)")[-1]:
            time.sleep(2.0)  # let the user see "You moved — paused."
            subprocess.run(["screencapture", "-x", str(ROOT / "runs" / f"live-{name}-paused{pauses}.png")])
            click_resume()
            time.sleep(1.0)
        time.sleep(0.05)
    out.close()
    text = log_path.read_text()
    print(text[-1500:])
    run_dir = re.search(r"run dir: (\S+)", text).group(1)
    print(json.dumps(json.loads(Path(run_dir, "result.json").read_text()), indent=1))


if __name__ == "__main__":
    main()
