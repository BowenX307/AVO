"""The E2 chart as a standalone SVG (light and dark), drawn from scripts/run_e2.py's rows.

Small multiples, one panel per way of clicking (alone / AVO with SAM / AVO with
SAM and the name); across, button size; up, how often the click hit; one line
per tremor level. Tremor is a magnitude, so the lines are one ramp, light (a
steady hand) to dark (12 px), in the brand's greys; the left end, where the
lines spread, carries direct labels and a legend sits above.
"""

from __future__ import annotations

PANELS = [("alone", "Clicking alone"), ("avo_sam", "AVO · rough box, SAM only"), ("avo_name", "AVO · rough box + the name")]
W, H = 960, 400
TOP, BOTTOM, LEFT, GAP = 132, 52, 52, 36
PANEL_W = (W - LEFT - 20 - GAP * 2) / 3
PLOT_H = H - TOP - BOTTOM
# Tremor levels, light (steady) to dark (most tremor); dark mode runs the same ramp reversed.
RAMP_LIGHT = {0: "#A0A09C", 4: "#707070", 8: "#404040", 12: "#0A0A0A"}
RAMP_DARK = {0: "#5E5E5C", 4: "#8E8E8B", 8: "#BEBEBA", 12: "#F5F5F2"}


def draw(rows: list[dict], res: dict) -> str:
    sizes = res["settings"]["sizes"]
    sigmas = res["settings"]["sigmas"]
    n = res["settings"]["trials"]

    def x(panel: int, size: int) -> float:
        i = sizes.index(size)
        return LEFT + panel * (PANEL_W + GAP) + 50 + i * (PANEL_W - 64) / (len(sizes) - 1)

    def y(rate: float) -> float:
        return TOP + (1 - rate) * PLOT_H

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
        f'aria-labelledby="t d" font-family="Geist, Inter, -apple-system, sans-serif">',
        '<title id="t">Hit rate by button size, with a simulated tremor</title>',
        f'<desc id="d">{n} simulated clicks per point. Alone, a 12 px button is hit 23% of the time at 12 px of tremor; '
        "with a rough box and the button's name, AVO hits every time.</desc>",
        "<style>",
        ".bg{fill:#F5F5F2}.ink{fill:#0A0A0A}.muted{fill:#6B6B68}.grid{stroke:#D9D9D6;stroke-width:1}",
        "".join(f".s{s}{{stroke:{c}}}.f{s}{{fill:{c}}}" for s, c in RAMP_LIGHT.items()),
        "@media (prefers-color-scheme: dark){.bg{fill:#0A0A0A}.ink{fill:#F5F5F2}.muted{fill:#A6A6A3}.grid{stroke:#2E2E2C}"
        + "".join(f".s{s}{{stroke:{c}}}.f{s}{{fill:{c}}}" for s, c in RAMP_DARK.items()) + "}",
        "text{font-size:12px}.title{font-size:17px;font-weight:600}.panel{font-size:13px;font-weight:600}",
        "</style>",
        f'<rect class="bg" width="{W}" height="{H}" rx="14"/>',
        f'<text class="ink title" x="{LEFT - 28}" y="34">How often a shaky click hits the button</text>',
        f'<text class="muted" x="{LEFT - 28}" y="54">Simulated tremor: each aim lands off by a random offset '
        f"(standard deviation σ). {n} clicks per point, demo/targets.html.</text>",
    ]
    # Legend: one line key per tremor level.
    lx = LEFT - 28
    out.append(f'<text class="muted" x="{lx}" y="86">Tremor σ</text>')
    lx += 64
    for s in sigmas:
        out.append(f'<line class="s{s}" x1="{lx}" y1="82" x2="{lx + 20}" y2="82" stroke-width="2" stroke-linecap="round"/>'
                   f'<text class="ink" x="{lx + 26}" y="86">{s} px</text>')
        lx += 80
    for p, (key, title) in enumerate(PANELS):
        x0 = LEFT + p * (PANEL_W + GAP)
        out.append(f'<text class="ink panel" x="{x0}" y="{TOP - 18}">{title}</text>')
        for rate in (0, 0.25, 0.5, 0.75, 1.0):
            out.append(f'<line class="grid" x1="{x0}" x2="{x0 + PANEL_W}" y1="{y(rate):.1f}" y2="{y(rate):.1f}"/>')
            if p == 0:
                out.append(f'<text class="muted" x="{x0 - 8}" y="{y(rate) + 4:.1f}" text-anchor="end">{int(rate * 100)}%</text>')
        for s in sizes:
            out.append(f'<text class="muted" x="{x(p, s):.1f}" y="{TOP + PLOT_H + 20}" text-anchor="middle">{s}</text>')
        out.append(f'<text class="muted" x="{x0 + PANEL_W / 2:.1f}" y="{TOP + PLOT_H + 40}" text-anchor="middle">button size (px)</text>')
        # Draw the steadiest first, so the heaviest tremor sits on top.
        for s in sigmas:
            pts = [(x(p, r["size"]), y(r[key])) for r in rows if r["sigma"] == s]
            path = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f} {py:.1f}" for i, (px, py) in enumerate(pts))
            out.append(f'<path class="s{s}" d="{path}" fill="none" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
            for px, py in pts:
                out.append(f'<circle class="f{s}" cx="{px:.1f}" cy="{py:.1f}" r="4" stroke="none"/>')
        # The value at the smallest button, where the lines spread; one label if they all meet.
        firsts = {s: next(r[key] for r in rows if r["sigma"] == s and r["size"] == sizes[0]) for s in sigmas}
        if len(set(firsts.values())) == 1:
            out.append(f'<text class="ink" x="{x(p, sizes[0]):.1f}" y="{y(firsts[sigmas[0]]) + 22:.1f}">'
                       f"{round(firsts[sigmas[0]] * 100)}% at every σ and size</text>")
        else:
            placed: list[float] = []
            for s in sorted(sigmas, key=lambda s: -firsts[s]):
                ly = y(firsts[s]) + 4
                if any(abs(ly - q) < 13 for q in placed):
                    continue  # too close to a label already placed; the legend and the table carry it
                placed.append(ly)
                out.append(f'<text class="ink" x="{x(p, sizes[0]) - 9:.1f}" y="{ly:.1f}" text-anchor="end">'
                           f"{round(firsts[s] * 100)}%</text>")
    out.append("</svg>")
    return "\n".join(out)
