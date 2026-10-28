"""Best-effort client for the Electron overlay (overlay/).

If the overlay isn't running, every call is a no-op and the agent works as before.
Coordinates sent here are screen points, the same space pyautogui uses.
"""

from __future__ import annotations

import json
import os

from websockets.sync.client import connect

OVERLAY_URL = f"ws://127.0.0.1:{os.environ.get('AVO_OVERLAY_PORT', '8765')}"


class Overlay:
    def __init__(self, url: str | None = OVERLAY_URL):
        """Connect to the overlay; url=None gives a disabled client."""
        self._ws = None
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
            self._ws.send(json.dumps({"type": type, **fields}, ensure_ascii=False))
        except Exception:
            self._ws = None  # overlay went away mid-run

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
