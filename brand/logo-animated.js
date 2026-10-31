// The AVO logo, drawn the way AVO rings a target: one stroke per letter.
//
//   <svg class="logo">…</svg>                    (AVOLogo.markup() writes one)
//   AVOLogo.drawLogo(svgEl, { onDone })          ~2.3 s: Λ, then V, then O
//
// Λ starts bottom left, up to the apex, down to the right. V mirrors it. O
// starts at half past ten and goes clockwise, about 4% past closed (over its
// own start), then settles back; a faint glow and halo mark the end.
// With "reduce motion" on, the full logo shows at once. A classic script
// (not a module) so it also loads from file:// in the Electron overlay.

(function (root) {
  "use strict";

  const SVG = "http://www.w3.org/2000/svg";
  const EASE_DRAW = "cubic-bezier(0.65, 0, 0.35, 1)"; // --avo-ease-draw
  const EASE = "cubic-bezier(0.2, 0.8, 0.2, 1)"; // --avo-ease
  const O = { cx: 290, cy: 50, r: 41 };
  const OVERSHOOT = 0.04; // of the circle
  // [start, duration] in ms; 60 ms rests between letters.
  const TIMES = { a: [150, 520], v: [730, 520], o: [1310, 650], pulse: [1960, 300] };
  const TOTAL = 2260;

  // A point on O's circle, `deg` clockwise from twelve o'clock.
  function onCircle(deg, r = O.r) {
    const t = (deg * Math.PI) / 180;
    return [O.cx + r * Math.sin(t), O.cy - r * Math.cos(t)];
  }
  const f = (n) => n.toFixed(2);

  function ringPath() {
    const [sx, sy] = onCircle(-45); // half past ten
    const [ex, ey] = onCircle(135);
    return `M${f(sx)} ${f(sy)} A${O.r} ${O.r} 0 1 1 ${f(ex)} ${f(ey)} A${O.r} ${O.r} 0 1 1 ${f(sx)} ${f(sy)} Z`;
  }

  // The ring plus its overshoot: a short arc on past the start, on the ring itself.
  function drawnRingPath() {
    const [sx, sy] = onCircle(-45);
    const [ex, ey] = onCircle(135);
    const [px, py] = onCircle(-45 + 360 * OVERSHOOT);
    return (
      `M${f(sx)} ${f(sy)} A${O.r} ${O.r} 0 1 1 ${f(ex)} ${f(ey)} A${O.r} ${O.r} 0 1 1 ${f(sx)} ${f(sy)} ` +
      `A${O.r} ${O.r} 0 0 1 ${f(px)} ${f(py)}`
    );
  }

  const PATHS = { a: "M10 90 L50 10 L90 90", v: "M126 10 L166 90 L206 10", o: ringPath() };

  // Inline SVG for the logo. strokeWidth 8 is the master; small sizes (the
  // island) read better a little heavier.
  function markup({ className = "avo-logo", strokeWidth = 8, title = "AVO" } = {}) {
    return (
      `<svg class="${className}" viewBox="0 0 340 100" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" ` +
      `stroke-linejoin="miter" stroke-linecap="square" role="img" aria-label="${title}">` +
      `<path class="avo-a" d="${PATHS.a}"/><path class="avo-v" d="${PATHS.v}"/><path class="avo-o" d="${PATHS.o}"/></svg>`
    );
  }

  function reducedMotion() {
    try {
      return root.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  }

  let uid = 0;

  // Draw the logo in `svg` (which holds .avo-a, .avo-v, .avo-o paths).
  // Returns { finished, finish(), seek(ms) }: finish() jumps to the end
  // (skip), seek() freezes the drawing at a moment (for stills).
  function drawLogo(svg, { onDone, speed = 1, reduced = reducedMotion() } = {}) {
    const paths = { a: svg.querySelector(".avo-a"), v: svg.querySelector(".avo-v"), o: svg.querySelector(".avo-o") };
    let done = false;
    let resolveFinished;
    const finished = new Promise((r) => (resolveFinished = r));
    const complete = () => {
      if (done) return;
      done = true;
      cleanup();
      onDone?.();
      resolveFinished();
    };

    if (reduced || typeof svg.animate !== "function") {
      queueMicrotask(complete);
      return { finished, finish: complete, seek() {} };
    }

    const ms = (n) => n / speed;
    const animations = [];
    const extras = [];

    // Λ and V: one dash the length of the path, slid in. The gap is a little
    // longer than the dash so nothing (not even a square cap) shows before the start.
    for (const key of ["a", "v"]) {
      const p = paths[key];
      const len = p.getTotalLength();
      p.style.strokeDasharray = `${len} ${len + 2}`;
      animations.push(
        p.animate([{ strokeDashoffset: len + 1 }, { strokeDashoffset: 0 }], {
          delay: ms(TIMES[key][0]),
          duration: ms(TIMES[key][1]),
          easing: EASE_DRAW,
          fill: "both",
        }),
      );
    }

    // O: drawn past closed, then eased back to exactly closed.
    const o = paths.o;
    o.setAttribute("d", drawnRingPath());
    const total = o.getTotalLength();
    const tail = total - 2 * Math.PI * O.r;
    o.style.strokeDasharray = `${total} ${total + 2}`;
    animations.push(
      o.animate(
        [
          { strokeDashoffset: total + 1, easing: EASE_DRAW },
          { strokeDashoffset: 0, offset: 0.86, easing: EASE },
          { strokeDashoffset: tail },
        ],
        { delay: ms(TIMES.o[0]), duration: ms(TIMES.o[1]), fill: "both" },
      ),
    );

    // The finish: O glows for a moment and a faint halo spreads out from it.
    const id = `avo-glow-${++uid}`;
    const defs = document.createElementNS(SVG, "defs");
    defs.innerHTML = `<filter id="${id}" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5"/></filter>`;
    const glow = document.createElementNS(SVG, "path");
    glow.setAttribute("d", PATHS.o);
    glow.setAttribute("filter", `url(#${id})`);
    glow.style.opacity = "0";
    const halo = document.createElementNS(SVG, "path");
    halo.setAttribute("d", PATHS.o);
    halo.setAttribute("stroke-width", "1.5");
    Object.assign(halo.style, { opacity: "0", transformBox: "view-box", transformOrigin: `${O.cx}px ${O.cy}px` });
    svg.prepend(defs);
    svg.append(glow, halo);
    extras.push(defs, glow, halo);
    const pulse = { delay: ms(TIMES.pulse[0]), duration: ms(TIMES.pulse[1]), fill: "both" };
    animations.push(
      glow.animate([{ opacity: 0 }, { opacity: 0.5, offset: 0.3 }, { opacity: 0 }], { ...pulse, easing: "ease-out" }),
      halo.animate(
        [
          { opacity: 0, transform: "scale(1)" },
          { opacity: 0.4, transform: "scale(1.04)", offset: 0.1 },
          { opacity: 0, transform: "scale(1.38)" },
        ],
        { ...pulse, easing: EASE },
      ),
    );

    function cleanup() {
      o.setAttribute("d", PATHS.o);
      for (const p of Object.values(paths)) p.style.strokeDasharray = "";
      for (const a of animations) a.cancel();
      for (const e of extras) e.remove();
    }

    Promise.all(animations.map((a) => a.finished)).then(complete, () => {});

    return {
      finished,
      finish: complete,
      seek(t) {
        for (const a of animations) {
          a.pause();
          a.currentTime = ms(t);
        }
      },
    };
  }

  root.AVOLogo = { drawLogo, markup, PATHS, TOTAL };
  root.drawLogo = drawLogo;
})(typeof window !== "undefined" ? window : globalThis);
