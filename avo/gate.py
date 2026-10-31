"""The confidence gate: before a pointer batch runs, decide whether to ask the user.

assess() runs the model review and SAM in parallel and adds repeat detection.
A batch is flagged when the review says the click misses its target or doesn't
move the task forward, when it repeats a recent batch, or when the click picks
one of several options that fit the task equally well (two people named Alex). SAM can overrule a
"missed" review: when every click sits on a mask SAM is sure about, the reviewer
has misread the location (real misses, in gaps or just past a button's edge,
score lower in SAM), and its progress verdict, built on that misreading, is
dropped too. A "hit but no progress" review still asks.

The review is the slow half (median 3.7 s against SAM's 1.9 s). When SAM comes
back at least SAM_CONFIDENT sure of every click and the batch isn't a repeat,
the batch goes ahead without waiting for it; the review still finishes in the
background and is logged as late_review, to keep calibrating. Never when the
user boxed what the task is about (skip_ok=False): those tasks are where a
"which one?" question matters most.

ask_user() then points at the target on screen and asks "OK to click here?":
yes runs the batch, no lets the user click the right spot, stop ends the task.
For an ambiguous click it rings every candidate and asks "Which one did you mean?".
"""

from __future__ import annotations

import io
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from openai import OpenAI
from PIL import Image, ImageDraw

from avo.confidence import repeats, review
from avo.driver import MacDriver
from avo.llm import data_url
from avo.overlay import Overlay
from avo.sam import segment
from avo.snap import Target, clickable_at, nearby

GATED_KINDS = ("click", "double_click", "drag")
POINTER_KINDS = ("click", "double_click", "move", "scroll", "drag")
MAX_HIGHLIGHT_FRACTION = 0.25  # ignore SAM boxes bigger than this share of the screen
SAM_SURE = 0.9  # SAM confidence at which a point on its mask overrides a "missed" review
# SAM confidence on every click at which the review isn't waited for. On the 39
# labelled baseline clicks, 0.8 skips 6 reviews and still catches 14/14 wrong
# clicks (0.5 let a click between two fields through, at 0.668); the "which
# Alex?" clicks scored 0.59-0.71, below it.
SAM_CONFIDENT = 0.8


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
    candidates: list[dict] = field(default_factory=list)  # ambiguous click: [{label, x, y}], screenshot px
    plain: list[str] = field(default_factory=list)  # the reasons, in words for the user
    seconds: float = 0.0
    late_review: Future | None = None  # the review not waited for (SAM was confident), still running

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
            "candidates": self.candidates,
            "seconds": round(self.seconds, 1),
            "review_skipped": self.late_review is not None,
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
    skip_ok: bool = True,
) -> Assessment | None:
    """Score a batch; None if it has no click or drag to check.

    skip_ok: go ahead without the review when SAM is confident (SAM_CONFIDENT).
    """
    i = first_gated(actions)
    if i is None:
        return None
    target = _point(actions[i])
    points = [p for a in actions if a["type"] in GATED_KINDS and (p := _point(a))]
    t0 = time.time()
    pool = ThreadPoolExecutor(1 + len(points))
    f_review = pool.submit(review, client, task, past, png, actions, note)
    f_sams = [pool.submit(segment, client, png, points=[p]) for p in points]
    best = []  # per click point: its most confident segment, or None
    for f in f_sams:
        try:
            segments, _ = f.result()
        except Exception as e:
            print(f"SAM failed: {type(e).__name__}: {e}")
            segments = []
        best.append(max(segments, key=lambda s: s.confidence or 0) if segments else None)
    rep = repeats(history, actions)
    confident = skip_ok and not rep and all(
        seg is not None and (seg.confidence or 0) >= SAM_CONFIDENT and seg.contains(*p) for p, seg in zip(points, best)
    )
    late, rev = None, None
    if confident:
        late = f_review  # don't wait; it is logged when it lands
    else:
        try:
            rev = f_review.result()
        except Exception as e:  # a failed review shouldn't kill the run
            print(f"review failed: {type(e).__name__}: {e}")
    pool.shutdown(wait=False)

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

    under = (rev or {}).get("element_under_click", "")
    hit = (rev or {}).get("hits_intended_target", True)
    forward = (rev or {}).get("moves_task_forward", True)
    if sam_vouches and not hit:
        # The reviewer misread where the clicks land; its "no progress" call
        # usually follows from that misreading, so drop both.
        hit = forward = True
    w, h = screen_size
    candidates = [
        c for c in (rev or {}).get("candidates") or [] if 0 <= c["x"] < w and 0 <= c["y"] < h
    ]
    if not ((rev or {}).get("ambiguous") and len(candidates) >= 2):
        candidates = []
    reasons, plain = [], []
    if candidates:
        reasons.append("More than one fits: " + " or ".join(c["label"] for c in candidates))
        plain.append("Your task fits more than one of these.")
    if not hit:
        reasons.append(f"May miss — under the click: {under}" if under else "The click may miss its target")
        plain.append("It might miss what it's aiming at.")
    if not forward:
        reasons.append("This step may not move the task forward")
        plain.append("It might not help with your task.")
    if rep:
        reasons.append("Repeats an earlier action")
        plain.append("It already tried this once.")
    return Assessment(
        flagged=bool(reasons),
        reasons=reasons,
        review=rev,
        repeat=rep,
        target=target,
        box=box,
        sam_conf=sam_conf,
        on_mask=on_mask,
        candidates=candidates,
        plain=plain,
        seconds=time.time() - t0,
        late_review=late,
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

    choice is "yes", "no" (actions rewritten to the user's point), "stop", or
    "pause" (the user paused instead of answering).
    If the overlay can't ask, proceeds with the original actions.
    """
    if a.candidates:
        return _ask_which(overlay, driver, a, actions)
    x, y = driver.to_screen(*a.target)
    box = None
    if a.box:
        l, t = driver.to_screen(a.box[0], a.box[1])
        r, b = driver.to_screen(a.box[2], a.box[3])
        box = [l, t, r, b]
    print(f"asking ok: {(a.review or {}).get('element_under_click', '')} @({x},{y})", flush=True)
    answer = overlay.ask(
        x=x,
        y=y,
        box=box,
        intent=note,
        under=(a.review or {}).get("element_under_click", ""),
        reason=" ".join(a.plain),
        loupe=loupe(png, a.target),
    )
    if answer is None:
        print("overlay unavailable; proceeding without confirmation")
        return "yes", actions, None
    choice = answer.get("choice")
    if choice == "no" and answer.get("x") is not None:
        return "no", correct(actions, driver.to_shot(answer["x"], answer["y"])), answer
    if choice in ("stop", "pause"):
        return choice, [], answer
    return "yes", actions, answer


def _ask_which(
    overlay: Overlay, driver: MacDriver, a: Assessment, actions: list[dict]
) -> tuple[str, list[dict], dict | None]:
    """Ring every candidate of an ambiguous click and let the user pick one."""
    shown = []
    for c in a.candidates:
        x, y = driver.to_screen(c["x"], c["y"])
        control = _locate(c["label"], x, y)
        if control:
            l, t, w, h = control.frame
            box, (x, y) = [l, t, l + w, t + h], control.center
        else:
            box = [x - 60, y - 16, x + 60, y + 16]
        shown.append({"label": c["label"], "x": round(x), "y": round(y), "box": [round(v) for v in box]})
    print("asking which: " + "; ".join(f"{c['label']} @({c['x']},{c['y']})" for c in shown), flush=True)
    # The candidate nearest the planned click is the agent's own guess.
    px, py = driver.to_screen(*a.target)
    guess = min(range(len(shown)), key=lambda i: (shown[i]["x"] - px) ** 2 + (shown[i]["y"] - py) ** 2)
    shown[guess]["guess"] = True
    answer = overlay.ask(
        x=shown[guess]["x"],
        y=shown[guess]["y"],
        candidates=shown,
        intent=a.plain[0],  # the ambiguity; other doubts don't matter once the user picks
        reason="",
    )
    if answer is None:
        print("overlay unavailable; proceeding without confirmation")
        return "yes", actions, None
    choice = answer.get("choice")
    if choice == "pick" and 0 <= answer.get("index", -1) < len(shown):
        pick = shown[answer["index"]]
        answer["label"] = pick["label"]
        return "pick", correct(actions, driver.to_shot(pick["x"], pick["y"])), answer
    if choice == "no" and answer.get("x") is not None:
        return "no", correct(actions, driver.to_shot(answer["x"], answer["y"])), answer
    if choice in ("stop", "pause"):
        return choice, [], answer
    return "yes", actions, answer


LOCATE_RADIUS = 90  # points: how far from the model's estimate a candidate may really be
FILLER = {"option", "button", "item", "the", "a", "menu", "link", "field"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if w not in FILLER}


def _locate(label: str, x: float, y: float) -> Target | None:
    """The on-screen control a candidate means.

    The model's coordinates for a candidate can be off by a row, so look nearby
    for the control whose text shares the most words with its label (nearest
    wins a tie); without a text match, whatever control is at the point.
    """
    want = _words(label)
    best, best_score = None, 0
    for t in nearby(x, y, LOCATE_RADIUS):  # nearest first
        score = len(want & _words(t.label))
        if score > best_score:
            best, best_score = t, score
    return best or clickable_at(x, y)


def correct(actions: list[dict], point: tuple[int, int]) -> list[dict]:
    """Move the first click/drag to the user's point and drop later pointer actions.

    Actions after the next pointer action were planned around the wrong target,
    so they're dropped; the model re-plans from the next screenshot.
    """
    i = first_gated(actions)
    fixed = {k: v for k, v in actions[i].items() if k != "_snap"}  # the user's point, not a snap
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
