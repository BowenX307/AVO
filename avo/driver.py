"""macOS screen driver: capture screenshots and execute computer_call actions.

Two coordinate layers sit between the model and the mouse:
  1. Resize: screenshots are scaled to a fixed width (1280) so the model sees a
     stable image size for the whole task.
  2. Retina: the raw capture may be in physical pixels while the mouse moves in
     screen points (typically 2x apart on Retina displays).
The model returns coordinates in the screenshot it saw; to_screen() undoes both.

Every mouse and keyboard event goes out through avo/events.py, marked as AVO's,
so avo/human.py can tell them from the user's. A batch stops part-way when
interrupt() says so (the user took over, or paused): Interrupted. Before each
pointer action the screen around the target is compared with the screenshot
the model planned on; if it changed, the rest of the batch is skipped:
ScreenChanged.
"""

from __future__ import annotations

import io
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

import mss
import Quartz
from PIL import Image, ImageChops, ImageStat

from avo import events
from avo.desktop import OVERLAY_TITLES
from avo.events import chord
from avo.overlay import Overlay

SHOT_WIDTH = 1280
ACTION_PAUSE = 0.3  # seconds between actions in one batch
WAIT_SECONDS = 1.0  # duration of a "wait" action
# Scrolling. The docs don't give scroll_x/scroll_y units, but muse-spark sends small
# counts (always 6 so far), i.e. wheel notches, not pixels. Values above
# SCROLL_NOTCH_MAX are taken as screenshot pixels instead.
SCROLL_PX_PER_LINE = 40  # screen points per wheel line (measured in Chrome)
SCROLL_LINES_PER_NOTCH = 2  # 6 notches ~ half a screen
SCROLL_NOTCH_MAX = 50
SCROLL_LINE_PAUSE = 0.015  # one line at a time, so it reads as a smooth scroll
GLIDE_MS = 450  # overlay cursor travel time before each pointer action
SNAP_PAUSE = 0.25  # beat between showing the model's aim and gliding to the snapped spot
POLL_S = 0.02  # how often waits check for an interruption
FRESH_BOX = 96  # screenshot px: the square around a target compared before acting on it
FRESH_THRESHOLD = 0.08  # mean colour change (0-1) in that square that counts as "the screen changed"
FRESH_KINDS = ("click", "double_click", "drag", "scroll")

# Model key names (lowercased) -> key names in avo/events.py (pyautogui's).
KEY_MAP = {
    "enter": "enter",
    "return": "enter",
    "esc": "esc",
    "escape": "esc",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "option",
    "option": "option",
    "opt": "option",
    "cmd": "command",
    "command": "command",
    "meta": "command",
    "super": "command",
    "win": "command",
}


IMAGE_OPTIONS = (
    Quartz.kCGWindowImageBoundsIgnoreFraming
    | Quartz.kCGWindowImageShouldBeOpaque
    | Quartz.kCGWindowImageNominalResolution
)  # as mss uses


def capture_below_overlay(mon: dict) -> Image.Image | None:
    """The screen as it is underneath the overlay's windows, or None if the overlay isn't up.

    CGWindowListCreateImage can capture only what lies below a given window; the
    overlay floats above everything else, so below its lowest window is the
    whole screen minus AVO.
    """
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    ours = [w["kCGWindowNumber"] for w in windows if w.get("kCGWindowName") in OVERLAY_TITLES]
    if not ours:
        return None
    lowest = ours[-1]  # the list runs front to back
    rect = Quartz.CGRectMake(mon["left"], mon["top"], mon["width"], mon["height"])
    image = Quartz.CGWindowListCreateImage(rect, Quartz.kCGWindowListOptionOnScreenBelowWindow, lowest, IMAGE_OPTIONS)
    if image is None:
        return None
    w, h = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
    row = Quartz.CGImageGetBytesPerRow(image)
    return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", row, 1).convert("RGB")


class Interrupted(Exception):
    """The batch stopped part-way because interrupt() fired."""

    def __init__(self, reason: str, done: int):
        super().__init__(f"interrupted ({reason}) after {done} action(s)")
        self.reason, self.done = reason, done


class ScreenChanged(Exception):
    """The screen around the next target changed since the model looked; it wasn't run."""

    def __init__(self, done: int, diff: float, png: bytes):
        super().__init__(f"screen changed before action {done + 1} (diff {diff:.3f})")
        self.done, self.diff, self.png = done, diff, png


def region_diff(a: Image.Image, b: Image.Image, x: float, y: float, box: int = FRESH_BOX) -> float:
    """Mean colour difference (0-1, over R, G and B) between two screenshots in a box x box square around (x, y).

    Colour, not grey: in a live run a blue Approve button was replaced by a brown
    "Request changes" button of the same brightness, a grey-level change of 0.033.
    In colour it is 0.129.
    """
    w, h = a.size
    left = min(max(round(x) - box // 2, 0), max(w - box, 0))
    top = min(max(round(y) - box // 2, 0), max(h - box, 0))
    crop = (left, top, left + box, top + box)
    diff = ImageChops.difference(a.crop(crop).convert("RGB"), b.crop(crop).convert("RGB"))
    return sum(ImageStat.Stat(diff).mean) / 3 / 255


def _target(a: dict) -> tuple[float, float] | None:
    if a["type"] == "drag":
        path = a.get("path") or []
        return (path[0]["x"], path[0]["y"]) if path else None
    return (a["x"], a["y"]) if "x" in a else None


@dataclass
class Geometry:
    """How the last screenshot maps onto the screen."""

    left: int  # monitor origin, in points
    top: int
    retina: float  # raw capture pixels per screen point
    resize: float  # screenshot pixels per raw capture pixel
    shot_size: tuple[int, int]


class MacDriver:
    def __init__(
        self,
        shot_width: int = SHOT_WIDTH,
        overlay: Overlay | None = None,
        interrupt: Callable[[], str | None] | None = None,
        fresh_threshold: float | None = FRESH_THRESHOLD,
    ):
        """interrupt: polled during a batch; a non-empty reason stops it (Interrupted).
        fresh_threshold: None turns off the look-again check before pointer actions."""
        self.shot_width = shot_width
        self.geometry: Geometry | None = None
        self.overlay = overlay or Overlay(url=None)
        self.interrupt = interrupt or (lambda: None)
        self.fresh_threshold = fresh_threshold
        self.fresh_checks: list[dict] = []  # this batch's look-again checks, for the run log

    # ---- screenshots -------------------------------------------------------

    def screenshot(self) -> bytes:
        """Capture the main display, scale to shot_width, return PNG bytes."""
        return _png(self.grab())

    def grab(self) -> Image.Image:
        """The main display scaled to shot_width.

        The overlay's windows are left out (see capture_below_overlay), so the
        model never sees AVO's own cursor, island or questions, while a screen
        recording still does.
        """
        with mss.MSS() as sct:
            mon = sct.monitors[1]  # main display, in points
            img = capture_below_overlay(mon)
            if img is None:
                raw = sct.grab(mon)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        retina = img.width / mon["width"]
        resize = self.shot_width / img.width
        shot_h = round(img.height * resize)
        if img.size != (self.shot_width, shot_h):
            img = img.resize((self.shot_width, shot_h), Image.LANCZOS)

        self.geometry = Geometry(mon["left"], mon["top"], retina, resize, img.size)
        return img

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot pixel coordinates -> screen point coordinates."""
        g = self.geometry
        if g is None:
            raise RuntimeError("screenshot() must be called before executing actions")
        px = x / g.resize / g.retina + g.left
        py = y / g.resize / g.retina + g.top
        return round(px), round(py)

    def to_shot(self, x: float, y: float) -> tuple[int, int]:
        """Screen point coordinates -> screenshot pixel coordinates (inverse of to_screen)."""
        g = self.geometry
        if g is None:
            raise RuntimeError("screenshot() must be called first")
        return round((x - g.left) * g.retina * g.resize), round((y - g.top) * g.retina * g.resize)

    # ---- actions -----------------------------------------------------------

    def execute(self, actions: list[dict], seen: bytes | None = None) -> None:
        """Run a computer_call action batch in order.

        seen: the screenshot the model planned on. Before each click, double
        click, drag or scroll, the screen around its target is compared with it;
        a change over fresh_threshold raises ScreenChanged with the new screen.
        Raises Interrupted as soon as interrupt() fires.
        """
        self.fresh_checks = []
        before = Image.open(io.BytesIO(seen)).convert("RGB") if seen and self.fresh_threshold is not None else None
        for i, action in enumerate(actions):
            if i:
                self._sleep(ACTION_PAUSE, i)
            self._check_interrupt(i)
            point = _target(action) if action["type"] in FRESH_KINDS else None
            if before is not None and point is not None:
                now = self.grab()
                diff = region_diff(before, now, *point) if now.size == before.size else 1.0
                changed = diff > self.fresh_threshold
                self.fresh_checks.append({"action": i, "point": list(point), "diff": round(diff, 4), "skipped": changed})
                if changed:
                    raise ScreenChanged(i, diff, _png(now))
            self._execute_one(action, i)

    def _check_interrupt(self, done: int) -> None:
        reason = self.interrupt()
        if reason:
            raise Interrupted(reason, done)

    def _sleep(self, seconds: float, done: int) -> None:
        """time.sleep that ends the batch the moment interrupt() fires."""
        end = time.time() + seconds
        while True:
            self._check_interrupt(done)
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(POLL_S, left))

    def _point(self, x: int, y: int, done: int = 0) -> None:
        """Glide the overlay cursor to (x, y) so the user sees where we're going."""
        if self.overlay.active:
            self.overlay.send("move", x=x, y=y, ms=GLIDE_MS)
            self._sleep(GLIDE_MS / 1000, done)

    def _show_snap(self, a: dict, done: int) -> None:
        """For a snapped click, first show where the model aimed, then the fix."""
        snapped = a.get("_snap")
        if snapped and self.overlay.active:
            self._point(*self.to_screen(*snapped["from"]), done)
            self.overlay.send("snap", label=snapped["label"])
            self._sleep(SNAP_PAUSE, done)

    def _execute_one(self, a: dict, done: int = 0) -> None:
        """One action; `done` is how many of the batch ran before it (for Interrupted)."""
        kind = a["type"]
        if kind == "click":
            x, y = self.to_screen(a["x"], a["y"])
            button = a.get("button", "left")
            self._show_snap(a, done)
            self._point(x, y, done)
            self._check_interrupt(done)
            self.overlay.send("click", x=x, y=y, button=button)
            if button in ("back", "forward"):
                # No mouse back/forward on a trackpad; use the browser shortcut.
                chord(["command", "[" if button == "back" else "]"])
            else:
                events.click(x, y, button={"wheel": "middle"}.get(button, button))
        elif kind == "double_click":
            x, y = self.to_screen(a["x"], a["y"])
            self._show_snap(a, done)
            self._point(x, y, done)
            self._check_interrupt(done)
            self.overlay.send("click", x=x, y=y, button="double")
            events.click(x, y, clicks=2)
        elif kind == "move":
            x, y = self.to_screen(a["x"], a["y"])
            self._point(x, y, done)
            events.move(x, y, duration=0.1)
        elif kind == "drag":
            path = [self.to_screen(p["x"], p["y"]) for p in a["path"]]
            self._point(*path[0], done)
            self._check_interrupt(done)
            events.move(*path[0])
            events.down(*path[0])
            try:
                for p in path[1:]:
                    self.overlay.send("move", x=p[0], y=p[1], ms=150)
                    events.move(*p, duration=0.15, button="left")
            finally:
                events.up(*events.position())  # never leave the button held
        elif kind == "scroll":
            x, y = self.to_screen(a["x"], a["y"])
            self._point(x, y, done)
            self.overlay.send("scroll", dy=a.get("scroll_y") or 0)
            events.move(x, y)
            # Model: positive scroll_y scrolls down. Wheel lines: positive scrolls up.
            self._scroll(-self._lines(a.get("scroll_y") or 0), 0, done)
            self._scroll(0, -self._lines(a.get("scroll_x") or 0), done)
        elif kind == "keypress":
            keys = [KEY_MAP.get(k.lower(), k.lower()) for k in a["keys"]]
            self.overlay.send("keys", keys=keys)
            chord(keys)
        elif kind == "type":
            self.overlay.send("type", text=a["text"])
            self._type(a["text"])
        elif kind == "wait":
            self._sleep(WAIT_SECONDS, done)
        elif kind == "screenshot":
            pass  # the agent loop sends a fresh screenshot after every batch
        else:
            raise ValueError(f"unknown action type: {kind!r}")

    def _lines(self, delta: float) -> int:
        """A model scroll delta -> wheel lines."""
        if abs(delta) <= SCROLL_NOTCH_MAX:
            return round(delta * SCROLL_LINES_PER_NOTCH)
        g = self.geometry
        return round(delta / g.resize / g.retina / SCROLL_PX_PER_LINE)

    def _scroll(self, lines_y: int, lines_x: int, done: int) -> None:
        """One line at a time, so it reads as a smooth scroll (and can stop part-way)."""
        for _ in range(max(abs(lines_y), abs(lines_x))):
            self._check_interrupt(done)
            events.scroll((lines_y > 0) - (lines_y < 0), (lines_x > 0) - (lines_x < 0))
            time.sleep(SCROLL_LINE_PAUSE)
            lines_y -= (lines_y > 0) - (lines_y < 0)
            lines_x -= (lines_x > 0) - (lines_x < 0)

    @staticmethod
    def _type(text: str) -> None:
        # Paste rather than type keystrokes: keystrokes go through the active
        # input method (e.g. Pinyin swallows "example.com"), and key codes
        # can't type non-ASCII at all. Restores the user's clipboard text after.
        saved = subprocess.run(["pbpaste"], capture_output=True).stdout
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        chord(["command", "v"])
        time.sleep(0.2)  # let the target app read the clipboard before restoring
        subprocess.run(["pbcopy"], input=saved, check=True)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
