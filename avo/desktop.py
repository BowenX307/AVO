"""What is on the desktop: the front app, windows, AVO's own overlay windows."""

from __future__ import annotations

import subprocess

import AppKit
import Quartz

OVERLAY_TITLES = ("AVO pointer", "AVO island", "AVO question")  # overlay/*.html <title>s


def frontmost_app() -> str | None:
    """Bundle id of the frontmost app, e.g. com.google.Chrome.

    Asks lsappinfo (~6 ms): NSWorkspace.frontmostApplication() only updates
    when the main thread runs an AppKit run loop, which this process doesn't.
    """
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(
        ["lsappinfo", "info", "-only", "bundleid", asn], capture_output=True, text=True
    ).stdout
    return info.split("=", 1)[1].strip().strip('"') if "=" in info else None


def _bundle(pid: int) -> str | None:
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return app.bundleIdentifier() if app else None


def overlay_windows() -> list[dict]:
    """AVO's overlay windows on screen: [{title, box (l, t, r, b) in points, pid, bundle}]."""
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
        title = w.get("kCGWindowName")
        if title not in OVERLAY_TITLES:
            continue
        b = w["kCGWindowBounds"]
        box = (b["X"], b["Y"], b["X"] + b["Width"], b["Y"] + b["Height"])
        pid = w["kCGWindowOwnerPID"]
        out.append({"title": title, "box": box, "pid": pid, "bundle": _bundle(pid)})
    return out
