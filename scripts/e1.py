"""E1: the same tasks with macOS Voice Control and with AVO, timed. Serves the timer page.

    uv run scripts/e1.py            # then open http://127.0.0.1:8777/e1
    uv run scripts/e1.py --count    # afterwards: count what was said in each task (Meta ASR)

The timer page (eval/e1/index.html) starts a task, opens its start page, and
records the microphone while the task runs, so how many things were said can
be counted afterwards from the audio, the same way for both tools (Voice
Control keeps no log). The demo pages report what was sent, booked, submitted
or assigned to /report, and the Finder task is checked on disk, so success is
checked by the page, not by memory. The real-site tasks (Wikipedia, Meta
Careers) are judged by the person on the Done form.

Audio stays in eval/e1/audio/ (not committed); eval/e1/results.json gets the
times, counts and checks. Don't run scripts/run_demo.py at the same time: both
use port 8777.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_demo import check as page_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
E1 = ROOT / "eval" / "e1"
AUDIO = E1 / "audio"
RESULTS = E1 / "results.json"
PORT = 8777
DESKTOP = Path.home() / "Desktop"
NOTES, ARCHIVE = DESKTOP / "e1-notes.txt", DESKTOP / "e1-archive"
TOOLS = ("voice-control", "avo")


def tasks() -> list[dict]:
    return json.loads((E1 / "tasks.json").read_text())


def load() -> list[dict]:
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else []


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.report: dict | None = None  # the last thing the demo page reported
        self.report_t: float | None = None  # when (the task's end, for the pages that report)
        self.current: dict | None = None  # the task being timed
        self.audio = None  # its open audio file
        self.moved_t: float | None = None  # when the Finder task's file reached its folder


def reset_finder() -> None:
    """A made-up file on the Desktop and an empty folder for it."""
    if ARCHIVE.exists():
        shutil.rmtree(ARCHIVE)
    ARCHIVE.mkdir()
    NOTES.write_text("Made-up notes for AVO's E1 test. Safe to delete.\n")


def clean_finder() -> None:
    for p in (NOTES, ARCHIVE):
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


def judge(spec: dict, report: dict | None, answer: str) -> tuple[bool | None, str]:
    """Did it work? None when only the person can tell (they say so on the Done form)."""
    c = spec["check"]
    if "moved" in c:
        ok = (ARCHIVE / NOTES.name).exists() and not NOTES.exists()
        return ok, "moved into e1-archive" if ok else "not moved"
    if "answer" in c:
        ok = c["answer"] in answer
        return ok, f"answered {answer!r}"
    if "manual" in c:
        return None, c["manual"]
    return page_check({"id": spec["id"], "check": c}, report)


def latest_avo_run(since: float) -> dict:
    """The AVO run started during this task: its utterances, steps, cost, result."""
    runs = sorted((ROOT / "runs").glob("2*/summary.json"), key=lambda p: p.stat().st_mtime)
    for p in reversed(runs):
        if p.stat().st_mtime < since:
            break
        s = json.loads(p.read_text())
        return {
            "run": p.parent.name, "result": s.get("result"), "steps": s.get("steps"),
            "utterances": (s.get("counts") or {}).get("utterances"), "usd": (s.get("api") or {}).get("usd"),
        }
    return {}


def serve(state: State) -> ThreadingHTTPServer:
    specs = {t["id"]: t for t in tasks()}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT), **kw)

        def log_message(self, *a):
            pass

        def body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def reply(self, data, code=200):
            out = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            if self.path in ("/e1", "/e1/"):
                self.send_response(302)
                self.send_header("Location", "/eval/e1/index.html")
                return self.end_headers()
            if self.path == "/e1/state":
                return self.reply({"tasks": list(specs.values()), "results": load(), "tools": TOOLS})
            return super().do_GET()

        def do_POST(self):
            if self.path == "/report":
                with state.lock:
                    try:
                        state.report, state.report_t = json.loads(self.body()), time.time()
                    except json.JSONDecodeError:
                        pass
                self.send_response(204)
                return self.end_headers()
            if self.path == "/e1/audio":
                data = self.body()
                with state.lock:
                    if state.audio:
                        state.audio.write(data)
                self.send_response(204)
                return self.end_headers()
            msg = json.loads(self.body() or b"{}")
            if self.path == "/e1/start":
                spec, tool = specs[msg["task"]], msg["tool"]
                with state.lock:
                    if state.audio:
                        state.audio.close()
                    state.report = state.report_t = state.moved_t = None
                    if spec["start"] == "finder":
                        reset_finder()
                    AUDIO.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    wav = AUDIO / f"{stamp}-{spec['id']}-{tool}.pcm"
                    state.audio = wav.open("wb")
                    state.current = {"task": spec["id"], "tool": tool, "t0": time.time(), "audio": wav.name}
                url = spec["start"]
                if url == "finder":
                    subprocess.run(["osascript", "-e", 'tell application "Finder" to activate'])
                elif url.startswith("demo/"):
                    url = f"http://127.0.0.1:{PORT}/{url}"
                if url != "finder":
                    subprocess.run(["open", "-a", "Google Chrome", url])
                return self.reply({"ok": True, "url": url})
            if self.path == "/e1/end":
                with state.lock:
                    cur, report, report_t = state.current, state.report, state.report_t
                    if state.audio:
                        state.audio.close()
                        state.audio = None
                    state.current = None
                if not cur:
                    return self.reply({"ok": False, "error": "no task running"}, 400)
                spec = specs[cur["task"]]
                ok, what = judge(spec, report, msg.get("answer", ""))
                if ok is None:
                    ok = msg.get("worked")
                # Time to the page's last report (the send, the booking) when there is one;
                # else to the Done click, which includes getting back to the timer tab.
                t1 = msg.get("t1") or time.time()
                if spec["start"] == "finder":
                    report_t = state.moved_t
                end = report_t if ok and report_t and report_t < t1 else t1
                row = {
                    **cur, "seconds": round(end - cur["t0"], 1), "to_done_click_s": round(t1 - cur["t0"], 1),
                    "timed_by": "page" if end != t1 else "done click", "outcome": msg.get("outcome"),
                    "ok": bool(ok) and msg.get("outcome") == "done", "check": what, "answer": msg.get("answer", ""),
                    "mistakes": msg.get("mistakes"), "note": msg.get("note", ""),
                    "avo": latest_avo_run(cur["t0"]) if cur["tool"] == "avo" else None,
                    "by": msg.get("by", "person"),
                    "said": None,  # filled in by --count (by a script: AVO's own count of the task and answers)
                    "audio_s": round((AUDIO / cur["audio"]).stat().st_size / 48000, 1) if (AUDIO / cur["audio"]).exists() else 0,
                    "at": datetime.fromtimestamp(cur["t0"]).isoformat(timespec="seconds"),
                }
                if row["by"] == "script":
                    row["said"] = (row["avo"] or {}).get("utterances")
                rows = load() + [row]
                RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
                if spec["start"] == "finder":
                    clean_finder()
                return self.reply({"ok": True, "row": row})
            if self.path == "/e1/discard":
                rows = [r for r in load() if not (r["task"] == msg["task"] and r["tool"] == msg["tool"])]
                RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
                return self.reply({"ok": True})
            self.send_error(404)

    def watch_finder():
        while True:
            time.sleep(0.3)
            cur = state.current
            if cur and cur["task"] == "finder-move" and state.moved_t is None and (ARCHIVE / NOTES.name).exists():
                state.moved_t = time.time()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=watch_finder, daemon=True).start()
    return server


# ---- counting what was said, from the audio (Meta realtime ASR, ENDPOINTING) ----

ASR_URL = "wss://api.meta.ai/v1/asr/realtime"
CHUNK = 24000 * 2 // 10  # 100 ms of 24 kHz 16-bit mono


def transcribe(pcm: bytes) -> list[str]:
    """The utterances in a recording: one per stretch of speech the model marks as complete."""
    from websockets.sync.client import connect

    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise SystemExit("MODEL_API_KEY is not set")
    said: list[str] = []
    with connect(ASR_URL, max_size=None) as ws:
        ws.send(json.dumps({
            "authorization": {"accessToken": f"Bearer {key}"}, "audioEncoding": "PCM_24KHZ",
            "model": "muse-voice-transcribe-1.0", "mode": "ENDPOINTING", "emitAudioProgress": False,
            "languageBias": ["English"],
        }))
        done = threading.Event()

        def read():
            try:
                for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "speechComplete" and str(m.get("transcript", "")).strip():
                        said.append(m["transcript"].strip())
                    elif m.get("type") == "error":
                        print("  ASR error:", m.get("message"))
            except Exception:
                pass
            done.set()

        threading.Thread(target=read, daemon=True).start()
        for i in range(0, len(pcm), CHUNK):
            ws.send(pcm[i : i + CHUNK])
            time.sleep(0.02)  # faster than real time; the endpointing goes by the audio itself
        ws.send(json.dumps({"type": "endStream"}))
        done.wait(30)
    return said


def count() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    rows = load()
    for r in rows:
        if r.get("said") is not None:
            continue
        pcm_path = AUDIO / r["audio"]
        if not pcm_path.exists():
            print(f"{r['task']} · {r['tool']}: no audio")
            continue
        pcm = pcm_path.read_bytes()
        pcm = pcm[: int((r["seconds"] + 1.5) * 48000) // 2 * 2]  # only the timed part (+1.5 s for the last word)
        said = transcribe(pcm)
        r["said"] = len(said)
        r["audio_s"] = round(len(pcm) / 48000, 1)
        (pcm_path.with_suffix(".json")).write_text(json.dumps(said, ensure_ascii=False, indent=2))
        print(f"{r['task']:16} {r['tool']:13} {len(said):3} said in {r['audio_s']:.0f}s audio")
        RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", action="store_true", help="count utterances in the recorded audio")
    args = parser.parse_args()
    if args.count:
        return count()
    server = serve(State())
    print(f"E1 timer: http://127.0.0.1:{PORT}/e1   (Ctrl+C to stop)")
    subprocess.run(["open", "-a", "Google Chrome", f"http://127.0.0.1:{PORT}/e1"])
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        clean_finder()
        server.shutdown()


if __name__ == "__main__":
    main()
