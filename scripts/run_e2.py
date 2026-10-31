"""E2: a shaky hand clicking small buttons, alone vs with AVO. Offline: no screen taken over.

    node scripts/e2_capture.mjs           # once: eval/e2/targets.png + targets.json
    uv run scripts/run_e2.py              # SAM calls (~600, about $1.50), then the table
    uv run scripts/run_e2.py --plot-only  # redraw docs/images/e2-tremor(-light).svg from results.json

Tremor is simulated: every point the hand aims at lands off by a Gaussian
offset, standard deviation SIGMA px on each axis (0 = a steady hand).

  - alone: the hand clicks at the button's centre (plus the offset).
  - AVO, SAM only: the hand drags a rough box around the button (MARGIN px
    beyond it, each corner off by the offset); SAM 3.1 segments the box and
    AVO clicks the middle of the mask it returns.
  - AVO, SAM + name: the user also says the button's name. AVO looks for a
    button of that name near the box (here the page's own boxes, which are
    what macOS accessibility reports for Chrome) and clicks its middle; no
    such button near the box: the SAM-only click.

Every row of demo/targets.html has the same eight names, so the name alone
never says which row: the box does. A hit is the click inside the target's
box; a click on another button is a wrong hit.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from avo.llm import make_client
from avo.meter import Meter
from avo.sam import segment

ROOT = Path(__file__).resolve().parent.parent
E2 = ROOT / "eval" / "e2"
CHART = ROOT / "docs" / "images" / "e2-tremor.svg"
SIZES = [12, 18, 24, 32, 44]
SIGMAS = [0, 4, 8, 12]
TRIALS = 30
MARGIN = 8  # px the intended box reaches beyond the button
NAME_TOL = 16  # px around the box in which a button of the spoken name counts
SEED = 2026
WORKERS = 8


def inside(p, r) -> bool:
    return r[0] <= p[0] < r[2] and r[1] <= p[1] < r[3]


def center(r):
    return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


def outcome(p, target, buttons) -> str:
    if p is None:
        return "miss"
    if inside(p, target["rect"]):
        return "hit"
    return "wrong" if any(inside(p, b["rect"]) for b in buttons) else "miss"


def trials(buttons) -> list[dict]:
    rng = random.Random(SEED)
    out = []
    for size in SIZES:
        row = [b for b in buttons if b["size"] == size]
        for sigma in SIGMAS:
            for i in range(TRIALS):
                t = rng.choice(row)
                cx, cy = center(t["rect"])
                click = (cx + rng.gauss(0, sigma), cy + rng.gauss(0, sigma))
                l, tp, r, b = t["rect"]
                c1 = (l - MARGIN + rng.gauss(0, sigma), tp - MARGIN + rng.gauss(0, sigma))
                c2 = (r + MARGIN + rng.gauss(0, sigma), b + MARGIN + rng.gauss(0, sigma))
                box = [min(c1[0], c2[0]), min(c1[1], c2[1]), max(c1[0], c2[0]), max(c1[1], c2[1])]
                if box[2] - box[0] < 4:
                    box[2] = box[0] + 4
                if box[3] - box[1] < 4:
                    box[3] = box[1] + 4
                out.append({"size": size, "sigma": sigma, "i": i, "target": t["label"], "target_rect": t["rect"],
                            "click": click, "box": [round(v) for v in box]})
    return out


def sam_point(client, png: bytes, box) -> tuple[tuple[float, float] | None, float, float | None]:
    """Where AVO clicks from SAM alone: the middle of the most confident mask. (point, seconds, confidence)"""
    t0 = time.time()
    try:
        segs, _ = segment(client, png, box=tuple(box))
    except Exception as e:
        print(f"SAM failed: {type(e).__name__}: {e}")
        return None, time.time() - t0, None
    seconds = time.time() - t0
    if not segs:
        return None, seconds, None
    s = max(segs, key=lambda s: s.confidence or 0)
    xs = ys = n = 0
    w = s.mask.width
    for k, v in enumerate(s.mask.getdata()):
        if v:
            xs += k % w
            ys += k // w
            n += 1
    if not n:
        return None, seconds, s.confidence
    return (s.box[0] + xs / n, s.box[1] + ys / n), seconds, s.confidence


def by_name(name: str, box, buttons) -> dict | None:
    l, t, r, b = box[0] - NAME_TOL, box[1] - NAME_TOL, box[2] + NAME_TOL, box[3] + NAME_TOL
    near = [x for x in buttons if x["label"] == name
            and x["rect"][0] < r and x["rect"][2] > l and x["rect"][1] < b and x["rect"][3] > t]
    bc = center(box)
    return min(near, key=lambda x: (center(x["rect"])[0] - bc[0]) ** 2 + (center(x["rect"])[1] - bc[1]) ** 2, default=None)


def run_trials() -> dict:
    meta = json.loads((E2 / "targets.json").read_text())
    buttons = meta["buttons"]
    png = (E2 / "targets.png").read_bytes()
    assert Image.open(io.BytesIO(png)).size == (meta["width"], meta["height"])
    ts = trials(buttons)
    meter = Meter()
    client = meter.wrap(make_client())
    print(f"{len(ts)} trials; SAM on each rough box ({WORKERS} at a time)")
    with ThreadPoolExecutor(WORKERS) as pool:
        sams = list(pool.map(lambda t: sam_point(client, png, t["box"]), ts))
    for t, (p, secs, conf) in zip(ts, sams):
        target = next(b for b in buttons if b["label"] == t["target"] and b["size"] == t["size"])
        t["alone"] = outcome(t["click"], target, buttons)
        t["sam_click"] = [round(v, 1) for v in p] if p else None
        t["sam_conf"] = conf
        t["sam_s"] = round(secs, 2)
        t["avo_sam"] = outcome(p, target, buttons)
        found = by_name(t["target"], t["box"], buttons)
        t["name_found"] = bool(found)
        t["avo_name"] = outcome(center(found["rect"]) if found else p, target, buttons)
    api = meter.summary()
    return {
        "settings": {"sizes": SIZES, "sigmas": SIGMAS, "trials": TRIALS, "margin": MARGIN, "name_tol": NAME_TOL,
                     "seed": SEED, "screenshot": "eval/e2/targets.png (1280x800, 1x)", "sam": "sam-3.1, box prompt"},
        "api": {"calls": api["calls"], "usd": api["usd"]},
        "sam_seconds_median": round(statistics.median(t["sam_s"] for t in ts), 2),
        "trials": ts,
    }


def summarize(res: dict) -> list[dict]:
    rows = []
    for size in res["settings"]["sizes"]:
        for sigma in res["settings"]["sigmas"]:
            ts = [t for t in res["trials"] if t["size"] == size and t["sigma"] == sigma]
            row = {"size": size, "sigma": sigma, "n": len(ts)}
            for g in ("alone", "avo_sam", "avo_name"):
                row[g] = sum(t[g] == "hit" for t in ts) / len(ts)
                row[g + "_wrong"] = sum(t[g] == "wrong" for t in ts) / len(ts)
            rows.append(row)
    return rows


def table(rows) -> str:
    out = [f"{'size':>4} {'σ':>3} | {'alone':>6} {'(wrong)':>7} | {'AVO SAM':>7} {'(wrong)':>7} | {'AVO +name':>9} {'(wrong)':>7}"]
    for r in rows:
        out.append(f"{r['size']:>4} {r['sigma']:>3} | {r['alone']:>6.0%} {r['alone_wrong']:>7.0%} | "
                   f"{r['avo_sam']:>7.0%} {r['avo_sam_wrong']:>7.0%} | {r['avo_name']:>9.0%} {r['avo_name_wrong']:>7.0%}")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plot-only", action="store_true", help="redraw the chart from eval/e2/results.json")
    args = parser.parse_args()
    out = E2 / "results.json"
    if args.plot_only:
        res = json.loads(out.read_text())
    else:
        res = run_trials()
        out.write_text(json.dumps(res, indent=1))
        print(f"wrote {out}  ({res['api']['calls']} API calls, ${res['api']['usd']:.2f}; "
              f"SAM median {res['sam_seconds_median']} s)")
    rows = summarize(res)
    print(table(rows))
    try:
        from e2_chart import draw  # scripts/e2_chart.py

        CHART.parent.mkdir(parents=True, exist_ok=True)
        CHART.write_text(draw(rows, res))
        CHART.with_name(CHART.stem + "-light.svg").write_text(draw(rows, res, dark=False))  # the website is light only
        print(f"wrote {CHART} and its light-only copy")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
