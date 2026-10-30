"""Notice when the person, not AVO, touches the mouse or keyboard mid-task, and pause.

Two signals:
  - an event tap: a listen-only Quartz tap on a background thread sees every
    mouse and keyboard event. AVO marks its own events (avo/events.py), so an
    unmarked one is the user. Rules (judge()) leave out jitter under 20 points,
    events on AVO's island or question card, clicks that answer a question,
    and the Option-Space pause key (the overlay handles that one itself).
    Needs Input Monitoring; without it only the second signal runs.
  - the front app: if another app comes to the front while AVO isn't acting,
    the user switched to it.

On the first such event the guard trips: it tells the overlay right away
("You moved — paused.", within a few ms), and the driver, which polls
tripped between and during actions, stops the batch it is running. The agent
loop then holds until the user resumes (or, with human_resume="auto", until
they have kept still for AUTO_RESUME_S).
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

import Quartz

from avo.desktop import frontmost_app, overlay_windows
from avo.events import is_avo
from avo.overlay import Overlay

JITTER_PT = 20  # cumulative pointer travel below this is a nudge, not the user taking over
MOVE_WINDOW_S = 0.5  # a pause this long between moves starts a new nudge
SETTLE_S = 1.5  # after a question or a resume, moving the mouse away doesn't count
APP_POLL_S = 0.25
OVERLAY_CACHE_S = 0.5  # trackpad scrolls come in bursts; don't list windows for each
AUTO_RESUME_S = 3.0
SPACE_KEYCODE = 49

MOUSE_DOWNS = {Quartz.kCGEventLeftMouseDown: "click", Quartz.kCGEventRightMouseDown: "click", Quartz.kCGEventOtherMouseDown: "click"}
MOVES = {Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged}
LISTENED = [
    Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged,
    Quartz.kCGEventOtherMouseDragged, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown,
    Quartz.kCGEventOtherMouseDown, Quartz.kCGEventScrollWheel, Quartz.kCGEventKeyDown, Quartz.kCGEventFlagsChanged,
]


@dataclass
class Input:
    """One input event, reduced to what the rules need."""

    kind: str  # "move", "click", "scroll", "key", "flags"
    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    avo: bool = False  # carries AVO_MAGIC
    keycode: int = -1
    flags: int = 0


def from_cg(kind_code: int, event) -> Input | None:
    if kind_code in MOVES:
        kind = "move"
    elif kind_code in MOUSE_DOWNS:
        kind = "click"
    elif kind_code == Quartz.kCGEventScrollWheel:
        kind = "scroll"
    elif kind_code == Quartz.kCGEventKeyDown:
        kind = "key"
    elif kind_code == Quartz.kCGEventFlagsChanged:
        kind = "flags"
    else:
        return None
    p = Quartz.CGEventGetLocation(event)
    return Input(
        kind=kind,
        x=p.x,
        y=p.y,
        dx=Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX),
        dy=Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY),
        avo=is_avo(event),
        keycode=Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode),
        flags=Quartz.CGEventGetFlags(event),
    )


def is_summon(e: Input) -> bool:
    """Option-Space, the overlay's pause key."""
    mods = e.flags & (Quartz.kCGEventFlagMaskAlternate | Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskControl)
    return e.kind == "key" and e.keycode == SPACE_KEYCODE and mods == Quartz.kCGEventFlagMaskAlternate


@dataclass
class Rules:
    """Which events mean the user is acting. No Quartz state, so tests can feed it events."""

    on_overlay: Callable[[float, float], bool] = lambda x, y: False  # is (x, y) on the island or question card?
    asking: bool = False  # a question is up: clicking answers it
    quiet_until: float = 0.0  # moves before this don't count (just answered or resumed)
    _travel: float = 0.0
    _last_move: float = field(default=-math.inf)
    _last_pos: tuple[float, float] | None = None  # where the pointer was after the last move, anyone's

    def judge(self, e: Input, now: float) -> str | None:
        """'mouse' or 'keyboard' if this event is the user taking over, else None."""
        if e.kind == "move":
            # Travel is measured between positions: posted events (remote control,
            # automation) carry no deltas, and AVO's own moves reset the start point.
            step = math.dist((e.x, e.y), self._last_pos) if self._last_pos else math.hypot(e.dx, e.dy)
            self._last_pos = (e.x, e.y)
            if e.avo or self.asking:
                return None
            if now - self._last_move > MOVE_WINDOW_S:
                self._travel = 0.0
            self._last_move = now
            if now < self.quiet_until:
                return None
            self._travel += step
            return "mouse" if self._travel >= JITTER_PT else None
        if e.avo or self.asking:
            return None
        if e.kind == "flags":  # a modifier alone, e.g. the Option of Option-Space; wait for the key
            return None
        if e.kind == "key":
            return None if is_summon(e) else "keyboard"
        if self.on_overlay(e.x, e.y):  # clicks and scrolls on AVO's own island or card
            return None
        return "mouse"

    def reset(self, now: float) -> None:
        self._travel = 0.0
        self.quiet_until = now + SETTLE_S


@dataclass
class Interruption:
    at: float
    reason: str  # "mouse", "keyboard" or "app"
    detail: str = ""
    stop_ms: float | None = None  # how long a running batch took to stop (None: nothing was running)
    paused_s: float | None = None
    avo_clicks_while_paused: int = 0  # should stay 0

    def to_log(self) -> dict:
        return {
            "at": time.strftime("%H:%M:%S", time.localtime(self.at)) + f".{int(self.at % 1 * 1000):03d}",
            "reason": self.reason,
            "detail": self.detail,
            "stop_ms": None if self.stop_ms is None else round(self.stop_ms),
            "paused_s": None if self.paused_s is None else round(self.paused_s, 2),
            "avo_clicks_while_paused": self.avo_clicks_while_paused,
        }


class HumanGuard:
    """Watches for the user taking over while a task runs. start() it, stop() it after."""

    def __init__(self, overlay: Overlay, use_tap: bool = True, enabled: bool = True):
        """enabled=False gives a guard that never trips (--no-human-guard)."""
        self.overlay = overlay
        self.enabled = enabled
        self.rules = Rules(on_overlay=self._on_overlay)
        self.tripped = threading.Event()
        self.interruptions: list[Interruption] = []
        self.last_human = 0.0  # last time the user did anything (for auto-resume)
        self.tap_ok = False
        self._use_tap = use_tap
        self._acting = 0
        self._expected_app: str | None = None
        self._overlay_apps: set[str] = set()
        self._loop = None
        self._running = False
        self._lock = threading.Lock()
        self._overlay_boxes: tuple[float, list] = (0.0, [])

    # ---- lifecycle ----

    def start(self) -> None:
        if not self.enabled:
            return
        self._running = True
        self._overlay_apps = {w["bundle"] for w in overlay_windows() if w.get("bundle")}
        self._expected_app = frontmost_app()
        if self._use_tap:
            if Quartz.CGPreflightListenEventAccess():
                threading.Thread(target=self._tap_thread, name="avo-tap", daemon=True).start()
            else:
                # Don't trigger the system prompt; the user grants Input Monitoring themselves.
                print("human guard: no Input Monitoring permission; watching only for front-app changes")
        threading.Thread(target=self._app_thread, name="avo-front-app", daemon=True).start()

    def stop(self) -> None:
        self._running = False
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)

    # ---- what the agent tells it ----

    @contextmanager
    def asking(self):
        """A question is on screen: the user's clicks answer it."""
        self.rules.asking = True
        try:
            yield
        finally:
            self.rules.asking = False
            self.rules.reset(time.time())

    @contextmanager
    def acting(self):
        """AVO is running actions (or switching apps itself): front-app changes are its own."""
        self._acting += 1
        try:
            yield
        finally:
            self._acting -= 1
            if not self._acting:
                self._note_front()

    def resume(self) -> None:
        """After the user resumes: forget the trip and take the screen as it now is."""
        now = time.time()
        if self.interruptions and self.interruptions[-1].paused_s is None:
            self.interruptions[-1].paused_s = now - self.interruptions[-1].at
        self.rules.reset(now)
        self._note_front()
        self.tripped.clear()

    def _note_front(self) -> None:
        """Take the front app as the one AVO works in; never AVO's own overlay (clicking Resume can focus it)."""
        if not self._running:
            return
        front = frontmost_app()
        if front and front not in self._overlay_apps:
            self._expected_app = front

    def overlay_in_front(self) -> bool:
        return frontmost_app() in self._overlay_apps

    def stopped_batch(self) -> None:
        """The driver has stopped the batch that was running when the guard tripped."""
        if self.interruptions and self.interruptions[-1].stop_ms is None:
            self.interruptions[-1].stop_ms = (time.time() - self.interruptions[-1].at) * 1000

    # ---- tripping ----

    def _trip(self, reason: str, detail: str = "") -> None:
        now = time.time()
        self.last_human = now
        with self._lock:
            if self.tripped.is_set() or not self._running:
                return
            self.tripped.set()
            self.interruptions.append(Interruption(now, reason, detail))
        self.overlay.send("interrupt", text="You moved — paused.")
        print(f"human guard: {reason} {detail}".rstrip(), flush=True)

    def feed(self, e: Input, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if e.avo and e.kind == "click" and self.tripped.is_set() and self.interruptions:
            self.interruptions[-1].avo_clicks_while_paused += 1  # a bug if it ever happens
        reason = self.rules.judge(e, now)
        if reason is None:
            return
        if self.tripped.is_set():
            self.last_human = now  # still moving: push back an auto-resume
            return
        self._trip(reason, f"{e.kind} at ({e.x:.0f},{e.y:.0f})" if reason == "mouse" else f"key {e.keycode}")

    def _on_overlay(self, x: float, y: float) -> bool:
        at, boxes = self._overlay_boxes
        if time.time() - at > OVERLAY_CACHE_S:
            boxes = [w["box"] for w in overlay_windows() if w["title"] in ("AVO island", "AVO question")]
            self._overlay_boxes = (time.time(), boxes)
        return any(l <= x <= r and t <= y <= b for l, t, r, b in boxes)

    # ---- threads ----

    def _tap_thread(self) -> None:
        mask = 0
        for kind in LISTENED:
            mask |= Quartz.CGEventMaskBit(kind)

        def callback(_proxy, kind, event, _refcon):
            if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(tap, True)
                return event
            e = from_cg(kind, event)
            if e is not None:
                try:
                    self.feed(e)
                except Exception as err:  # never let the tap die on one bad event
                    print(f"human guard: {type(err).__name__}: {err}")
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionListenOnly, mask, callback, None
        )
        if tap is None:
            print("human guard: could not create the event tap; watching only for front-app changes")
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self.tap_ok = True
        Quartz.CFRunLoopRun()

    def _app_thread(self) -> None:
        while self._running:
            time.sleep(APP_POLL_S)
            if not (self._acting or self.rules.asking or self.tripped.is_set()):
                self.check_front(frontmost_app())

    def check_front(self, front: str | None) -> None:
        """Trip if another app than the one AVO left in front is there now (not AVO's overlay)."""
        if self._acting or self.rules.asking:
            return
        if front and self._expected_app and front != self._expected_app and front not in self._overlay_apps:
            self._trip("app", f"{self._expected_app} -> {front}")
