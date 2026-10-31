"""Notices actions that change nothing on screen: the sign of a stuck agent.

Keys pressed into the wrong window, a click between two fields, a page that is
already at the top: the screen stays the same, the model sees the same picture
and tries the same thing again. On 10/31 that was cmd+L pressed 19 times into
AVO's own overlay. The gate's repeat check only covers clicks; this covers
every kind of action, by comparing the screen before and after a batch.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops

# A batch "changed nothing" when fewer pixels than this differ (at screenshot
# size, 1280x832 here). Measured over every saved run: actions that did nothing
# differ by 0 pixels (12 once, a hover tint); the smallest real effect, a
# field getting focus, was 125. A blinking caret is ~15-30.
MIN_CHANGED_PX = 40
PIXEL_DIFF = 32  # a pixel counts as changed when a channel moves by more than this (0-255)
MENU_BAR = 0.03  # top share of the screen left out: the clock ticks there
STUCK_AFTER = 3  # batches in a row that changed nothing
# Settling (driver.settle): two frames in a row count as still when fewer pixels
# than this differ, or the change is no wider than a text caret. A loading
# page is never still: Chrome's tab spinner keeps turning.
STILL_PX = 8
CARET_W = 3

# Batches of only these expect no change, so they neither count nor clear a streak.
PASSIVE_KINDS = ("wait", "screenshot", "move")
KEY_NAMES = {"cmd": "⌘", "shift": "⇧", "alt": "⌥", "option": "⌥", "ctrl": "⌃", "control": "⌃"}

NO_EFFECT_NOTE = (
    "Nothing on screen changed after your last actions. If you pressed keys or typed, the "
    "keyboard focus may be in another window or field: click the target first. Don't repeat "
    "the same action; try something different."
)
STUCK_NOTE = (
    "AVO paused because your last {n} actions changed nothing on screen. The user has resumed "
    "and may have fixed something. Look at this screenshot afresh and take a different approach."
)


def _image(png: bytes | Image.Image) -> Image.Image:
    return png if isinstance(png, Image.Image) else Image.open(io.BytesIO(png))


Box = tuple[int, int, int, int]  # (l, t, r, b) in screenshot pixels


def _changes(before: bytes | Image.Image, after: bytes | Image.Image, area: Box | None = None) -> Image.Image | None:
    """A mask of the pixels that differ, within `area` (default: the whole screen),
    menu bar left out; None if the sizes differ."""
    a, b = _image(before).convert("RGB"), _image(after).convert("RGB")
    if a.size != b.size:
        return None
    w, h = a.size
    l, t, r, btm = area or (0, 0, w, h)
    box = (max(0, l), max(int(h * MENU_BAR), t), min(w, r), min(h, btm))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        return None
    r, g, bl = ImageChops.difference(a.crop(box), b.crop(box)).split()
    most = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    return most.point(lambda v: 255 if v > PIXEL_DIFF else 0)


def changed_pixels(before: bytes | Image.Image, after: bytes | Image.Image, area: Box | None = None) -> int:
    """How many pixels differ between two screenshots (within `area`), menu bar left out."""
    mask = _changes(before, after, area)
    return mask.histogram()[255] if mask else _image(after).size[0] * _image(after).size[1]


def still(before: bytes | Image.Image, after: bytes | Image.Image, area: Box | None = None) -> bool:
    """Has the screen (within `area`) stopped changing between these two frames, a blinking caret aside?"""
    mask = _changes(before, after, area)
    if mask is None:
        return False
    if mask.histogram()[255] < STILL_PX:
        return True
    l, _, r, _ = mask.getbbox()
    return r - l <= CARET_W


def plain(actions: list[dict]) -> str:
    """What a batch did, in a few words for the island: "pressing ⌘L", "typing"."""
    kinds = [a["type"] for a in actions if a["type"] not in PASSIVE_KINDS]
    if not kinds:
        return "waiting"
    if kinds[-1] == "keypress":
        keys = next(a.get("keys") or [] for a in reversed(actions) if a["type"] == "keypress")
        return "pressing " + "".join(KEY_NAMES.get(k.lower(), k.upper() if len(k) == 1 else k) for k in keys)
    return {
        "type": "typing",
        "click": "clicking there",
        "double_click": "clicking there",
        "scroll": "scrolling",
        "drag": "dragging",
    }.get(kinds[-1], kinds[-1])


class EffectWatch:
    """Counts batches in a row that changed nothing on screen."""

    def __init__(self, stuck_after: int = STUCK_AFTER):
        self.stuck_after = stuck_after
        self.streak = 0
        self.last_px = 0  # changed pixels of the last batch, for the run log

    def record(self, actions: list[dict], before, after, area: Box | None = None) -> str:
        """'changed', 'passive' (only waited), 'none' (no effect) or 'stuck' (no effect, stuck_after times in a row).

        area: where to look, the window being worked in (desktop.work_area).
        """
        self.last_px = changed_pixels(before, after, area)
        if self.last_px >= MIN_CHANGED_PX:
            self.streak = 0
            return "changed"
        if all(a["type"] in PASSIVE_KINDS for a in actions):
            return "passive"
        self.streak += 1
        return "stuck" if self.streak >= self.stuck_after else "none"

    def reset(self) -> None:
        """The screen may have changed under us (the user took over, a click was called off)."""
        self.streak = 0
