"""Snap a click that lands just off its target onto the nearest clickable element.

muse-spark's clicks are often 10-30 px off: in the gap between two form fields,
just below a button. macOS accessibility (AX) knows the exact frame of every
button, link and text field on screen, Chrome's web content included, so when
a click lands on nothing clickable we hit-test the neighbourhood and move the
click onto the closest clickable element. No API call; a few ms.

The click is left alone when it already sits on something clickable (whether
it's the *right* thing is the review's job), when nothing clickable is near,
or when two candidates are about equally close (the gate asks the user then).
All coordinates here are screen points, the space pyautogui and AX share.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import ApplicationServices as AX

CLICKABLE = {
    "AXButton", "AXLink", "AXTextField", "AXTextArea", "AXComboBox", "AXPopUpButton",
    "AXMenuButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXMenuBarItem",
    "AXDockItem", "AXSlider", "AXIncrementor", "AXDisclosureTriangle", "AXTab",
}
RADIUS = 36  # how far to look for a target, in points
GRID = 6  # hit-test spacing, in points
ANCESTORS = 3  # a link's text is a child of the link: look this far up
AMBIGUOUS = 0.8  # don't snap unless the nearest is clearly nearer than the runner-up
INSET = 4  # keep the snapped point this far inside the element's edge

_system = AX.AXUIElementCreateSystemWide()


@dataclass
class Target:
    role: str
    label: str
    frame: tuple[float, float, float, float]  # left, top, width, height

    def distance(self, x: float, y: float) -> float:
        """How far a click at (x, y) is from aiming at this element.

        Across, the distance to the element's span (a wide text field can be
        aimed at anywhere along it); up and down, the distance to its middle
        line, so a click in the gap between two fields belongs to the nearer one.
        """
        l, t, w, h = self.frame
        dx = max(l - x, 0, x - (l + w))
        return math.hypot(dx, y - (t + h / 2))

    def inside(self, x: float, y: float) -> tuple[float, float]:
        """The point in the element closest to (x, y), kept INSET from its edges."""
        l, t, w, h = self.frame
        sx = min(max(x, l + INSET), l + w - INSET) if w > 2 * INSET else l + w / 2
        sy = min(max(y, t + INSET), t + h - INSET) if h > 2 * INSET else t + h / 2
        return sx, sy


def _attr(el, name):
    err, value = AX.AXUIElementCopyAttributeValue(el, name, None)
    return None if err else value


def _frame(el) -> tuple[float, float, float, float] | None:
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    _, p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
    _, s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    return p.x, p.y, s.width, s.height


def clickable_at(x: float, y: float) -> Target | None:
    """The clickable element at a screen point (or its clickable ancestor)."""
    err, el = AX.AXUIElementCopyElementAtPosition(_system, x, y, None)
    for _ in range(ANCESTORS + 1):
        if err or el is None:
            return None
        role = _attr(el, "AXRole")
        if role in CLICKABLE:
            frame = _frame(el)
            if not frame or frame[2] * frame[3] == 0:
                return None
            label = _attr(el, "AXTitle") or _attr(el, "AXDescription") or _attr(el, "AXPlaceholderValue") or ""
            return Target(role, str(label)[:40], frame)
        el = _attr(el, "AXParent")
    return None


def warm_up(x: float, y: float) -> None:
    """Chrome builds its accessibility tree lazily, on the first AX query."""
    clickable_at(x, y)


def snap(x: float, y: float) -> tuple[float, float, Target] | None:
    """Where to click instead of (x, y), or None to leave the click alone."""
    if clickable_at(x, y):
        return None
    seen: dict[tuple, Target] = {}
    steps = range(-RADIUS, RADIUS + 1, GRID)
    for dy in steps:
        for dx in steps:
            if dx * dx + dy * dy > RADIUS * RADIUS:
                continue
            t = clickable_at(x + dx, y + dy)
            if t:
                seen.setdefault(tuple(round(v) for v in t.frame), t)
    if not seen:
        return None
    ranked = sorted(seen.values(), key=lambda t: t.distance(x, y))
    if len(ranked) > 1 and ranked[0].distance(x, y) > AMBIGUOUS * ranked[1].distance(x, y):
        return None
    sx, sy = ranked[0].inside(x, y)
    if math.dist((x, y), (sx, sy)) < 1:  # already on it (the first hit-test came back empty)
        return None
    return sx, sy, ranked[0]
