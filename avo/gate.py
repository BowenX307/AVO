"""The confidence gate: before a pointer batch runs, decide whether to ask the user.

assess() runs the model review and SAM in parallel and adds repeat detection.
A batch is flagged when the review says the click misses its target or doesn't
move the task forward, or when it repeats a recent batch. SAM can overrule a
"missed" review: when every click sits on a mask SAM is sure about, the reviewer
has misread the location (real misses, in gaps or just past a button's edge,
score lower in SAM), and its progress verdict, built on that misreading, is
dropped too. A "hit but no progress" review still asks.

ask_user() then points at the target on screen and asks "Is this the one?":
yes runs the batch, no lets the user click the right spot, stop ends the task.
"""

from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from openai import OpenAI
from PIL import Image, ImageDraw

from avo.confidence import repeats, review
from avo.driver import MacDriver
from avo.llm import data_url
from avo.overlay import Overlay
from avo.sam import segment

GATED_KINDS = ("click", "double_click", "drag")
POINTER_KINDS = ("click", "double_click", "move", "scroll", "drag")
MAX_HIGHLIGHT_FRACTION = 0.25  # ignore SAM boxes bigger than this share of the screen
SAM_SURE = 0.9  # SAM confidence at which a point on its mask overrides a "missed" review


@dataclass
class Assessment:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    review: dict | None = None
    repeat: bool = False
    target: tuple[int, int] | None = None  # screenshot px of the first gated action
    box: tuple[float, float, float, float] | None = None  # SAM box around it, screenshot px
    sam_conf: float | None = None
    on_mask: bool = False
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
            "on_mask": self.on_mask,
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
    points = [p for a in actions if a["type"] in GATED_KINDS and (p := _point(a))]
    t0 = time.time()
    with ThreadPoolExecutor(1 + len(points)) as pool:
        f_review = pool.submit(review, client, task, past, png, actions, note)
        f_sams = [pool.submit(segment, client, png, points=[p]) for p in points]
        try:
            rev = f_review.result()
        except Exception as e:  # a failed review shouldn't kill the run
            print(f"review failed: {type(e).__name__}: {e}")
            rev = None
        best = []  # per click point: its most confident segment, or None
        for f in f_sams:
            try:
                segments, _ = f.result()
            except Exception as e:
                print(f"SAM failed: {type(e).__name__}: {e}")
                segments = []
            best.append(max(segments, key=lambda s: s.confidence or 0) if segments else None)

    # SAM vouches for the batch only if it is sure about every click point.
    sure = [
        seg is not None and (seg.confidence or 0) >= SAM_SURE and seg.contains(*p)
        for p, seg in zip(points, best)
    ]
    sam_vouches = all(sure)
    box, sam_conf, on_mask = None, None, False
    seg = best[0]
    if seg is not None:
        sam_conf = round(seg.confidence or 0, 3)
        on_mask = seg.contains(*target)
        l, t, r, b = seg.box
        if (r - l) * (b - t) <= MAX_HIGHLIGHT_FRACTION * screen_size[0] * screen_size[1]:
            box = seg.box

    rep = repeats(history, actions)
    under = (rev or {}).get("element_under_click", "")
    hit = (rev or {}).get("hits_intended_target", True)
    forward = (rev or {}).get("moves_task_forward", True)
    if sam_vouches and not hit:
        # The reviewer misread where the clicks land; its "no progress" call
        # usually follows from that misreading, so drop both.
        hit = forward = True
    reasons = []
    if not hit:
        reasons.append(f"May miss — under the click: {under}" if under else "The click may miss its target")
    if not forward:
        reasons.append("This step may not move the task forward")
    if rep:
        reasons.append("Repeats an earlier action")
    return Assessment(
        flagged=bool(reasons),
        reasons=reasons,
        review=rev,
        repeat=rep,
        target=target,
        box=box,
        sam_conf=sam_conf,
        on_mask=on_mask,
        seconds=time.time() - t0,
    )


LOUPE_W, LOUPE_H, LOUPE_ZOOM = 134, 84, 4  # crop size in screenshot px; shown at 268x168


def loupe(png: bytes, point: tuple[int, int]) -> str:
    """A magnified crop around the click point with a crosshair, as a data URL."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    x, y = point
    left = min(max(x - LOUPE_W // 2, 0), img.width - LOUPE_W)
    top = min(max(y - LOUPE_H // 2, 0), img.height - LOUPE_H)
    crop = img.crop((left, top, left + LOUPE_W, top + LOUPE_H)).resize(
        (LOUPE_W * LOUPE_ZOOM, LOUPE_H * LOUPE_ZOOM), Image.LANCZOS
    )
    cx, cy = (x - left) * LOUPE_ZOOM, (y - top) * LOUPE_ZOOM
    d = ImageDraw.Draw(crop)
    for width, color in ((7, (255, 255, 255)), (3, (255, 59, 48))):
        d.line((cx - 40, cy, cx - 10, cy), fill=color, width=width)
        d.line((cx + 10, cy, cx + 40, cy), fill=color, width=width)
        d.line((cx, cy - 40, cx, cy - 10), fill=color, width=width)
        d.line((cx, cy + 10, cx, cy + 40), fill=color, width=width)
    d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=(255, 59, 48), outline=(255, 255, 255), width=3)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return data_url(buf.getvalue())


def ask_user(
    overlay: Overlay, driver: MacDriver, a: Assessment, actions: list[dict], note: str, png: bytes
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
        reason="; ".join(a.reasons),
        loupe=loupe(png, a.target),
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
