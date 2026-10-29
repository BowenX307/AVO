"""The confidence gate: before a pointer batch runs, decide whether to ask the user.

assess() runs the model review and SAM in parallel (SAM only supplies the
target's outline for the on-screen highlight; in the baseline it added false
alarms without catching more mistakes) and adds repeat detection. A batch is
flagged when the review says the click misses its target or doesn't move the
task forward, or when it repeats a recent batch.

ask_user() then points at the target on screen and asks "是这个吗？":
yes runs the batch, no lets the user click the right spot, stop ends the task.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from openai import OpenAI

from avo.confidence import repeats, review
from avo.driver import MacDriver
from avo.overlay import Overlay
from avo.sam import segment

GATED_KINDS = ("click", "double_click", "drag")
POINTER_KINDS = ("click", "double_click", "move", "scroll", "drag")
MAX_HIGHLIGHT_FRACTION = 0.25  # ignore SAM boxes bigger than this share of the screen


@dataclass
class Assessment:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    review: dict | None = None
    repeat: bool = False
    target: tuple[int, int] | None = None  # screenshot px of the first gated action
    box: tuple[float, float, float, float] | None = None  # SAM box around it, screenshot px
    sam_conf: float | None = None
    seconds: float = 0.0

    def to_log(self) -> dict:
        return {
            "flagged": self.flagged,
            "reasons": self.reasons,
            "review": self.review,
            "repeat": self.repeat,
            "target": self.target,
            "box": [round(v) for v in self.box] if self.box else None,
            "sam_conf": self.sam_conf,
            "seconds": round(self.seconds, 1),
        }


def _point(a: dict) -> tuple[int, int] | None:
    if a["type"] == "drag":
        path = a.get("path") or []
        return (path[0]["x"], path[0]["y"]) if path else None
    return (a["x"], a["y"]) if "x" in a else None


def first_gated(actions: list[dict]) -> int | None:
    return next((i for i, a in enumerate(actions) if a["type"] in GATED_KINDS), None)


def assess(
    client: OpenAI,
    task: str,
    past: list[str],
    history: list[list[dict]],
    png: bytes,
    actions: list[dict],
    note: str,
    screen_size: tuple[int, int],
) -> Assessment | None:
    """Score a batch; None if it has no click or drag to check."""
    i = first_gated(actions)
    if i is None:
        return None
    target = _point(actions[i])
    t0 = time.time()
    with ThreadPoolExecutor(2) as pool:
        f_review = pool.submit(review, client, task, past, png, actions, note)
        f_sam = pool.submit(segment, client, png, points=[target])
        try:
            rev = f_review.result()
        except Exception as e:  # a failed review shouldn't kill the run
            print(f"review failed: {type(e).__name__}: {e}")
            rev = None
        try:
            segments, _ = f_sam.result()
        except Exception as e:
            print(f"SAM failed: {type(e).__name__}: {e}")
            segments = []

    box, sam_conf = None, None
    if segments:
        seg = max(segments, key=lambda s: s.confidence or 0)
        sam_conf = round(seg.confidence or 0, 3)
        l, t, r, b = seg.box
        if (r - l) * (b - t) <= MAX_HIGHLIGHT_FRACTION * screen_size[0] * screen_size[1]:
            box = seg.box

    rep = repeats(history, actions)
    reasons = []
    if rev and not rev.get("hits_intended_target", True):
        reasons.append("点击位置可能没落在目标上")
    if rev and not rev.get("moves_task_forward", True):
        reasons.append("这一步可能不会推进任务")
    if rep:
        reasons.append("和前面做过的动作重复")
    return Assessment(
        flagged=bool(reasons),
        reasons=reasons,
        review=rev,
        repeat=rep,
        target=target,
        box=box,
        sam_conf=sam_conf,
        seconds=time.time() - t0,
    )


def ask_user(
    overlay: Overlay, driver: MacDriver, a: Assessment, actions: list[dict], note: str
) -> tuple[str, list[dict], dict | None]:
    """Point at the target and ask. Returns (choice, actions to run, raw answer).

    choice is "yes", "no" (actions rewritten to the user's point) or "stop".
    If the overlay can't ask, proceeds with the original actions.
    """
    x, y = driver.to_screen(*a.target)
    box = None
    if a.box:
        l, t = driver.to_screen(a.box[0], a.box[1])
        r, b = driver.to_screen(a.box[2], a.box[3])
        box = [l, t, r, b]
    answer = overlay.ask(
        x=x,
        y=y,
        box=box,
        intent=note or (a.review or {}).get("element_under_click", ""),
        reason="；".join(a.reasons),
    )
    if answer is None:
        print("overlay unavailable; proceeding without confirmation")
        return "yes", actions, None
    choice = answer.get("choice")
    if choice == "no" and answer.get("x") is not None:
        return "no", correct(actions, driver.to_shot(answer["x"], answer["y"])), answer
    if choice == "stop":
        return "stop", [], answer
    return "yes", actions, answer


def correct(actions: list[dict], point: tuple[int, int]) -> list[dict]:
    """Move the first click/drag to the user's point and drop later pointer actions.

    Actions after the next pointer action were planned around the wrong target,
    so they're dropped; the model re-plans from the next screenshot.
    """
    i = first_gated(actions)
    fixed = dict(actions[i])
    x, y = point
    if fixed["type"] == "drag":
        fixed["path"] = [{"x": x, "y": y}] + list(fixed["path"][1:])
    else:
        fixed["x"], fixed["y"] = x, y
    out = list(actions[:i]) + [fixed]
    for a in actions[i + 1 :]:
        if a["type"] in POINTER_KINDS:
            break
        out.append(a)
    return out
