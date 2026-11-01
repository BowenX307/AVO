"""Computer-tool agent loop on the Meta Model API (Responses API)."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import AppKit
from openai import OpenAI

from avo import confirm
from avo.desktop import frontmost_app, has_window, overlay_windows
from avo.driver import FRESH_THRESHOLD, Interrupted, MacDriver, ScreenChanged
from avo.confidence import repeats_dead
from avo.effect import (
    DEAD_REPEAT_NOTE, NO_EFFECT_NOTE, STUCK_AFTER, STUCK_REPLAN_NOTE, EffectWatch, plain,
)
from avo.gate import ask_user, assess, correct
from avo.helper import front_title, screen_text
from avo.human import AUTO_RESUME_S, HumanGuard
from avo.llm import MODEL, NoKey, data_url, describe, friendly, make_client
from avo.meter import Meter
from avo.overlay import Overlay
from avo.permissions import explain as explain_permissions
from avo.permissions import missing as missing_permissions
from avo.planner import REPLAN_MODEL, Plan, make_plan
from avo.regions import Region, highlight, mark, prepare
from avo.regions import describe as describe_regions
from avo.snap import location, snap, warm_up

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

SYSTEM_PROMPT = """\
You are operating a macOS computer through screenshots of its main display.
- Use macOS shortcuts: Command (cmd) instead of Ctrl, e.g. cmd+L to focus the browser address bar.
- Coordinates are pixels in the most recent screenshot.
- Do only what the task asks. Do not close windows, quit apps, or change settings unless asked.
- Work without asking the user questions. When the task is done, stop calling the computer \
tool and reply with a one-sentence summary of what you did (with the answer, if one was asked for).
- Each turn you see only the current screen and a log of your earlier messages and actions, \
not earlier screenshots. So when the screen shows something the task needs (a title, a number, \
a requirement), write it down in your message right away: that message is your only memory of it.
- Stop as soon as the task is done. For a search or "find / show me" task, that is when the \
result is on screen. Don't scroll back and forth to re-check what you have already written down.
- Put actions you can predict in one turn, since each turn costs seconds: to open a page, press \
cmd+L, type the address and press Enter together; to fill a form, click and type into every \
field you can already see in the same turn. End a turn only when the next action depends on \
something you can't see yet (a page loading, a menu's options). After each turn AVO waits \
until the screen stops changing, so don't end a turn with wait.
- After you move, delete or send something, it disappearing from where it was means it \
worked: look at the destination once if you can, then stop and reply. Don't go looking for it again.
- If the task can't be done here (the option or page it needs doesn't exist), stop and say \
what is missing, instead of searching on and on.
- Don't open or read the user's email, messages, files or other personal things unless the \
task asks for them. To look something up, use the web.
- Always write in English, even when the task is in another language."""

# AVO's own tools next to the computer tool: opening a page or an app directly
# beats focusing the address bar and pasting, or finding the Dock icon.
FUNCTION_TOOLS = [
    {
        "type": "function",
        "name": "open_url",
        "description": "Open a web address in a new browser tab. Use this instead of typing into the address bar.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "e.g. https://example.com"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "open_app",
        "description": "Bring a macOS app to the front, starting it if needed. Use this instead of the Dock or Spotlight.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "e.g. Google Chrome, Calculator, Notes"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]
TOOLS_PROMPT = (
    "\n- To open a web address, call open_url; to switch to or start an app, call open_app. They are "
    "faster and more reliable than the keyboard or the Dock. Use the computer tool for everything else."
)


class RunLog:
    """Saves each step's screenshot, actions and model output under runs/<timestamp>/."""

    def __init__(self, task: str, settings: dict | None = None):
        self.dir = RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "task.txt").write_text(task + "\n", encoding="utf-8")
        self.settings = settings or {}

    def screenshot(self, step: int, png: bytes) -> None:
        (self.dir / f"step{step:02d}.png").write_bytes(png)

    def response(self, step: int, response, extra: dict | None = None) -> None:
        record = {
            "response_id": response.id,
            "usage": response.usage.model_dump() if response.usage else None,
            "output_text": response.output_text,
            "output": [item.model_dump() for item in response.output],
            **(extra or {}),
        }
        path = self.dir / f"step{step:02d}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def annotate(self, step: int, **fields) -> None:
        """Merge extra fields into an already-written step record."""
        path = self.dir / f"step{step:02d}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(fields)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def summary(self, result: str, steps: int, elapsed: float) -> None:
        record = {"result": result, "steps": steps, "elapsed_s": round(elapsed, 1), **self.settings}
        (self.dir / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save(self, name: str, record: dict) -> None:
        """A JSON file in the run dir: summary.json (cost, time, asks), plan.json."""
        (self.dir / name).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def computer_calls(response) -> list[dict]:
    # Dump to dicts: the SDK's typed models don't know Meta's `actions` field.
    return [item.model_dump() for item in response.output if item.type == "computer_call"]


def function_calls(response) -> list[dict]:
    return [item.model_dump() for item in response.output if item.type == "function_call"]


def as_action(fn: dict) -> dict | None:
    """A function call to one of FUNCTION_TOOLS as a driver action, e.g. {"type": "open_url", "url": ...}."""
    if fn.get("name") not in {t["name"] for t in FUNCTION_TOOLS}:
        return None
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    return {"type": fn["name"], **args}


def snap_clicks(driver: MacDriver, actions: list[dict]) -> list[dict]:
    """Move left clicks that land just off a clickable element onto it (see avo/snap.py).

    A snapped click keeps where the model aimed under "_snap", so the overlay can
    show the correction and the run log records it.
    """
    out = []
    for a in actions:
        if a["type"] in ("click", "double_click") and a.get("button") in (None, "left"):
            hit = snap(*driver.to_screen(a["x"], a["y"]))
            if hit:
                x, y, target = hit
                sx, sy = driver.to_shot(x, y)
                a = {**a, "x": sx, "y": sy, "_snap": {"from": [a["x"], a["y"]], "role": target.role, "label": target.label}}
        out.append(a)
    return out


# Words by which a task points at the boxes the user drew. A task that names
# none of them didn't mean its box: 10/31, a stray box over the mail page sent
# "find a hotel near UIUC" into the inbox.
BOX_LETTERS = re.compile(r"(?<![\w'])[AB](?![\w'])")
BOX_WORDS = re.compile(r"\b(this|these|those|here|it|them|box|boxed|selected|selection|highlighted)\b", re.I)
MIN_BOX_PT = 12  # a box thinner than this is a click, not a box


def boxes_meant(task: str, regions: dict[str, tuple[int, int, int, int]]) -> dict[str, tuple[int, int, int, int]]:
    """The boxes the task is about: none if it doesn't refer to a box; never a sliver."""
    if not (BOX_LETTERS.search(task) or BOX_WORDS.search(task)):
        return {}
    return {k: b for k, b in regions.items() if b[2] - b[0] >= MIN_BOX_PT and b[3] - b[1] >= MIN_BOX_PT}


# Places with the user's own messages. Entering one the task doesn't mention is
# asked about once.
PERSONAL_APPS = {
    "com.apple.mail", "com.microsoft.Outlook", "com.apple.MobileSMS", "com.tinyspeck.slackmacgap",
    "com.tencent.xinWeChat", "net.whatsapp.WhatsApp", "ru.keepcoder.Telegram", "com.hnc.Discord",
}
PERSONAL_TITLE = re.compile(r"\b(mail|inbox|gmail|outlook|messages)\b", re.I)
MAIL_TASK = re.compile(r"\b(e-?mails?|mail|inbox|messages?|repl(y|ies)|texts?|dms?|chats?)\b", re.I)
OFF_TRACK_AFTER = 2  # steps in a row the review says don't move the task forward


def personal_unasked(task: str, app: str | None, title: str) -> bool:
    """True if AVO is in the user's mail or messages and the task doesn't mention them."""
    return (app in PERSONAL_APPS or bool(PERSONAL_TITLE.search(title))) and not MAIL_TASK.search(task)


STUCK_EXPLAIN = """\
An agent tried this task on a macOS screen and got stuck: even after a fresh plan, its actions \
stopped changing anything. The screenshot shows where it is now.
Task: {task}
Its last steps:
{past}
In one short sentence for the user (English, under 25 words), say what seems to be missing or \
blocking the task here, e.g. "This page doesn't seem to have a two-factor setting." No apologies."""


def why_stuck(client: OpenAI, task: str, past: list[str], png: bytes, what: str) -> str:
    """One sentence on why the task can't go on from this screen; a plain fallback if the call fails."""
    fallback = f"stuck: {what} changes nothing, even after a fresh plan"
    try:
        r = client.responses.create(
            model=MODEL,
            reasoning={"effort": "minimal"},
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": STUCK_EXPLAIN.format(task=task, past="\n".join(past[-6:]) or "(none)")},
                {"type": "input_image", "image_url": data_url(png)},
            ]}],
        )
        said = " ".join((r.output_text or "").split())
        return said[:200] if said else fallback
    except Exception as e:
        print(f"explaining the stop: {type(e).__name__}: {e}")
        return fallback


NO_WORDS = {"no", "nope", "nah", "don't", "dont", "do", "not", "send", "it", "cancel", "stop", "wait", "please", "thanks"}


def wants_changes(said: str) -> bool:
    """True if a spoken "no" carries more than a no: "no, make it shorter", "no, add my name"."""
    words = re.findall(r"[a-z']+", (said or "").lower())
    return len([w for w in words if w not in NO_WORDS]) >= 2


def options_on_screen(options: list[str], app: str | None) -> bool:
    """True if every option's words are on the app's window (accessibility text)."""
    if not options or not app:
        return False
    try:
        on_screen = set(re.findall(r"\w+", screen_text(app).lower()))
    except Exception as e:
        print(f"screen text: {type(e).__name__}: {e}")
        return False
    if not on_screen:
        return False
    for option in options:
        need = {w for w in re.findall(r"\w+", option.lower()) if len(w) > 2 or w.isdigit()}
        if not need or not need <= on_screen:
            return False
    return True


def focus(bundle: str) -> None:
    """Bring an app to the front, which gives it the keyboard.

    Activates the running app rather than `open -b`, which also asks it to
    reopen: that brought back a Chrome window the user had minimized (11/01),
    and makes Finder open a new window.
    """
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
    if apps:
        apps[0].activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    else:
        subprocess.run(["open", "-b", bundle])
    time.sleep(0.3)


KEY_KINDS = ("keypress", "type")


def ask_model(client: OpenAI, overlay: Overlay, label: str, effort: str | None, tools: list[dict], **kwargs):
    """responses.create with a visible waiting line, so a slow reply doesn't look like a hang.

    Returns (response, seconds).
    """
    print(f"[{label}] waiting for model...", end="", flush=True)
    overlay.send("status", text="Thinking…", busy=True)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    t0 = time.time()
    response = client.responses.create(model=MODEL, tools=tools, **kwargs)
    seconds = time.time() - t0
    print(f" {seconds:.1f}s")
    return response, seconds


RESUME_NOTE = (
    "The user interacted with the screen while AVO was paused. The screen may have changed. "
    "Re-evaluate from the current screenshot before acting."
)
CHANGED_NOTE = (
    "Your previous actions were NOT executed because the screen changed before the click. "
    "Decide again from this screenshot."
)

STILL_STEPS = 5  # this many batches in a row without a click or typing: nudge to finish
MAX_REPLANS = 2  # fresh plans after getting stuck, per run


class StopTask(Exception):
    """The user stopped the task before it began (Stop on the planner's question)."""


class Terminated(BaseException):  # not Exception: a check's "except Exception" mustn't swallow it (11/01: it did)
    """SIGTERM: the overlay ends a task that didn't stop within a second of "stop"
    (it may be waiting on the model). Raised in the main thread, so even a
    pending API call ends; the run still writes its summary and says so."""


def _on_sigterm(_signum, _frame):
    raise Terminated()

LOOKING_KINDS = ("scroll", "keypress", "wait", "screenshot", "move")


def only_looking(done: list[list[dict]]) -> bool:
    """True if the last STILL_STEPS batches only scrolled or pressed keys."""
    recent = done[-STILL_STEPS:]
    return len(recent) == STILL_STEPS and all(a["type"] in LOOKING_KINDS for batch in recent for a in batch)


def fresh_input(
    task: str, history: list[str], png: bytes, nudge: bool = False, regions: list[Region] = (), brief: str = ""
) -> list[dict]:
    """A self-contained turn: the task, the plan (brief), what was done so far as text, and the current screen.

    Boxes the user selected are drawn on the first turn's screenshot and described in text.
    """
    text = task + (f"\n\n{brief}" if brief else "")
    if regions:
        text += "\n\n" + describe_regions(regions, first=not history)
        if not history:
            png = mark(png, regions)
    if history:
        text += (
            "\n\nSteps already taken (the screenshot shows the screen after them):\n"
            + "\n".join(history)
        )
    text += "\n\nWrite your messages in English, whatever language the task is in."  # the island shows them
    if nudge:
        text += (
            f"\n\nYour last {STILL_STEPS} steps only scrolled or pressed keys. If what the task "
            "needs is on screen or in your notes above, stop now and reply with the answer."
        )
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": text},
                {"type": "input_image", "image_url": data_url(png)},
            ],
        }
    ]


def run(
    task: str,
    max_steps: int = 20,
    max_usd: float = 0.5,
    effort: str | None = "minimal",
    history: str = "text",
    gate: bool = True,
    snap_to_controls: bool = True,
    regions: dict[str, tuple[int, int, int, int]] | None = None,
    human_guard: bool = True,
    human_resume: str = "manual",
    freshness: float | None = FRESH_THRESHOLD,
    app: str | None = None,
    tools: bool = True,
    plan: str = "auto",
    voice: bool = False,
    overlay: Overlay | None = None,
    confirm_mode: str = "all",
    stay_in: tuple[str, str] | None = None,
) -> str:
    """Run the computer-tool loop.

    Stops at max_steps model turns or once the run's API calls cost max_usd.

    Before each batch runs, avo/confirm.py decides whether it needs the user's
    OK: a 1 s countdown on the ringed target for an ordinary step, a spoken or
    clicked yes for an irreversible one or for any unusual safety check.

    effort: reasoning effort ("minimal" ... "max"); None uses the model default.
    history: "full" chains turns with previous_response_id, so the model sees every
        earlier screenshot (input grows each step, and so does latency). "text" sends
        each turn fresh with only the latest screenshot plus a text log of past steps.
    gate: before a click/drag batch runs, check it (model review + repeat detection)
        and, if it looks wrong, point at the target on screen and ask the user.
        Needs the overlay; without it the run proceeds unchecked.
    snap_to_controls: move clicks that land just off a button or field onto it
        (macOS accessibility), before the gate looks at them.
    regions: boxes the user dragged over the screen, {"A": (l, t, r, b)} in screen
        points; the model gets their positions and close-ups (avo/regions.py).
    human_guard: pause the moment the user touches the mouse or keyboard or
        switches apps (avo/human.py).
    human_resume: "manual" waits for Resume; "auto" resumes once the user has
        kept still for AUTO_RESUME_S (always auto without the overlay).
    freshness: before each pointer action, skip the rest of the batch if the
        screen around its target changed by more than this since the model
        looked (0-1, mean grey-level change); None turns the check off.
    app: bundle id of the app to work in, e.g. com.google.Chrome. The island
        passes the app the user was in: typing the task into the island brings
        AVO's own overlay to the front, and keys pressed then go nowhere.
        Default: the front app, unless that is the overlay.
    tools: offer the model open_url and open_app (FUNCTION_TOOLS) next to the
        computer tool.
    plan: "auto" plans first (avo/planner.py) unless the user boxed what the
        task is about; "always"; "never". The plan's start is opened before the
        first turn, its question asked, and the plan shown to the model each turn.
    voice: the task was spoken (the overlay's voice input). The human guard then
        counts only clicks, not pointer moves, scrolls or keys (avo/human.py).
    overlay: who sees AVO and answers it; default the Electron overlay. Scripted
        runs pass a ScriptedUser (avo/scripted.py).
    confirm_mode: "all" (avo/confirm.py), "irreversible" (no countdown, only the
        yes before irreversible steps and unusual checks) or "off" (nothing
        asked). The last two exist for the E3 comparison on demo pages, where a
        script plays the user.
    stay_in: (app bundle id, a word in the window title) the run must stay in,
        e.g. ("com.google.Chrome", "Campus Mail"). Before each batch runs,
        anything else in front stops the run. Scripted runs use it (10/31: a
        scripted run that lost its demo tab opened the real Mail app).
    """
    if plan not in ("auto", "always", "never"):
        raise ValueError(f"plan must be 'auto', 'always' or 'never', not {plan!r}")
    if history not in ("full", "text"):
        raise ValueError(f"history must be 'full' or 'text', not {history!r}")
    if human_resume not in ("manual", "auto"):
        raise ValueError(f"human_resume must be 'manual' or 'auto', not {human_resume!r}")
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_sigterm)
    gaps = [g for g in missing_permissions() if g["essential"]]
    if gaps:  # without these AVO is blind (Screen Recording) or its clicks go nowhere (Accessibility)
        print(f"result: stopped: {explain_permissions(gaps)}")
        if (o := overlay or Overlay()).active:
            o.send("done", text=explain_permissions(gaps), ok=False, error=True)
            o.close()
        return "stopped: missing permission: " + ", ".join(g["name"] for g in gaps)
    meter = Meter()  # every API call of the run, for summary.json
    try:
        client = meter.wrap(make_client())
    except NoKey as e:
        print(f"result: stopped: {e}")
        if (o := overlay or Overlay()).active:
            o.send("done", text=friendly(e), ok=False, error=True)
            o.close()
        return f"stopped: {e}"
    if confirm_mode not in ("all", "irreversible", "off"):
        raise ValueError(f"confirm_mode must be 'all', 'irreversible' or 'off', not {confirm_mode!r}")
    overlay = overlay or Overlay()
    guard = HumanGuard(overlay, enabled=human_guard, voice=voice)

    def interrupted() -> str | None:
        if guard.tripped.is_set():
            return "human"
        return "pause" if overlay.pause_requested() else None

    driver = MacDriver(overlay=overlay, interrupt=interrupted, fresh_threshold=freshness)
    gate = gate and overlay.active
    log = RunLog(
        task,
        {
            "model": MODEL, "effort": effort, "history": history, "gate": gate, "snap": snap_to_controls,
            "human_guard": human_guard, "human_resume": human_resume, "freshness": freshness, "tools": tools,
            "plan": plan, "voice": voice, "confirm_mode": confirm_mode,
            "answerer": getattr(overlay, "answerer", "overlay" if overlay.active else "terminal"),
        },
    )
    tool_list = [{"type": "computer"}] + (FUNCTION_TOOLS if tools else [])
    instructions = SYSTEM_PROMPT + (TOOLS_PROMPT if tools else "")
    started = time.time()
    print(f"run dir: {log.dir}  (effort={effort}, history={history}, gate={gate})")
    print(f"overlay: {'connected' if overlay.active else 'not running (cd overlay && npm start)'}")

    def hold(reason: str) -> bool:
        """Wait out a pause: the user took over ("human"), pressed Option-Space ("pause") or AVO got stuck ("stuck").

        True once resumed, False if the user stopped the task.
        """
        overlay.pause()
        if reason == "human" and (human_resume == "auto" or not overlay.active):
            shown = [None]

            def still() -> bool:
                left = AUTO_RESUME_S - (time.time() - guard.last_human)
                if left <= 0:
                    return True
                if shown[0] != (n := int(left) + 1):
                    shown[0] = n
                    overlay.send("interrupt", text=f"You moved — resuming in {n}s")
                return False

            if overlay.active:
                ok = overlay.wait_while_paused(tick=still)
            else:
                while not still():
                    time.sleep(0.25)
                ok = True
        else:
            ok = overlay.wait_while_paused()
        guard.resume()
        return ok

    ours: set[str] = set()  # the overlay app's bundle id: never the one to work in
    work_app: str | None = None

    def refocus() -> None:
        """Clicking the island (Resume, an answer) brings the overlay to the front: hand the keyboard back."""
        if work_app and frontmost_app() in ours:
            focus(work_app)

    spent: dict[str, float] = defaultdict(float)  # where the run's time went, for summary.json

    @contextmanager
    def timed(key: str):
        t0 = time.time()
        try:
            yield
        finally:
            spent[key] += time.time() - t0

    def checked_task() -> str:
        """The task with the user's answers, as the model and the review see it every turn.

        An answer is final: 10/31, after the user picked David Smith, the model
        said "you picked the wrong professor" and the review asked again 4 times.
        """
        said = []
        if the_plan and the_plan.answer:
            said.append(f"Asked “{the_plan.question}”, the user answered: {the_plan.answer}.")
        said += [f"Asked which one, the user chose: {p}." for p in picks.values()]
        if not said:
            return task
        return task + "\n(" + " ".join(said) + " These answers are final: follow them, don't switch away, and don't ask again.)"

    def make(screen: bytes, past: list[str] = (), boxed: bool = False) -> Plan | None:
        """A plan from this screen, or None if the planner fails (a plan helps; it isn't needed).

        boxed: the user boxed what the task is about, on this screen. The plan
        then starts here and asks nothing up front (the gate asks on screen).
        """
        overlay.send("status", text="Planning…", busy=True, phase="planning")
        said = task + ("\n(The user drew boxes on the current screen around what this is about.)" if boxed else "")
        try:
            with timed("planning"):
                p = make_plan(client, said, screen, past, effort=effort, model=REPLAN_MODEL if past else MODEL)
        except Exception as e:
            print(f"planner: {type(e).__name__}: {e}")
            return None
        if boxed:
            p.start_kind, p.start_target, p.question, p.options = "here", "", "", []
        print(f"{'re-plan, ' + REPLAN_MODEL if past else 'plan'} ({p.seconds:.1f}s): start {p.start_kind} {p.start_target}".rstrip())
        for i, s in enumerate(p.steps, 1):
            print(f"   {i}. {s}")
        print(f"   done when: {p.done_when}" + (f"\n   question: {p.question} {p.options}" if p.question else ""))
        return p

    result = "stopped: step budget reached"
    step = 0
    past: list[str] = []  # text history, used when history == "text"
    done: list[list[dict]] = []  # executed batches, for repeat detection
    asked = corrected = skipped = stuck = no_effect = snaps = replans = 0
    the_plan: Plan | None = None
    effect = EffectWatch()
    confirmations: list[dict] = []  # every confirmation asked or counted down, for summary.json
    picks: dict[frozenset, str] = {}  # the user's answer to each "which one?", by its set of options
    not_forward = 0  # click batches in a row the review says don't move the task forward
    dead: list[list[dict]] = []  # batches that changed nothing on screen, not to be run again
    last_dead = False  # the last batch changed nothing (or was refused for repeating one that didn't)
    personal_ok = False  # the user said yes to AVO being in their mail or messages
    draft = ""  # the last longer text typed: shown before a send
    try:
        ours = {w["bundle"] for w in overlay_windows() if w.get("bundle")}
        work_app = app or frontmost_app()
        if work_app in ours:
            work_app = None
            print("warning: AVO's overlay is in front and no app to work in was given")
        if work_app and work_app != "com.apple.finder" and not has_window(work_app):
            # Its windows are minimized or closed: the user is on the desktop. Don't
            # bring the app back over it (11/01: Chrome came back, then got minimized again).
            print(f"{work_app} has no window on screen: working on the desktop")
            work_app = "com.apple.finder"
        if work_app and frontmost_app() != work_app:
            focus(work_app)
        guard.start()
        png = driver.screenshot()
        log.screenshot(0, png)
        if snap_to_controls:
            warm_up(*driver.to_screen(640, 400))  # Chrome builds its AX tree on first use
        meant = boxes_meant(task, regions or {})
        if regions and not meant:
            print(f"boxes ignored: the task doesn't refer to them ({', '.join(regions)})")
            log.settings["boxes_ignored"] = {k: list(v) for k, v in regions.items()}
            overlay.send("select-end", keep=False)  # take the stray box off the screen
        selected = prepare(driver, meant) if meant else []
        if selected:
            log.settings["regions"] = {r.label: r.shot for r in selected}
            highlight(client, overlay, png, selected, log.dir)  # SAM, alongside the first model call
        if plan in ("always", "auto"):
            the_plan = make(png, boxed=bool(selected))
        if the_plan and the_plan.question and options_on_screen(the_plan.options, work_app):
            # "Which Professor Smith?" with both on screen: don't ask in words now;
            # the gate rings the two on screen and asks when the agent gets there.
            print("   (the options are on screen: AVO asks when it gets there, pointing at them)")
            the_plan.deferred, the_plan.question, the_plan.options = the_plan.question, "", []
        if the_plan and the_plan.question:
            if not overlay.active:
                print("   (no overlay to ask on: going with the most likely reading)")
            else:
                asked += 1
                overlay.send("status", text="AVO has a question — see the card", busy=False)
                with guard.acting(), guard.asking(), timed("you"):
                    answer = overlay.ask(kind="clarify", question=the_plan.question, options=the_plan.options)
                    refocus()
                choice = (answer or {}).get("choice")
                print(f"user: {choice}" + (f" {the_plan.options[answer['index']]}" if choice == "pick" else ""))
                if guard.tripped.is_set() and choice == "pick":
                    # The user clicked or typed while AVO planned, then answered: they're here, go on.
                    guard.resume()
                    overlay.send("resumed")
                if choice == "pick":
                    the_plan.answer = the_plan.options[answer["index"]]
                elif choice == "pause":
                    with timed("paused"):
                        if not hold("pause"):
                            choice = "stop"
                if choice in ("stop", None):
                    result = "stopped: user stopped the task"
                    raise StopTask
        start = the_plan.start_action() if the_plan and not selected and not stay_in else None  # confined: open nothing
        if start:  # open the plan's start ourselves: no turn spent on the address bar or the Dock
            print(f"[0] opening the start: {describe([start])}")
            overlay.send("status", text=f"Plan · {len(the_plan.steps)} steps", busy=True)
            opened = False
            guard.arm()
            try:
                with guard.acting(), timed("actions"):
                    driver.work_app = work_app
                    driver.execute([start])
                opened = True
            except Interrupted as e:  # the user moved, or paused, while AVO planned
                print(f"[0] paused before opening the start ({e.reason})")
                with timed("paused"):
                    if not hold(e.reason):
                        result = "stopped: user stopped the task"
                        raise StopTask
                refocus()
                # Not opened: the plan's brief still says "Begin at", so the model opens it.
            if driver.failures:
                print("[0] failed: " + "; ".join(driver.failures))
            the_plan.opened = opened and not driver.failures
            if opened:
                past.append(f"0. (AVO, before your first turn) {describe([start])}"
                            + (f"\n   Note: {'; '.join(driver.failures)}" if driver.failures else ""))
                done.append([start])
            else:
                past.append(f"0. (AVO, before your first turn) paused before opening the start\n   Note: {RESUME_NOTE}")
            png = driver.settled_screenshot()
            spent["settle"] += driver.settle_s
            log.screenshot(0, png)
            if (front := frontmost_app()) and front not in ours:
                work_app = front
        if the_plan:
            log.save("plan.json", the_plan.to_log())
        response, seconds = ask_model(
            client, overlay, "1", effort, tool_list, instructions=instructions,
            input=fresh_input(checked_task(), past, png, regions=selected, brief=the_plan.brief() if the_plan else ""),
        )

        for step in range(1, max_steps + 1):
            if (usd := meter.summary()["usd"]) >= max_usd:
                result = f"stopped: cost limit reached (${usd:.2f} of ${max_usd:.2f})"
                break
            calls = computer_calls(response)
            fns = function_calls(response)
            if not calls and not fns:
                log.response(step, response, {"model_seconds": round(seconds, 1)})
                result = response.output_text or "(model finished without a message)"
                break
            if len(calls) > 1:
                print(f"warning: {len(calls)} computer_calls in one response; handling the first")

            call = calls[0] if calls else None
            notes: list[str] = []  # for the model's next turn
            # open_url / open_app first: computer actions in the same turn were planned on this screen.
            actions = [act for f in fns if (act := as_action(f))]
            notes += [f"There is no tool called {f.get('name')!r}." for f in fns if not as_action(f)]
            if call:
                actions += call.get("actions") or ([call["action"]] if call.get("action") else [])
            checks = (call or {}).get("pending_safety_checks") or []
            log.response(step, response, {"actions": actions, "model_seconds": round(seconds, 1)})
            note = (response.output_text or "").strip()
            if note:
                print(f"[{step}] model: {note}")
                if not guard.tripped.is_set():
                    overlay.send("status", text=note, busy=False, step=step)  # the island shows "Step N · note"
            print(f"[{step}] actions: {json.dumps(actions, ensure_ascii=False)}")

            if snap_to_controls:
                actions = snap_clicks(driver, actions)
                snapped = [a["_snap"] | {"to": [a["x"], a["y"]]} for a in actions if "_snap" in a]
                if snapped:
                    snaps += len(snapped)
                    print(f"[{step}] snapped: " + ", ".join(f"{s['from']}->{s['to']} {s['label'] or s['role']}" for s in snapped))
                    log.annotate(step, snapped=snapped)

            stop = interrupted()
            a = None  # this step's gate assessment
            answered = False  # the user answered a question about this batch
            skip = False  # the user said no to this batch: nothing runs
            # The same batch as one that just did nothing on screen: don't run it
            # again (10/31: "View all results" clicked three more times, 0 px each).
            blocked = not stop and repeats_dead(dead, actions)
            if blocked:
                skip = True
                print(f"[{step}] not run: the same as a batch that changed nothing")
                notes.append(DEAD_REPEAT_NOTE)
                log.annotate(step, blocked="repeats a batch that changed nothing")
            if gate and not stop and not skip:
                overlay.send("status", text="Checking this step…", busy=True)
                with timed("checks"):
                    a = assess(client, checked_task(), past, done, png, actions, note, driver.geometry.shot_size,
                               where=(lambda x, y: location(*driver.to_screen(x, y))) if snap_to_controls else None)
                stop = interrupted()  # the user may have taken over during the check
                if a and not stop:
                    print(f"[{step}] gate: {'FLAG ' + '; '.join(a.reasons) if a.flagged else 'ok'} ({a.seconds:.1f}s)")
                    entry = a.to_log()
                    if a.flagged and a.candidates and (prior := picks.get(frozenset(c["label"] for c in a.candidates))):
                        # Asked this before: use that answer, don't ask again.
                        chosen = next(c for c in a.candidates if c["label"] == prior)
                        if a.target and min(a.candidates, key=lambda c: (c["x"] - a.target[0]) ** 2 + (c["y"] - a.target[1]) ** 2) is not chosen:
                            actions = correct(actions, (chosen["x"], chosen["y"]))
                        print(f"[{step}] gate: asked before; the user chose {prior}")
                        entry["user"] = {"choice": "pick", "answer": {"label": prior, "how": "earlier"}, "actions": actions}
                        a.flagged = False
                        answered = True
                    if a.flagged:
                        asked += 1
                        front = work_app or frontmost_app()
                        ask = "Which one did you mean? Pick on screen" if a.candidates else "AVO needs your OK — see the highlight"
                        overlay.send("status", text=ask, busy=False)
                        with guard.acting(), guard.asking(), timed("you"):
                            choice, actions, answer = ask_user(overlay, driver, a, actions, note, png, client=client)
                            if front:  # answering on the overlay may have focused it
                                focus(front)
                        print(f"[{step}] user: {choice}" + (f" -> {describe(actions)}" if choice == "no" else ""))
                        entry["user"] = {"choice": choice, "answer": answer, "actions": actions}
                        answered = choice in ("yes", "pick", "no")
                        if choice == "stop":
                            log.annotate(step, gate=entry)
                            result = "stopped: user stopped the task"
                            break
                        if choice == "pause":
                            stop = "pause"
                        elif choice == "pick":
                            corrected += 1
                            note = (note + " " if note else "") + f"(the user chose “{answer['label']}”)"
                            picks[frozenset(c["label"] for c in a.candidates)] = answer["label"]
                        elif choice == "no":
                            corrected += 1
                            note = (note + " " if note else "") + "(the user corrected the click point on screen)"
                        elif choice == "none":
                            corrected += 1
                            skip = True
                            said = (answer or {}).get("said") or ""
                            notes.append(
                                f"AVO asked the user about this step; they said none of the options is right"
                                + (f": “{said}”" if said else "") + ". It was NOT executed. Do what they said."
                            )
                        else:
                            note = (note + " " if note else "") + "(the user confirmed this step)"
                    log.annotate(step, gate=entry)

            # Off track: in the user's mail or messages though the task doesn't
            # mention them (asked once a run), or the review saying, step after
            # step, that this doesn't move the task forward.
            if a and a.review:
                not_forward = not_forward + 1 if not a.review.get("moves_task_forward", True) else 0
            off = None
            if not stop and not skip and actions and not answered and overlay.active:
                if not personal_ok and personal_unasked(task, work_app, front_title(work_app)):
                    off = ("personal", "AVO is in your mail or messages — OK?", "Your task doesn’t mention them.")
                elif not_forward >= OFF_TRACK_AFTER and not last_dead:
                    # (After a batch that did nothing, the stuck detector handles it, not a question.)
                    off = ("forward", "AVO may be off track — keep going?",
                           f"AVO’s own check says its last {not_forward} steps don’t move your task forward.")
            if off:
                kind, title, why = off
                asked += 1
                overlay.send("status", text="AVO has a question — see the card", busy=False)
                with guard.acting(), guard.asking(), timed("you"):
                    target, _ = confirm.target_of(actions, driver.to_screen)
                    answer = overlay.ask(kind="confirm", title=title, reason=why, codes=[],
                                         intent=f"Next: {confirm.saying(actions, target)}" + (f" — {note}" if note else ""))
                    refocus()
                choice = (answer or {}).get("choice")
                print(f"[{step}] off track ({kind}): user {choice}")
                log.annotate(step, off_track={"kind": kind, "choice": choice, "said": (answer or {}).get("said")})
                not_forward = 0
                if choice in ("stop", None):
                    result = "stopped: user stopped the task"
                    break
                if choice == "pause":
                    stop = "pause"
                elif choice == "yes":
                    personal_ok = personal_ok or kind == "personal"
                else:
                    corrected += 1
                    skip = True
                    said = (answer or {}).get("said") or ""
                    notes.append(
                        ("The user doesn't want AVO in their mail or messages for this task: do it another way, e.g. on the web."
                         if kind == "personal" else
                         "The user says this isn't helping with the task. Rethink the approach from the current screen.")
                        + f" Your actions were NOT executed." + (f" They said: “{said}”" if said else "")
                    )

            if not stop and not skip and actions:
                if work_app and any(a["type"] in KEY_KINDS for a in actions) and frontmost_app() in ours:
                    focus(work_app)  # so the countdown rings the field the keys will go to
                with guard.acting(), guard.asking(), timed("you"):
                    ok = confirm.before(
                        step, actions, checks, overlay, driver.to_screen,
                        review=a.review if a else None, answered=answered, draft=draft, mode=confirm_mode,
                        app=work_app,
                    )
                    if ok.method in ("yes", "terminal") or ok.choice != "go":
                        refocus()  # answering on the island brings the overlay to the front
                if ok.method != "none":
                    confirmations.append({"step": step, **ok.to_log()})
                    log.annotate(step, confirmation=ok.to_log())
                    print(f"[{step}] confirmation: {ok.method} -> {ok.choice}"
                          + (f" ({ok.irreversible})" if ok.irreversible else "") + (f" said “{ok.said}”" if ok.said else ""))
                if ok.choice == "stop":
                    result = "stopped: user stopped the task"
                    break
                if ok.choice == "pause":
                    stop = "pause"
                elif ok.choice == "no" and ok.irreversible and not wants_changes(ok.said):
                    # A plain "no" to sending (submitting, deleting…) means don't: end the
                    # task. 11/01: skipping only the step made the model try Send again and
                    # again. A "no" with changes ("no, make it shorter") goes on below.
                    name = ok.irreversible.strip().rstrip(".")
                    result = f"stopped: not done — you said no to “{name}”"
                    break
                elif ok.choice == "no":
                    skip = True
                    notes.append(
                        f"The user said no to this: {describe(actions)}. It was NOT executed; don't repeat it."
                        + (f" They said: “{ok.said}”. Do what they ask first." if ok.said else "")
                    )

            if stay_in and actions:
                front, title = frontmost_app(), front_title(stay_in[0])
                if front != stay_in[0] or stay_in[1].lower() not in title.lower():
                    result = f"stopped: left {stay_in[1]} (in front: {front} · {title[:60]})"
                    print(f"[{step}] {result}")
                    break

            executed: list[dict] = []
            changed: ScreenChanged | None = None
            if not stop and not skip:
                print(f"[{step}] executing {len(actions)} action(s)", flush=True)
                guard.arm()
                try:
                    with guard.acting(), timed("actions"):
                        if work_app and any(a["type"] in KEY_KINDS for a in actions) and frontmost_app() in ours:
                            print(f"[{step}] the overlay had the keyboard; giving it back to {work_app}")
                            focus(work_app)
                        driver.work_app = work_app
                        driver.execute(actions, seen=png)
                    executed = actions
                except Interrupted as e:
                    executed, stop = actions[: e.done], e.reason
                    if e.reason == "human":
                        guard.stopped_batch()
                except ScreenChanged as e:
                    executed, changed = actions[: e.done], e
                if driver.fresh_checks:
                    log.annotate(step, fresh=driver.fresh_checks)
                if driver.failures:
                    print(f"[{step}] failed: " + "; ".join(driver.failures))
                    notes += driver.failures

            if stop:
                # The user may change the screen while paused, so what was left of
                # this batch is dropped and the model re-plans from a fresh screenshot.
                left = len(actions) - len(executed)
                print(f"[{step}] paused ({'you moved' if stop == 'human' else 'by user'}); {left} action(s) not run")
                with timed("paused"):
                    resumed = hold(stop)
                if not resumed:
                    log.annotate(step, paused="stopped", executed=len(executed))
                    result = "stopped: user stopped the task"
                    break
                refocus()
                print(f"[{step}] resumed; re-planning")
                log.annotate(step, paused="resumed", interrupt=stop, executed=len(executed))
                notes.append(RESUME_NOTE)
            elif changed:
                skipped += 1
                print(f"[{step}] screen changed before action {changed.done + 1} (diff {changed.diff:.3f}); not run")
                notes.append(
                    CHANGED_NOTE if not executed
                    else f"Only the first {len(executed)} of your actions ran; the rest were NOT executed because "
                    "the screen changed before the click. Decide again from this screenshot."
                )

            if (front := frontmost_app()) and front not in ours:
                work_app = front  # refocus this after a question, not whatever the user clicked meanwhile
            seen = png
            if changed and not stop:
                png = changed.png  # the screen as it was when the click was called off
            else:
                png = driver.settled_screenshot()  # once a page load or a menu has finished moving
                spent["settle"] += driver.settle_s
                log.annotate(step, settle_s=driver.settle_s)
            log.screenshot(step, png)

            # Did the batch do anything? Nothing changing on screen, again and
            # again, means the agent is stuck: say so to the model the first
            # time, and after STUCK_AFTER in a row stop and ask the user.
            verdict = None
            if blocked:
                verdict = effect.blocked()  # asking for a dead batch again is no progress either
            elif stop or changed or skip:
                effect.reset()  # the user or the page moved things, or nothing ran: count afresh
            else:
                verdict = effect.record(executed, seen, png, driver.settle_area)
                log.annotate(step, changed_px=effect.last_px)
                if verdict in ("none", "stuck"):
                    dead.append(executed)
            last_dead = verdict in ("none", "stuck")
            if verdict is not None:
                no_effect += verdict in ("none", "stuck")
                if verdict == "none" and not blocked:
                    print(f"[{step}] nothing changed on screen")
                    notes.append(NO_EFFECT_NOTE)
                elif verdict == "stuck" and stuck == 0:
                    # The first time: try another way without bothering the user (a
                    # fresh plan from Muse Wonder); stop and ask only if it sticks again.
                    stuck += 1
                    what = plain(executed or actions)
                    print(f"[{step}] stuck: {what} changed nothing, {effect.streak} times in a row; trying another way")
                    log.annotate(step, stuck=what, stuck_action="re-plan")
                    overlay.send("status", text="Trying another way…", busy=True)
                    effect.reset()
                    if the_plan and replans < MAX_REPLANS and (fresh := make(png, past)):
                        replans += 1
                        fresh.question, fresh.answer = the_plan.question, the_plan.answer  # asked once is enough
                        the_plan = fresh
                        log.save("plan.json", the_plan.to_log() | {"replans": replans})
                        notes.append(STUCK_REPLAN_NOTE.format(n=STUCK_AFTER))
                    else:
                        notes.append(NO_EFFECT_NOTE)
                elif verdict == "stuck":
                    # The second time, after a fresh plan already failed: stop and say why,
                    # rather than wait for a Resume that can't help (checklist 3.3).
                    stuck += 1
                    what = plain(executed or actions)
                    print(f"[{step}] stuck again: {what} changed nothing, {effect.streak} times in a row; stopping")
                    log.annotate(step, stuck=what, stuck_action="stop")
                    result = f"stopped: {why_stuck(client, task, past, png, what)}"
                    break

            draft = next((a["text"] for a in reversed(executed) if a["type"] == "type" and len(a.get("text") or "") >= 15), draft)
            line = f"{step}. {note + ' | ' if note else ''}{describe(executed) or '(nothing executed)'}"
            past.append("\n".join([line] + [f"   Note: {n}" for n in notes]))
            done.append(executed)

            if history == "full":
                replies = [
                    {"type": "function_call_output", "call_id": f["call_id"], "output": "done"} for f in fns
                ]
                if call:
                    replies.append({
                        "type": "computer_call_output",
                        "call_id": call["call_id"],
                        "acknowledged_safety_checks": checks,
                        "output": {"type": "computer_screenshot", "image_url": data_url(png)},
                    })
                said = [{"type": "input_text", "text": " ".join(notes)}] if notes else []
                if not call:  # no computer_call_output to carry the screenshot
                    said.append({"type": "input_image", "image_url": data_url(png)})
                response, seconds = ask_model(
                    client,
                    overlay,
                    str(step + 1),
                    effort,
                    tool_list,
                    previous_response_id=response.id,
                    input=replies + ([{"role": "user", "content": said}] if said else []),
                )
            else:
                # Checks were approved above, before executing; a fresh turn has no
                # computer_call_output to carry the acknowledgement.
                response, seconds = ask_model(
                    client,
                    overlay,
                    str(step + 1),
                    effort,
                    tool_list,
                    instructions=instructions,
                    input=fresh_input(
                        checked_task(), past, png, nudge=only_looking(done), regions=selected,
                        brief=the_plan.brief() if the_plan else "",
                    ),
                )
        else:
            log.response(max_steps + 1, response)  # the reply we never acted on
            last = next((p.split(" | ")[0].split(". ", 1)[-1] for p in reversed(past) if " | " in p), "")
            result = f"stopped: step limit ({max_steps}) reached" + (f"; last step: {last}" if last else "")

    except StopTask:
        pass
    except Terminated:
        result = "stopped: user stopped the task"
    except BaseException as e:  # incl. Ctrl+C and the mouse-in-a-corner emergency stop
        result = f"crashed: {friendly(e)}"
        raise
    finally:
        guard.stop()
        log.settings.update(
            asked=asked, corrected=corrected, skipped_screen_changed=skipped, stuck=stuck,
            interruptions=[i.to_log() for i in guard.interruptions],
        )
        elapsed = time.time() - started
        log.summary(result, step, elapsed)
        print(f"result: {result}")
        if guard.interruptions:
            print("interruptions: " + json.dumps([i.to_log() for i in guard.interruptions]))
        ok = not result.startswith(("crashed", "stopped"))
        api = meter.summary()
        spent["model"] = (api["by_purpose"].get("act") or {}).get("seconds_total", 0.0)
        spent["other"] = max(0.0, elapsed - sum(v for k, v in spent.items() if k != "other"))
        log.save("summary.json", {
            "task": task, "result": result, "ok": ok, "steps": step, "elapsed_s": round(elapsed, 1),
            "time_s": {k: round(v, 1) for k, v in sorted(spent.items(), key=lambda kv: -kv[1])},
            "api": api,
            "counts": {
                "asked": asked, "corrected": corrected, "no_effect": no_effect, "stuck": stuck, "snapped": snaps,
                "skipped_screen_changed": skipped, "interruptions": len(guard.interruptions), "replans": replans,
                "confirmations": len(confirmations), "declined": sum(1 for c in confirmations if c.get("declined")),
                # What the user had to say: the task, each answer, each spoken yes / no.
                "utterances": 1 + asked + sum(1 for c in confirmations if c["method"] in ("yes", "terminal")
                                              or (c["method"] == "countdown" and c.get("declined"))),
            },
            "confirmations": confirmations,
            "settings": {k: v for k, v in log.settings.items() if k not in ("interruptions", "regions")},
        })
        parts = ", ".join(f"{k} {v:.0f}s" for k, v in sorted(spent.items(), key=lambda kv: -kv[1]) if v >= 0.5)
        print(f"summary: {step} steps in {elapsed:.0f}s ({parts}); ${api['usd']:.3f} over {api['calls']} API calls")
        # "stopped: user stopped the task" -> "User stopped the task" on screen
        shown = result.strip() if ok else result.split(": ", 1)[-1].strip()
        shown = shown[:1].upper() + shown[1:]
        overlay.send("done", text=shown, ok=ok, error=result.startswith("crashed"))
        overlay.close()
    return result
