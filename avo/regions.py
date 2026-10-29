"""The boxes the user drags over the screen before typing a task: A, then optionally B.

The model gets each box's position in the screenshot plus a close-up image of it,
so "move this there" or "assign A to the person in B" is grounded in what the
user pointed at. SAM 3.1 segments each box (the box is the prompt) and the
overlay fades the mask in over the selection, showing the user what AVO took
the selection to be.
"""

from __future__ import annotations

import base64
import io
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from PIL import Image, ImageFilter

from avo.driver import MacDriver
from avo.overlay import Overlay
from avo.sam import segment

CROP_PAD = 8  # screenshot px of context around each close-up
MIN_CROP_W = 320  # upscale smaller close-ups to this width

_pool = ThreadPoolExecutor(2)


@dataclass
class Region:
    label: str
    screen: tuple[int, int, int, int]  # screen points, as dragged
    shot: tuple[int, int, int, int]  # the same box in screenshot px
    closeup: bytes  # PNG


def prepare(driver: MacDriver, png: bytes, boxes: dict[str, tuple[int, int, int, int]]) -> list[Region]:
    img = Image.open(io.BytesIO(png))
    out = []
    for label, (l, t, r, b) in sorted(boxes.items()):
        sl, st = driver.to_shot(l, t)
        sr, sb = driver.to_shot(r, b)
        crop = img.crop((max(sl - CROP_PAD, 0), max(st - CROP_PAD, 0), min(sr + CROP_PAD, img.width), min(sb + CROP_PAD, img.height)))
        if crop.width < MIN_CROP_W:
            scale = MIN_CROP_W / crop.width
            crop = crop.resize((MIN_CROP_W, round(crop.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        out.append(Region(label, (l, t, r, b), (sl, st, sr, sb), buf.getvalue()))
    return out


def describe(regions: list[Region]) -> str:
    lines = [f"- {r.label}: ({r.shot[0]},{r.shot[1]})-({r.shot[2]},{r.shot[3]})" for r in regions]
    names = " and ".join(r.label for r in regions)
    return (
        "Before typing the task, the user dragged boxes over the screen to show what it is about "
        "(pixel boxes in the screenshot as it was then):\n" + "\n".join(lines) + "\n"
        f"The images after the screenshot are close-ups of {names}, in that order. In the task, "
        f"{names}, \"this\", \"that\" and \"here\" refer to these boxes."
    )


def _mask_image(png: bytes, seg, box: tuple[int, int, int, int]) -> str:
    """SAM's mask inside the selection: a soft white fill with a crisp white edge, as a data URL."""
    size = Image.open(io.BytesIO(png)).size
    mask = seg.full_mask(size).crop(box)
    edge = mask.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(3))
    out = Image.new("RGBA", mask.size, (255, 255, 255, 0))
    out.paste(Image.new("RGBA", mask.size, (255, 255, 255, 64)), (0, 0), mask)
    out.paste(Image.new("RGBA", mask.size, (255, 255, 255, 255)), (0, 0), edge)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def highlight(client: OpenAI, overlay: Overlay, png: bytes, regions: list[Region], log_dir: Path) -> list[Future]:
    """Segment each region in the background; show each mask on the overlay as it arrives."""

    def one(r: Region) -> None:
        segments, _ = segment(client, png, box=r.shot)
        if not segments:
            return
        best = max(segments, key=lambda s: s.confidence or 0)
        (log_dir / f"region_{r.label}.txt").write_text(
            f"box {r.shot} -> sam box {tuple(round(v) for v in best.box)} conf {best.confidence}\n"
        )
        overlay.send("regions", regions=[{"label": r.label, "box": list(r.screen), "mask": _mask_image(png, best, r.shot)}])

    return [_pool.submit(one, r) for r in regions]
