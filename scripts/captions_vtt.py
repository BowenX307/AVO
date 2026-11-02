"""Write the WebVTT caption tracks from site/media/captions.json (the same lines the page overlays).

    python3 scripts/captions_vtt.py
"""

import json
import subprocess
from pathlib import Path

MEDIA = Path(__file__).resolve().parent.parent / "site" / "media"


def stamp(t: float) -> str:
    return f"00:{int(t // 60):02d}:{t % 60:06.3f}"


def duration(clip: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(MEDIA / f"{clip}.mp4")], capture_output=True, text=True).stdout
    return float(out.strip())


for clip, cues in json.loads((MEDIA / "captions.json").read_text()).items():
    end = duration(clip)
    lines = ["WEBVTT", ""]
    for i, c in enumerate(cues):
        until = cues[i + 1]["t"] if i + 1 < len(cues) else end
        lines += [f"{stamp(c['t'])} --> {stamp(until)}", c["text"], ""]
    (MEDIA / f"{clip}.vtt").write_text("\n".join(lines))
    print(f"{clip}.vtt: {len(cues)} cues")
