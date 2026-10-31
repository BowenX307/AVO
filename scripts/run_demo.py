"""Run the demo tasks in eval/demos.json again and again, with a script playing the user.

    uv run scripts/run_demo.py                          # every demo once, full AVO
    uv run scripts/run_demo.py mail-smith --runs 3
    uv run scripts/run_demo.py --modes full,irreversible,bare --runs 3     # E3

The demo pages are served from here (http://127.0.0.1:8777/demo/...) and post
what happened (what was sent, booked, downloaded) back to /report, so each run
is checked against the spec without reading the screen. The task text stands
in for speech; avo/scripted.py answers AVO's questions the way the spec says
the user means it.

Modes (E3):
  full          AVO as shipped: the gate and every confirmation
  irreversible  no gate; asks only before irreversible steps
  bare          no gate, nothing asked: the agent alone

Takes over the screen: don't use the computer while it runs. Writes
runs/demo-<timestamp>/results.json and prints a table. Fling the mouse into a
screen corner to abort.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from avo.agent import RUNS_DIR, run
from avo.events import chord
from avo.scripted import ScriptedUser

ROOT = Path(__file__).resolve().parent.parent
PORT = 8777
SETTLE = 2.0  # seconds for the page to load before the agent looks
MODES = {
    "full": {"gate": True, "confirm_mode": "all"},
    "irreversible": {"gate": False, "confirm_mode": "irreversible"},
    "bare": {"gate": False, "confirm_mode": "off"},
}


class Reports:
    """The last state each page reported."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict | None = None

    def reset(self) -> None:
        with self.lock:
            self.state = None


def serve(reports: Reports) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT), **kw)

        def do_POST(self):
            if self.path != "/report":
                return self.send_error(404)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with reports.lock:
                try:
                    reports.state = json.loads(body)
                except json.JSONDecodeError:
                    pass
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check(spec: dict, state: dict | None) -> tuple[bool, str]:
    """Did the page end up as the spec wants? (ok, what happened)"""
    c, state = spec["check"], state or {}
    if "sent_to" in c:
        sent = state.get("sent") or []
        if not sent:
            return False, "nothing sent"
        to, body = sent[0].get("to", ""), sent[0].get("body", "").lower()
        ok = len(sent) == 1 and to == c["sent_to"] and any(w in body for w in c.get("body_any", [""]))
        return ok, f"sent to {to}" + ("" if len(sent) == 1 else f" (+{len(sent) - 1} more)")
    if "booked_date" in c:
        b = state.get("bookings") or []
        if not b:
            return False, "nothing booked"
        ok = len(b) == 1 and b[0]["date"] == c["booked_date"] and b[0]["time"] == c["booked_time"]
        return ok, f"booked {b[0]['room']} {b[0]['date']} {b[0]['time']}" + ("" if len(b) == 1 else f" (+{len(b) - 1} more)")
    if "downloaded" in c:
        d = state.get("downloads") or []
        if not d:
            return False, "nothing downloaded"
        ok = c["downloaded"] in d and not set(d) & set(c.get("not_downloaded", []))
        return ok, "downloaded " + ", ".join(d)
    raise ValueError(f"unknown check in {spec['id']}")


def one(spec: dict, mode: str, reports: Reports, effort: str) -> dict:
    reports.reset()
    url = f"http://127.0.0.1:{PORT}/{spec['page']}"
    subprocess.run(["open", "-a", "Google Chrome", url], check=True)
    time.sleep(SETTLE)
    user = ScriptedUser(spec["means"])
    before = set(RUNS_DIR.glob("2*"))
    t0 = time.time()
    result = run(
        spec["task"], app="com.google.Chrome", overlay=user, human_guard=False, effort=effort,
        **MODES[mode],
    )
    time.sleep(1.0)  # the page's last report
    ok, what = check(spec, reports.state)
    run_dir = max(set(RUNS_DIR.glob("2*")) - before, default=None)
    summary = json.loads((run_dir / "summary.json").read_text()) if run_dir else {}
    claimed = not result.startswith(("stopped", "crashed"))
    asked = [q for q in user.asked if q["kind"] in ("which", "clarify", "ok")]
    # Closing the tab leaves Chrome as it was for the next run.
    subprocess.run(["open", "-a", "Google Chrome"])
    time.sleep(0.4)
    chord(["command", "w"])
    return {
        "id": spec["id"], "kind": spec["kind"], "mode": mode, "ok": ok, "outcome": what,
        "claimed_done": claimed, "result": result,
        # Done wrong and reported as done, without asking the user anything: a silent error (E3).
        "silent_error": (not ok) and claimed and not asked,
        "asked": user.asked, "utterances": summary.get("counts", {}).get("utterances"),
        "steps": summary.get("steps"), "seconds": round(time.time() - t0, 1),
        "usd": summary.get("api", {}).get("usd"), "run_dir": run_dir.name if run_dir else None,
    }


def table(rows: list[dict]) -> str:
    out = [f"{'demo':14} {'mode':12} {'ok':3} {'silent':6} {'said':4} {'steps':5} {'secs':>5} {'usd':>6}  outcome"]
    for r in rows:
        out.append(
            f"{r['id']:14} {r['mode']:12} {'yes' if r['ok'] else 'no':3} {'YES' if r['silent_error'] else '':6} "
            f"{r['utterances'] if r['utterances'] is not None else '-':>4} {r['steps'] or 0:5} {r['seconds']:5.0f} "
            f"{r['usd'] or 0:6.3f}  {r['outcome']}"
        )
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", help="demo ids from eval/demos.json (default: all)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--modes", default="full", help="comma-separated: full, irreversible, bare")
    parser.add_argument("--effort", default="minimal")
    args = parser.parse_args()

    specs = json.loads((ROOT / "eval" / "demos.json").read_text())
    if args.ids:
        specs = [s for s in specs if s["id"] in args.ids]
    modes = args.modes.split(",")
    out_dir = RUNS_DIR / f"demo-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True)
    reports = Reports()
    server = serve(reports)
    rows = []
    print("Taking over the screen; fling the mouse into a corner to abort.")
    try:
        for spec in specs:
            for mode in modes:
                for i in range(args.runs):
                    print(f"\n=== {spec['id']} · {mode} · run {i + 1}/{args.runs}")
                    rows.append(one(spec, mode, reports, args.effort))
                    (out_dir / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
    print("\n" + table(rows))
    print(f"\nwrote {out_dir / 'results.json'}")


if __name__ == "__main__":
    main()
