"""Build a labeling page for a baseline batch: one card per agent step.

    uv run scripts/review_baseline.py runs/baseline-<timestamp>
    open runs/baseline-<timestamp>/review.html

Each step shows the screenshot the model acted on, with its click/drag/scroll
points marked, plus the model's note and actions. Mark each step 对/错/不确定
and each run as success or not; labels persist in the browser and export as
labels.json (save it into the batch folder).
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
        kind = "动作" if actions else "结束回答"
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
          <div class="n">第 {n} 步 · {kind} · 模型 {rec.get("model_seconds", "?")} 秒</div>
          {f'<div class="note">{html.escape(note)}</div>' if note else ""}
          {f'<pre>{html.escape(json.dumps(actions, ensure_ascii=False, indent=1))}</pre>' if actions else ""}
          <div class="vote">
            <button data-v="ok">对</button><button data-v="bad">错</button><button data-v="unsure">不确定</button>
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

    sections = []
    for entry in summary:
        if not entry["run_dir"]:
            continue
        run_dir = batch.parent / entry["run_dir"]
        t = tasks.get(entry["task_id"], {})
        cards, _ = step_cards(run_dir)
        sections.append(
            f"""
    <section class="run" data-run="{entry['run_dir']}">
      <h2>{html.escape(entry['task_id'])} <span class="mode">{entry['mode']}</span>
        <span class="stat">{entry.get('steps')} 步 · {entry.get('elapsed_s')} 秒</span></h2>
      <p><b>任务</b> {html.escape(t.get('task', ''))}</p>
      <p><b>正确结果</b> {html.escape(t.get('expect', ''))}</p>
      <p><b>agent 说</b> {html.escape(str(entry.get('result', '')))}</p>
      <div class="success">整个任务成功了吗？
        <button data-v="ok">成功</button><button data-v="bad">失败</button>
      </div>
      {cards}
    </section>"""
        )

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>AVO 基线标注</title>
<style>
  body {{ margin: 0; font: 14px/1.5 -apple-system, "PingFang SC", sans-serif; background: #f5f5f7; color: #1d1d1f; }}
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
</style></head>
<body>
<header><h1>AVO 基线标注 · {batch.name}</h1><span id="count"></span><button id="export">导出 labels.json</button></header>
{''.join(sections)}
<script>
  const KEY = "avo-labels:{batch.name}";
  let labels = {{}};
  try {{ labels = JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch {{}}
  const save = () => {{ try {{ localStorage.setItem(KEY, JSON.stringify(labels)); }} catch {{}} count(); }};
  function count() {{
    const total = document.querySelectorAll(".step").length;
    const done = [...document.querySelectorAll(".step")].filter(s => labels[s.dataset.key]).length;
    document.getElementById("count").textContent = `已标 ${{done}} / ${{total}} 步`;
  }}
  function bind(box, key) {{
    const btns = box.querySelectorAll("button");
    const paint = () => btns.forEach(b => b.classList.toggle("on", labels[key] === b.dataset.v));
    btns.forEach(b => b.addEventListener("click", () => {{
      labels[key] = labels[key] === b.dataset.v ? undefined : b.dataset.v; save(); paint();
    }}));
    paint();
  }}
  document.querySelectorAll(".step").forEach(s => bind(s.querySelector(".vote"), s.dataset.key));
  document.querySelectorAll(".run").forEach(r => bind(r.querySelector(".success"), "run:" + r.dataset.run));
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
