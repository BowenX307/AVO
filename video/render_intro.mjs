// Render video/intro.html to video/out/avo-intro.mp4: 1920x1080, 60 fps, with a click.
//   node video/render_intro.mjs
// Each frame is set from time alone (render(ms) in the page) and captured, so the
// result is smooth whatever the machine's speed. The click is synthesized with
// ffmpeg: a short bright tick (two decaying tones and a tiny noise transient).
import { spawn, execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRAMES = path.join(HERE, "out", "intro-frames");
const OUT = path.join(HERE, "out", "avo-intro.mp4");
const FPS = 60;
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
rmSync(FRAMES, { recursive: true, force: true });
mkdirSync(FRAMES, { recursive: true });

const port = 9341;
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
  `--remote-debugging-port=${port}`, "--user-data-dir=" + path.join(HERE, "out", ".chrome"), "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let list; for (let i = 0; i < 50; i++) { try { list = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(200); } }
const ws = new WebSocket(list.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const pend = new Map();
ws.addEventListener("message", (m) => { const d = JSON.parse(m.data); if (pend.has(d.id)) { pend.get(d.id)(d.result); pend.delete(d.id); } });
const send = (method, params = {}) => new Promise((r) => { pend.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: "file://" + path.join(HERE, "intro.html") });
await sleep(1500);
const T = (await send("Runtime.evaluate", { returnByValue: true, expression: "window.T" })).result.value;
const n = Math.round((T.end / 1000) * FPS);
for (let i = 0; i < n; i++) {
  await send("Runtime.evaluate", { expression: `render(${(i * 1000) / FPS})` });
  const shot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(path.join(FRAMES, `f${String(i).padStart(4, "0")}.png`), Buffer.from(shot.data, "base64"));
}
chrome.kill();

const click = path.join(HERE, "out", "click.wav");
execFileSync("ffmpeg", ["-v", "error", "-y", "-f", "lavfi", "-i",
  "aevalsrc='0.55*sin(2*PI*1900*t)*exp(-t*55)+0.32*sin(2*PI*3800*t)*exp(-t*110)+0.22*(2*random(0)-1)*exp(-t*900)':s=48000:d=0.25",
  "-af", "afade=t=in:d=0.002,highpass=f=300", click]);
execFileSync("ffmpeg", ["-v", "error", "-y", "-framerate", String(FPS), "-i", path.join(FRAMES, "f%04d.png"), "-i", click,
  "-filter_complex", `[1:a]adelay=${T.click}|${T.click},apad[a]`, "-map", "0:v", "-map", "[a]", "-shortest",
  "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", OUT]);
console.log(`wrote ${OUT}: ${n} frames, click at ${T.click} ms`);
