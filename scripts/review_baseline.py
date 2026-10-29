"""Build a labeling page for a baseline batch: one card per agent step.

    uv run scripts/review_baseline.py runs/baseline-<timestamp>
    open runs/baseline-<timestamp>/review.html

Each step shows the screenshot the model acted on, with its click/drag/scroll
points marked, plus the model's note and actions. If the batch has
draft_labels.json (Claude's labels from reading the screenshots), each draft
shows as a dashed button: click to agree or pick another answer. Only labels
you click are exported, as labels.json (save it into the batch folder).
"""

import argparse
import html
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
POINTER_KINDS = ("click", "double_click", "move", "scroll")


def markers(actions: list[dict]) -> str:
    """SVG marks in screenshot pixel space (the image is 1280 wide)."""
    out = []
    for i, a in enumerate(actions or [], 1):
        if a["type"] in POINTER_KINDS:
            x, y = a["x"], a["y"]
            out.append(
                f'<circle cx="{x}" cy="{y}" r="16" class="mk"/>'
                f'<circle cx="{x}" cy="{y}" r="3" class="dot"/>'
                f'<text x="{x + 20}" y="{y - 12}" class="lbl">{i}</text>'
            )
        elif a["type"] == "drag" and a.get("path"):
            pts = " ".join(f"{p['x']},{p['y']}" for p in a["path"])
            out.append(f'<polyline points="{pts}" class="drag"/>')
    return "".join(out)


def step_cards(run_dir: Path) -> tuple[str, int]:
    cards = []
    steps = sorted(run_dir.glob("step*.json"))
    for f in steps:
        n = int(f.stem[4:])
        rec = json.loads(f.read_text(encoding="utf-8"))
        actions = rec.get("actions")
        shot = f"../{run_dir.name}/step{n - 1:02d}.png"
        note = (rec.get("output_text") or "").strip()
        kind = "actions" if actions else "final answer"
        w, h = Image.open(run_dir / f"step{n - 1:02d}.png").size
        size = f"{w} {h}"
        cards.append(
            f"""
      <div class="step" data-key="{run_dir.name}:{n}">
        <div class="shot">
          <img src="{shot}" loading="lazy">
          <svg viewBox="0 0 {size}" preserveAspectRatio="xMinYMin meet">{markers(actions)}</svg>
        </div>
        <div class="meta">
          <div class="n">Step {n} · {kind} · model {rec.get("model_seconds", "?")}s</div>
          {f'<div class="note">{html.escape(note)}</div>' if note else ""}
          {f'<pre>{html.escape(json.dumps(actions, ensure_ascii=False, indent=1))}</pre>' if actions else ""}
          <div class="vote">
            <button data-v="ok">Right</button><button data-v="bad">Wrong</button><button data-v="unsure">Unsure</button>
          </div>
        </div>
      </div>"""
        )
    return "".join(cards), len(steps)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()

    batch = args.batch.resolve()
    summary = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in json.loads((ROOT / "eval/tasks.json").read_text(encoding="utf-8"))}

    draft_file = batch / "draft_labels.json"
    drafts = json.loads(draft_file.read_text()) if draft_file.exists() else {}
    sections = []
    for entry in summary:
        if entry["run_dir"] and str(entry.get("result", "")).startswith(("stopped", "crashed")):
            drafts.setdefault(f"run:{entry['run_dir']}", "bad")
        elif entry["run_dir"]:
            drafts.setdefault(f"run:{entry['run_dir']}", "ok")
        if not entry["run_dir"]:
            continue
        run_dir = batch.parent / entry["run_dir"]
        t = tasks.get(entry["task_id"], {})
        cards, _ = step_cards(run_dir)
        sections.append(
            f"""
    <section class="run" data-run="{entry['run_dir']}">
      <h2>{html.escape(entry['task_id'])} <span class="mode">{entry['mode']}</span>
        <span class="stat">{entry.get('steps')} steps · {entry.get('elapsed_s')}s</span></h2>
      <p><b>Task</b> {html.escape(t.get('task', ''))}</p>
      <p><b>Expected</b> {html.escape(t.get('expect', ''))}</p>
      <p><b>Agent said</b> {html.escape(str(entry.get('result', '')))}</p>
      <div class="success">Did the whole task succeed?
        <button data-v="ok">Succeeded</button><button data-v="bad">Failed</button>
        <button class="accept">Agree with all drafts in this task</button>
      </div>
      {cards}
    </section>"""
        )

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AVO Baseline Labels</title>
<style>
  body {{ margin: 0; font: 14px/1.5 -apple-system, sans-serif; background: #f5f5f7; color: #1d1d1f; }}
  header {{ position: sticky; top: 0; z-index: 5; display: flex; gap: 16px; align-items: center;
           padding: 12px 24px; background: rgba(255,255,255,.85); backdrop-filter: blur(20px);
           border-bottom: 1px solid #ddd; }}
  header h1 {{ font-size: 16px; margin: 0; flex: 1; }}
  .run {{ max-width: 1400px; margin: 24px auto; padding: 16px 24px; background: #fff; border-radius: 14px; }}
  .run h2 {{ margin: 0 0 8px; font-size: 18px; }}
  .mode {{ font-size: 12px; padding: 2px 8px; border-radius: 8px; background: #e8f0fe; color: #1a56db; vertical-align: middle; }}
  .stat {{ font-size: 13px; font-weight: 400; color: #6e6e73; margin-left: 8px; }}
  .run p {{ margin: 4px 0; }}
  .success {{ margin: 10px 0 16px; }}
  .step {{ display: grid; grid-template-columns: minmax(0, 3fr) minmax(260px, 1fr); gap: 16px;
          padding: 14px 0; border-top: 1px solid #eee; }}
  .shot {{ position: relative; }}
  .shot img {{ width: 100%; display: block; border-radius: 8px; border: 1px solid #ddd; }}
  .shot svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
  .mk {{ fill: rgba(255,59,48,.18); stroke: #ff3b30; stroke-width: 3; }}
  .dot {{ fill: #ff3b30; }}
  .lbl {{ fill: #ff3b30; font: 700 22px -apple-system; paint-order: stroke; stroke: #fff; stroke-width: 4; }}
  .drag {{ fill: none; stroke: #ff3b30; stroke-width: 4; stroke-dasharray: 8 6; }}
  .n {{ font-weight: 600; }}
  .note {{ margin: 6px 0; padding: 6px 10px; background: #f2f2f7; border-radius: 8px; }}
  pre {{ font-size: 12px; white-space: pre-wrap; word-break: break-all; background: #fafafa; padding: 8px; border-radius: 6px; }}
  button {{ font: inherit; padding: 4px 14px; margin-right: 6px; border: 1px solid #ccc; border-radius: 8px; background: #fff; cursor: pointer; }}
  button.on[data-v=ok] {{ background: #34c759; border-color: #34c759; color: #fff; }}
  button.on[data-v=bad] {{ background: #ff3b30; border-color: #ff3b30; color: #fff; }}
  button.on[data-v=unsure] {{ background: #ff9f0a; border-color: #ff9f0a; color: #fff; }}
  button.draft {{ border: 2px dashed #8e8e93; }}
  button.draft::after {{ content: " (draft)"; font-size: 11px; color: #8e8e93; }}
  .accept {{ margin-left: 12px; color: #0a84ff; border-color: #0a84ff; }}
  .help {{ max-width: 1400px; margin: 16px auto 0; padding: 0 24px; color: #6e6e73; }}
</style></head>
<body>
<header><h1>AVO baseline labels · {batch.name}</h1><span id="count"></span><button id="export">Export labels.json</button></header>
<p class="help">Was each step the right move, aimed at the right spot? Dashed buttons are Claude's drafts:
click one to agree, or click another answer. Only clicked labels are exported.</p>
{''.join(sections)}
<script>
  const KEY = "avo-labels:{batch.name}";
  const DRAFT = {json.dumps(drafts)};
  let labels = {{}};
  try {{ labels = JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch {{}}
  const save = () => {{ try {{ localStorage.setItem(KEY, JSON.stringify(labels)); }} catch {{}} count(); }};
  function count() {{
    const total = document.querySelectorAll(".step").length;
    const done = [...document.querySelectorAll(".step")].filter(s => labels[s.dataset.key]).length;
    document.getElementById("count").textContent = `Labeled ${{done}} / ${{total}} steps`;
  }}
  function bind(box, key) {{
    const btns = box.querySelectorAll("button[data-v]");
    const paint = () => btns.forEach(b => {{
      b.classList.toggle("on", labels[key] === b.dataset.v);
      b.classList.toggle("draft", !labels[key] && DRAFT[key] === b.dataset.v);
    }});
    box.paint = paint;
    btns.forEach(b => b.addEventListener("click", () => {{
      labels[key] = labels[key] === b.dataset.v ? undefined : b.dataset.v; save(); paint();
    }}));
    paint();
  }}
  document.querySelectorAll(".step").forEach(s => bind(s.querySelector(".vote"), s.dataset.key));
  document.querySelectorAll(".run").forEach(r => {{
    const runKey = "run:" + r.dataset.run;
    bind(r.querySelector(".success"), runKey);
    r.querySelector(".accept").addEventListener("click", () => {{
      const keys = [runKey, ...[...r.querySelectorAll(".step")].map(s => s.dataset.key)];
      keys.forEach(k => {{ if (!labels[k] && DRAFT[k]) labels[k] = DRAFT[k]; }});
      save();
      r.querySelectorAll(".vote, .success").forEach(b => b.paint());
    }});
  }});
  document.getElementById("export").addEventListener("click", () => {{
    const blob = new Blob([JSON.stringify(labels, null, 2)], {{ type: "application/json" }});
    const a = Object.assign(document.createElement("a"), {{ href: URL.createObjectURL(blob), download: "labels.json" }});
    a.click();
  }});
  count();
</script>
</body></html>"""
    (batch / "review.html").write_text(page, encoding="utf-8")
    print(f"wrote {batch / 'review.html'}")


if __name__ == "__main__":
    main()
