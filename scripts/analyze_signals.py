"""Compare confidence signals against step labels for a baseline batch.

    uv run scripts/analyze_signals.py runs/baseline-<timestamp>

Reads sam_scores.json, consistency.json and review.json (--review review_v3.json:
the review with the ambiguity question, 10/31), plus labels.json (human labels
exported from review.html) or, if that's missing, draft_labels.json. Only steps
with at least one click and an ok/bad label are counted. For each flagging rule,
prints how many wrong steps it catches and how many right steps it flags.
"""

import argparse
import json
from pathlib import Path

SAM_CONF_MIN = 0.5
AGREE_MIN = 0.75  # flag when fewer than 3 of 4 samples agree
REVIEW_CONF_MIN = 0.5
GATE_SAM_SURE = 0.9  # the 10/29 gate: SAM this sure overruled a "missed" review
GATE_SAM_CLEAR = 0.8  # avo/gate.py SAM_CLEAR


def load(batch: Path, review_file: str = "review.json") -> tuple[list[dict], str]:
    label_file = batch / "labels.json"
    if not label_file.exists():
        label_file = batch / "draft_labels.json"
    labels = json.loads(label_file.read_text())
    sam = json.loads((batch / "sam_scores.json").read_text())
    cons = {f"{r['run_dir']}:{r['step']}": r for r in json.loads((batch / "consistency.json").read_text())}
    revs = {f"{r['run_dir']}:{r['step']}": r for r in json.loads((batch / review_file).read_text())}

    steps: dict[str, dict] = {}
    for c in sam:
        key = f"{c['run_dir']}:{c['step']}"
        s = steps.setdefault(key, {"key": key, "task_id": c["task_id"], "confs": [], "off_mask": False})
        if not s["confs"]:  # first click of the step, the one the gate checks
            s["first_conf"], s["first_on_mask"] = c.get("sam_conf", 0.0), c.get("on_mask", False)
        s["confs"].append(c.get("sam_conf", 0.0))
        s["off_mask"] |= not c.get("on_mask", True)
    rows = []
    for key, s in steps.items():
        label = labels.get(key)
        if label not in ("ok", "bad"):
            continue
        agreement = cons[key]["agreement"] if key in cons else None
        rev = revs.get(key, {}).get("review", {})
        review_flag = bool(rev) and (
            not rev.get("hits_intended_target", True)
            or not rev.get("moves_task_forward", True)
            or rev.get("confidence", 1) < REVIEW_CONF_MIN
        )
        rows.append(
            {
                **s,
                "label": label,
                "sam_min": min(s["confs"]),
                "agreement": agreement,
                "review": review_flag,
                "rev": rev,
                "repeat": revs.get(key, {}).get("repeat", False),
            }
        )
    return rows, label_file.name


def sam_flag(r: dict) -> bool:
    return r["sam_min"] < SAM_CONF_MIN or r["off_mask"]


def cons_flag(r: dict) -> bool:
    return r["agreement"] is not None and r["agreement"] < AGREE_MIN


def old_gate_flag(r: dict) -> bool:
    """The gate from 10/29 to 10/31: review (miss / no progress) with a SAM veto, plus repeats."""
    rev = r["rev"] or {}
    sam_vouches = not r["off_mask"] and r["sam_min"] >= GATE_SAM_SURE  # every click on a sure mask
    hit = rev.get("hits_intended_target", True)
    forward = rev.get("moves_task_forward", True)
    if sam_vouches and not hit:
        hit = forward = True
    return (not hit) or (not forward) or r["repeat"]


def gate_flag(r: dict) -> bool:
    """The live gate's rule (avo/gate.py), without its accessibility check (not in saved runs); keep in sync.

    Ambiguous: ask. Else every click on a mask SAM is GATE_SAM_CLEAR sure of: go.
    Else the review's "hits its target" decides.
    """
    rev = r["rev"] or {}
    if rev.get("ambiguous") and len(rev.get("candidates") or []) >= 2:
        return True
    if not r["off_mask"] and r["sam_min"] >= GATE_SAM_CLEAR:
        return False
    return not rev or not rev.get("hits_intended_target", True)


RULES = {
    "SAM: conf < 0.5 or off mask": sam_flag,
    "consistency: agreement < 0.75": cons_flag,
    "review: miss / no progress / < 0.5": lambda r: r["review"],
    "repeat of a recent batch": lambda r: r["repeat"],
    "SAM or review": lambda r: sam_flag(r) or r["review"],
    "SAM or review or repeat": lambda r: sam_flag(r) or r["review"] or r["repeat"],
    "review or repeat": lambda r: r["review"] or r["repeat"],
    "10/29 gate (review + SAM veto + repeat)": old_gate_flag,
    "gate: ambiguity, SAM >= 0.8, else review": gate_flag,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch", type=Path)
    parser.add_argument("--review", default="review.json", help="which review scores file to use")
    args = parser.parse_args()

    rows, source = load(args.batch, args.review)
    bad = [r for r in rows if r["label"] == "bad"]
    good = [r for r in rows if r["label"] == "ok"]
    print(f"labels from {source}: {len(rows)} clicking steps ({len(bad)} wrong, {len(good)} right)\n")

    print(f"{'step':34} {'label':5} {'SAM min':>7} {'off':>4} {'agree':>6} {'review':>7} {'repeat':>7}")
    for r in sorted(rows, key=lambda r: r["key"]):
        agree = "-" if r["agreement"] is None else f"{r['agreement']:.2f}"
        print(f"{r['task_id'] + ' ' + r['key'].split(':')[1]:34} {r['label']:5} {r['sam_min']:7.2f} "
              f"{'yes' if r['off_mask'] else '':>4} {agree:>6} {'flag' if r['review'] else '':>7} "
              f"{'yes' if r['repeat'] else '':>7}")

    print(f"\n{'rule':36} {'catches wrong':>14} {'flags right':>12}")
    for name, rule in RULES.items():
        caught = sum(rule(r) for r in bad)
        false = sum(rule(r) for r in good)
        print(f"{name:36} {f'{caught}/{len(bad)}':>14} {f'{false}/{len(good)}':>12}")


if __name__ == "__main__":
    main()
