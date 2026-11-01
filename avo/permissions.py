"""Which macOS permissions AVO is missing, and where to turn each on.

macOS grants these to the app AVO was started from (a terminal, or the
overlay), not to Python. Without one AVO used to fail silently: no Screen
Recording, and the model saw only the wallpaper; no Accessibility, and clicks
went nowhere. Now the overlay says what's missing (avo/helper.py checks every
few seconds), and the agent won't start without the essential two.

The microphone is checked by the overlay itself, the first time you speak.
"""

from __future__ import annotations

import ApplicationServices as AX
import Quartz

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?{pane}"

# id, name in System Settings, its pane, what AVO can't do without it, essential (no task without it)
PERMISSIONS = [
    ("screen", "Screen Recording", "Privacy_ScreenCapture", "see the screen", True),
    ("accessibility", "Accessibility", "Privacy_Accessibility", "click, type and read buttons", True),
    ("input", "Input Monitoring", "Privacy_ListenEvent", "hear the right-⌥ tap or notice you taking over", False),
]

CHECKS = {
    "screen": lambda: bool(Quartz.CGPreflightScreenCaptureAccess()),
    "accessibility": lambda: bool(AX.AXIsProcessTrusted()),
    "input": lambda: bool(Quartz.CGPreflightListenEventAccess()),
}


def missing() -> list[dict]:
    """The permissions AVO doesn't have, essential ones first. A check that errors counts as granted."""
    out = []
    for pid, name, pane, why, essential in PERMISSIONS:
        try:
            ok = CHECKS[pid]()
        except Exception:
            ok = True
        if not ok:
            out.append({"id": pid, "name": name, "why": why, "essential": essential,
                        "url": SETTINGS_URL.format(pane=pane)})
    return out


def explain(gaps: list[dict]) -> str:
    """One sentence: which permissions, what for, and where."""
    names = " and ".join(g["name"] for g in gaps)
    whys = "; ".join(f"{g['name']} to {g['why']}" for g in gaps)
    return (f"AVO needs {names} ({whys}). Turn it on in System Settings → Privacy & Security, "
            "for the app you start AVO from, then quit and reopen that app.")
