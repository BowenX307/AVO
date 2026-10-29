"""Check click refinement on the clicks users corrected in live runs.

    uv run scripts/eval_refine.py

For every gated step where the user answered "No" and clicked the right spot,
re-runs the review (which now also places click 1's intended element in the
zoomed crop) and asks: does the refined point land on the element the user
clicked? "Same element" = inside SAM's mask for the user's point. Also reports
whether the gate would have snapped automatically (SAM sure about the refined
point) instead of asking.
"""

import json
import math
from pathlib import Path

from avo.agent import RUNS_DIR
from avo.confidence import review
from avo.gate import SNAP_SURE
from avo.llm import describe, make_client
from avo.sam import segment


def best(segments):
    return max(segments, key=lambda s: s.confidence or 0) if segments else None


def cases():
    for run in sorted(RUNS_DIR.glob("2*")):
        task_file = run / "task.txt"
        if not task_file.exists():
            continue
        past = []
        for f in sorted(run.glob("step*.json")):
            rec = json.loads(f.read_text())
            n = int(f.stem[4:])
            actions = rec.get("actions") or []
            note = (rec.get("output_text") or "").strip()
            user = (rec.get("gate") or {}).get("user") or {}
            fixed = [a for a in user.get("actions", []) if a["type"] in ("click", "double_click")]
            if user.get("choice") == "no" and fixed:
                yield {
                    "run": run.name, "step": n, "task": task_file.read_text().strip(),
                    "past": list(past), "png": (run / f"step{n - 1:02d}.png").read_bytes(),
                    "actions": actions, "note": note, "user": (fixed[0]["x"], fixed[0]["y"]),
                }
            past.append(f"{n}. {note + ' | ' if note else ''}{describe(actions)}")


def main() -> None:
    client = make_client()
    rows = []
    for c in cases():
        rev = review(client, c["task"], c["past"], c["png"], c["actions"], c["note"])
        first = next(a for a in c["actions"] if a["type"] in ("click", "double_click"))
        model = (first["x"], first["y"])
        user_seg = best(segment(client, c["png"], points=[c["user"]])[0])
        target = tuple(rev["target"]) if rev.get("target") else None
        tgt_seg = best(segment(client, c["png"], points=[target])[0]) if target else None
        row = {
            "run": c["run"], "step": c["step"], "model": model, "user": c["user"], "refined": target,
            "model_err": round(math.dist(model, c["user"])),
            "refined_err": round(math.dist(target, c["user"])) if target else None,
            "refined_on_user_element": bool(target and user_seg and user_seg.contains(*target)),
            "refined_sam": round(tgt_seg.confidence or 0, 2) if tgt_seg else None,
            "would_snap": bool(tgt_seg and (tgt_seg.confidence or 0) >= SNAP_SURE and tgt_seg.contains(*target)),
            "hit": rev.get("hits_intended_target"), "under": rev.get("element_under_click"),
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    on = sum(r["refined_on_user_element"] for r in rows)
    snap = [r for r in rows if r["would_snap"]]
    print(f"\nrefined point on the user's element: {on}/{len(rows)}")
    print(f"would snap without asking: {len(snap)}/{len(rows)}, "
          f"of which on the user's element: {sum(r['refined_on_user_element'] for r in snap)}")
    out = RUNS_DIR / "refine_eval.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
