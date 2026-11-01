"""Did a drag move something into something? AVO checks it itself.

11/01, "move 20261016.pdf into math": the drag worked, the file vanished from
the desktop, and the model, not finding it any more, opened Finder to look for
it. Told "if it's gone, it moved", it still went looking. So AVO looks before
and after the drag: what was under the start point (the file) and under the
drop point (the folder). Afterwards the file is gone and the folder is still
where it was: it moved into the folder, and the model is told so as a fact,
with both names.

Finder's desktop icons are read from Finder's own list (avo/desktop.py), so a
window in front doesn't hide them; anywhere else, from the accessibility
hit-test at the two points (a list row, a board card, a column).

Also catches a drag that starts and ends at the same point: the model's first
drag was like that twice on 11/01, and it moves nothing.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from avo.desktop import desktop_icons
from avo.snap import item_at

MIN_DRAG_PX = 6  # screenshot px: a drag shorter than this doesn't move anything
ICON_PAD = (8, 8, 8, 30)  # points around a desktop icon's image (left, top, right, bottom: its name is below)

ZERO_DRAG_NOTE = (
    "Your drag started and ended at the same point, so nothing moved. It was NOT executed. "
    "A drag needs a path from the item to where it should be dropped: the first point on the "
    "item, the last point on the destination."
)


COVER_PAD = 60  # screenshot px around a drag's ends that the effect check also looks at


def cover(area: tuple[int, int, int, int] | None, actions: list[dict]) -> tuple[int, int, int, int] | None:
    """The effect check's area (screenshot px), grown to take in where each drag started and ended.

    The area is the window in front; a desktop icon dragged into a folder is
    outside it, and the move looked like nothing happened. None stays None (whole screen).
    """
    if area is None:
        return None
    l, t, r, b = area
    for a in actions:
        for p in (a.get("path") or [])[:1] + (a.get("path") or [])[-1:] if a["type"] == "drag" else []:
            l, t = min(l, p["x"] - COVER_PAD), min(t, p["y"] - COVER_PAD)
            r, b = max(r, p["x"] + COVER_PAD), max(b, p["y"] + COVER_PAD)
    return max(l, 0), max(t, 0), r, b


def zero_length(actions: list[dict]) -> bool:
    """True if a drag in the batch goes nowhere."""
    for a in actions:
        if a["type"] == "drag":
            path = a.get("path") or []
            if len(path) < 2 or math.dist((path[0]["x"], path[0]["y"]), (path[-1]["x"], path[-1]["y"])) < MIN_DRAG_PX:
                return True
    return False


def _icon_at(icons: dict[str, tuple], x: float, y: float) -> str:
    l_, t_, r_, b_ = ICON_PAD
    hits = [
        (math.dist((x, y), (l + w / 2, t + h / 2)), name)
        for name, (l, t, w, h) in icons.items()
        if l - l_ <= x <= l + w + r_ and t - t_ <= y <= t + h + b_
    ]
    return min(hits)[1] if hits else ""


@dataclass
class Drag:
    start: tuple[float, float]  # screen points
    end: tuple[float, float]
    item: str  # what was under the start before the drag
    target: str  # what was under the drop point
    desktop: bool  # both are desktop icons


@dataclass
class Moved:
    item: str
    target: str

    def note(self) -> str:
        return (
            f"AVO checked: “{self.item}” is no longer where you picked it up, and you dropped it on "
            f"“{self.target}”, so it is now in “{self.target}”. That move is done and worked. If it was "
            "all the task asked, reply now in one sentence with no more actions. Don't open "
            f"“{self.target}” or search for “{self.item}” to check."
        )


def before(actions: list[dict], to_screen: Callable[[float, float], tuple[float, float]]) -> list[Drag]:
    """What each drag in the batch picks up and where it drops it, looked at before it runs."""
    drags = [a for a in actions if a["type"] == "drag" and len(a.get("path") or []) >= 2]
    if not drags:
        return []
    icons = desktop_icons()
    out = []
    for a in drags:
        start = to_screen(a["path"][0]["x"], a["path"][0]["y"])
        end = to_screen(a["path"][-1]["x"], a["path"][-1]["y"])
        on_desktop = _icon_at(icons, *start), _icon_at(icons, *end)
        if all(on_desktop) and on_desktop[0] != on_desktop[1]:
            out.append(Drag(start, end, *on_desktop, desktop=True))
        else:
            out.append(Drag(start, end, item_at(*start), item_at(*end), desktop=False))
    return out


def after(drags: list[Drag]) -> list[Moved]:
    """The drags that moved their item into what it was dropped on."""
    moved = []
    icons = desktop_icons() if any(d.desktop for d in drags) else {}
    for d in drags:
        if not d.item or not d.target or d.item == d.target:
            continue
        if d.desktop:
            ok = d.item not in icons and d.target in icons
        else:
            # The item's place now shows something else, and the drop point still
            # shows the destination (not the item, which would mean it was only moved over).
            ok = item_at(*d.start) != d.item and item_at(*d.end) == d.target
        if ok:
            moved.append(Moved(d.item, d.target))
    return moved
