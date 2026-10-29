"""Score every action step in a baseline batch with model review and repeat detection.

    uv run scripts/score_review.py runs/baseline-<timestamp> [--effort minimal] [--out review.json]

Rebuilds what the agent saw at each step (history=text runs only), asks a
reviewer call whether the planned batch is right, and flags batches that
repeat a recent one. Writes review.json into the batch folder.
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from avo.agent import describe, make_client
from avo.confidence import repeats, review

WORKERS = 8


def steps(batch: Path) -> list[dict]:
    summary = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
    items = []
    for entry in summary:
        run_dir = batch.parent / entry["run_dir"]
        if json.loads((run_dir / "result.json").read_text()).get("history") != "text":
            print(f"skip {entry['run_dir']}: not a history=text run")
            continue
        task = (run_dir / "task.txt").read_text(encoding="utf-8").strip()
        past, history = [], []
        for f in sorted(run_dir.glob("step*.json")):
            n = int(f.stem[4:])
            rec = json.loads(f.read_text(encoding="utf-8"))
            actions = rec.get("actions") or []
            if not actions:
                continue
            note = (rec.get("output_text") or "").strip()
            items.append(
                {
                    "task_id": entry["task_id"],
                    "run_dir": entry["run_dir"],
                    "step": n,
                    "task": task,
                    "past": list(past),
                    "note": note,
                    "actions": actions,
                    "repeat": repeats(history, actions),
                    "shot": str(run_dir / f"step{n - 1:02d}.png"),
                }
            )
            past.append(f"{n}. {note + ' | ' if note else ''}{describe(actions)}")
            history.append(actions)
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch", type=Path)
    parser.add_argument("--effort", default="minimal")
    parser.add_argument("--out", default="review.json")
    args = parser.parse_args()

    items = steps(args.batch)
    print(f"{len(items)} action steps to review (effort={args.effort})")
    client = make_client()

    def one(item: dict) -> dict:
        t0 = time.time()
        try:
            r = review(
                client, item["task"], item["past"], Path(item["shot"]).read_bytes(),
                item["actions"], item["note"], effort=args.effort,
            )
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}"}
        return {"seconds": round(time.time() - t0, 1), **r}

    with ThreadPoolExecutor(WORKERS) as pool:
        reviews = list(pool.map(one, items))

    results = []
    for item, r in zip(items, reviews):
        results.append(
            {k: item[k] for k in ("task_id", "run_dir", "step", "repeat")}
            | {"actions": describe(item["actions"]), "review": r}
        )
        if "error" in r:
            print(f"{item['task_id']:14} step{item['step']:02d} ERROR {r['error'][:100]}")
            continue
        print(
            f"{item['task_id']:14} step{item['step']:02d} conf={r['confidence']:.2f} "
            f"hit={'Y' if r['hits_intended_target'] else 'N'} fwd={'Y' if r['moves_task_forward'] else 'N'} "
            f"rep={'Y' if item['repeat'] else ' '} {r['seconds']:4.1f}s | {r['element_under_click'][:60]}"
        )
    out = args.batch / args.out
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
