// AVO site: the opening, the second cursor, and headings that rise into view.
// Needs brand/logo-animated.js (AVOLogo) loaded first. No framework, no build.

(function () {
  "use strict";

  const root = document.documentElement;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const EASE = "cubic-bezier(0.2, 0.8, 0.2, 1)"; // --avo-ease
  const EASE_DRAW = "cubic-bezier(0.65, 0, 0.35, 1)"; // --avo-ease-draw
  root.classList.add("js");

  // A lightly damped spring as a CSS easing (same as the overlay cursor).
  const SPRING = (() => {
    const zeta = 0.72, w = 9.5, wd = w * Math.sqrt(1 - zeta * zeta);
    const x = (t) => 1 - Math.exp(-zeta * w * t) * (Math.cos(wd * t) + ((zeta * w) / wd) * Math.sin(wd * t));
    const stops = Array.from({ length: 33 }, (_, i) => x(i / 32).toFixed(4));
    stops[32] = "1";
    return `linear(${stops.join(", ")})`;
  })();

  // The overlay's cursor: the logo's Λ as an open arrow, tip at (0, 0).
  const CURSOR = `<svg width="1" height="1" aria-hidden="true"><g transform="rotate(-30)" fill="none" stroke-linejoin="miter" stroke-linecap="square">
    <path d="M-7.5 23.94 L0 8.94 L7.5 23.94" stroke="#0A0A0A" stroke-width="8"/><path d="M-7.5 23.94 L0 8.94 L7.5 23.94" stroke="#F5F5F2" stroke-width="5"/></g></svg>`;

  // A rounded rectangle path w x h drawn from "half past ten", clockwise, once
  // around and no further.
  function ringPath(w, h, r) {
    const k = r * (1 - Math.SQRT1_2);
    const f = (n) => n.toFixed(1);
    return (
      `M${f(k)} ${f(k)} A${r} ${r} 0 0 1 ${f(r)} 0 H${f(w - r)} A${r} ${r} 0 0 1 ${f(w)} ${f(r)} ` +
      `V${f(h - r)} A${r} ${r} 0 0 1 ${f(w - r)} ${f(h)} H${f(r)} A${r} ${r} 0 0 1 0 ${f(h - r)} ` +
      `V${f(r)} A${r} ${r} 0 0 1 ${f(k)} ${f(k)}`
    );
  }

  function storage(fn) {
    try {
      return fn(sessionStorage);
    } catch {
      return null; // private mode, blocked storage: just play / don't remember
    }
  }

  // ---- the opening (index.html only) ----
  // The logo draws on black; a small AVO cursor slides in and clicks the O; the
  // O opens into the page (a circle growing from its centre) while the logo
  // shrinks into the nav. Any key, click, wheel or touch skips to the end.
  function opening() {
    const veil = document.getElementById("intro");
    const big = document.getElementById("intro-logo");
    if (!root.classList.contains("intro") || !veil || !big || !window.AVOLogo) return root.classList.remove("intro");
    storage((s) => s.setItem("avo-intro", "1"));
    const svg = big.querySelector("svg");
    const navLogo = document.querySelector(".nav .brand svg");
    const running = [];
    let ended = false;
    let drawing = null;
    const extras = [];

    function end() {
      if (ended) return;
      ended = true;
      drawing?.finish();
      running.forEach((a) => a.cancel());
      extras.forEach((e) => e.remove());
      big.remove();
      veil.remove();
      root.classList.remove("intro");
      for (const t of ["keydown", "pointerdown", "wheel", "touchstart"]) removeEventListener(t, end, true);
    }
    for (const t of ["keydown", "pointerdown", "wheel", "touchstart"]) addEventListener(t, end, { capture: true, passive: true });
    clearTimeout(window.__avoIntroFailsafe); // set in <head>; this script took over

    drawing = AVOLogo.drawLogo(svg, { speed: 0.9, onDone: () => !ended && clickTheO() });
    svg.classList.add("drawing"); // hidden until now, so the finished logo never flashes first

    function clickTheO() {
      const box = svg.getBoundingClientRect();
      const ox = box.left + (290 / 340) * box.width;
      const oy = box.top + box.height / 2;
      const cursor = document.createElement("div");
      cursor.className = "intro-cursor";
      cursor.innerHTML = CURSOR;
      document.body.append(cursor);
      extras.push(cursor);
      const glide = cursor.animate(
        [{ transform: `translate(${innerWidth + 30}px, ${innerHeight + 30}px)` }, { transform: `translate(${ox}px, ${oy}px)` }],
        { duration: 520, easing: SPRING, fill: "forwards" },
      );
      running.push(glide);
      glide.finished.then(() => {
        if (ended) return;
        const press = cursor.firstChild.animate([{ transform: "scale(1)" }, { transform: "scale(0.9)" }, { transform: "scale(1)" }], { duration: 200, easing: EASE });
        const ripple = document.createElement("div");
        ripple.className = "intro-ripple";
        Object.assign(ripple.style, { left: `${ox}px`, top: `${oy}px` });
        document.body.append(ripple);
        extras.push(ripple);
        running.push(press, ripple.animate([{ transform: "scale(0.3)", opacity: 1 }, { transform: "scale(1.6)", opacity: 0 }], { duration: 320, easing: EASE, fill: "forwards" }));
        setTimeout(() => !ended && open(ox, oy, cursor), 140);
      }, () => {});
    }

    function open(ox, oy, cursor) {
      veil.style.setProperty("--ox", `${ox}px`);
      veil.style.setProperty("--oy", `${oy}px`);
      const far = Math.hypot(Math.max(ox, innerWidth - ox), Math.max(oy, innerHeight - oy));
      const from = big.getBoundingClientRect();
      const to = navLogo.getBoundingClientRect();
      const dx = to.left - from.left, dy = to.top - from.top, s = to.width / from.width;
      big.style.transformOrigin = "0 0";
      big.style.transform = "none";
      big.style.left = `${from.left}px`;
      big.style.top = `${from.top}px`;
      const reveal = veil.animate({ "--hole": ["0px", `${far + 2}px`] }, { duration: 900, easing: EASE_DRAW, fill: "forwards" });
      running.push(
        reveal,
        big.animate(
          [{ transform: "none", color: "#F5F5F2" }, { transform: `translate(${dx}px, ${dy}px) scale(${s})`, color: "#0A0A0A" }],
          { duration: 900, easing: EASE_DRAW, fill: "forwards" },
        ),
        cursor.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 320, easing: EASE, fill: "forwards" }),
      );
      reveal.finished.then(end, () => {});
    }
  }

  // ---- the second cursor (desktop with a mouse, motion allowed) ----
  // A thin ring that trails the pointer by ~120 ms; over a button it leaves the
  // pointer and draws a ring around the button instead: one stroke, once around.
  function secondCursor() {
    if (reduced || !matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    const ring = document.createElement("div");
    ring.className = "cursor2 off";
    document.body.append(ring);
    const trail = [];
    const LAG = 120;
    let hovered = null;
    addEventListener("pointermove", (e) => {
      if (e.pointerType !== "mouse") return;
      trail.push([performance.now(), e.clientX, e.clientY]);
      ring.classList.toggle("off", !!hovered);
    }, { passive: true });
    document.addEventListener("mouseleave", () => ring.classList.add("off"));
    (function frame(now) {
      const t = now - LAG;
      while (trail.length > 2 && trail[1][0] <= t) trail.shift();
      if (trail.length) {
        let [t0, x, y] = trail[0];
        if (trail[1] && t > t0) {
          const [t1, x1, y1] = trail[1];
          const k = Math.min(1, (t - t0) / (t1 - t0 || 1));
          x += (x1 - x) * k;
          y += (y1 - y) * k;
        }
        ring.style.transform = `translate(${x}px, ${y}px)`;
      }
      requestAnimationFrame(frame);
    })(performance.now());

    for (const el of document.querySelectorAll(".btn, .nav nav a, .brand")) {
      el.addEventListener("pointerenter", () => {
        hovered = el;
        ring.classList.add("off");
        el.querySelectorAll(".trace").forEach((t) => t.remove()); // one ring per hover, never two stacked
        const box = el.getBoundingClientRect();
        const w = box.width + 14, h = box.height + 14;
        const trace = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        trace.classList.add("trace");
        const r = Math.min(h / 2, w / 2, getComputedStyle(el).borderRadius === "0px" ? 8 : h / 2);
        trace.innerHTML = `<path d="${ringPath(w, h, r)}"/>`;
        if (getComputedStyle(el).position === "static") el.style.position = "relative";
        el.append(trace);
        const p = trace.firstChild;
        const total = p.getTotalLength();
        p.style.strokeDasharray = `${total} ${total + 2}`;
        p.animate([{ strokeDashoffset: total + 1 }, { strokeDashoffset: 0 }], { duration: 850, easing: EASE_DRAW, fill: "forwards" });
      });
      el.addEventListener("pointerleave", () => {
        hovered = null;
        ring.classList.remove("off");
        for (const t of el.querySelectorAll(".trace")) t.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 180, fill: "forwards" }).finished.then(() => t.remove());
      });
    }
  }

  // ---- headings rise into view, once ----
  function reveals() {
    const items = document.querySelectorAll("[data-reveal]");
    if (!("IntersectionObserver" in window)) return items.forEach((el) => el.classList.add("in"));
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      },
      { rootMargin: "0px 0px -8% 0px" },
    );
    items.forEach((el) => io.observe(el));
  }

  // ---- Voice Control vs AVO: the steps appear one by one, each side at its own pace, and the counts tick up ----
  function compare() {
    const box = document.querySelector(".compare.anim");
    if (!box) return;
    const sides = [...box.children].map((side, i) => ({
      items: [...side.querySelectorAll("li")],
      tally: side.querySelector("[data-tally]"),
      every: i === 0 ? 520 : 260, // commands come slowly; AVO's three sentences quickly
    }));
    const show = (side, n) => {
      side.items.forEach((li, k) => li.classList.toggle("in", k < n));
      if (side.tally) side.tally.textContent = n;
    };
    if (reduced || !("IntersectionObserver" in window)) return sides.forEach((side) => show(side, side.items.length));
    sides.forEach((side) => show(side, 0));
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        io.disconnect();
        for (const side of sides) side.items.forEach((_, k) => setTimeout(() => show(side, k + 1), 200 + k * side.every));
      },
      { rootMargin: "0px 0px -25% 0px" },
    );
    io.observe(box);
  }

  // ---- docs: mark the section being read in the side list ----
  function toc() {
    const links = [...document.querySelectorAll(".toc a")];
    if (!links.length || !("IntersectionObserver" in window)) return;
    const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          links.forEach((a) => a.classList.remove("on"));
          byId.get(e.target.id)?.classList.add("on");
        }
      },
      { rootMargin: "-20% 0px -70% 0px" },
    );
    byId.forEach((_, id) => {
      const s = document.getElementById(id);
      if (s) io.observe(s);
    });
  }

  opening();
  secondCursor();
  reveals();
  compare();
  toc();
})();
