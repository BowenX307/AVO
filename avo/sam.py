"""SAM 3.1 segmentation on screenshots via the Meta Responses API.

Three prompt kinds, all in the screenshot's own pixel space:
  - text:  a short noun phrase ("folder icon"); returns every match
  - box:   (left, top, right, bottom), e.g. a rectangle the user dragged
  - point: (x, y), e.g. where the agent is about to click
Box and point prompts describe one object; text prompts may return many.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from meta_sam_parser import (
    CompletedOutcome,
    FramePrompt,
    PromptBox,
    PromptObject,
    PromptPoint,
    SegmentationBoxRecord,
    SegmentationMaskRecord,
    build_object_prompt,
    decode_mask_to_raster,
    image_segmentation_format,
)
from openai import OpenAI
from PIL import Image

MODEL = "sam-3.1"


@dataclass
class Segment:
    object_id: str
    box: tuple[float, float, float, float]  # left, top, right, bottom (half-open), image px
    confidence: float | None
    mask: Image.Image  # mode "L", 0/255, covering `box` (box-local)

    @property
    def area(self) -> int:
        """Mask pixel count in image pixels."""
        return sum(1 for v in self.mask.getdata() if v)

    def contains(self, x: float, y: float) -> bool:
        """Whether image point (x, y) lies on the mask."""
        left, top, right, bottom = self.box
        if not (left <= x < right and top <= y < bottom):
            return False
        mx, my = int(x - left), int(y - top)
        mx = min(mx, self.mask.width - 1)
        my = min(my, self.mask.height - 1)
        return self.mask.getpixel((mx, my)) > 0

    def full_mask(self, size: tuple[int, int]) -> Image.Image:
        """The mask pasted into a blank image of the full screenshot size."""
        canvas = Image.new("L", size, 0)
        canvas.paste(self.mask, (round(self.box[0]), round(self.box[1])))
        return canvas


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _parse(output_text: str) -> list[Segment]:
    parser = image_segmentation_format().create_parser()
    parser.push(output_text)
    result = parser.finish(CompletedOutcome()).result

    boxes = {r.object_id: r for r in result.records if isinstance(r, SegmentationBoxRecord)}
    segments = []
    for r in result.records:
        if not isinstance(r, SegmentationMaskRecord):
            continue
        b = r.bounds
        raster = decode_mask_to_raster(r.mask)
        local = Image.frombytes("L", (r.mask.width, r.mask.height), bytes(v * 255 for v in raster))
        # The raster may be coarser than the box; stretch it over the box.
        box_w, box_h = round(b.right - b.left), round(b.bottom - b.top)
        if local.size != (box_w, box_h):
            local = local.resize((max(box_w, 1), max(box_h, 1)), Image.NEAREST)
        box_rec = boxes.get(r.object_id)
        confidence = r.confidence if r.confidence is not None else getattr(box_rec, "confidence", None)
        segments.append(Segment(r.object_id, (b.left, b.top, b.right, b.bottom), confidence, local))
    return segments


def segment(
    client: OpenAI,
    png: bytes,
    *,
    text: str | None = None,
    box: tuple[int, int, int, int] | None = None,
    points: list[tuple[int, int]] | None = None,
    negative_points: list[tuple[int, int]] | None = None,
) -> tuple[list[Segment], str]:
    """Segment a PNG screenshot. Returns (segments, raw output text).

    Give either `text`, or a `box` and/or `points` for one object.
    `box` is (left, top, right, bottom) with right/bottom exclusive.
    """
    size = Image.open(io.BytesIO(png)).size
    if text is not None:
        prompt = text
    else:
        pts = [PromptPoint(x, y) for x, y in points or []]
        pts += [PromptPoint(x, y, "negative") for x, y in negative_points or []]
        prompt = build_object_prompt(
            size=size,
            objects=[
                PromptObject(
                    id=0,
                    prompts=[FramePrompt(box=PromptBox(*box) if box else None, points=pts)],
                )
            ],
        )

    response = client.responses.create(
        model=MODEL,
        metadata={"include_confidence": "true"},
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": _data_url(png)},
                ],
            }
        ],
    )
    return _parse(response.output_text), response.output_text


def render(png: bytes, segments: list[Segment], marks: list[tuple[int, int]] = ()) -> Image.Image:
    """Screenshot with each segment's mask tinted, its box outlined, and marks as dots."""
    from PIL import ImageDraw

    base = Image.open(io.BytesIO(png)).convert("RGBA")
    colors = [(184, 220, 106), (255, 140, 66), (90, 170, 255), (240, 90, 160), (160, 110, 255)]
    for i, seg in enumerate(segments):
        color = colors[i % len(colors)]
        tint = Image.new("RGBA", base.size, color + (110,))
        base.paste(tint, (0, 0), seg.full_mask(base.size).point(lambda v: 110 if v else 0))
        draw = ImageDraw.Draw(base)
        draw.rectangle(seg.box, outline=color + (255,), width=2)
        label = f"{seg.object_id}" + (f" {seg.confidence:.2f}" if seg.confidence is not None else "")
        draw.text((seg.box[0] + 3, max(seg.box[1] - 13, 0)), label, fill=color + (255,))
    draw = ImageDraw.Draw(base)
    for x, y in marks:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(255, 40, 40, 255), outline=(255, 255, 255, 255))
    return base.convert("RGB")
