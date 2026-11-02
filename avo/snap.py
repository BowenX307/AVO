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
All coordinates here are screen points, the space mouse events and AX share.
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
# Table parts count too: a grid of buttons laid out in a <table> (11/02, the room
# demo: a click between two "2:00 PM" buttons landed on an empty AXCell, so it
# wasn't snapped and AVO asked "OK to click here?").
BLANK = {"AXGroup", "AXWebArea", "AXScrollArea", "AXLayoutArea", "AXSplitGroup", "AXUnknown", "AXGenericElement",
         "AXCell", "AXRow", "AXColumn", "AXTable"}
AMBIGUOUS = 0.8  # don't snap unless the nearest is clearly nearer than the runner-up
INSET = 4  # keep the snapped point this far inside the element's edge
DESCEND, MAX_KIDS = 3, 40  # how many parents, and children each, to look through past an empty element

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


def _contains(el, x: float, y: float) -> bool:
    f = _frame(el)
    return bool(f) and f[0] <= x < f[0] + f[2] and f[1] <= y < f[1] + f[3]


def _descend(el, x: float, y: float):
    """The control under (x, y) that a hit-test on layout element `el` missed, if any.

    11/01: hit-testing a Chrome tab returns an empty AXGroup over the tab strip,
    not the tab (an AXRadioButton, a sibling of that group), so a click on a tab
    looked like a click on nothing: AVO snapped it down onto the address bar,
    or asked. Look at the children of the group and its parents (and into a tab
    group) for a control containing the point. A few dozen AX calls, so only
    for the one point being judged, not for every point nearby() probes.
    """
    for _ in range(DESCEND):
        el = _attr(el, "AXParent") if el is not None else None
        if el is None:
            return None
        for kid in (_attr(el, "AXChildren") or [])[:MAX_KIDS]:
            role = _attr(kid, "AXRole")
            if role in CLICKABLE and _contains(kid, x, y):
                return kid
            if role == "AXTabGroup" and _contains(kid, x, y):
                for tab in (_attr(kid, "AXChildren") or [])[:MAX_KIDS]:
                    if _attr(tab, "AXRole") in CLICKABLE and _contains(tab, x, y):
                        return tab
    return None


def clickable_at(x: float, y: float, deep: bool = False) -> Target | None:
    """The clickable element at a screen point (or its clickable ancestor).

    deep: also look past an empty layout element the hit-test stopped at (_descend).
    """
    err, el = AX.AXUIElementCopyElementAtPosition(_system, x, y, None)
    if deep and not err and el is not None and _attr(el, "AXRole") in BLANK and not _text(el):
        el = _descend(el, x, y) or el
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


def item_at(x: float, y: float) -> str:
    """The name of what's at a screen point: a file or folder icon, a row, a card ("" if none).

    Used to check a drag (avo/agent.py): Finder's desktop icons are AXImage
    elements titled with the file's name, which the hit-test finds even
    though they aren't controls.
    """
    err, el = AX.AXUIElementCopyElementAtPosition(_system, x, y, None)
    for _ in range(ANCESTORS):
        if err or el is None:
            return ""
        for name in ("AXTitle", "AXFilename", "AXValue", "AXDescription"):
            value = _attr(el, name)
            if isinstance(value, str) and value.strip():
                return value.strip()[:80]
        el = _attr(el, "AXParent")
    return ""


def on_blank(x: float, y: float) -> bool:
    """True if (x, y) is on layout-only space: no control, no text."""
    err, el = AX.AXUIElementCopyElementAtPosition(_system, x, y, None)
    if err or el is None:
        return False
    return _attr(el, "AXRole") in BLANK and not _text(el) and _descend(el, x, y) is None


def focused() -> Target | None:
    """The element with keyboard focus: where the next keys or typed text go."""
    el = _attr(_system, "AXFocusedUIElement")
    if el is None:
        return None
    frame = _frame(el)
    if not frame or frame[2] * frame[3] == 0:
        return None
    return Target(_attr(el, "AXRole") or "", _text(el)[:40], frame)


def in_page(x: float, y: float) -> bool:
    """True if (x, y) is inside a web page's content, not the browser's own toolbar or extensions."""
    err, el = AX.AXUIElementCopyElementAtPosition(_system, x, y, None)
    for _ in range(40):
        if err or el is None:
            return False
        if _attr(el, "AXRole") == "AXWebArea":
            return True
        el = _attr(el, "AXParent")
    return False


def location(x: float, y: float) -> str | None:
    """What the accessibility tree says about a click point, for the gate (avo/gate.py).

    "on" a control; "gap" when the point is on layout-only space with controls
    nearby (a click between two fields, which snap() leaves alone); None when it
    can't tell, e.g. a canvas or an app without accessibility info.
    """
    if clickable_at(x, y, deep=True):
        return "on"
    if on_blank(x, y) and nearby(x, y):
        return "gap"
    return None


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
    if clickable_at(x, y, deep=True) or not on_blank(x, y):
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
