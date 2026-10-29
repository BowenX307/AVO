"""Compare confidence signals against step labels for a baseline batch.

    uv run scripts/analyze_signals.py runs/baseline-<timestamp>

Reads sam_scores.json and consistency.json, plus labels.json (human labels
exported from review.html) or, if that's missing, draft_labels.json. Only steps
with at least one click and an ok/bad label are counted. For each flagging rule,
prints how many wrong steps it catches and how many right steps it flags.
"""

import argparse
import json
from pathlib import Path

SAM_CONF_MIN = 0.5
AGREE_MIN = 0.75  # flag when fewer than 3 of 4 samples agree


def load(batch: Path) -> tuple[list[dict], str]:
    label_file = batch / "labels.json"
    if not label_file.exists():
        label_file = batch / "draft_labels.json"
    labels = json.loads(label_file.read_text())
    sam = json.loads((batch / "sam_scores.json").read_text())
    cons = {f"{r['run_dir']}:{r['step']}": r for r in json.loads((batch / "consistency.json").read_text())}

    steps: dict[str, dict] = {}
    for c in sam:
        key = f"{c['run_dir']}:{c['step']}"
        s = steps.setdefault(key, {"key": key, "task_id": c["task_id"], "confs": [], "off_mask": False})
        s["confs"].append(c.get("sam_conf", 0.0))
        s["off_mask"] |= not c.get("on_mask", True)
    rows = []
    for key, s in steps.items():
        label = labels.get(key)
        if label not in ("ok", "bad"):
            continue
        agreement = cons[key]["agreement"] if key in cons else None
        rows.append({**s, "label": label, "sam_min": min(s["confs"]), "agreement": agreement})
    return rows, label_file.name


RULES = {
    "SAM: conf < 0.5 or off mask": lambda r: r["sam_min"] < SAM_CONF_MIN or r["off_mask"],
    "consistency: agreement < 0.75": lambda r: r["agreement"] is not None and r["agreement"] < AGREE_MIN,
    "either signal": lambda r: (r["sam_min"] < SAM_CONF_MIN or r["off_mask"])
    or (r["agreement"] is not None and r["agreement"] < AGREE_MIN),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()

    rows, source = load(args.batch)
    bad = [r for r in rows if r["label"] == "bad"]
    good = [r for r in rows if r["label"] == "ok"]
    print(f"labels from {source}: {len(rows)} clicking steps ({len(bad)} wrong, {len(good)} right)\n")

    print(f"{'step':34} {'label':5} {'SAM min':>7} {'off':>4} {'agree':>6}")
    for r in sorted(rows, key=lambda r: r["key"]):
        agree = "-" if r["agreement"] is None else f"{r['agreement']:.2f}"
        print(f"{r['task_id'] + ' ' + r['key'].split(':')[1]:34} {r['label']:5} {r['sam_min']:7.2f} "
              f"{'yes' if r['off_mask'] else '':>4} {agree:>6}")

    print(f"\n{'rule':34} {'catches wrong':>14} {'flags right':>12}")
    for name, rule in RULES.items():
        caught = sum(rule(r) for r in bad)
        false = sum(rule(r) for r in good)
        print(f"{name:34} {f'{caught}/{len(bad)}':>14} {f'{false}/{len(good)}':>12}")


if __name__ == "__main__":
    main()
