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


def has_window(bundle: str | None) -> bool:
    """True if the app has an ordinary window on screen (not minimized, not hidden)."""
    if not bundle:
        return False
    for w in Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    ):
        b = w["kCGWindowBounds"]
        if w.get("kCGWindowLayer") == 0 and b["Width"] >= 80 and b["Height"] >= 40 and _bundle(w["kCGWindowOwnerPID"]) == bundle:
            return True
    return False


def desktop_icons() -> dict[str, tuple[float, float, float, float]]:
    """The icons on the desktop: name -> frame (l, t, w, h) in screen points.

    Finder lists them in its accessibility tree whether or not windows cover
    them ({} when Finder isn't running or the list can't be read).
    """
    import ApplicationServices as AX

    from avo.snap import _attr, _frame

    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")
    if not apps:
        return {}
    out = {}
    try:
        for area in _attr(AX.AXUIElementCreateApplication(apps[0].processIdentifier()), "AXChildren") or []:
            if _attr(area, "AXRole") != "AXScrollArea":
                continue
            for group in _attr(area, "AXChildren") or []:
                for icon in _attr(group, "AXChildren") or []:
                    name, frame = _attr(icon, "AXTitle"), _frame(icon)
                    if name and frame:
                        out[str(name)] = frame
    except Exception:
        return {}
    return out


def work_area() -> tuple[float, float, float, float] | None:
    """Screen points (l, t, r, b) around the window being worked in.

    The topmost ordinary window (not AVO's), plus any menus or popups its app
    has open above it. Whether the screen is still, or an action did
    anything, is judged here only: elsewhere a chat app streams text or a
    spinner turns, and that isn't the task's doing.
    """
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    )  # front to back

    def box(w):
        b = w["kCGWindowBounds"]
        return (b["X"], b["Y"], b["X"] + b["Width"], b["Y"] + b["Height"])

    def visible(w):
        b = w["kCGWindowBounds"]
        return w.get("kCGWindowAlpha", 1) > 0 and b["Width"] >= 80 and b["Height"] >= 40

    front = next(
        (w for w in windows if w.get("kCGWindowLayer") == 0 and visible(w) and w.get("kCGWindowName") not in OVERLAY_TITLES),
        None,
    )
    if front is None:
        return None
    pid = front["kCGWindowOwnerPID"]
    boxes = [box(front)] + [
        box(w) for w in windows if w["kCGWindowOwnerPID"] == pid and w.get("kCGWindowLayer", 0) > 0 and visible(w)
    ]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


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
