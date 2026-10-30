"""Best-effort client for the Electron overlay (overlay/).

If the overlay isn't running, every call is a no-op and the agent works as before.
Coordinates sent here are screen points, the same space the mouse events use.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Callable

from websockets.sync.client import connect

OVERLAY_URL = f"ws://127.0.0.1:{os.environ.get('AVO_OVERLAY_PORT', '8765')}"


class Overlay:
    def __init__(self, url: str | None = OVERLAY_URL):
        """Connect to the overlay; url=None gives a disabled client."""
        self._ws = None
        self._control = None  # "pause" or "stop" from the user, until resumed
        self._send_lock = threading.Lock()  # the human guard sends from its own thread
        if url is None:
            return
        try:
            self._ws = connect(url, open_timeout=0.5)
        except Exception:  # refused, timed out, bad handshake: run without it
            pass

    @property
    def active(self) -> bool:
        return self._ws is not None

    def send(self, type: str, **fields) -> None:
        if self._ws is None:
            return
        try:
            with self._send_lock:
                self._ws.send(json.dumps({"type": type, **fields}, ensure_ascii=False))
        except Exception:
            self._ws = None  # overlay went away mid-run

    def ask(self, **fields) -> dict | None:
        """Show a confirm bubble and block until the user answers it.

        Returns the overlay's answer, e.g. {"choice": "yes"}, {"choice": "no", "x": .., "y": ..}
        (the corrected point, in screen points) or {"choice": "stop"}; None if the
        overlay isn't running or goes away.
        """
        if self._ws is None:
            return None
        request_id = uuid.uuid4().hex[:8]
        self.send("confirm", id=request_id, **fields)
        while self._ws is not None:
            try:
                msg = json.loads(self._ws.recv())
            except Exception:
                self._ws = None
                return None
            if msg.get("type") == "answer" and msg.get("id") == request_id:
                return msg
        return None

    def pause_requested(self) -> bool:
        """Non-blocking: has the user paused (Option-Space) or stopped the task?"""
        while self._ws is not None and self._control != "stop":
            try:
                msg = json.loads(self._ws.recv(timeout=0))
            except TimeoutError:
                break
            except Exception:
                self._ws = None
                break
            kind = msg.get("type")
            if kind in ("pause", "stop"):
                self._control = kind
            elif kind == "resume":  # paused and resumed between checks
                self._control = None
        return self._control is not None

    def pause(self) -> None:
        """Mark the task paused, e.g. when the user paused instead of answering a question."""
        self._control = "pause"

    def wait_while_paused(self, tick: Callable[[], bool] | None = None, every: float = 0.25) -> bool:
        """Block until the user resumes (True) or stops (False).

        tick, if given, is called every `every` seconds while paused; when it
        returns True the task resumes by itself (auto-resume) and the overlay is told.
        If the overlay goes away while paused, the task stops: nobody is left to resume it.
        """
        while self._ws is not None and self._control == "pause":
            try:
                msg = json.loads(self._ws.recv(timeout=every if tick else None))
            except TimeoutError:
                if tick():
                    self._control = None
                    self.send("resumed")
                continue
            except Exception:
                self._ws = None
                break
            if msg.get("type") == "resume":
                self._control = None
            elif msg.get("type") == "stop":
                self._control = "stop"
        return self._control is None

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
