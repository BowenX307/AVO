"""Confidence signals for a proposed action batch.

  - review: a second muse-spark call looks at the screenshot with the click
    points marked (plus a zoomed crop) and judges whether each click hits the
    intended element and whether the batch moves the task forward.
  - repeats: flags a batch that redoes a recent click/type batch, a sign the
    agent is stuck in a loop. No API call.
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

Judge strictly, as two separate questions:
1. Location only: what UI element is exactly under each click point? A point in the empty gap
   between two fields, just outside a button's edge, or on a heading next to a link hits
   nothing. hits_intended_target says whether the point lies on the element the agent says
   it wants; it is NOT about whether that element is the best thing to click now.
2. Progress: given the steps already taken and the current screen, does this batch move the
   task forward? Doing sub-steps in a different reasonable order (e.g. filling form fields in
   any order) is progress. Repeating an action that already happened, undoing finished work,
   or acting when the task already looks complete is not.
Then give your confidence (0 to 1) that executing this batch now is correct, and say what
the screen should show right after it. Write element_under_click and expected_after in short
Chinese (at most 25 characters each)."""

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "element_under_click": {"type": "string"},
        "hits_intended_target": {"type": "boolean"},
        "moves_task_forward": {"type": "boolean"},
        "confidence": {"type": "number"},
        "expected_after": {"type": "string"},
    },
    "required": [
        "element_under_click",
        "hits_intended_target",
        "moves_task_forward",
        "confidence",
        "expected_after",
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


def repeats(history: list[list[dict]], actions: list[dict]) -> bool:
    """True if this click/type batch matches one of the last REPEAT_WINDOW batches."""
    if not any(a["type"] in ("click", "double_click", "type", "drag") for a in actions):
        return False  # scrolling or pressing keys again is often legitimate
    sig = _signature(actions)
    return any(_same(sig, _signature(h)) for h in history[-REPEAT_WINDOW:])
