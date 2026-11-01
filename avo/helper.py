"""A small process the overlay keeps running for voice input.

    uv run python -m avo.helper        (overlay/main.js starts it)

Two jobs, one JSON object per line:
  - The summon key. A tap of right Option (press and release, nothing else in
    between) starts listening; while AVO works, the same tap stops it. A tap,
    not a hold: holding a key down is itself hard for some people. Electron's
    global shortcuts can't bind a lone modifier, so this listens with a
    listen-only event tap (Input Monitoring, which AVO already uses).
      out: {"event": "summon"}
  - Words for the speech recogniser. Names on screen ("Smith", "Physics 212",
    "Reply") are read through macOS accessibility and passed as keywords, so
    "Smith" isn't heard as "Smyth".
      in:  {"id": 3, "cmd": "keywords", "bundle": "com.google.Chrome"}
      out: {"id": 3, "keywords": ["Reply", "Anna Smith", ...]}
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import deque

import ApplicationServices as AX
import Quartz
from AppKit import NSRunningApplication

from avo.events import is_avo
from avo.snap import _attr, _frame, clickable_at

RIGHT_OPTION = 61  # kVK_RightOption
TAP_S = 0.45  # a press and release within this, with nothing in between, is a tap
MODIFIERS = (
    Quartz.kCGEventFlagMaskShift | Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskSecondaryFn
)
MAX_KEYWORDS = 80
MAX_KEYWORD_CHARS = 30  # Meta rejects the whole session over a longer keyword
BUDGET_S = 0.4  # time to spend walking the accessibility tree
MAX_NODES = 3000
NAMED_ROLES = {"AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXTab", "AXRadioButton", "AXCheckBox",
               "AXPopUpButton", "AXCell", "AXRow", "AXHeading", "AXTextField", "AXTextArea"}
TEXT_ROLES = {"AXStaticText"}
PROPER = re.compile(r"\b(?:Prof\.|Dr\.)?\s?[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,2}(?:\s+\d{2,4})?")
COMMON = {"The", "This", "That", "Your", "You", "Hi", "Hello", "Hey", "Just", "Our", "Please", "Select", "Re"}

_out = threading.Lock()


def emit(obj: dict) -> None:
    with _out:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ---- the summon key ----

class TapDetector:
    """Right Option pressed and released quickly, on its own."""

    def __init__(self):
        self.down_at: float | None = None

    def feed(self, kind: int, keycode: int, flags: int, now: float) -> bool:
        """True when this event completes a tap."""
        if kind == Quartz.kCGEventFlagsChanged and keycode == RIGHT_OPTION:
            if flags & Quartz.kCGEventFlagMaskAlternate:  # pressed
                self.down_at = None if flags & MODIFIERS else now
                return False
            tapped = self.down_at is not None and now - self.down_at <= TAP_S
            self.down_at = None
            return tapped
        if kind != Quartz.kCGEventFlagsChanged or keycode != RIGHT_OPTION:
            self.down_at = None  # Option used with a key or a click: not a tap
        return False


def watch_keys() -> None:
    if not Quartz.CGPreflightListenEventAccess():
        emit({"event": "error", "text": "no Input Monitoring permission: the summon key won't work"})
        return
    detector = TapDetector()
    kinds = [Quartz.kCGEventFlagsChanged, Quartz.kCGEventKeyDown, Quartz.kCGEventLeftMouseDown,
             Quartz.kCGEventRightMouseDown, Quartz.kCGEventOtherMouseDown]
    mask = 0
    for k in kinds:
        mask |= Quartz.CGEventMaskBit(k)

    def callback(_proxy, kind, event, _refcon):
        if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(tap, True)
            return event
        try:
            if not is_avo(event) and detector.feed(
                kind,
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode),
                Quartz.CGEventGetFlags(event),
                time.monotonic(),
            ):
                emit({"event": "summon"})
        except Exception as err:  # never let the tap die on one bad event
            emit({"event": "error", "text": f"{type(err).__name__}: {err}"})
        return event

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionListenOnly, mask, callback, None
    )
    if tap is None:
        emit({"event": "error", "text": "could not create the event tap"})
        return
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    Quartz.CFRunLoopRun()


# ---- words on screen ----

def texts(bundle: str | None):
    """(role, text) for the words on the app's front window, breadth first, within BUDGET_S."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) if bundle else []
    if not apps:
        return
    app = AX.AXUIElementCreateApplication(apps[0].processIdentifier())
    AX.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)  # Chrome, Electron: build the web tree
    window = _attr(app, "AXFocusedWindow") or next(iter(_attr(app, "AXWindows") or []), None)
    if window is None:
        return
    if frame := _frame(window):
        # Chrome puts a page into the tree only once something asks about a point
        # on it; the first read after that is still partial, later ones are full.
        l, t, w, h = frame
        clickable_at(l + w / 2, t + h / 2)
    queue = deque([window])
    seen, t0 = 0, time.monotonic()
    while queue and seen < MAX_NODES and time.monotonic() - t0 < BUDGET_S:
        el = queue.popleft()
        seen += 1
        role = _attr(el, "AXRole") or ""
        found = [_attr(el, n) for n in ("AXTitle", "AXDescription")]
        if role in TEXT_ROLES or role in ("AXLink", "AXButton"):
            found.append(_attr(el, "AXValue"))
        for t in found:
            if isinstance(t, str) and t.strip():
                yield role, " ".join(t.split())
        queue.extend(_attr(el, "AXChildren") or [])


def front_title(bundle: str | None) -> str:
    """The title of the app's front window, e.g. "Campus Mail — Inbox - Google Chrome"."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) if bundle else []
    if not apps:
        return ""
    window = _attr(AX.AXUIElementCreateApplication(apps[0].processIdentifier()), "AXFocusedWindow")
    title = _attr(window, "AXTitle") if window is not None else None
    return title if isinstance(title, str) else ""


def screen_text(bundle: str | None) -> str:
    """All the words on the app's front window, for "is this on screen?" checks."""
    return " ".join(t for _, t in texts(bundle))


def keywords(bundle: str | None) -> list[str]:
    """Names on the app's front window: control labels first, then proper names in its text."""
    labels: list[str] = []
    names: list[str] = []
    for role, t in texts(bundle):
        if role in NAMED_ROLES and 2 <= len(t) <= MAX_KEYWORD_CHARS:
            labels.append(t)
        names += [m.strip() for m in PROPER.findall(t) if m.strip() not in COMMON and len(m.strip()) <= MAX_KEYWORD_CHARS]
    out, keys = [], set()
    for t in labels + names:
        k = t.lower()
        if k not in keys and re.search(r"[A-Za-z]", t):
            keys.add(k)
            out.append(t)
    return out[:MAX_KEYWORDS]


def serve() -> None:
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if req.get("cmd") == "keywords":
            try:
                words = keywords(req.get("bundle"))
            except Exception as err:
                emit({"id": req.get("id"), "keywords": [], "error": f"{type(err).__name__}: {err}"})
                continue
            emit({"id": req.get("id"), "keywords": words})


def main() -> None:
    threading.Thread(target=serve, name="avo-helper-stdin", daemon=True).start()
    emit({"event": "ready"})
    watch_keys()  # runs the tap's run loop on the main thread
    while True:  # no tap (no permission): keep serving keywords
        time.sleep(3600)


if __name__ == "__main__":
    main()
