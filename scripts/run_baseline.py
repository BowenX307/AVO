"""Run the baseline tasks in eval/tasks.json, resetting the screen before each one.

    uv run scripts/run_baseline.py                    # every task, both modes
    uv run scripts/run_baseline.py --modes fast --only wiki-year,expense

Modes:
    fast  effort=minimal, history=text (latest screenshot + text log)
    full  model's default effort, history=full (every past screenshot)

Writes runs/baseline-<timestamp>/summary.json (one row per run, with its cost
and where the time went, from the run's own summary.json) and prints a table;
label the runs with review_baseline.py.
Fling the mouse into a screen corner to abort the whole batch.
"""

import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from avo.agent import RUNS_DIR, run
from avo.events import FailSafeException, chord

ROOT = Path(__file__).resolve().parent.parent
MODES = {
    "fast": {"effort": "minimal", "history": "text"},
    "full": {"effort": None, "history": "full"},
}
SETUP_SETTLE = 2.5  # seconds for the page or app to load before the agent looks


def setup(spec: dict) -> None:
    if "open_url" in spec:
        url = spec["open_url"]
        if not url.startswith("http"):
            url = (ROOT / url).resolve().as_uri()
        subprocess.run(["open", "-a", "Google Chrome", url], check=True)
    if "open_app" in spec:
        subprocess.run(["open", "-a", spec["open_app"]], check=True)
        time.sleep(1)
        chord(["esc"])  # Calculator: clear any previous entry
        chord(["esc"])
    time.sleep(SETUP_SETTLE)


def latest_run_dir(before: set[Path]) -> Path | None:
    new = sorted(set(RUNS_DIR.glob("2*")) - before)
    return new[-1] if new else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modes", default="fast,full")
    parser.add_argument("--only", help="comma-separated task ids")
    parser.add_argument("--max-steps", type=int, default=15)
    args = parser.parse_args()

    tasks = json.loads((ROOT / "eval/tasks.json").read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    modes = args.modes.split(",")

    out = RUNS_DIR / datetime.now().strftime("baseline-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    summary = []
    print(f"{len(tasks)} tasks x {len(modes)} modes -> {out}")

    for mode in modes:
        for t in tasks:
            print(f"\n=== {mode} / {t['id']}: {t['task']}")
            setup(t["setup"])
            before = set(RUNS_DIR.glob("2*"))
            try:
                # gate/snap off: the baseline measures the unassisted agent.
                result = run(t["task"], max_steps=args.max_steps, gate=False, snap_to_controls=False, **MODES[mode])
            except (KeyboardInterrupt, FailSafeException):
                print("aborted by user")
                (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
                raise
            except Exception as e:  # API error etc.: record and keep going
                result = f"crashed: {type(e).__name__}: {e}"
                print(result)
            run_dir = latest_run_dir(before)
            info = json.loads((run_dir / "result.json").read_text()) if run_dir else {}
            report = json.loads((run_dir / "summary.json").read_text()) if run_dir and (run_dir / "summary.json").exists() else {}
            summary.append(
                {
                    "task_id": t["id"],
                    "mode": mode,
                    "run_dir": run_dir.name if run_dir else None,
                    "result": result,
                    "steps": info.get("steps"),
                    "elapsed_s": info.get("elapsed_s"),
                    "usd": (report.get("api") or {}).get("usd"),
                    "api_calls": (report.get("api") or {}).get("calls"),
                    "time_s": report.get("time_s"),
                    "counts": report.get("counts"),
                }
            )
            (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print(f"\ndone: {out / 'summary.json'}\n")
    print(table(summary))


def table(rows: list[dict]) -> str:
    """The runs as a plain-text table: steps, seconds, cost, asks, stuck, result."""
    lines = [f"{'task':16} {'mode':5} {'steps':>5} {'sec':>6} {'usd':>7} {'calls':>5} {'asked':>5} {'stuck':>5}  result"]
    for r in rows:
        c = r.get("counts") or {}
        usd = f"{r['usd']:.3f}" if r.get("usd") is not None else "-"
        lines.append(
            f"{r['task_id'][:16]:16} {r['mode']:5} {r.get('steps') or 0:>5} {r.get('elapsed_s') or 0:>6.0f} {usd:>7} "
            f"{r.get('api_calls') or 0:>5} {c.get('asked', 0):>5} {c.get('stuck', 0):>5}  {str(r['result'])[:60]}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
