"""The confidence gate: before a pointer batch runs, decide whether to ask the user.

Each question costs the user an answer, so the gate asks on two primary
signals and lets the model review break ties:

  1. Choice: does the click pick one of several options that all fit the task
     (two people named Alex, two Professor Smiths)? Then always ask "Which one?".
  2. Location: is every click on a clear target? The accessibility tree decides
     when it knows (a control under the point: yes; layout-only space between
     controls: no); elsewhere SAM must be at least SAM_CLEAR sure, with the
     point on its mask.

Not ambiguous and a clear location: go. Not ambiguous but the location is in
doubt: the review's "hits its target" verdict decides. The review's "moves the
task forward" verdict and repeat detection no longer ask by themselves: on the
39 labelled baseline clicks they caused most false alarms (a done task's
repeated clicks), and a click that does nothing is caught afterwards by the
stuck detector (avo/effect.py). Both are still logged.

The choice question comes from the review (the same call), so the gate waits
for it on every click batch; SAM runs alongside.

ask_user() then points at the target on screen and asks "OK to click here?":
yes runs the batch, no lets the user click the right spot, stop ends the task.
For an ambiguous click it rings every candidate and asks "Which one did you mean?".
"""

from __future__ import annotations

import io
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from openai import OpenAI
from PIL import Image, ImageDraw

from avo import remote
from avo.confidence import repeats, review
from avo.driver import MacDriver
from avo.llm import data_url
from avo.overlay import Overlay
from avo.sam import segment
from avo.snap import Target, clickable_at, nearby

GATED_KINDS = ("click", "double_click", "drag")
POINTER_KINDS = ("click", "double_click", "move", "scroll", "drag")
MAX_HIGHLIGHT_FRACTION = 0.25  # ignore SAM boxes bigger than this share of the screen
# SAM confidence (point on the mask) at which a click's location counts as clear.
# On the 39 labelled baseline clicks, 0.7-0.9 all catch 14/14 wrong clicks with
# 1-2/25 false alarms (0.65 and below let a click between two fields through, at
# 0.668). On the 38 live asks you answered, 0.8 still asks about 18/23 real
# problems against 14/23 at 0.7: gaps between fields there scored 0.73-0.79.
SAM_CLEAR = 0.8
# Testing only: AVO_ASK_FIRST=1 asks "OK to click here?" on a run's first click
# even when the gate wouldn't, so the No + click-the-right-spot path can be tried
# on demand (11/01: the expense form got no question at all).
_ask_first = os.environ.get("AVO_ASK_FIRST") == "1"


@dataclass
class Assessment:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    review: dict | None = None
    repeat: bool = False  # logged; doesn't ask by itself
    target: tuple[int, int] | None = None  # screenshot px of the first gated action
    box: tuple[float, float, float, float] | None = None  # SAM box around it, screenshot px
    sam_conf: float | None = None
    on_mask: bool = False
    places: list[str | None] = field(default_factory=list)  # per click: the accessibility verdict, "on" / "gap" / None
    clear: bool = False  # every click on a clear target
    candidates: list[dict] = field(default_factory=list)  # ambiguous click: [{label, x, y}], screenshot px
    plain: list[str] = field(default_factory=list)  # the reasons, in words for the user
    seconds: float = 0.0
    controls: list[dict] = field(default_factory=list)  # controls near a stray click 1: [{label, x, y}], screenshot px
    meant: dict | None = None  # the one of them the review says click 1 meant: click its center instead

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
            "places": self.places,
            "clear": self.clear,
            "candidates": self.candidates,
            "controls": [{k: c[k] for k in ("label", "x", "y")} for c in self.controls],
            "meant": {k: self.meant[k] for k in ("label", "x", "y")} if self.meant else None,
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
    where: Callable[[int, int], str | None] | None = None,
    near: Callable[[int, int], list[dict]] | None = None,
) -> Assessment | None:
    """Score a batch; None if it has no click or drag to check.

    where: the accessibility verdict for a click point in screenshot px ("on",
    "gap" or None, see avo/snap.py location); None to go on SAM alone.
    near: the named controls around a point ({label, x, y}, screenshot px). When
    click 1 isn't on a control, the review is shown them and says which one the
    agent meant; the click then goes to that control's center (Assessment.meant).
    """
    i = first_gated(actions)
    if i is None:
        return None
    target = _point(actions[i])
    points = [p for a in actions if a["type"] in GATED_KINDS and (p := _point(a))]
    t0 = time.time()
    controls = []
    if near and where:
        try:
            # Only a click accessibility places in the empty gap between controls: a click
            # on content it can't name (an email row, a card) is not a miss, and listing
            # buttons then talked the review into a different target.
            if where(*target) == "gap":
                controls = near(*target)
        except Exception as e:
            print(f"accessibility check failed: {type(e).__name__}: {e}")
    pool = ThreadPoolExecutor(1 + len(points))
    f_review = pool.submit(review, client, task, past, png, actions, note, controls=controls or None)
    f_sams = [pool.submit(segment, client, png, points=[p]) for p in points]
    places = []
    for p in points:  # meanwhile: tens of ms, no API call
        try:
            places.append(where(*p) if where else None)
        except Exception as e:
            print(f"accessibility check failed: {type(e).__name__}: {e}")
            places.append(None)
    best = []  # per click point: its most confident segment, or None
    for f in f_sams:
        try:
            segments, _ = f.result()
        except Exception as e:
            print(f"SAM failed: {type(e).__name__}: {e}")
            segments = []
        best.append(max(segments, key=lambda s: s.confidence or 0) if segments else None)
    rev = None
    try:
        rev = f_review.result()
    except Exception as e:  # a failed review shouldn't kill the run
        print(f"review failed: {type(e).__name__}: {e}")
    pool.shutdown(wait=False)
    rep = repeats(history, actions)

    def clear_at(p, seg, place) -> bool:
        if place is not None:  # the accessibility tree knows: on a control, or in a gap
            return place == "on"
        return seg is not None and (seg.confidence or 0) >= SAM_CLEAR and seg.contains(*p)

    clear = all(clear_at(p, seg, place) for p, seg, place in zip(points, best, places))
    box, sam_conf, on_mask = None, None, False
    seg = best[0]
    if seg is not None:
        sam_conf = round(seg.confidence or 0, 3)
        on_mask = seg.contains(*target)
        l, t, r, b = seg.box
        if (r - l) * (b - t) <= MAX_HIGHLIGHT_FRACTION * screen_size[0] * screen_size[1]:
            box = seg.box

    under = (rev or {}).get("element_under_click", "")
    w, h = screen_size
    candidates = [
        c for c in (rev or {}).get("candidates") or [] if 0 <= c["x"] < w and 0 <= c["y"] < h
    ]
    if not ((rev or {}).get("ambiguous") and len(candidates) >= 2):
        candidates = []
    elif len({" ".join(c["label"].lower().split()) for c in candidates}) < len(candidates):
        # "2:00 PM or 2:00 PM" (10/31, two rooms): nobody can answer that, so it isn't a question.
        candidates = []
    meant = None
    k = (rev or {}).get("meant_control") or 0
    if controls and 1 <= k <= len(controls):
        meant = controls[k - 1]
    reasons, plain = [], []
    others_clear = all(clear_at(p, seg, place) for p, seg, place in list(zip(points, best, places))[1:])
    if candidates:
        reasons.append("More than one fits: " + " or ".join(c["label"] for c in candidates))
        plain.append("Your task fits more than one of these.")
    elif not clear and meant and others_clear:
        pass  # click 1 missed, but the review names the control it meant: AVO clicks that (agent.py)
    elif not clear:
        # The location is in doubt: the review breaks the tie. Without a review, ask.
        if rev is None or not rev.get("hits_intended_target", True):
            reasons.append(f"May miss — under the click: {under}" if under else "The click may miss its target")
            plain.append("It might miss what it's aiming at.")
    global _ask_first
    if _ask_first and not reasons:
        _ask_first = False
        reasons.append("Test: AVO_ASK_FIRST")
        plain.append("Test mode: AVO asks on the first click.")
    return Assessment(
        flagged=bool(reasons),
        reasons=reasons,
        review=rev,
        repeat=rep,
        target=target,
        box=box,
        sam_conf=sam_conf,
        on_mask=on_mask,
        places=places,
        clear=clear,
        candidates=candidates,
        plain=plain,
        seconds=time.time() - t0,
        controls=controls,
        meant=meant if (meant and not clear and not candidates) else None,
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
    overlay: Overlay, driver: MacDriver, a: Assessment, actions: list[dict], note: str, png: bytes,
    client: OpenAI | None = None,
) -> tuple[str, list[dict], dict | None]:
    """Point at the target and ask. Returns (choice, actions to run, raw answer).

    choice is "yes", "no" (actions rewritten to the user's point), "stop",
    "pause" (the user paused instead of answering), or "none" (said "none of
    these" or "no, ..." aloud: nothing runs; answer["said"] has their words).
    If the overlay can't ask, proceeds with the original actions.
    """
    if a.candidates:
        return _ask_which(overlay, driver, a, actions, png, client)
    x, y = driver.to_screen(*a.target)
    box = None
    if a.box:
        l, t = driver.to_screen(a.box[0], a.box[1])
        r, b = driver.to_screen(a.box[2], a.box[3])
        box = [l, t, r, b]
    print(f"asking ok: {(a.review or {}).get('element_under_click', '')} @({x},{y})", flush=True)
    shot_box = list(a.box) if a.box else [a.target[0] - 28, a.target[1] - 18, a.target[0] + 28, a.target[1] + 18]
    answer = overlay.ask(
        x=x,
        y=y,
        box=box,
        crop=remote.crop(png, [shot_box], numbered=False) if remote.on() else None,
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
    if choice in ("stop", "pause", "none"):
        return choice, [], answer
    return "yes", actions, answer


def _ring_from_sam(client: OpenAI, png: bytes, point: tuple[int, int], driver: MacDriver) -> list[float] | None:
    """The box of the element SAM segments at a point (screen points), or None."""
    try:
        segments, _ = segment(client, png, points=[point])
    except Exception as e:
        print(f"SAM failed: {type(e).__name__}: {e}")
        return None
    seg = max(segments, key=lambda s: s.confidence or 0, default=None)
    if seg is None:
        return None
    w, h = driver.geometry.shot_size
    l, t, r, b = seg.box
    if (r - l) * (b - t) > MAX_HIGHLIGHT_FRACTION * w * h:
        return None  # the whole page, not an element
    return [*driver.to_screen(l, t), *driver.to_screen(r, b)]


def _ask_which(
    overlay: Overlay, driver: MacDriver, a: Assessment, actions: list[dict], png: bytes = b"",
    client: OpenAI | None = None,
) -> tuple[str, list[dict], dict | None]:
    """Ring every candidate of an ambiguous click and let the user pick one.

    Each ring goes on the control whose text matches the candidate (macOS
    accessibility). Where that finds nothing (10/31, in Edge: every ring fell
    back to a small fixed box at the model's guess), poke the point so the
    browser builds its accessibility tree and look once more, then ring the
    element SAM segments at the guess.
    """
    shown, missing = [], []
    for c in a.candidates:
        x, y = driver.to_screen(c["x"], c["y"])
        control = _locate(c["label"], x, y)
        if control is None:
            clickable_at(x, y)
            time.sleep(0.2)
            control = _locate(c["label"], x, y)
        if control:
            l, t, w, h = control.frame
            box, (x, y) = [l, t, l + w, t + h], control.center
        else:
            box = [x - 60, y - 16, x + 60, y + 16]
            missing.append(len(shown))
        shown.append({"label": c["label"], "x": round(x), "y": round(y), "box": [round(v) for v in box]})
    if missing and client is not None and png:
        with ThreadPoolExecutor(len(missing)) as pool:
            boxes = list(pool.map(lambda i: _ring_from_sam(client, png, (a.candidates[i]["x"], a.candidates[i]["y"]), driver), missing))
        for i, box in zip(missing, boxes):
            if box:
                shown[i]["box"] = [round(v) for v in box]
                shown[i]["x"], shown[i]["y"] = round((box[0] + box[2]) / 2), round((box[1] + box[3]) / 2)
                shown[i]["ring"] = "sam"
    print("asking which: " + "; ".join(f"{c['label']} @({c['x']},{c['y']})" for c in shown), flush=True)
    # The candidate nearest the planned click is the agent's own guess.
    px, py = driver.to_screen(*a.target)
    guess = min(range(len(shown)), key=lambda i: (shown[i]["x"] - px) ** 2 + (shown[i]["y"] - py) ** 2)
    shown[guess]["guess"] = True
    crop = None
    if remote.on():  # the phone sees just this part of the screen, ringed and numbered like the overlay
        crop = remote.crop(png, [[*driver.to_shot(c["box"][0], c["box"][1]), *driver.to_shot(c["box"][2], c["box"][3])] for c in shown])
    answer = overlay.ask(
        x=shown[guess]["x"],
        y=shown[guess]["y"],
        candidates=shown,
        crop=crop,
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
    if choice in ("stop", "pause", "none"):
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


FIX_RADIUS = 48  # points: how far a click may be from the control the review names
# Words that say where the click is, not what's there ("Empty space above Reply button").
PLACE_WORDS = {"empty", "space", "blank", "area", "gap", "above", "below", "under", "over", "left", "right",
               "of", "near", "next", "to", "beside", "between", "just", "edge", "outside", "white", "background"}


def named_fix(a: Assessment, driver: MacDriver) -> tuple[list[float], Target] | None:
    """The control a "may miss" click was clearly meant for, to click instead of asking.

    11/01: the review said "Empty space above Reply button", a Reply button was
    right there, and the user still had to read a card and answer it. When the
    review's words name exactly one control within FIX_RADIUS of the click,
    AVO clicks that control (screen point returned). Never for a choice between
    options, and never when two nearby controls match.
    """
    if not a.flagged or a.candidates or not a.review or a.review.get("hits_intended_target", True):
        return None
    want = _words(a.review.get("element_under_click") or "") - PLACE_WORDS
    if not want:
        return None
    x, y = driver.to_screen(*a.target)
    matches = [t for t in nearby(x, y, FIX_RADIUS) if want & _words(t.label)]
    if len({tuple(round(v) for v in t.frame) for t in matches}) != 1:
        return None
    t = matches[0]
    return list(t.inside(*t.center)), t


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
