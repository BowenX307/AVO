"""Computer-tool agent loop on the Meta Model API (Responses API)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import termios
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from avo.driver import MacDriver
from avo.overlay import Overlay

MODEL = "muse-spark-1.3"
BASE_URL = "https://api.meta.ai/v1"
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
SETTLE_SECONDS = 0.8  # let the UI settle before the post-action screenshot

SYSTEM_PROMPT = """\
You are operating a macOS computer through screenshots of its main display.
- Use macOS shortcuts: Command (cmd) instead of Ctrl, e.g. cmd+L to focus the browser address bar.
- Coordinates are pixels in the most recent screenshot.
- Do only what the task asks. Do not close windows, quit apps, or change settings unless asked.
- Work without asking the user questions. When the task is done, stop calling the computer \
tool and reply with a one-sentence summary of what you did."""


def make_client() -> OpenAI:
    load_dotenv()
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise SystemExit("MODEL_API_KEY is not set (put it in .env or the environment)")
    return OpenAI(base_url=BASE_URL, api_key=key)


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


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

    def __init__(self, task: str):
        self.dir = RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "task.txt").write_text(task + "\n", encoding="utf-8")

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

    def summary(self, result: str, steps: int, elapsed: float) -> None:
        record = {"result": result, "steps": steps, "elapsed_s": round(elapsed, 1)}
        (self.dir / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def computer_calls(response) -> list[dict]:
    # Dump to dicts: the SDK's typed models don't know Meta's `actions` field.
    return [item.model_dump() for item in response.output if item.type == "computer_call"]


def ask_model(client: OpenAI, overlay: Overlay, label: str, **kwargs):
    """responses.create with a visible waiting line, so a slow reply doesn't look like a hang."""
    print(f"[{label}] waiting for model...", end="", flush=True)
    overlay.send("status", text="AVO 思考中…", busy=True)
    t0 = time.time()
    response = client.responses.create(model=MODEL, tools=[{"type": "computer"}], **kwargs)
    print(f" {time.time() - t0:.1f}s")
    return response


def run(task: str, max_steps: int = 20, auto_ack: bool = True) -> str:
    client = make_client()
    overlay = Overlay()
    driver = MacDriver(overlay=overlay)
    log = RunLog(task)
    started = time.time()
    print(f"run dir: {log.dir}")
    print(f"overlay: {'connected' if overlay.active else 'not running (cd overlay && npm start)'}")

    result = "stopped: step budget reached"
    step = 0
    try:
        png = driver.screenshot()
        log.screenshot(0, png)
        response = ask_model(
            client,
            overlay,
            "1",
            instructions=SYSTEM_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": task},
                        {"type": "input_image", "image_url": data_url(png)},
                    ],
                }
            ],
        )

        for step in range(1, max_steps + 1):
            calls = computer_calls(response)
            if not calls:
                log.response(step, response)
                result = response.output_text or "(model finished without a message)"
                break
            if len(calls) > 1:
                print(f"warning: {len(calls)} computer_calls in one response; handling the first")

            call = calls[0]
            actions = call.get("actions") or ([call["action"]] if call.get("action") else [])
            checks = call.get("pending_safety_checks") or []
            log.response(step, response, {"actions": actions})
            if response.output_text:
                print(f"[{step}] model: {response.output_text}")
                overlay.send("status", text=response.output_text.strip(), busy=False)
            print(f"[{step}] actions: {json.dumps(actions, ensure_ascii=False)}")

            if checks and not approve_safety_checks(step, checks, auto_ack):
                result = "stopped: safety check declined by user"
                break

            driver.execute(actions)
            time.sleep(SETTLE_SECONDS)
            png = driver.screenshot()
            log.screenshot(step, png)

            response = ask_model(
                client,
                overlay,
                str(step + 1),
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
            log.response(max_steps + 1, response)  # the reply we never acted on

    except BaseException as e:  # incl. Ctrl+C and pyautogui's FailSafeException
        result = f"crashed: {type(e).__name__}: {e}"
        raise
    finally:
        log.summary(result, step, time.time() - started)
        print(f"result: {result}")
        overlay.send("done", text=result.strip())
        overlay.close()
    return result
