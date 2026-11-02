"""Cut the site's clips from a raw screen recording, zooming in on where AVO is acting.

    uv run scripts/cut_clips.py            # writes site/media/{smith,say,points}.mp4 and .jpg

Each clip is a few real-time segments of site/media/raw/smith.mov joined by short
crossfades. In each segment the frame eases in on where things happen (the island
while you speak, the two ringed Smiths, the reply, the confirm card); the last
segment eases back out so the loop starts again on the whole screen. Positions are
fractions of the frame (0-1), picked from the recording.

Each segment is rendered on its own (all at once at full resolution ran out of
memory), with zoompan, which works out every frame (crop + a changing scale reads
the size once, so an easing zoom drifted to one side). zoompan's own timestamps are
off, so they are set from the frame count.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "site" / "media" / "raw" / "smith.mov"
OUT = ROOT / "site" / "media"
W, H = 2940, 1840  # the recording after cropping the 72 px menu-bar / notch strip off the top
TOP = 72
OUT_W, OUT_H = 1600, 1002
FPS = 60
FADE = 0.35

# (start, end, speed, zoom from, zoom to, ease seconds, centre x, centre y), times in the recording
CLIPS = {
    "smith": [
        (1.2, 7.2, 1.25, 1.0, 1.9, 1.4, 0.50, 0.07),  # saying it: the island's big type
        (17.4, 20.2, 1.0, 1.7, 1.75, 2.8, 0.40, 0.24),  # the two Smiths ringed, the question
        (52.0, 54.5, 1.0, 1.9, 1.95, 2.5, 0.60, 0.58),  # the reply, written
        (57.8, 60.1, 1.0, 1.8, 1.85, 2.3, 0.42, 0.30),  # Send — OK?
        (60.6, 63.6, 1.0, 1.5, 1.0, 1.8, 0.50, 0.55),  # sent; back to the whole screen
    ],
    "say": [
        (0.8, 8.0, 1.0, 1.0, 2.0, 1.6, 0.50, 0.07),
    ],
    "points": [
        (17.2, 20.2, 1.0, 1.0, 1.7, 1.0, 0.40, 0.24),
        (21.6, 24.0, 1.0, 1.7, 1.6, 2.4, 0.36, 0.33),
    ],
}
POSTER = {"smith": 19.0, "say": 6.6, "points": 18.6}


def zoom(z0: float, z1: float, d: float) -> str:
    """Zoom easing from z0 to z1 over the first d seconds of the segment (smoothstep), as an ffmpeg expression."""
    if z0 == z1:
        return f"{z0}"
    s = f"min(it/{d:.3f},1)"
    return f"({z0}+({z1 - z0:.3f})*{s}*{s}*(3-2*{s}))"


def length(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return float(out.strip())


def render(seg: tuple, out: Path) -> float:
    """One segment, zoomed, at real speed (or `speed`); returns its length in seconds."""
    start, end, speed, z0, z1, d, cx, cy = seg
    z = zoom(z0, z1, d * speed)  # 'it' runs in recording seconds
    vf = (
        f"crop={W}:{H}:0:{TOP},"
        f"zoompan=z='{z}':x='max(0,min(iw-iw/zoom,{cx}*iw-iw/zoom/2))':y='max(0,min(ih-ih/zoom,{cy}*ih-ih/zoom/2))'"
        f":d=1:s={OUT_W}x{OUT_H}:fps={FPS},"
        f"setpts='N/({FPS * speed})/TB',fps={FPS},format=yuv420p"
    )
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start}", "-t", f"{end - start}", "-i", str(RAW),
                    "-vf", vf, "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "14", str(out)], check=True)
    return length(out)


def cut(name: str, segs: list[tuple]) -> None:
    out = OUT / f"{name}.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        parts = [Path(tmp) / f"{i}.mp4" for i in range(len(segs))]
        lengths = [render(seg, part) for seg, part in zip(segs, parts)]
        inputs = [a for p in parts for a in ("-i", str(p))]
        graph, last, offset = [], "0:v", 0.0
        for i in range(1, len(parts)):
            offset += lengths[i - 1] - FADE  # each crossfade overlaps the two segments by FADE
            graph.append(f"[{last}][{i}:v]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[x{i}]")
            last = f"x{i}"
        cmd = ["ffmpeg", "-v", "error", "-y", *inputs]
        if graph:
            cmd += ["-filter_complex", ";".join(graph), "-map", f"[{last}]"]
        cmd += ["-an", "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(POSTER[name]), "-i", str(RAW), "-frames:v", "1",
                    "-vf", f"crop={W}:{H}:0:{TOP},scale={OUT_W}:-2", "-q:v", "4", str(OUT / f"{name}.jpg")], check=True)
    print(f"{name}.mp4: {length(out):.2f} s, {out.stat().st_size // 1024} KB (segments {', '.join(f'{x:.2f}' for x in lengths)})")


if __name__ == "__main__":
    for name, segs in CLIPS.items():
        cut(name, segs)
