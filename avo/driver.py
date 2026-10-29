"""macOS screen driver: capture screenshots and execute computer_call actions.

Two coordinate layers sit between the model and the mouse:
  1. Resize: screenshots are scaled to a fixed width (1280) so the model sees a
     stable image size for the whole task.
  2. Retina: the raw capture may be in physical pixels while pyautogui moves in
     screen points (typically 2x apart on Retina displays).
The model returns coordinates in the screenshot it saw; to_screen() undoes both.
"""

from __future__ import annotations

import io
import subprocess
import time
from dataclasses import dataclass

import mss
import pyautogui
import Quartz
from PIL import Image
from pyautogui._pyautogui_osx import keyboardMapping

from avo.overlay import Overlay

SHOT_WIDTH = 1280
ACTION_PAUSE = 0.3  # seconds between actions in one batch
WAIT_SECONDS = 1.0  # duration of a "wait" action
# Scrolling. The docs don't give scroll_x/scroll_y units, but muse-spark sends small
# counts (always 6 so far), i.e. wheel notches, not pixels. Values above
# SCROLL_NOTCH_MAX are taken as screenshot pixels instead.
SCROLL_PX_PER_LINE = 40  # screen points per pyautogui scroll line (measured in Chrome)
SCROLL_LINES_PER_NOTCH = 2  # 6 notches ~ half a screen
SCROLL_NOTCH_MAX = 50
SCROLL_LINE_PAUSE = 0.015  # one line at a time, so it reads as a smooth scroll
GLIDE_MS = 450  # overlay cursor travel time before each pointer action
SNAP_PAUSE = 0.25  # beat between showing the model's aim and gliding to the snapped spot

pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort
pyautogui.PAUSE = 0.05

# Model key names (lowercased) -> pyautogui key names on macOS.
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


MODIFIER_FLAGS = {
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
}


def chord(keys: list[str]) -> None:
    """Press a key combination, e.g. ["command", "v"].

    pyautogui.hotkey presses the modifier as a separate key and relies on macOS
    to remember it is held; with another app watching modifier keys, that state
    got lost and cmd+v typed a bare "v" into the page. Here every key event
    carries the modifier flags itself.
    """
    flags = 0
    for k in keys:
        flags |= MODIFIER_FLAGS.get(k, 0)
    main = [k for k in keys if k not in MODIFIER_FLAGS]
    if not main:  # modifiers alone
        pyautogui.hotkey(*keys, interval=0.05)
        return
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for k in main:
        code = keyboardMapping[k]
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(source, code, down)
            Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.02)


OVERLAY_TITLES = ("AVO pointer", "AVO island", "AVO question")  # overlay/*.html <title>s
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


@dataclass
class Geometry:
    """How the last screenshot maps onto the screen."""

    left: int  # monitor origin, in points
    top: int
    retina: float  # raw capture pixels per screen point
    resize: float  # screenshot pixels per raw capture pixel
    shot_size: tuple[int, int]


class MacDriver:
    def __init__(self, shot_width: int = SHOT_WIDTH, overlay: Overlay | None = None):
        self.shot_width = shot_width
        self.geometry: Geometry | None = None
        self.overlay = overlay or Overlay(url=None)

    # ---- screenshots -------------------------------------------------------

    def screenshot(self) -> bytes:
        """Capture the main display, scale to shot_width, return PNG bytes.

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
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

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

    def execute(self, actions: list[dict]) -> None:
        """Run a computer_call action batch in order."""
        for i, action in enumerate(actions):
            if i:
                time.sleep(ACTION_PAUSE)
            self._execute_one(action)

    def _point(self, x: int, y: int) -> None:
        """Glide the overlay cursor to (x, y) so the user sees where we're going."""
        if self.overlay.active:
            self.overlay.send("move", x=x, y=y, ms=GLIDE_MS)
            time.sleep(GLIDE_MS / 1000)

    def _show_snap(self, a: dict) -> None:
        """For a snapped click, first show where the model aimed, then the fix."""
        snapped = a.get("_snap")
        if snapped and self.overlay.active:
            self._point(*self.to_screen(*snapped["from"]))
            self.overlay.send("snap", label=snapped["label"])
            time.sleep(SNAP_PAUSE)

    def _execute_one(self, a: dict) -> None:
        kind = a["type"]
        if kind == "click":
            x, y = self.to_screen(a["x"], a["y"])
            button = a.get("button", "left")
            self._show_snap(a)
            self._point(x, y)
            self.overlay.send("click", x=x, y=y, button=button)
            if button in ("back", "forward"):
                # No mouse back/forward on a trackpad; use the browser shortcut.
                chord(["command", "[" if button == "back" else "]"])
            else:
                button = {"wheel": "middle"}.get(button, button)
                pyautogui.click(x, y, button=button)
        elif kind == "double_click":
            x, y = self.to_screen(a["x"], a["y"])
            self._show_snap(a)
            self._point(x, y)
            self.overlay.send("click", x=x, y=y, button="double")
            pyautogui.doubleClick(x, y)
        elif kind == "move":
            x, y = self.to_screen(a["x"], a["y"])
            self._point(x, y)
            pyautogui.moveTo(x, y, duration=0.1)
        elif kind == "drag":
            path = [self.to_screen(p["x"], p["y"]) for p in a["path"]]
            self._point(*path[0])
            pyautogui.moveTo(*path[0])
            pyautogui.mouseDown()
            for p in path[1:]:
                self.overlay.send("move", x=p[0], y=p[1], ms=150)
                pyautogui.moveTo(*p, duration=0.15)
            pyautogui.mouseUp()
        elif kind == "scroll":
            x, y = self.to_screen(a["x"], a["y"])
            self._point(x, y)
            self.overlay.send("scroll", dy=a.get("scroll_y") or 0)
            pyautogui.moveTo(x, y)
            # Model: positive scroll_y scrolls down. pyautogui: positive scrolls up.
            self._scroll(pyautogui.scroll, -self._lines(a.get("scroll_y") or 0))
            self._scroll(pyautogui.hscroll, self._lines(a.get("scroll_x") or 0))
        elif kind == "keypress":
            keys = [KEY_MAP.get(k.lower(), k.lower()) for k in a["keys"]]
            self.overlay.send("keys", keys=keys)
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                chord(keys)
        elif kind == "type":
            self.overlay.send("type", text=a["text"])
            self._type(a["text"])
        elif kind == "wait":
            time.sleep(WAIT_SECONDS)
        elif kind == "screenshot":
            pass  # the agent loop sends a fresh screenshot after every batch
        else:
            raise ValueError(f"unknown action type: {kind!r}")

    def _lines(self, delta: float) -> int:
        """A model scroll delta -> pyautogui scroll lines."""
        if abs(delta) <= SCROLL_NOTCH_MAX:
            return round(delta * SCROLL_LINES_PER_NOTCH)
        g = self.geometry
        return round(delta / g.resize / g.retina / SCROLL_PX_PER_LINE)

    @staticmethod
    def _scroll(fn, lines: int) -> None:
        step = 1 if lines > 0 else -1
        for _ in range(abs(lines)):
            fn(step)
            time.sleep(SCROLL_LINE_PAUSE)

    @staticmethod
    def _type(text: str) -> None:
        # Paste rather than type keystrokes: keystrokes go through the active
        # input method (e.g. Pinyin swallows "example.com"), and pyautogui
        # can't type non-ASCII at all. Restores the user's clipboard text after.
        saved = subprocess.run(["pbpaste"], capture_output=True).stdout
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        chord(["command", "v"])
        time.sleep(0.2)  # let the target app read the clipboard before restoring
        subprocess.run(["pbcopy"], input=saved, check=True)
