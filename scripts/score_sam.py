"""Score every click in a baseline batch with SAM 3.1 (target-clarity signal).

    uv run scripts/score_sam.py runs/baseline-<timestamp>

For each click, runs a point prompt on the screenshot the model acted on and
records SAM's confidence, the mask area, and whether the point lies on the mask.
Writes sam_scores.json into the batch folder.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from avo.agent import make_client
from avo.sam import segment

CLICK_KINDS = ("click", "double_click")
WORKERS = 6


def clicks(batch: Path) -> list[dict]:
    summary = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
    items = []
    for entry in summary:
        run_dir = batch.parent / entry["run_dir"]
        for f in sorted(run_dir.glob("step*.json")):
            n = int(f.stem[4:])
            actions = json.loads(f.read_text(encoding="utf-8")).get("actions") or []
            for i, a in enumerate(actions):
                if a["type"] in CLICK_KINDS:
                    items.append(
                        {
                            "task_id": entry["task_id"],
                            "run_dir": entry["run_dir"],
                            "step": n,
                            "action_index": i,
                            "x": a["x"],
                            "y": a["y"],
                            "shot": str(run_dir / f"step{n - 1:02d}.png"),
                        }
                    )
    return items


def score(client, item: dict) -> dict:
    png = Path(item["shot"]).read_bytes()
    try:
        segments, _ = segment(client, png, points=[(item["x"], item["y"])])
    except Exception as e:  # keep going; record the failure
        return {**item, "error": f"{type(e).__name__}: {e}"}
    if not segments:
        return {**item, "sam_conf": 0.0, "area": 0, "on_mask": False, "box": None}
    s = max(segments, key=lambda s: s.confidence or 0)
    return {
        **item,
        "sam_conf": round(s.confidence or 0, 3),
        "area": s.area,
        "on_mask": s.contains(item["x"], item["y"]),
        "box": [round(v) for v in s.box],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()

    items = clicks(args.batch)
    print(f"{len(items)} clicks to score")
    client = make_client()
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(lambda it: score(client, it), items))

    out = args.batch / "sam_scores.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        if "error" in r:
            print(f"{r['task_id']:14} step{r['step']:02d} ({r['x']},{r['y']}) ERROR {r['error'][:80]}")
        else:
            print(
                f"{r['task_id']:14} step{r['step']:02d} ({r['x']:4},{r['y']:4}) "
                f"conf={r['sam_conf']:.2f} area={r['area']:6} on_mask={r['on_mask']} box={r['box']}"
            )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
