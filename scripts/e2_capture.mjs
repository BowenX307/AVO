// E2 step 1: screenshot demo/targets.html as AVO would see it (1280x800, 1x)
// and save every button's name and box, without taking over the screen.
//
//     node scripts/e2_capture.mjs        -> eval/e2/targets.png, eval/e2/targets.json
//
// Uses headless Chrome over the DevTools protocol, and quits it afterwards.

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "eval", "e2");
const W = 1280, H = 800;
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(path.join(tmpdir(), "avo-e2-"))}`, `--window-size=${W},${H}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  let list;
  for (let i = 0; i < 50 && !list; i++) {
    try { list = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(100); }
  }
  const ws = new WebSocket(list.find((t) => t.type === "page").webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) (pending.get(m.id)(m), pending.delete(m.id));
  });
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: pathToFileURL(path.join(ROOT, "demo", "targets.html")).href });
  await sleep(800);
  const shot = await send("Page.captureScreenshot", { format: "png" });
  const rects = await send("Runtime.evaluate", { expression: "window.targets()", returnByValue: true });
  mkdirSync(OUT, { recursive: true });
  writeFileSync(path.join(OUT, "targets.png"), Buffer.from(shot.result.data, "base64"));
  writeFileSync(path.join(OUT, "targets.json"), JSON.stringify({ width: W, height: H, buttons: rects.result.result.value }, null, 1));
  console.log(`wrote ${path.join(OUT, "targets.png")} and targets.json (${rects.result.result.value.length} buttons)`);
} finally {
  chrome.kill();
}
process.exit(0);
