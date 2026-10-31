// AVO overlay. Two windows over the main display, kept out of the model's
// screenshots (the agent captures below them):
//   - pointer: full-screen, transparent, click-through; the agent cursor, click
//     ripples, key/typing chips, selection corners and the rings around a
//     target (index.html)
//   - island: a black glass capsule at the top centre of the screen
//     (island.html). Idle it shrinks to the AVO mark. Click it to type a task,
//     or press Option-Space to first drag a box over what the task is about (A,
//     then optionally B); the island then moves under the box as the input.
//     While the agent works it widens to show the step, Option-Space pauses
//     it, and when the agent asks it opens into the question card.
// The island is real macOS glass (vibrancy, rounded in glass.js).
// The Python agent connects over a local WebSocket and sends events.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFile, execFileSync, spawn } = require("child_process");
const { app, BrowserWindow, globalShortcut, ipcMain, nativeTheme, screen } = require("electron");
const { WebSocketServer } = require("ws");
const { roundGlass } = require("./glass");

const PORT = Number(process.env.AVO_OVERLAY_PORT || 8765);
// The overlay shows up in screen recordings; the agent's own screenshots
// capture only what lies below it (avo/driver.py). Set to 1 to hide it from
// all capture instead.
const HIDE_FROM_CAPTURE = process.env.AVO_OVERLAY_HIDE_FROM_CAPTURE === "1";
const ROOT = path.resolve(__dirname, "..");
const ISLAND_H = 36; // the one-line island states (island.html --h)
const ISLAND_TOP = 8; // px between the menu bar and the island
const ISLAND_GAP = 14; // px between a selection box and the input under it
const PILL_RADIUS = ISLAND_H / 2;
const CARD_RADIUS = 14; // --avo-radius-card: the input and the question card
const CARD_GAP = 14; // px between a target and the card, when the card must sit beside it
const SUMMON_KEY = "Alt+Space";
const DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"];

let pointer = null;
let island = null;
const clients = new Set(); // connected agents
let pendingAsk = null; // the confirm request being answered: { id, rect }
let task = null; // the agent process started from the island, if any
let lastRun = null; // { text, regions, app } of the last task started from the island, for Retry
let returnTo = null; // bundle id of the app to refocus after typing a task

nativeTheme.themeSource = "dark"; // black glass, whatever the macOS appearance

function float(win, clickThrough = true) {
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(clickThrough);
  win.setContentProtection(HIDE_FROM_CAPTURE);
}

const common = {
  frame: false,
  resizable: false,
  movable: false,
  focusable: false, // never takes keyboard focus from the app below
  skipTaskbar: true,
  acceptFirstMouse: true, // a click on an inactive window still counts
  webPreferences: { preload: path.join(__dirname, "preload.js") },
};

const glass = {
  transparent: true,
  backgroundColor: "#00000000",
  vibrancy: "hud",
  visualEffectState: "active",
  hasShadow: true,
};

function createPointer() {
  const { bounds } = screen.getPrimaryDisplay();
  pointer = new BrowserWindow({ ...common, ...bounds, hasShadow: false, transparent: true, enableLargerThanScreen: true });
  float(pointer);
  pointer.setBounds(bounds); // macOS may push a new window below the menu bar
  pointer.loadFile("index.html");
  pointer.showInactive();
}

let islandAnchor = null; // a selection box [l, t, r, b] to sit under while typing

const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

function islandBounds(state, width, height = ISLAND_H) {
  const wa = screen.getPrimaryDisplay().workArea;
  let x = wa.x + (wa.width - width) / 2;
  let y = wa.y + ISLAND_TOP;
  if (state === "input" && islandAnchor) {
    const [l, t, r, b] = islandAnchor;
    x = clamp((l + r) / 2 - width / 2, wa.x + 12, wa.x + wa.width - width - 12);
    y = b + ISLAND_GAP;
    if (y + height > wa.y + wa.height - 8) y = Math.max(wa.y + 8, t - ISLAND_GAP - height); // flip above
  } else if (state === "ask" && pendingAsk) {
    // The card opens at the top. If that would cover what it asks about, it
    // slides along the top to clear it, and failing that sits next to the
    // target, as the old bubble did.
    const { l, t, r, b } = pendingAsk.rect;
    const m = 16;
    if (x < r + m && x + width > l - m && y < b + m && y + height > t - m) {
      if (r + m + width <= wa.x + wa.width - 12) x = r + m;
      else if (l - m - width >= wa.x + 12) x = l - m - width;
      else {
        x = clamp((l + r) / 2 - width / 2, wa.x + 12, wa.x + wa.width - width - 12);
        y = b + CARD_GAP;
        if (y + height > wa.y + wa.height - 8) y = Math.max(wa.y + 8, t - CARD_GAP - height); // flip above near the Dock
      }
    }
  }
  return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) };
}

function createIsland() {
  island = new BrowserWindow({ ...common, ...glass, ...islandBounds("idle", 72), show: false });
  float(island, false); // idle: clickable
  island.loadFile("island.html");
  island.once("ready-to-show", () => {
    roundGlass(island, PILL_RADIUS);
    island.showInactive();
  });
}

// ---- island ----

let islandState = "idle";
const CLICKABLE = new Set(["idle", "input", "paused", "ask", "error"]);

// The window's own opacity, eased: the glass is native, so CSS can't fade it.
let fadeTimer = null;
function fade(to, ms, then) {
  clearInterval(fadeTimer);
  const from = island.getOpacity();
  const t0 = Date.now();
  fadeTimer = setInterval(() => {
    const k = Math.min(1, (Date.now() - t0) / ms);
    island.setOpacity(from + (to - from) * (1 - (1 - k) ** 3));
    if (k === 1) {
      clearInterval(fadeTimer);
      then?.();
    }
  }, 16);
}

// A change of shape in place morphs (native animated resize). A change of
// place (top of the screen <-> under a selection or a target) doesn't fly
// across the screen: the island fades out, and fades back in at the new spot
// while rising 6px.
let morph = 0;
ipcMain.on("island-size", (_e, { state, width, height }) => {
  islandState = state;
  // Clickable only when it's waiting for the user; while the agent works, the
  // agent's clicks must pass through it.
  island.setIgnoreMouseEvents(!CLICKABLE.has(state));
  const to = islandBounds(state, width, height);
  const from = island.getBounds();
  const card = state === "input" || state === "ask";
  if (!card) roundGlass(island, PILL_RADIUS); // rounder before shrinking to a pill...
  const far = Math.hypot(to.x + to.width / 2 - (from.x + from.width / 2), to.y - from.y) > 120;
  const id = ++morph;
  if (far && island.isVisible()) {
    fade(0, 110, () => {
      if (id !== morph) return;
      island.setBounds({ ...to, y: to.y + 6 });
      island.setBounds(to, true);
      fade(1, 240);
    });
  } else {
    island.setBounds(to, true);
    if (island.getOpacity() < 1) fade(1, 120);
  }
  if (card) setTimeout(() => id === morph && roundGlass(island, CARD_RADIUS), far ? 120 : 260); // ...squarer once grown into a card
  setTimeout(() => island.invalidateShadow(), 350); // shadow follows the new shape
});

// ---- pause ----
// Option-Space while the agent works asks it to pause; it stops at once, mid-batch
// if need be (an in-flight model call finishes first, its actions unused), and
// after Resume it re-plans from a fresh screenshot. The agent also pauses itself
// when the user touches the mouse or keyboard ("interrupt" below).

let paused = false;

function tellAgents(msg) {
  const data = JSON.stringify(msg);
  for (const ws of clients) ws.send(data);
}

function togglePause() {
  if (pendingAsk) {
    // Pausing while a question is up withdraws it; the agent drops that step.
    answer({ choice: "pause" });
    paused = true;
    island.webContents.send("avo", { type: "paused" });
    return;
  }
  paused = !paused;
  tellAgents({ type: paused ? "pause" : "resume" });
  island.webContents.send("avo", paused ? { type: "paused" } : { type: "status", text: "Resuming…", busy: true });
}

ipcMain.on("island-control", (_e, { cmd }) => {
  if (cmd === "retry") return retry();
  if (cmd === "dismiss") return island.webContents.send("avo", { type: "idle" });
  if (!paused) return;
  if (cmd === "resume") togglePause();
  else if (cmd === "stop") {
    paused = false;
    tellAgents({ type: "stop" });
    island.webContents.send("avo", { type: "status", text: "Stopping…", busy: true });
  }
});

// Option-Space: type a task when idle, pause or resume while the agent works.
function summon() {
  if (clients.size) togglePause();
  else startSelect();
}

// ---- box selection ----

let selecting = false;
let regions = []; // [{label, box}] in screen points
const AT_REST = new Set(["idle", "launch", "done", "warn", "error"]); // no task, nothing typed

function startSelect() {
  if (selecting || task || !AT_REST.has(islandState)) return;
  selecting = true;
  regions = [];
  returnTo = userApp;
  pointer.setIgnoreMouseEvents(false);
  pointer.webContents.send("avo", { type: "select-start" });
  island.webContents.send("avo", { type: "guide", text: "Select A" });
  globalShortcut.register("Escape", cancelInput);
}

function endSelect(keep) {
  selecting = false;
  islandAnchor = null;
  if (!pendingAsk) pointer.setIgnoreMouseEvents(true);
  pointer.webContents.send("avo", { type: "select-end", keep });
}

ipcMain.on("select-box", (_e, { label, box }) => {
  if (!selecting) return;
  const o = pointer.getBounds();
  const screenBox = [box[0] + o.x, box[1] + o.y, box[2] + o.x, box[3] + o.y];
  regions = regions.filter((r) => r.label !== label).concat({ label, box: screenBox });
  islandAnchor = screenBox;
  openInput(regions.length > 1 ? "Tell AVO what to do with A → B" : "Tell AVO what to do with A");
});

function bundleOf(asn) {
  try {
    const info = execFileSync("lsappinfo", ["info", "-only", "bundleid", asn], { encoding: "utf8" });
    return info.includes("=") ? info.split("=")[1].trim().replace(/"/g, "") : null;
  } catch {
    return null;
  }
}

function frontApp() {
  try {
    return bundleOf(execFileSync("lsappinfo", ["front"], { encoding: "utf8" }).trim());
  } catch {
    return null;
  }
}

// ---- the app the user is in ----
// A task runs in the app the user was in before reaching for AVO. Asking what
// is in front once the island opens is too late: clicking the island has
// already brought the overlay to the front, and the agent's keys then went
// nowhere (10/31: cmd+L pressed 19 times). So follow the front app as it
// changes and remember the last one that isn't the overlay.

let ownBundle = null;
let userApp = null;
let frontWatch = null;

function watchFrontApp() {
  if (!ownBundle) {
    try {
      ownBundle = bundleOf(execFileSync("lsappinfo", ["find", `pid=${process.pid}`], { encoding: "utf8" }).trim());
    } catch {}
  }
  const front = frontApp();
  if (front && front !== ownBundle) userApp = front;
  frontWatch = spawn("lsappinfo", ["listen", "+becameFrontmost", "forever"]);
  let pending = "";
  frontWatch.stdout.on("data", (chunk) => {
    const lines = (pending + chunk).split("\n");
    pending = lines.pop();
    for (const line of lines) {
      const id = /"CFBundleIdentifier"="([^"]+)"/.exec(line)?.[1];
      if (id && id !== ownBundle) userApp = id;
    }
  });
  frontWatch.on("exit", () => {
    frontWatch = null;
    if (!quitting) setTimeout(watchFrontApp, 1000);
  });
}

let quitting = false;
app.on("will-quit", () => {
  quitting = true;
  frontWatch?.kill();
});

function openInput(placeholder) {
  if (task || clients.size) return;
  if (islandState !== "input") {
    if (!selecting) {
      if (islandState !== "idle") return;
      returnTo = userApp;
    }
    island.setFocusable(true);
    app.focus({ steal: true });
    island.focus();
    // A system-wide Esc while typing: with a Chinese input method active, the
    // page's own keydown never sees Esc.
    globalShortcut.register("Escape", cancelInput);
  }
  island.webContents.send("avo", { type: "input", placeholder });
}

function cancelInput() {
  if (!selecting && islandState !== "input") return;
  if (selecting) endSelect(false);
  island.webContents.send("avo", { type: "idle" });
  closeInput();
}

function closeInput() {
  globalShortcut.unregister("Escape");
  island.setFocusable(false);
  if (returnTo) execFile("open", ["-b", returnTo]); // give the keyboard back
  returnTo = null;
}

ipcMain.on("island-open", () => openInput());

ipcMain.on("island-cancel", cancelInput);

ipcMain.on("island-submit", (_e, { text }) => {
  if (task) return;
  const chosen = regions;
  const run = (lastRun = { text, regions: chosen, app: returnTo });
  if (selecting) endSelect(true); // keep the boxes, faint, while the agent works
  island.webContents.send("avo", { type: "status", text: "Starting…", busy: true });
  closeInput();
  setTimeout(() => runTask(run), 400); // let the app below take focus first
});

// Retry after AVO got stuck: the same task and boxes, with the same app in front.
function retry() {
  if (!lastRun || clients.size) return;
  island.webContents.send("avo", { type: "status", text: "Starting…", busy: true });
  if (lastRun.app) execFile("open", ["-b", lastRun.app]);
  const go = () => setTimeout(() => runTask(lastRun), 400);
  if (task) task.once("exit", go);
  else go();
}

function findUv() {
  const dirs = (process.env.PATH || "").split(":").concat([path.join(os.homedir(), ".local/bin"), "/opt/homebrew/bin"]);
  for (const d of dirs) {
    const p = path.join(d, "uv");
    if (fs.existsSync(p)) return p;
  }
  return "uv";
}

// Run the agent on a typed task, in the app the user was in. Its events (status,
// confirm, done) come back over the WebSocket like any other run; this only
// watches for a silent crash.
function runTask({ text, regions: chosen = [], app: workApp }) {
  fs.mkdirSync(path.join(ROOT, "runs"), { recursive: true });
  const log = fs.openSync(path.join(ROOT, "runs", "island.log"), "a");
  fs.writeSync(log, `\n=== ${new Date().toISOString()} ${text}  (in ${workApp || "the front app"})\n`);
  let finished = false;
  const args = ["run", "scripts/run_task.py", text];
  for (const r of chosen) args.push("--region", `${r.label}=${r.box.map(Math.round).join(",")}`);
  if (workApp) args.push("--app", workApp);
  task = spawn(findUv(), args, { cwd: ROOT, stdio: ["ignore", log, log], env: { ...process.env, PYTHONUNBUFFERED: "1" } });
  task.sawDone = () => (finished = true);
  task.on("exit", (code) => {
    fs.closeSync(log);
    task = null;
    if (!finished) {
      island.webContents.send("avo", { type: "done", text: `AVO stopped (exit ${code}). See runs/island.log`, ok: false, retry: true });
    }
  });
}

// ---- asking ----
// The question is the island opened into a card (island.html); the rings
// are on the pointer window. With several candidates, the number keys pick one.

function bindDigits(n) {
  DIGITS.slice(0, n).forEach((d, i) => globalShortcut.register(d, () => answer({ choice: "pick", index: i })));
}

function unbindDigits() {
  for (const d of DIGITS) if (globalShortcut.isRegistered(d)) globalShortcut.unregister(d);
}

function answer(fields) {
  if (!pendingAsk) return;
  const msg = { type: "answer", id: pendingAsk.id, ...fields };
  pendingAsk = null;
  unbindDigits();
  pointer.setIgnoreMouseEvents(true);
  pointer.webContents.send("avo", { type: "answered", choice: fields.choice, index: fields.index });
  island.webContents.send("avo", { type: "answered" });
  tellAgents(msg);
}

ipcMain.on("bubble-choice", (_e, { choice, index }) => {
  if (choice === "no") pointer.webContents.send("avo", { type: "correct" });
  else answer(choice === "pick" ? { choice, index } : { choice });
});

// From the pointer window: a corrected point (converted back to screen points)
// or a candidate ring the user clicked.
ipcMain.on("answer", (_e, a) => {
  const o = pointer.getBounds();
  const fields = { ...a };
  if (typeof a.x === "number") {
    fields.x = a.x + o.x;
    fields.y = a.y + o.y;
  }
  answer(fields);
});

// ---- events from the agent ----

// Events carry screen point coordinates; the pointer window wants its own.
function toPointerWindow(msg) {
  const o = pointer.getBounds();
  const out = { ...msg };
  if (typeof msg.x === "number" && typeof msg.y === "number") {
    out.x = msg.x - o.x;
    out.y = msg.y - o.y;
  }
  const shift = ([l, t, r, b]) => [l - o.x, t - o.y, r - o.x, b - o.y];
  if (Array.isArray(msg.box)) out.box = shift(msg.box);
  if (Array.isArray(msg.candidates)) {
    out.candidates = msg.candidates.map((c) => ({ ...c, x: c.x - o.x, y: c.y - o.y, box: shift(c.box) }));
  }
  if (Array.isArray(msg.regions)) out.regions = msg.regions.map((r) => ({ ...r, box: r.box && shift(r.box) }));
  return out;
}

function route(msg) {
  switch (msg.type) {
    case "status":
      if (!paused) island.webContents.send("avo", msg); // keep showing "Paused"
      break; // the pointer shows a thinking ring by the cursor
    case "interrupt":
      // The agent noticed the user taking over and paused itself ("You moved —
      // paused.", or a countdown with auto-resume). Resume / Stop / Option-Space
      // work as for any pause.
      paused = true;
      island.webContents.send("avo", { type: "paused", text: msg.text });
      return;
    case "resumed": // auto-resumed by the agent
      paused = false;
      island.webContents.send("avo", { type: "status", text: "Resuming…", busy: true });
      return;
    case "done":
      paused = false;
      task?.sawDone();
      island.webContents.send("avo", { ...msg, retry: !!task }); // Retry only what the island started
      break;
    case "hide":
      island.webContents.send("avo", msg);
      break;
    case "confirm": {
      // The card keeps clear of the target, or of all the candidates.
      const boxes = msg.candidates ? msg.candidates.map((c) => c.box) : [msg.box || [msg.x - 28, msg.y - 18, msg.x + 28, msg.y + 18]];
      const [l, t, r, b] = [0, 1, 2, 3].map((i) => (i < 2 ? Math.min : Math.max)(...boxes.map((x) => x[i])));
      pendingAsk = { id: msg.id, rect: { l, t, r: r + (msg.candidates ? 40 : 0), b } }; // + the number tags right of the rings
      pointer.setIgnoreMouseEvents(false); // the corrected point is clicked on the pointer window
      island.webContents.send("avo", msg);
      if (msg.candidates) bindDigits(msg.candidates.length);
      break;
    }
  }
  pointer.webContents.send("avo", toPointerWindow(msg));
}

function startServer() {
  const wss = new WebSocketServer({ host: "127.0.0.1", port: PORT });
  wss.on("connection", (ws) => {
    clients.add(ws);
    ws.on("close", () => {
      clients.delete(ws);
      if (clients.size === 0) paused = false;
      // An agent that quits mid-question must not leave the screen unclickable.
      if (clients.size === 0 && pendingAsk) {
        pendingAsk = null;
        unbindDigits();
        pointer.setIgnoreMouseEvents(true);
        pointer.webContents.send("avo", { type: "cancel" });
        island.webContents.send("avo", { type: "cancel" });
      }
    });
    ws.on("message", (data) => {
      let msg;
      try {
        msg = JSON.parse(data.toString());
      } catch {
        return;
      }
      route(msg);
    });
  });
  console.log(`AVO overlay listening on ws://127.0.0.1:${PORT}`);
}

app.dock?.hide(); // no Dock icon, not in Cmd-Tab
app.whenReady().then(() => {
  watchFrontApp();
  createPointer();
  createIsland();
  startServer();
  if (!globalShortcut.register(SUMMON_KEY, summon)) console.log(`could not register ${SUMMON_KEY}`);
});
app.on("will-quit", () => globalShortcut.unregisterAll());
