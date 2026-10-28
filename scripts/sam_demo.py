"""Run SAM 3.1 on a screenshot and save a visualization under runs/sam-<timestamp>/.

    uv run scripts/sam_demo.py --text "folder icon"
    uv run scripts/sam_demo.py --point 644,245
    uv run scripts/sam_demo.py --box 40,420,650,446
    uv run scripts/sam_demo.py --image runs/.../step01.png --point 544,643

Coordinates are pixels in the screenshot (1280 wide, as the agent sees it).
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

from avo.agent import RUNS_DIR, make_client
from avo.driver import MacDriver
from avo.sam import render, segment


def ints(text: str) -> tuple[int, ...]:
    return tuple(int(v) for v in text.split(","))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--text", help="noun phrase, e.g. 'search button'")
    prompt.add_argument("--point", type=ints, help="x,y")
    prompt.add_argument("--box", type=ints, help="left,top,right,bottom")
    parser.add_argument("--image", type=Path, help="PNG to use instead of a fresh screenshot")
    args = parser.parse_args()

    png = args.image.read_bytes() if args.image else MacDriver().screenshot()
    out = RUNS_DIR / datetime.now().strftime("sam-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "input.png").write_bytes(png)

    t0 = time.time()
    segments, raw = segment(
        make_client(),
        png,
        text=args.text,
        box=args.box,
        points=[args.point] if args.point else None,
    )
    print(f"{len(segments)} segment(s) in {time.time() - t0:.1f}s")
    for s in segments:
        conf = f"{s.confidence:.2f}" if s.confidence is not None else "-"
        print(f"  id={s.object_id} box={[round(v) for v in s.box]} conf={conf} area={s.area}px")

    (out / "raw.txt").write_text(raw, encoding="utf-8")
    render(png, segments, marks=[args.point] if args.point else []).save(out / "segments.png")
    print(f"saved {out / 'segments.png'}")


if __name__ == "__main__":
    main()
