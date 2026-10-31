"""Computer-tool agent loop on the Meta Model API (Responses API)."""

from __future__ import annotations

import json
import subprocess
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from avo import confirm
from avo.desktop import frontmost_app, overlay_windows
from avo.driver import FRESH_THRESHOLD, Interrupted, MacDriver, ScreenChanged
from avo.effect import NO_EFFECT_NOTE, STUCK_AFTER, STUCK_NOTE, EffectWatch, plain
from avo.gate import ask_user, assess
from avo.human import AUTO_RESUME_S, HumanGuard
from avo.llm import MODEL, data_url, describe, make_client
from avo.meter import Meter
from avo.overlay import Overlay
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


def focus(bundle: str) -> None:
    """Bring an app to the front, which gives it the keyboard."""
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
) -> str:
    """Run the computer-tool loop.

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
    """
    if plan not in ("auto", "always", "never"):
        raise ValueError(f"plan must be 'auto', 'always' or 'never', not {plan!r}")
    if history not in ("full", "text"):
        raise ValueError(f"history must be 'full' or 'text', not {history!r}")
    if human_resume not in ("manual", "auto"):
        raise ValueError(f"human_resume must be 'manual' or 'auto', not {human_resume!r}")
    meter = Meter()  # every API call of the run, for summary.json
    client = meter.wrap(make_client())
    overlay = Overlay()
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
            "plan": plan, "voice": voice,
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

    def make(screen: bytes, past: list[str] = ()) -> Plan | None:
        """A plan from this screen, or None if the planner fails (a plan helps; it isn't needed)."""
        overlay.send("status", text="Planning…", busy=True, phase="planning")
        try:
            with timed("planning"):
                p = make_plan(client, task, screen, past, effort=effort, model=REPLAN_MODEL if past else MODEL)
        except Exception as e:
            print(f"planner: {type(e).__name__}: {e}")
            return None
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
    draft = ""  # the last longer text typed: shown before a send
    try:
        ours = {w["bundle"] for w in overlay_windows() if w.get("bundle")}
        work_app = app or frontmost_app()
        if work_app in ours:
            work_app = None
            print("warning: AVO's overlay is in front and no app to work in was given")
        if work_app and frontmost_app() != work_app:
            focus(work_app)
        guard.start()
        png = driver.screenshot()
        log.screenshot(0, png)
        if snap_to_controls:
            warm_up(*driver.to_screen(640, 400))  # Chrome builds its AX tree on first use
        selected = prepare(driver, regions) if regions else []
        if selected:
            log.settings["regions"] = {r.label: r.shot for r in selected}
            highlight(client, overlay, png, selected, log.dir)  # SAM, alongside the first model call
        if plan == "always" or (plan == "auto" and not selected):
            the_plan = make(png)
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
                if choice == "pick":
                    the_plan.answer = the_plan.options[answer["index"]]
                elif choice == "pause":
                    with timed("paused"):
                        if not hold("pause"):
                            choice = "stop"
                if choice in ("stop", None):
                    result = "stopped: user stopped the task"
                    raise StopTask
        start = the_plan.start_action() if the_plan and not selected else None
        if start:  # open the plan's start ourselves: no turn spent on the address bar or the Dock
            print(f"[0] opening the start: {describe([start])}")
            overlay.send("status", text=f"Plan · {len(the_plan.steps)} steps", busy=True)
            opened = False
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
            input=fresh_input(task, past, png, regions=selected, brief=the_plan.brief() if the_plan else ""),
        )

        for step in range(1, max_steps + 1):
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
            if gate and not stop:
                overlay.send("status", text="Checking this step…", busy=True)
                with timed("checks"):
                    a = assess(client, task, past, done, png, actions, note, driver.geometry.shot_size,
                               where=(lambda x, y: location(*driver.to_screen(x, y))) if snap_to_controls else None)
                stop = interrupted()  # the user may have taken over during the check
                if a and not stop:
                    print(f"[{step}] gate: {'FLAG ' + '; '.join(a.reasons) if a.flagged else 'ok'} ({a.seconds:.1f}s)")
                    entry = a.to_log()
                    if a.flagged:
                        asked += 1
                        front = work_app or frontmost_app()
                        ask = "Which one did you mean? Pick on screen" if a.candidates else "AVO needs your OK — see the highlight"
                        overlay.send("status", text=ask, busy=False)
                        with guard.acting(), guard.asking(), timed("you"):
                            choice, actions, answer = ask_user(overlay, driver, a, actions, note, png)
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
                        elif choice == "no":
                            corrected += 1
                            note = (note + " " if note else "") + "(the user corrected the click point on screen)"
                        else:
                            note = (note + " " if note else "") + "(the user confirmed this step)"
                    log.annotate(step, gate=entry)

            skip = False  # the user said no to this batch
            if not stop and actions:
                if work_app and any(a["type"] in KEY_KINDS for a in actions) and frontmost_app() in ours:
                    focus(work_app)  # so the countdown rings the field the keys will go to
                with guard.acting(), guard.asking(), timed("you"):
                    ok = confirm.before(
                        step, actions, checks, overlay, driver.to_screen,
                        review=a.review if a else None, answered=answered, draft=draft,
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
                elif ok.choice == "no":
                    skip = True
                    notes.append(
                        f"The user said no to this: {describe(actions)}. It was NOT executed; don't repeat it."
                        + (f" They said: “{ok.said}”" if ok.said else "")
                    )

            executed: list[dict] = []
            changed: ScreenChanged | None = None
            if not stop and not skip:
                print(f"[{step}] executing {len(actions)} action(s)", flush=True)
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
            if stop or changed or skip:
                effect.reset()  # the user or the page moved things, or nothing ran: count afresh
            else:
                verdict = effect.record(executed, seen, png, driver.settle_area)
                log.annotate(step, changed_px=effect.last_px)
                no_effect += verdict in ("none", "stuck")
                if verdict == "none":
                    print(f"[{step}] nothing changed on screen")
                    notes.append(NO_EFFECT_NOTE)
                elif verdict == "stuck":
                    stuck += 1
                    what = plain(executed)
                    print(f"[{step}] stuck: {what} changed nothing, {effect.streak} times in a row")
                    log.annotate(step, stuck=what)
                    if not overlay.active:
                        result = f"stopped: stuck, {what} changed nothing on screen"
                        break
                    overlay.send("interrupt", text=f"Stuck — {what} changes nothing")
                    with guard.asking(), timed("paused"):  # moving to Resume isn't "you moved"
                        resumed = hold("stuck")
                    if not resumed:
                        result = "stopped: user stopped the task"
                        break
                    refocus()
                    print(f"[{step}] resumed after getting stuck; re-planning")
                    effect.reset()
                    notes.append(STUCK_NOTE.format(n=STUCK_AFTER))
                    png = driver.screenshot()
                    log.screenshot(step, png)
                    if the_plan and replans < MAX_REPLANS and (fresh := make(png, past)):
                        replans += 1
                        fresh.question, fresh.answer = the_plan.question, the_plan.answer  # asked once is enough
                        the_plan = fresh
                        log.save("plan.json", the_plan.to_log() | {"replans": replans})

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
                        task, past, png, nudge=only_looking(done), regions=selected,
                        brief=the_plan.brief() if the_plan else "",
                    ),
                )
        else:
            log.response(max_steps + 1, response)  # the reply we never acted on

    except StopTask:
        pass
    except BaseException as e:  # incl. Ctrl+C and the mouse-in-a-corner emergency stop
        result = f"crashed: {type(e).__name__}: {e}"
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
            },
            "confirmations": confirmations,
            "settings": {k: v for k, v in log.settings.items() if k not in ("interruptions", "regions")},
        })
        parts = ", ".join(f"{k} {v:.0f}s" for k, v in sorted(spent.items(), key=lambda kv: -kv[1]) if v >= 0.5)
        print(f"summary: {step} steps in {elapsed:.0f}s ({parts}); ${api['usd']:.3f} over {api['calls']} API calls")
        # "stopped: user stopped the task" -> "User stopped the task" on screen
        shown = result.strip() if ok else result.split(": ", 1)[-1].strip().capitalize()
        overlay.send("done", text=shown, ok=ok)
        overlay.close()
    return result
