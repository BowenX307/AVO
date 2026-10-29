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

from avo.driver import MacDriver
from avo.gate import ask_user, assess
from avo.llm import MODEL, data_url, describe, make_client
from avo.overlay import Overlay

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
SETTLE_SECONDS = 0.8  # let the UI settle before the post-action screenshot

SYSTEM_PROMPT = """\
You are operating a macOS computer through screenshots of its main display.
- Use macOS shortcuts: Command (cmd) instead of Ctrl, e.g. cmd+L to focus the browser address bar.
- Coordinates are pixels in the most recent screenshot.
- Do only what the task asks. Do not close windows, quit apps, or change settings unless asked.
- Work without asking the user questions. When the task is done, stop calling the computer \
tool and reply with a one-sentence summary of what you did."""


# The generic check Meta attaches to every action. With auto_ack on (the user's
# standing approval), it is acknowledged without asking; any other code, e.g.
# malicious_instructions, still needs a human.
ROUTINE_CHECKS = {"computer_action_requires_confirmation"}


def frontmost_app() -> str | None:
    """Bundle id of the frontmost app, e.g. com.google.Chrome."""
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(
        ["lsappinfo", "info", "-only", "bundleid", asn], capture_output=True, text=True
    ).stdout
    return info.split("=", 1)[1].strip().strip('"') if "=" in info else None


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


def ask_model(client: OpenAI, overlay: Overlay, label: str, effort: str | None, **kwargs):
    """responses.create with a visible waiting line, so a slow reply doesn't look like a hang.

    Returns (response, seconds).
    """
    print(f"[{label}] waiting for model...", end="", flush=True)
    overlay.send("status", text="思考中…", busy=True)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    t0 = time.time()
    response = client.responses.create(model=MODEL, tools=[{"type": "computer"}], **kwargs)
    seconds = time.time() - t0
    print(f" {seconds:.1f}s")
    return response, seconds


def fresh_input(task: str, history: list[str], png: bytes) -> list[dict]:
    """A self-contained turn: the task, what was done so far as text, and the current screen."""
    text = task
    if history:
        text += (
            "\n\nSteps already taken (the screenshot shows the screen after them):\n"
            + "\n".join(history)
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
) -> str:
    """Run the computer-tool loop.

    effort: reasoning effort ("minimal" ... "max"); None uses the model default.
    history: "full" chains turns with previous_response_id, so the model sees every
        earlier screenshot (input grows each step, and so does latency). "text" sends
        each turn fresh with only the latest screenshot plus a text log of past steps.
    gate: before a click/drag batch runs, check it (model review + repeat detection)
        and, if it looks wrong, point at the target on screen and ask the user.
        Needs the overlay; without it the run proceeds unchecked.
    """
    if history not in ("full", "text"):
        raise ValueError(f"history must be 'full' or 'text', not {history!r}")
    client = make_client()
    overlay = Overlay()
    driver = MacDriver(overlay=overlay)
    gate = gate and overlay.active
    log = RunLog(task, {"model": MODEL, "effort": effort, "history": history, "gate": gate})
    started = time.time()
    print(f"run dir: {log.dir}  (effort={effort}, history={history}, gate={gate})")
    print(f"overlay: {'connected' if overlay.active else 'not running (cd overlay && npm start)'}")

    result = "stopped: step budget reached"
    step = 0
    past: list[str] = []  # text history, used when history == "text"
    done: list[list[dict]] = []  # executed batches, for repeat detection
    asked = corrected = 0
    try:
        png = driver.screenshot()
        log.screenshot(0, png)
        response, seconds = ask_model(
            client, overlay, "1", effort, instructions=SYSTEM_PROMPT, input=fresh_input(task, [], png)
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
            if note:
                print(f"[{step}] model: {note}")
                overlay.send("status", text=note, busy=False)
            print(f"[{step}] actions: {json.dumps(actions, ensure_ascii=False)}")

            if checks and not approve_safety_checks(step, checks, auto_ack):
                result = "stopped: safety check declined by user"
                break

            if gate:
                overlay.send("status", text="检查这一步…", busy=True)
                a = assess(client, task, past, done, png, actions, note, driver.geometry.shot_size)
                if a:
                    print(f"[{step}] gate: {'FLAG ' + '；'.join(a.reasons) if a.flagged else 'ok'} ({a.seconds:.1f}s)")
                    entry = a.to_log()
                    if a.flagged:
                        asked += 1
                        front = frontmost_app()
                        overlay.send("status", text="等你确认", busy=False)
                        choice, actions, answer = ask_user(overlay, driver, a, actions, note)
                        if front:  # answering on the overlay may have focused it
                            subprocess.run(["open", "-b", front])
                            time.sleep(0.3)
                        print(f"[{step}] user: {choice}" + (f" -> {describe(actions)}" if choice == "no" else ""))
                        entry["user"] = {"choice": choice, "answer": answer, "actions": actions}
                        if choice == "stop":
                            log.annotate(step, gate=entry)
                            result = "stopped: user stopped the task"
                            break
                        if choice == "no":
                            corrected += 1
                            note = (note + " " if note else "") + "(用户在屏幕上纠正了点击位置)"
                        else:
                            note = (note + " " if note else "") + "(用户确认了这一步)"
                    log.annotate(step, gate=entry)

            driver.execute(actions)
            time.sleep(SETTLE_SECONDS)
            png = driver.screenshot()
            log.screenshot(step, png)
            past.append(f"{step}. {note + ' | ' if note else ''}{describe(actions)}")
            done.append(actions)

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
                    ],
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
                    input=fresh_input(task, past, png),
                )
        else:
            log.response(max_steps + 1, response)  # the reply we never acted on

    except BaseException as e:  # incl. Ctrl+C and pyautogui's FailSafeException
        result = f"crashed: {type(e).__name__}: {e}"
        raise
    finally:
        log.settings.update(asked=asked, corrected=corrected)
        log.summary(result, step, time.time() - started)
        print(f"result: {result}")
        overlay.send("done", text=result.strip(), ok=not result.startswith(("crashed", "stopped")))
        overlay.close()
    return result
