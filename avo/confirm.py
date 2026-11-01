"""Before a batch runs: does it need the user's OK, and how is it asked?

Meta attaches the routine check `computer_action_requires_confirmation` to
nearly every batch (33 of 36 on 10/28). A spoken "yes" for each would make
every step cost an answer, the opposite of what AVO is for. So (Bowen, 10/31):

  - routine check only, an ordinary action: AVO rings the target on screen and
    counts down COUNTDOWN_S; saying "no" or "stop", or pressing Cancel, calls
    it off. That stands in for the confirmation only if the target really was
    shown: the overlay reports when the ring is up, and without a target to
    show (or without the overlay) AVO asks for a yes instead.
  - an irreversible action (send, submit, delete, pay, ...) or any other
    check code: a preview card, and nothing happens until the user says yes.
  - the user already answered a question about this very batch (the gate's
    "OK to click here?" / "Which one?"): that answer is the confirmation,
    unless Meta raised a check other than the routine one.

Every confirmation is logged: the check codes, how it was confirmed, whether
the user said no.
"""

from __future__ import annotations

import re
import subprocess
import sys
import termios
import time
from dataclasses import asdict, dataclass, field

from avo.desktop import frontmost_app
from avo.overlay import Overlay
from avo.snap import Target, clickable_at, focused

ROUTINE_CHECKS = {"computer_action_requires_confirmation"}
COUNTDOWN_S = 1.0
POINTER_KINDS = ("click", "double_click", "drag", "scroll")
# Words on a control (or in the reviewer's name for it) that mean the click
# can't be taken back.
IRREVERSIBLE = re.compile(
    r"\b(send|submit|delete|remove|pay|purchase|buy|place order|check ?out|confirm|transfer|"
    r"post|publish|book|reserve|unsubscribe)\b",
    re.I,
)
SEND_KEYS = ({"cmd", "enter"}, {"cmd", "return"}, {"ctrl", "enter"}, {"ctrl", "return"})
PREVIEW_CHARS = 400


@dataclass
class Confirmation:
    codes: list[str] = field(default_factory=list)
    method: str = "none"  # "countdown", "yes" (asked; said yes), "answered" (the gate's question), "terminal", "none", "off" (E3 modes)
    declined: bool = False
    how: str | None = None  # how the user answered: "voice", "click", "key", or None (the countdown ran out)
    irreversible: str | None = None  # what made it irreversible, e.g. "Send"
    shown: bool = False  # the target was ringed on screen
    said: str = ""  # what the user said when they said no, e.g. "no, the physics one"
    choice: str = "go"  # "go", "no", "stop" or "pause"

    def to_log(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}


def _point(a: dict) -> tuple[int, int] | None:
    if a["type"] == "drag":
        path = a.get("path") or []
        return (path[0]["x"], path[0]["y"]) if path else None
    return (a["x"], a["y"]) if "x" in a else None


def target_of(actions: list[dict], to_screen) -> tuple[Target | None, tuple[float, float] | None]:
    """What the batch acts on, in screen points: the control under its first click
    (or scroll), else the focused element for keys and typing; and the point."""
    for a in actions:
        if a["type"] in POINTER_KINDS and (p := _point(a)):
            x, y = to_screen(*p)
            return clickable_at(x, y), (x, y)
    if any(a["type"] in ("keypress", "type") for a in actions):
        t = focused()
        return t, (t.center if t else None)
    return None, None


def irreversible(actions: list[dict], target: Target | None, review: dict | None) -> str | None:
    """The name of what makes this batch irreversible, or None."""
    clicks = [a for a in actions if a["type"] in ("click", "double_click")]
    if clicks:
        for name in ((target.label if target else ""), (review or {}).get("element_under_click", "")):
            if name and IRREVERSIBLE.search(name):
                return name
    for a in actions:
        if a["type"] == "keypress" and {k.lower() for k in a.get("keys") or []} in SEND_KEYS:
            return "⌘Enter (send)"
    return None


def saying(actions: list[dict], target: Target | None) -> str:
    """The batch in a few words for the countdown: 'Clicking “Reply”', 'Typing into “Message”'."""
    name = f"“{target.label}”" if target and target.label else ""
    kinds = [a["type"] for a in actions if a["type"] not in ("wait", "screenshot", "move")]
    if "type" in kinds:
        return f"Typing into {name}" if name else "Typing"
    if kinds and kinds[-1] == "keypress":
        keys = next(a.get("keys") or [] for a in reversed(actions) if a["type"] == "keypress")
        return "Pressing " + "+".join(k.upper() if len(k) == 1 else k.capitalize() for k in keys)
    if "scroll" in kinds:
        return "Scrolling"
    if "drag" in kinds:
        return f"Dragging {name}".strip()
    return f"Clicking {name}" if name else "Clicking there"


def box_of(target: Target | None, point: tuple[float, float] | None) -> list[float] | None:
    if target:
        l, t, w, h = target.frame
        return [l, t, l + w, t + h]
    if point:
        x, y = point
        return [x - 28, y - 18, x + 28, y + 18]
    return None


def before(
    step: int,
    actions: list[dict],
    checks: list[dict],
    overlay: Overlay,
    to_screen,
    review: dict | None = None,
    answered: bool = False,
    draft: str = "",
    mode: str = "all",
    app: str | None = None,
) -> Confirmation:
    """Decide and, if needed, ask. The result's choice: "go", "no" (skip this batch), "stop", "pause".

    mode: "all"; "irreversible" (no countdown: only irreversible steps and
    unusual checks are asked); "off" (nothing asked). The last two are for the
    E3 comparison on demo pages, run by scripts/run_demo.py with a script
    playing the user.
    """
    codes = [c.get("code") or "?" for c in checks]
    other = [c for c in checks if c.get("code") not in ROUTINE_CHECKS]
    target, point = target_of(actions, to_screen)
    final = irreversible(actions, target, review)
    c = Confirmation(codes=codes, irreversible=final)
    if mode == "off":
        c.method = "off" if checks or final else "none"
        return c
    if not checks and not final:
        return c
    if mode == "irreversible" and not other and not final:
        c.method = "off"
        return c
    if answered and not other:
        c.method = "answered"
        return c
    box = box_of(target, point)
    if not other and not final and overlay.active and box:
        c.method = "countdown"
        answer = overlay.ask(kind="countdown", box=box, x=point[0], y=point[1], text=saying(actions, target),
                             seconds=COUNTDOWN_S)
        if answer is not None and answer.get("shown"):
            return _apply(c, answer)
        if answer is not None and answer.get("choice") in ("no", "stop", "pause"):
            return _apply(c, answer)
        # The ring never showed: the countdown doesn't count; ask for a yes below.
    c.method = "yes"
    if overlay.active:
        title = (
            f"{final.strip().rstrip('.')} — OK?" if final and len(final) <= 40
            else "Meta flagged this step" if other
            else "OK to do this?"
        )
        answer = overlay.ask(
            kind="confirm",
            title=title,
            intent=saying(actions, target),
            detail=preview(draft, app) if final else "",
            reason=" ".join(x.get("message") or x.get("code") or "" for x in other),
            codes=codes,
            box=box,
            x=point[0] if point else None,
            y=point[1] if point else None,
        )
        if answer is None:  # the overlay went away: nobody said yes
            c.choice, c.declined = "stop", True
            return c
        return _apply(c, answer)
    c.method = "terminal"
    ok = _ask_terminal(step, checks, final)
    c.choice, c.declined, c.how = ("go" if ok else "stop"), not ok, "key"
    return c


HEADER_FIELDS = re.compile(r"^(to|cc|bcc|subject|recipients?)$", re.I)


def preview(draft: str, app: str | None) -> str:
    """What an irreversible step will send: who it goes to and the subject (read off the
    form), then the text AVO typed."""
    try:
        from avo.helper import form_fields

        head = [f"{label}: {value}" for label, value in form_fields(app) if HEADER_FIELDS.match(label)]
    except Exception as e:
        print(f"form fields: {type(e).__name__}: {e}")
        head = []
    text = "\n".join(head[:4]) + ("\n\n" if head and draft else "") + draft
    return text[:PREVIEW_CHARS]


def _apply(c: Confirmation, answer: dict) -> Confirmation:
    choice = answer.get("choice")
    c.shown = bool(answer.get("shown")) or c.method == "yes"
    c.how = answer.get("how")
    c.said = answer.get("said") or ""
    if choice in ("go", "yes"):
        c.choice = "go"
    elif choice == "pause":
        c.choice = "pause"
    elif choice == "stop":
        c.choice, c.declined = "stop", True
    else:
        c.choice, c.declined = "no", True
    return c


def _ask_terminal(step: int, checks: list[dict], final: str | None) -> bool:
    """Without the overlay there is nothing to ring the target with: ask y/n in the terminal."""
    print(f"\n!! [{step}] AVO needs your OK before this action" + (f" ({final})" if final else ""))
    for x in checks:
        print(f"   [{x.get('code')}] {x.get('message')}")
    # Answering in the terminal focuses it; hand focus back afterwards so the
    # agent's keystrokes don't land in the terminal.
    front = frontmost_app()
    if sys.stdin.isatty():
        termios.tcflush(sys.stdin, termios.TCIFLUSH)  # drop keys pressed while waiting for the model
    try:
        while True:
            try:
                answer = input("Go ahead? [y/n]: ").strip().lower()
            except EOFError:  # no interactive terminal: nobody can say yes
                print("(no input available; declining)")
                return False
            if answer in ("y", "n"):
                return answer == "y"
    finally:
        if front:
            subprocess.run(["open", "-b", front])
            time.sleep(0.5)
