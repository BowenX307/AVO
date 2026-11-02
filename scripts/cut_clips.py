"""Cut the site's clips from a raw screen recording, zooming in on where AVO is acting.

    uv run scripts/cut_clips.py            # writes site/media/{smith,say,points}.mp4 and .jpg

Each clip is a few real-time segments of site/media/raw/smith.mov joined by short
crossfades. In each segment the frame eases in on where things happen (the island
while you speak, the two ringed Smiths, the reply, the confirm card); the last
segment eases back out so the loop starts again on the whole screen. Positions are
fractions of the frame (0-1), picked from the recording. The captions overlaid on
the page (site/media/captions.json) follow the same timeline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "site" / "media" / "raw" / "smith.mov"
OUT = ROOT / "site" / "media"
W, H = 2940, 1840  # the recording after cropping the 72 px menu-bar / notch strip off the top
TOP = 72
OUT_W = 1600
FADE = 0.35

# (start, end, speed, zoom from, zoom to, ease seconds, centre x, centre y)
CLIPS = {
    "smith": [
        (1.2, 7.2, 1.25, 1.0, 1.45, 1.2, 0.50, 0.10),  # saying it: the island's big type
        (17.4, 20.2, 1.0, 1.5, 1.55, 2.8, 0.40, 0.25),  # the two Smiths ringed, the question
        (52.0, 54.5, 1.0, 1.6, 1.6, 1.0, 0.60, 0.58),  # the reply, written
        (57.8, 60.1, 1.0, 1.5, 1.5, 1.0, 0.42, 0.32),  # Send — OK?
        (60.6, 63.6, 1.0, 1.3, 1.0, 1.6, 0.50, 0.55),  # sent; back to the whole screen
    ],
    "say": [
        (0.8, 8.0, 1.0, 1.0, 1.6, 1.4, 0.50, 0.08),
    ],
    "points": [
        (17.2, 20.2, 1.0, 1.0, 1.5, 0.9, 0.40, 0.25),
        (21.6, 24.0, 1.0, 1.5, 1.35, 2.4, 0.38, 0.35),
    ],
}
POSTER = {"smith": 19.0, "say": 6.6, "points": 18.6}


def zoom(z0: float, z1: float, d: float) -> str:
    """z(t) easing from z0 to z1 over d seconds (smoothstep), as an ffmpeg expression."""
    if z0 == z1:
        return f"{z0}"
    s = f"min(t/{d},1)"
    return f"({z0}+({z1 - z0})*{s}*{s}*(3-2*{s}))"


def segment(i: int, seg: tuple) -> str:
    start, end, speed, z0, z1, d, cx, cy = seg
    z = zoom(z0, z1, d * speed)  # t runs in source seconds inside the segment
    return (
        f"[0:v]trim={start}:{end},setpts=PTS-STARTPTS,"
        f"crop={W}:{H}:0:{TOP},"
        f"scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame:flags=bicubic,"
        f"crop={W}:{H}:x='max(0,min(iw-{W},{cx}*iw-{W // 2}))':y='max(0,min(ih-{H},{cy}*ih-{H // 2}))',"
        f"setpts=PTS/{speed},fps=60,scale={OUT_W}:-2:flags=lanczos,format=yuv420p[s{i}]"
    )


def cut(name: str, segs: list[tuple]) -> None:
    parts = [segment(i, s) for i, s in enumerate(segs)]
    durations = [(e - s) / sp for s, e, sp, *_ in segs]
    last, offset = "s0", 0.0
    for i in range(1, len(segs)):
        offset += durations[i - 1] - FADE  # each crossfade overlaps the two segments by FADE
        parts.append(f"[{last}][s{i}]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[x{i}]")
        last = f"x{i}"
    graph = ";".join(parts)
    out = OUT / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(RAW), "-filter_complex", graph, "-map", f"[{last}]",
                    "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-movflags", "+faststart", str(out)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(POSTER[name]), "-i", str(RAW), "-frames:v", "1",
                    "-vf", f"crop={W}:{H}:0:{TOP},scale={OUT_W}:-2", "-q:v", "4", str(OUT / f"{name}.jpg")], check=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True).stdout.strip()
    print(f"{name}.mp4: {float(dur):.2f} s, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    for name, segs in CLIPS.items():
        cut(name, segs)
