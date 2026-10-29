"""Snap a click that lands just off its target onto the nearest clickable element.

muse-spark's clicks are often 10-30 px off: in the gap between two form fields,
just below a button. macOS accessibility (AX) knows the exact frame of every
button, link and text field on screen, Chrome's web content included, so when
a click lands on nothing clickable we hit-test the neighbourhood and move the
click onto the closest clickable element. No API call; a few ms.

The click is left alone unless it lands on blank space: on something clickable
(whether it's the *right* thing is the review's job) or on content such as text
the page may handle clicks on itself. It is also left alone when nothing
clickable is near, or when two candidates are about equally close (the gate
asks the user then).
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
# Roles that are just layout: a click on one of these, with no text of its own, hit nothing.
BLANK = {"AXGroup", "AXWebArea", "AXScrollArea", "AXLayoutArea", "AXSplitGroup", "AXUnknown", "AXGenericElement"}
AMBIGUOUS = 0.8  # don't snap unless the nearest is clearly nearer than the runner-up
INSET = 4  # keep the snapped point this far inside the element's edge

_system = AX.AXUIElementCreateSystemWide()


@dataclass
class Target:
    role: str
    label: str
    frame: tuple[float, float, float, float]  # left, top, width, height

    @property
    def center(self) -> tuple[float, float]:
        l, t, w, h = self.frame
        return l + w / 2, t + h / 2

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
            return Target(role, _text(el)[:40], frame)
        el = _attr(el, "AXParent")
    return None


def _text(el) -> str:
    """An element's own words: title, description, placeholder, or its children's text."""
    for name in ("AXTitle", "AXDescription", "AXPlaceholderValue"):
        value = _attr(el, name)
        if value:
            return str(value)
    words = [str(_attr(k, "AXValue") or _attr(k, "AXTitle") or "") for k in (_attr(el, "AXChildren") or [])[:6]]
    return " ".join(w for w in words if w).strip()


def on_blank(x: float, y: float) -> bool:
    """True if (x, y) is on layout-only space: no control, no text."""
    err, el = AX.AXUIElementCopyElementAtPosition(_system, x, y, None)
    if err or el is None:
        return False
    return _attr(el, "AXRole") in BLANK and not _text(el)


def nearby(x: float, y: float, radius: float = RADIUS) -> list[Target]:
    """Clickable elements whose hit area comes within `radius` of (x, y), nearest first."""
    seen: dict[tuple, Target] = {}
    steps = range(-int(radius), int(radius) + 1, GRID)
    for dy in steps:
        for dx in steps:
            if dx * dx + dy * dy > radius * radius:
                continue
            t = clickable_at(x + dx, y + dy)
            if t:
                seen.setdefault(tuple(round(v) for v in t.frame), t)
    return sorted(seen.values(), key=lambda t: t.distance(x, y))


def warm_up(x: float, y: float) -> None:
    """Chrome builds its accessibility tree lazily, on the first AX query."""
    clickable_at(x, y)


def snap(x: float, y: float) -> tuple[float, float, Target] | None:
    """Where to click instead of (x, y), or None to leave the click alone."""
    if clickable_at(x, y) or not on_blank(x, y):
        return None
    ranked = nearby(x, y)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0].distance(x, y) > AMBIGUOUS * ranked[1].distance(x, y):
        return None
    sx, sy = ranked[0].inside(x, y)
    if math.dist((x, y), (sx, sy)) < 1:  # already on it (the first hit-test came back empty)
        return None
    return sx, sy, ranked[0]
