"""Confidence signals for a proposed action batch.

  - review: a second muse-spark call looks at the screenshot with the click
    points marked (plus a zoomed crop) and judges whether each click hits the
    intended element, whether the batch moves the task forward, and whether
    the click picks one of several options that fit the task equally well.
  - repeats: flags a batch that redoes a recent click/type batch for the third
    time, a sign the agent is stuck in a loop. No API call.
SAM target clarity lives in avo/sam.py.
"""

from __future__ import annotations

import io
import json
import math

from openai import OpenAI
from PIL import Image, ImageDraw

from avo.llm import MODEL, data_url, describe

POINTER_KINDS = ("click", "double_click", "move", "scroll")
CROP_W, CROP_H, CROP_ZOOM = 240, 150, 3

REVIEW_PROMPT = """\
You are checking another agent's next step before it is executed on a macOS screen.
Image 1 is the full screenshot. Each planned click is marked with a red ring and a number.
Image 2 is a zoomed crop around click 1 (the red crosshair is the exact click point).

Task: {task}
Steps already taken:
{past}
The agent says: {note}
Planned actions (pixel coordinates in image 1): {actions}

Judge strictly, as three separate questions:
1. Location only: what UI element is exactly under each click point? A point in the empty gap
   between two fields, just outside a button's edge, or on a heading next to a link hits
   nothing. hits_intended_target says whether the point lies on the element the agent says
   it wants; it is NOT about whether that element is the best thing to click now.
2. Progress: given the steps already taken and the current screen, does this batch move the
   task forward? Doing sub-steps in a different reasonable order (e.g. filling form fields in
   any order) is progress. Repeating an action that already happened, undoing finished work,
   or acting when the task already looks complete is not.
3. Ambiguity: does click 1 itself pick one of several on-screen options that all match what
   the task or the visible notes actually say? The options must be things click 1 could click
   instead (items in an open menu or list, buttons, rows). Opening a menu, card or form that
   will offer the choice later is not the choice yet: then ambiguous is false. Example: the notes say "Alex", and two different
   people named Alex are on screen. Judge this from the task and the screen, not from the
   agent's explanation, which may claim things the screen doesn't say. If the choice rests on
   an inference nothing on screen states (a budget task, so probably the person in Finance),
   it is still ambiguous: the user should decide, and the agent's pick is shown as its guess.
   It is not ambiguous when the task, notes or screen name the option outright (full name,
   role, or other detail that only one option has). If it is ambiguous, set ambiguous to true and list
   every option that fits as candidates (label: just the option's visible text, e.g. "Alex Chen";
   and its center x, y in image 1),
   including the one the agent picked. Otherwise ambiguous is false and candidates is empty.
Then give your confidence (0 to 1) that executing this batch now is correct, and say what
the screen should show right after it. Keep element_under_click and expected_after short
(English, at most 8 words each)."""

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "element_under_click": {"type": "string"},
        "hits_intended_target": {"type": "boolean"},
        "moves_task_forward": {"type": "boolean"},
        "confidence": {"type": "number"},
        "expected_after": {"type": "string"},
        "ambiguous": {"type": "boolean"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["label", "x", "y"],
            },
        },
    },
    "required": [
        "element_under_click",
        "hits_intended_target",
        "moves_task_forward",
        "confidence",
        "expected_after",
        "ambiguous",
        "candidates",
    ],
}


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def mark_clicks(png: bytes, actions: list[dict]) -> tuple[bytes, bytes | None]:
    """Full screenshot with numbered rings on pointer actions, and a zoomed crop of the first."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    points = [(a["x"], a["y"]) for a in actions if a["type"] in POINTER_KINDS]
    full = img.copy()
    draw = ImageDraw.Draw(full)
    for i, (x, y) in enumerate(points, 1):
        draw.ellipse((x - 14, y - 14, x + 14, y + 14), outline=(255, 0, 0), width=3)
        draw.text((x + 16, y - 20), str(i), fill=(255, 0, 0))
    if not points:
        return _png(full), None

    x, y = points[0]
    left = min(max(x - CROP_W // 2, 0), img.width - CROP_W)
    top = min(max(y - CROP_H // 2, 0), img.height - CROP_H)
    crop = img.crop((left, top, left + CROP_W, top + CROP_H)).resize(
        (CROP_W * CROP_ZOOM, CROP_H * CROP_ZOOM), Image.LANCZOS
    )
    cx, cy = (x - left) * CROP_ZOOM, (y - top) * CROP_ZOOM
    d = ImageDraw.Draw(crop)
    d.line((cx - 18, cy, cx + 18, cy), fill=(255, 0, 0), width=2)
    d.line((cx, cy - 18, cx, cy + 18), fill=(255, 0, 0), width=2)
    d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), outline=(255, 0, 0), width=2)
    return _png(full), _png(crop)


def review(
    client: OpenAI,
    task: str,
    past: list[str],
    png: bytes,
    actions: list[dict],
    note: str = "",
    effort: str | None = "minimal",
) -> dict:
    """Ask the model whether this batch is right. Returns the parsed review dict."""
    full, crop = mark_clicks(png, actions)
    prompt = REVIEW_PROMPT.format(
        task=task,
        past="\n".join(past) or "(none)",
        note=note or "(nothing)",
        actions=describe(actions),
    )
    content = [{"type": "input_text", "text": prompt}, {"type": "input_image", "image_url": data_url(full)}]
    if crop:
        content.append({"type": "input_image", "image_url": data_url(crop)})
    kwargs = {"reasoning": {"effort": effort}} if effort else {}
    response = client.responses.create(
        model=MODEL,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "review", "strict": True, "schema": REVIEW_SCHEMA}},
        **kwargs,
    )
    return json.loads(response.output_text)


# ---- repetition ----------------------------------------------------------

REPEAT_WINDOW = 6  # how many earlier batches to compare against
REPEAT_MIN = 2  # earlier matches needed: one retry (reopening a menu) is normal, a third go is a loop
REPEAT_PX = 15


def _signature(actions: list[dict]) -> list[tuple]:
    sig = []
    for a in actions:
        if a["type"] in ("wait", "screenshot"):
            continue
        point = (a["x"], a["y"]) if "x" in a and "y" in a else None
        detail = a.get("text") or "+".join(a.get("keys") or []) or a.get("button") or ""
        sig.append((a["type"], point, detail.lower()))
    return sig


def _same(a: list[tuple], b: list[tuple]) -> bool:
    if len(a) != len(b):
        return False
    for (ka, pa, da), (kb, pb, db) in zip(a, b):
        if ka != kb or da != db:
            return False
        if pa and pb and math.dist(pa, pb) > REPEAT_PX:
            return False
    return True


DEAD_WINDOW = 3  # how many recent no-effect batches a new batch is compared against


def repeats_dead(dead: list[list[dict]], actions: list[dict]) -> bool:
    """True if this batch is one of the last few that changed nothing on screen (within REPEAT_PX)."""
    sig = _signature(actions)
    return bool(sig) and any(_same(sig, _signature(d)) for d in dead[-DEAD_WINDOW:])


def repeats(history: list[list[dict]], actions: list[dict]) -> bool:
    """True if this click/type batch matches REPEAT_MIN of the last REPEAT_WINDOW batches."""
    if not any(a["type"] in ("click", "double_click", "type", "drag") for a in actions):
        return False  # scrolling or pressing keys again is often legitimate
    sig = _signature(actions)
    return sum(_same(sig, _signature(h)) for h in history[-REPEAT_WINDOW:]) >= REPEAT_MIN
