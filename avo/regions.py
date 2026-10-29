"""The boxes the user drags over the screen before typing a task: A, then optionally B.

The model gets each box drawn, labelled, on its first screenshot, plus the
boxes' coordinates as text, so "move this there" or "assign A to the person in
B" is grounded in what the user pointed at. (Separate close-up images confused
it: it clicked at a close-up's own coordinates.) SAM 3.1 segments each box
(the box is the prompt) and the overlay fades the mask in over the selection,
showing the user what AVO took the selection to be.
"""

from __future__ import annotations

import base64
import io
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFilter

from avo.driver import MacDriver
from avo.overlay import Overlay
from avo.sam import segment

MARK = (255, 0, 200)  # box colour on the model's screenshot (only the model sees it)

_pool = ThreadPoolExecutor(2)


@dataclass
class Region:
    label: str
    screen: tuple[int, int, int, int]  # screen points, as dragged
    shot: tuple[int, int, int, int]  # the same box in screenshot px


def prepare(driver: MacDriver, boxes: dict[str, tuple[int, int, int, int]]) -> list[Region]:
    out = []
    for label, (l, t, r, b) in sorted(boxes.items()):
        out.append(Region(label, (l, t, r, b), (*driver.to_shot(l, t), *driver.to_shot(r, b))))
    return out


def mark(png: bytes, regions: list[Region]) -> bytes:
    """The screenshot with each region drawn as a labelled box, for the model."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for r in regions:
        l, t, rr, b = r.shot
        draw.rectangle((l - 3, t - 3, rr + 3, b + 3), outline=MARK, width=3)
        draw.rectangle((l - 3, t - 22, l + 17, t - 3), fill=MARK)
        draw.text((l + 2, t - 20), r.label, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def describe(regions: list[Region], first: bool) -> str:
    lines = [f"- {r.label}: ({r.shot[0]},{r.shot[1]})-({r.shot[2]},{r.shot[3]})" for r in regions]
    names = " and ".join(r.label for r in regions)
    where = (
        "They are drawn on this screenshot as labelled magenta boxes; the boxes are not part of the screen."
        if first
        else "The screen may have changed since."
    )
    return (
        "Before typing the task, the user dragged boxes over the screen to show what it is about "
        "(pixel boxes in the first screenshot):\n" + "\n".join(lines) + "\n" + where + "\n"
        f"In the task, {names}, \"this\", \"that\" and \"here\" refer to these boxes."
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
