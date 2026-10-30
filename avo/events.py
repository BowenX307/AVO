"""The mouse and keyboard events AVO posts, each one marked as AVO's own.

Every event is a Quartz CGEvent with AVO_MAGIC in its kCGEventSourceUserData
field, so the event tap in avo/human.py can tell AVO's input from the user's:
anything without the mark is a person touching the mouse or keyboard.
All coordinates are screen points.

Emergency stop, as with pyautogui before: fling the mouse into a screen corner
and the next event raises FailSafeException.
"""

from __future__ import annotations

import time

import Quartz
from pyautogui._pyautogui_osx import keyboardMapping  # US key codes, a plain table

AVO_MAGIC = 0x41564F  # "AVO"
CATCH_UP = 0.01  # seconds for the OS to take in each event (as pyautogui waits)
MOVE_STEP_S = 0.01  # interpolation step for moves with a duration
CORNER_PT = 1  # this close to a screen corner counts as the emergency stop

MODIFIER_FLAGS = {
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
}
SHIFTED = set('~!@#$%^&*()_+{}|:"<>?')

BUTTONS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter),
}


class FailSafeException(Exception):
    """The user flung the mouse into a screen corner to stop AVO."""


_keyboard = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def _post(event) -> None:
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, AVO_MAGIC)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def is_avo(event) -> bool:
    return Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == AVO_MAGIC


def position() -> tuple[float, float]:
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def failsafe_check() -> None:
    x, y = position()
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    right, bottom = b.origin.x + b.size.width - 1, b.origin.y + b.size.height - 1
    for cx, cy in ((b.origin.x, b.origin.y), (right, b.origin.y), (b.origin.x, bottom), (right, bottom)):
        if abs(x - cx) <= CORNER_PT and abs(y - cy) <= CORNER_PT:
            raise FailSafeException("mouse in a screen corner: emergency stop")


def _mouse(kind: int, x: float, y: float, button: int, clicks: int = 0) -> None:
    event = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), button)
    if clicks:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
    _post(event)
    time.sleep(CATCH_UP)


def move(x: float, y: float, duration: float = 0.0, button: str | None = None) -> None:
    """Move the pointer, in a straight line over `duration` seconds; with a button held, it drags."""
    failsafe_check()
    kind, code = (BUTTONS[button][2], BUTTONS[button][3]) if button else (Quartz.kCGEventMouseMoved, 0)
    x0, y0 = position()
    steps = max(1, round(duration / MOVE_STEP_S))
    for i in range(1, steps + 1):
        t = i / steps
        _mouse(kind, x0 + (x - x0) * t, y0 + (y - y0) * t, code)
        if steps > 1:
            time.sleep(MOVE_STEP_S - CATCH_UP)


def down(x: float, y: float, button: str = "left") -> None:
    failsafe_check()
    _mouse(BUTTONS[button][0], x, y, BUTTONS[button][3], 1)


def up(x: float, y: float, button: str = "left") -> None:
    _mouse(BUTTONS[button][1], x, y, BUTTONS[button][3], 1)


def click(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    """Click at (x, y); clicks=2 is a real double click (click state 1, then 2)."""
    move(x, y)
    press_kind, release_kind, _, code = BUTTONS[button]
    for n in range(1, clicks + 1):
        _mouse(press_kind, x, y, code, n)
        _mouse(release_kind, x, y, code, n)


def drag(path: list[tuple[float, float]], segment_s: float = 0.15) -> None:
    move(*path[0])
    down(*path[0])
    for x, y in path[1:]:
        move(x, y, duration=segment_s, button="left")
    up(*path[-1])


def scroll(lines_y: int = 0, lines_x: int = 0) -> None:
    """One wheel event, in lines; positive lines_y scrolls up (content moves down), as on a wheel."""
    failsafe_check()
    event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, lines_y, lines_x)
    _post(event)


def _key(code: int, down_: bool, flags: int) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(_keyboard, code, down_)
    Quartz.CGEventSetFlags(event, flags)
    _post(event)
    time.sleep(0.02)


def chord(keys: list[str]) -> None:
    """Press a key or a combination, e.g. ["enter"] or ["command", "v"] (pyautogui key names).

    Every key event carries the modifier flags itself. Pressing the modifier as
    a separate key and relying on macOS to remember it is held (pyautogui's way)
    lost the modifier when another app watched modifier keys: cmd+v typed "v".
    """
    failsafe_check()
    flags = 0
    for k in keys:
        flags |= MODIFIER_FLAGS.get(k, 0)
    main = [k for k in keys if k not in MODIFIER_FLAGS]
    if not main:  # modifiers alone: press and release them
        codes = [keyboardMapping[k] for k in keys]
        for c in codes:
            _key(c, True, flags)
        for c in reversed(codes):
            _key(c, False, 0)
        return
    for k in main:
        extra = Quartz.kCGEventFlagMaskShift if len(k) == 1 and (k.isupper() or k in SHIFTED) else 0
        code = keyboardMapping.get(k.lower() if len(k) == 1 else k)
        if code is None:  # e.g. "fn": skip it, as pyautogui did
            print(f"unknown key {k!r}; skipped")
            continue
        _key(code, True, flags | extra)
        _key(code, False, flags | extra)
