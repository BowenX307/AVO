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
from PIL import Image

SHOT_WIDTH = 1280
ACTION_PAUSE = 0.3  # seconds between actions in one batch
WAIT_SECONDS = 1.0  # duration of a "wait" action
SCROLL_PX_PER_LINE = 20  # screen points per pyautogui scroll unit (rough)

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


@dataclass
class Geometry:
    """How the last screenshot maps onto the screen."""

    left: int  # monitor origin, in points
    top: int
    retina: float  # raw capture pixels per screen point
    resize: float  # screenshot pixels per raw capture pixel
    shot_size: tuple[int, int]


class MacDriver:
    def __init__(self, shot_width: int = SHOT_WIDTH):
        self.shot_width = shot_width
        self.geometry: Geometry | None = None

    # ---- screenshots -------------------------------------------------------

    def screenshot(self) -> bytes:
        """Capture the main display, scale to shot_width, return PNG bytes."""
        with mss.MSS() as sct:
            mon = sct.monitors[1]  # main display, in points
            raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        retina = raw.size[0] / mon["width"]
        resize = self.shot_width / raw.size[0]
        shot_h = round(raw.size[1] * resize)
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

    # ---- actions -----------------------------------------------------------

    def execute(self, actions: list[dict]) -> None:
        """Run a computer_call action batch in order."""
        for i, action in enumerate(actions):
            if i:
                time.sleep(ACTION_PAUSE)
            self._execute_one(action)

    def _execute_one(self, a: dict) -> None:
        kind = a["type"]
        if kind == "click":
            x, y = self.to_screen(a["x"], a["y"])
            button = a.get("button", "left")
            if button in ("back", "forward"):
                # No mouse back/forward on a trackpad; use the browser shortcut.
                pyautogui.hotkey("command", "[" if button == "back" else "]", interval=0.05)
            else:
                button = {"wheel": "middle"}.get(button, button)
                pyautogui.click(x, y, button=button)
        elif kind == "double_click":
            x, y = self.to_screen(a["x"], a["y"])
            pyautogui.doubleClick(x, y)
        elif kind == "move":
            x, y = self.to_screen(a["x"], a["y"])
            pyautogui.moveTo(x, y, duration=0.1)
        elif kind == "drag":
            path = [self.to_screen(p["x"], p["y"]) for p in a["path"]]
            pyautogui.moveTo(*path[0])
            pyautogui.mouseDown()
            for p in path[1:]:
                pyautogui.moveTo(*p, duration=0.15)
            pyautogui.mouseUp()
        elif kind == "scroll":
            x, y = self.to_screen(a["x"], a["y"])
            pyautogui.moveTo(x, y)
            g = self.geometry
            to_lines = lambda d: round(d / g.resize / g.retina / SCROLL_PX_PER_LINE)  # noqa: E731
            # Model: positive scroll_y scrolls down. pyautogui: positive scrolls up.
            if a.get("scroll_y"):
                pyautogui.scroll(-to_lines(a["scroll_y"]))
            if a.get("scroll_x"):
                pyautogui.hscroll(to_lines(a["scroll_x"]))
        elif kind == "keypress":
            keys = [KEY_MAP.get(k.lower(), k.lower()) for k in a["keys"]]
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys, interval=0.05)
        elif kind == "type":
            self._type(a["text"])
        elif kind == "wait":
            time.sleep(WAIT_SECONDS)
        elif kind == "screenshot":
            pass  # the agent loop sends a fresh screenshot after every batch
        else:
            raise ValueError(f"unknown action type: {kind!r}")

    @staticmethod
    def _type(text: str) -> None:
        if text.isascii():
            pyautogui.write(text, interval=0.01)
        else:
            # pyautogui can't type non-ASCII (e.g. Chinese); paste it instead.
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            pyautogui.hotkey("command", "v", interval=0.05)
