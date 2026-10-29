"""Score sampling consistency for every clicking step in a baseline batch.

    uv run scripts/score_consistency.py runs/baseline-<timestamp> [--samples 4]

For each step whose batch contains a click, re-sends the exact input the model
saw (task + text log of earlier steps + that screenshot) N more times and
checks whether each sample's first click lands near the original first click.
Only works for runs recorded with history=text, whose inputs can be rebuilt.
Writes consistency.json into the batch folder.
"""

import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from avo.agent import MODEL, SYSTEM_PROMPT, computer_calls, describe, fresh_input, make_client

CLICK_KINDS = ("click", "double_click")
AGREE_PX = 15  # a sample agrees if its first click is within this many screenshot px
WORKERS = 8


def first_click(actions: list[dict]) -> dict | None:
    return next((a for a in actions or [] if a["type"] in CLICK_KINDS), None)


def steps_to_score(batch: Path) -> list[dict]:
    summary = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
    items = []
    for entry in summary:
        run_dir = batch.parent / entry["run_dir"]
        result = json.loads((run_dir / "result.json").read_text())
        if result.get("history") != "text":
            print(f"skip {entry['run_dir']}: not a history=text run")
            continue
        task = (run_dir / "task.txt").read_text(encoding="utf-8").strip()
        past = []
        for f in sorted(run_dir.glob("step*.json")):
            n = int(f.stem[4:])
            rec = json.loads(f.read_text(encoding="utf-8"))
            actions = rec.get("actions") or []
            click = first_click(actions)
            if click:
                items.append(
                    {
                        "task_id": entry["task_id"],
                        "run_dir": entry["run_dir"],
                        "step": n,
                        "original": {"x": click["x"], "y": click["y"]},
                        "task": task,
                        "past": list(past),
                        "shot": str(run_dir / f"step{n - 1:02d}.png"),
                        "effort": result.get("effort"),
                    }
                )
            note = (rec.get("output_text") or "").strip()
            if actions:
                past.append(f"{n}. {note + ' | ' if note else ''}{describe(actions)}")
    return items


def sample(client, item: dict) -> dict:
    png = Path(item["shot"]).read_bytes()
    kwargs = {"reasoning": {"effort": item["effort"]}} if item["effort"] else {}
    try:
        r = client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            tools=[{"type": "computer"}],
            input=fresh_input(item["task"], item["past"], png),
            **kwargs,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    calls = computer_calls(r)
    actions = (calls[0].get("actions") or []) if calls else []
    click = first_click(actions)
    if click:
        return {"x": click["x"], "y": click["y"], "actions": describe(actions)}
    return {"x": None, "y": None, "actions": describe(actions) if calls else "FINISH"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch", type=Path)
    parser.add_argument("--samples", type=int, default=4)
    args = parser.parse_args()

    items = steps_to_score(args.batch)
    jobs = [(i, k) for i in range(len(items)) for k in range(args.samples)]
    print(f"{len(items)} steps x {args.samples} samples = {len(jobs)} calls")
    client = make_client()
    with ThreadPoolExecutor(WORKERS) as pool:
        outs = list(pool.map(lambda job: sample(client, items[job[0]]), jobs))

    results = []
    for i, item in enumerate(items):
        samples = outs[i * args.samples : (i + 1) * args.samples]
        ox, oy = item["original"]["x"], item["original"]["y"]
        dists = [
            math.dist((ox, oy), (s["x"], s["y"])) if s.get("x") is not None else None for s in samples
        ]
        agree = sum(1 for d in dists if d is not None and d <= AGREE_PX)
        results.append(
            {
                "task_id": item["task_id"],
                "run_dir": item["run_dir"],
                "step": item["step"],
                "original": item["original"],
                "samples": samples,
                "distances": [round(d, 1) if d is not None else None for d in dists],
                "agreement": round(agree / len(samples), 2),
            }
        )
        print(
            f"{item['task_id']:14} step{item['step']:02d} ({ox:4},{oy:4}) agree={agree}/{len(samples)} "
            f"dists={[round(d) if d is not None else '-' for d in dists]}"
        )

    out = args.batch / "consistency.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
