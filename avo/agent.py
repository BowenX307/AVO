"""Computer-tool agent loop on the Meta Model API (Responses API)."""

from __future__ import annotations

import json
import subprocess
import sys
import termios
import time
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from avo.desktop import frontmost_app, overlay_windows
from avo.driver import FRESH_THRESHOLD, Interrupted, MacDriver, ScreenChanged
from avo.gate import ask_user, assess
from avo.human import AUTO_RESUME_S, HumanGuard
from avo.llm import MODEL, data_url, describe, make_client
from avo.overlay import Overlay
from avo.regions import Region, highlight, mark, prepare
from avo.regions import describe as describe_regions
from avo.snap import snap, warm_up

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
SETTLE_SECONDS = 0.8  # let the UI settle before the post-action screenshot

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
- Always write in English, even when the task is in another language."""


# The generic check Meta attaches to every action. With auto_ack on (the user's
# standing approval), it is acknowledged without asking; any other code, e.g.
# malicious_instructions, still needs a human.
ROUTINE_CHECKS = {"computer_action_requires_confirmation"}


def approve_safety_checks(step: int, checks: list[dict], auto_ack: bool) -> bool:
    """Acknowledge routine checks if auto_ack; ask the human about everything else."""
    unusual = [c for c in checks if not (auto_ack and c.get("code") in ROUTINE_CHECKS)]
    if not unusual:
        print(f"[{step}] auto-acknowledged: {', '.join(c.get('code') for c in checks)}")
        return True

    print("\n!! The model raised safety checks before this action:")
    for c in unusual:
        print(f"   [{c.get('code')}] {c.get('message')}  (id={str(c.get('id'))[:16]}...)")
    # Answering in the terminal focuses it; hand focus back afterwards so the
    # agent's keystrokes don't land in the terminal.
    front = frontmost_app()
    if sys.stdin.isatty():
        # Drop keys pressed while waiting for the model (e.g. a stray Enter),
        # so they can't answer this prompt.
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    try:
        while True:
            try:
                answer = input("Acknowledge and continue? [y/n]: ").strip().lower()
            except EOFError:  # no interactive terminal: treat as declined
                print("(no input available; declining)")
                return False
            if answer in ("y", "n"):
                return answer == "y"
    finally:
        if front:
            subprocess.run(["open", "-b", front])
            time.sleep(0.5)


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


def computer_calls(response) -> list[dict]:
    # Dump to dicts: the SDK's typed models don't know Meta's `actions` field.
    return [item.model_dump() for item in response.output if item.type == "computer_call"]


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


def ask_model(client: OpenAI, overlay: Overlay, label: str, effort: str | None, **kwargs):
    """responses.create with a visible waiting line, so a slow reply doesn't look like a hang.

    Returns (response, seconds).
    """
    print(f"[{label}] waiting for model...", end="", flush=True)
    overlay.send("status", text="Thinking…", busy=True)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    t0 = time.time()
    response = client.responses.create(model=MODEL, tools=[{"type": "computer"}], **kwargs)
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

LOOKING_KINDS = ("scroll", "keypress", "wait", "screenshot", "move")


def only_looking(done: list[list[dict]]) -> bool:
    """True if the last STILL_STEPS batches only scrolled or pressed keys."""
    recent = done[-STILL_STEPS:]
    return len(recent) == STILL_STEPS and all(a["type"] in LOOKING_KINDS for batch in recent for a in batch)


def fresh_input(
    task: str, history: list[str], png: bytes, nudge: bool = False, regions: list[Region] = ()
) -> list[dict]:
    """A self-contained turn: the task, what was done so far as text, and the current screen.

    Boxes the user selected are drawn on the first turn's screenshot and described in text.
    """
    text = task
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
    auto_ack: bool = True,
    effort: str | None = "minimal",
    history: str = "text",
    gate: bool = True,
    snap_to_controls: bool = True,
    regions: dict[str, tuple[int, int, int, int]] | None = None,
    human_guard: bool = True,
    human_resume: str = "manual",
    freshness: float | None = FRESH_THRESHOLD,
) -> str:
    """Run the computer-tool loop.

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
    """
    if history not in ("full", "text"):
        raise ValueError(f"history must be 'full' or 'text', not {history!r}")
    if human_resume not in ("manual", "auto"):
        raise ValueError(f"human_resume must be 'manual' or 'auto', not {human_resume!r}")
    client = make_client()
    overlay = Overlay()
    guard = HumanGuard(overlay, enabled=human_guard)

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
            "human_guard": human_guard, "human_resume": human_resume, "freshness": freshness,
        },
    )
    started = time.time()
    print(f"run dir: {log.dir}  (effort={effort}, history={history}, gate={gate})")
    print(f"overlay: {'connected' if overlay.active else 'not running (cd overlay && npm start)'}")

    def hold(reason: str) -> bool:
        """Wait out a pause: the user took over ("human") or pressed Option-Space ("pause").

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

    result = "stopped: step budget reached"
    step = 0
    past: list[str] = []  # text history, used when history == "text"
    done: list[list[dict]] = []  # executed batches, for repeat detection
    asked = corrected = skipped = 0
    try:
        guard.start()
        png = driver.screenshot()
        log.screenshot(0, png)
        if snap_to_controls:
            warm_up(*driver.to_screen(640, 400))  # Chrome builds its AX tree on first use
        ours = {w["bundle"] for w in overlay_windows()}  # the overlay app: never the one to refocus
        work_app = frontmost_app()
        selected = prepare(driver, regions) if regions else []
        if selected:
            log.settings["regions"] = {r.label: r.shot for r in selected}
            highlight(client, overlay, png, selected, log.dir)  # SAM, alongside the first model call
        response, seconds = ask_model(
            client, overlay, "1", effort, instructions=SYSTEM_PROMPT, input=fresh_input(task, [], png, regions=selected)
        )

        for step in range(1, max_steps + 1):
            calls = computer_calls(response)
            if not calls:
                log.response(step, response, {"model_seconds": round(seconds, 1)})
                result = response.output_text or "(model finished without a message)"
                break
            if len(calls) > 1:
                print(f"warning: {len(calls)} computer_calls in one response; handling the first")

            call = calls[0]
            actions = call.get("actions") or ([call["action"]] if call.get("action") else [])
            checks = call.get("pending_safety_checks") or []
            log.response(step, response, {"actions": actions, "model_seconds": round(seconds, 1)})
            note = (response.output_text or "").strip()
            notes: list[str] = []  # for the model's next turn
            if note:
                print(f"[{step}] model: {note}")
                if not guard.tripped.is_set():
                    overlay.send("status", text=note, busy=False)
            print(f"[{step}] actions: {json.dumps(actions, ensure_ascii=False)}")

            if checks and not approve_safety_checks(step, checks, auto_ack):
                result = "stopped: safety check declined by user"
                break

            if snap_to_controls:
                actions = snap_clicks(driver, actions)
                snapped = [a["_snap"] | {"to": [a["x"], a["y"]]} for a in actions if "_snap" in a]
                if snapped:
                    print(f"[{step}] snapped: " + ", ".join(f"{s['from']}->{s['to']} {s['label'] or s['role']}" for s in snapped))
                    log.annotate(step, snapped=snapped)

            stop = interrupted()
            if gate and not stop:
                overlay.send("status", text="Checking this step…", busy=True)
                a = assess(client, task, past, done, png, actions, note, driver.geometry.shot_size)
                stop = interrupted()  # the user may have taken over during the check
                if a and not stop:
                    print(f"[{step}] gate: {'FLAG ' + '; '.join(a.reasons) if a.flagged else 'ok'} ({a.seconds:.1f}s)")
                    entry = a.to_log()
                    if a.flagged:
                        asked += 1
                        front = work_app or frontmost_app()
                        ask = "Which one did you mean? Pick on screen" if a.candidates else "AVO needs your OK — see the highlight"
                        overlay.send("status", text=ask, busy=False)
                        with guard.acting(), guard.asking():
                            choice, actions, answer = ask_user(overlay, driver, a, actions, note, png)
                            if front:  # answering on the overlay may have focused it
                                subprocess.run(["open", "-b", front])
                                time.sleep(0.3)
                        print(f"[{step}] user: {choice}" + (f" -> {describe(actions)}" if choice == "no" else ""))
                        entry["user"] = {"choice": choice, "answer": answer, "actions": actions}
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

            executed: list[dict] = []
            changed: ScreenChanged | None = None
            if not stop:
                print(f"[{step}] executing {len(actions)} action(s)", flush=True)
                try:
                    with guard.acting():
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

            if stop:
                # The user may change the screen while paused, so what was left of
                # this batch is dropped and the model re-plans from a fresh screenshot.
                left = len(actions) - len(executed)
                print(f"[{step}] paused ({'you moved' if stop == 'human' else 'by user'}); {left} action(s) not run")
                if not hold(stop):
                    log.annotate(step, paused="stopped", executed=len(executed))
                    result = "stopped: user stopped the task"
                    break
                if work_app and guard.overlay_in_front():  # clicking Resume can focus the overlay
                    subprocess.run(["open", "-b", work_app])
                    time.sleep(0.3)
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
            if changed and not stop:
                png = changed.png  # the screen as it was when the click was called off
            else:
                time.sleep(SETTLE_SECONDS)
                png = driver.screenshot()
            log.screenshot(step, png)
            line = f"{step}. {note + ' | ' if note else ''}{describe(executed) or '(nothing executed)'}"
            past.append("\n".join([line] + [f"   Note: {n}" for n in notes]))
            done.append(executed)

            if history == "full":
                response, seconds = ask_model(
                    client,
                    overlay,
                    str(step + 1),
                    effort,
                    previous_response_id=response.id,
                    input=[
                        {
                            "type": "computer_call_output",
                            "call_id": call["call_id"],
                            "acknowledged_safety_checks": checks,
                            "output": {"type": "computer_screenshot", "image_url": data_url(png)},
                        }
                    ]
                    + ([{"role": "user", "content": [{"type": "input_text", "text": " ".join(notes)}]}] if notes else []),
                )
            else:
                # Checks were approved above, before executing; a fresh turn has no
                # computer_call_output to carry the acknowledgement.
                response, seconds = ask_model(
                    client,
                    overlay,
                    str(step + 1),
                    effort,
                    instructions=SYSTEM_PROMPT,
                    input=fresh_input(task, past, png, nudge=only_looking(done), regions=selected),
                )
        else:
            log.response(max_steps + 1, response)  # the reply we never acted on

    except BaseException as e:  # incl. Ctrl+C and the mouse-in-a-corner emergency stop
        result = f"crashed: {type(e).__name__}: {e}"
        raise
    finally:
        guard.stop()
        log.settings.update(
            asked=asked, corrected=corrected, skipped_screen_changed=skipped,
            interruptions=[i.to_log() for i in guard.interruptions],
        )
        log.summary(result, step, time.time() - started)
        print(f"result: {result}")
        if guard.interruptions:
            print("interruptions: " + json.dumps([i.to_log() for i in guard.interruptions]))
        ok = not result.startswith(("crashed", "stopped"))
        # "stopped: user stopped the task" -> "User stopped the task" on screen
        shown = result.strip() if ok else result.split(": ", 1)[-1].strip().capitalize()
        overlay.send("done", text=shown, ok=ok)
        overlay.close()
    return result
