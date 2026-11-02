"""AVO Remote, the agent's side: what changes when a phone is paired.

The overlay starts the agent with AVO_REMOTE=1 while Remote is on (and
AVO_SOURCE=remote for a task sent from the phone). Then:

  - the countdown before an ordinary step lasts 2 s, not 1, so a "No" tapped
    on the phone can arrive (avo/confirm.py);
  - a question carries a JPEG of just the part of the screen it is about: the
    candidates' bounding box plus 40 px, with the same numbered rings as the
    overlay draws, at most 1080 px wide. Never the whole screen. It goes to the
    overlay with the question and from there only to the paired phone.

With Remote off nothing here runs, and AVO behaves as before.
"""

from __future__ import annotations

import base64
import io
import os

from PIL import Image, ImageDraw, ImageFont

MARGIN = 40  # screenshot px around the candidates
MAX_W = 1080
QUALITY = 80
REMOTE_COUNTDOWN_S = 2.0


def on() -> bool:
    return os.environ.get("AVO_REMOTE") == "1"


def source() -> str:
    return "remote" if os.environ.get("AVO_SOURCE") == "remote" else "mac"


def _font(size: int):
    for path in ("/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def crop(png: bytes, boxes: list[list[float]], numbered: bool = True) -> str | None:
    """A JPEG data URL of the region around `boxes` (screenshot px), each ringed (and numbered from 1)."""
    if not png or not boxes:
        return None
    img = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = img.size
    l = max(0, int(min(b[0] for b in boxes)) - MARGIN)
    t = max(0, int(min(b[1] for b in boxes)) - MARGIN)
    r = min(W, int(max(b[2] for b in boxes)) + MARGIN + (28 if numbered else 0))  # room for the number tags
    btm = min(H, int(max(b[3] for b in boxes)) + MARGIN)
    if r - l < 8 or btm - t < 8:
        return None
    region = img.crop((l, t, r, btm))
    d = ImageDraw.Draw(region)
    font = _font(15)
    for i, (bl, bt, br, bb) in enumerate(boxes, 1):
        box = (bl - l - 4, bt - t - 4, br - l + 4, bb - t + 4)
        # The overlay's ring: a white line with a dark edge, rounded.
        d.rounded_rectangle(box, radius=10, outline=(10, 10, 10), width=5)
        d.rounded_rectangle(box, radius=10, outline=(255, 255, 255), width=2)
        if numbered:
            cx, cy = box[2] + 14, (box[1] + box[3]) / 2
            d.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=(10, 10, 10), outline=(255, 255, 255), width=2)
            d.text((cx, cy), str(i), fill=(255, 255, 255), font=font, anchor="mm")
    if region.width > MAX_W:
        region = region.resize((MAX_W, round(region.height * MAX_W / region.width)), Image.LANCZOS)
    buf = io.BytesIO()
    region.save(buf, format="JPEG", quality=QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
