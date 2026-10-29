// AVO overlay. Three windows over the main display, kept out of the model's
// screenshots (the agent captures below them):
//   - pointer: full-screen, transparent, click-through; the agent cursor, click
//     ripples, key/typing chips and the spotlight on a target (index.html)
//   - island: a glass pill above the Dock (island.html). Idle it collapses to a
//     small circle. Click it to type a task, or press Option-Space to first drag
//     a box over what the task is about (A, then optionally B); the island
//     then moves under the box as the input. While the agent works it widens to
//     show what it is doing, and Option-Space pauses it.
//   - bubble: the glass "Is this the one?" card next to a target (bubble.html)
// Island and bubble are real macOS frosted glass (vibrancy, rounded in glass.js).
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
const ISLAND_H = 44; // every island state is this tall (island.html --h)
const ISLAND_GAP = 14; // px between the island and the Dock / bottom of work area
const GLASS_RADIUS = 22;
const BUBBLE_GAP = 14; // px between the target and the bubble
const SUMMON_KEY = "Alt+Space";

let pointer = null;
let island = null;
let bubble = null;
const clients = new Set(); // connected agents
let pendingAsk = null; // the confirm request being answered: { id, rect }
let task = null; // the agent process started from the island, if any
let returnTo = null; // bundle id of the app to refocus after typing a task

nativeTheme.themeSource = "light"; // light glass even when macOS is in dark mode

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
  vibrancy: "popover",
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

function islandBounds(width) {
  const wa = screen.getPrimaryDisplay().workArea;
  let x = wa.x + (wa.width - width) / 2;
  let y = wa.y + wa.height - ISLAND_H - ISLAND_GAP;
  if (islandAnchor) {
    const [l, t, r, b] = islandAnchor;
    x = Math.max(wa.x + 12, Math.min((l + r) / 2 - width / 2, wa.x + wa.width - width - 12));
    y = b + ISLAND_GAP;
    if (y + ISLAND_H > wa.y + wa.height - 8) y = Math.max(wa.y + 8, t - ISLAND_GAP - ISLAND_H); // flip above
  }
  return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: ISLAND_H };
}

function createIsland() {
  island = new BrowserWindow({ ...common, ...glass, ...islandBounds(ISLAND_H), show: false });
  float(island, false); // idle: clickable
  island.loadFile("island.html");
  island.once("ready-to-show", () => {
    roundGlass(island, GLASS_RADIUS);
    island.showInactive();
  });
}

function createBubble() {
  bubble = new BrowserWindow({ ...common, ...glass, width: 300, height: 300, show: false });
  float(bubble, false);
  bubble.loadFile("bubble.html");
  bubble.once("ready-to-show", () => roundGlass(bubble, GLASS_RADIUS));
}

// ---- island ----

let islandState = "idle";

ipcMain.on("island-size", (_e, { state, width }) => {
  islandState = state;
  // Clickable only when it's waiting for the user; while the agent works, the
  // agent's clicks must pass through it.
  island.setIgnoreMouseEvents(!(state === "idle" || state === "input" || state === "paused"));
  island.setBounds(islandBounds(width), true); // native, animated resize
  setTimeout(() => island.invalidateShadow(), 350); // shadow follows the new shape
});

// ---- pause ----
// Option-Space while the agent works asks it to pause; it stops before its next
// action (an in-flight model call finishes first) and drops that plan, so
// after Resume it re-plans from a fresh screenshot.

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

function startSelect() {
  if (selecting || task || islandState !== "idle") return;
  selecting = true;
  regions = [];
  returnTo = frontApp();
  pointer.setIgnoreMouseEvents(false);
  pointer.webContents.send("avo", { type: "select-start" });
  island.webContents.send("avo", { type: "guide", text: "Drag over what AVO should work on" });
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
  openInput(regions.length > 1 ? "What should AVO do with A and B?" : "What should AVO do with A?");
});

function frontApp() {
  try {
    const asn = execFileSync("lsappinfo", ["front"], { encoding: "utf8" }).trim();
    const info = execFileSync("lsappinfo", ["info", "-only", "bundleid", asn], { encoding: "utf8" });
    return info.includes("=") ? info.split("=")[1].trim().replace(/"/g, "") : null;
  } catch {
    return null;
  }
}

function openInput(placeholder) {
  if (task || clients.size) return;
  if (islandState !== "input") {
    if (!selecting) {
      if (islandState !== "idle") return;
      returnTo = frontApp();
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
  if (selecting) endSelect(true); // keep the boxes, faint, while the agent works
  island.webContents.send("avo", { type: "status", text: "Starting…", busy: true });
  closeInput();
  setTimeout(() => runTask(text, chosen), 400); // let the app below take focus first
});

function findUv() {
  const dirs = (process.env.PATH || "").split(":").concat([path.join(os.homedir(), ".local/bin"), "/opt/homebrew/bin"]);
  for (const d of dirs) {
    const p = path.join(d, "uv");
    if (fs.existsSync(p)) return p;
  }
  return "uv";
}

// Run the agent on a typed task. Its events (status, confirm, done) come back
// over the WebSocket like any other run; this only watches for a silent crash.
function runTask(text, chosen = []) {
  fs.mkdirSync(path.join(ROOT, "runs"), { recursive: true });
  const log = fs.openSync(path.join(ROOT, "runs", "island.log"), "a");
  fs.writeSync(log, `\n=== ${new Date().toISOString()} ${text}\n`);
  let finished = false;
  const args = ["run", "scripts/run_task.py", text];
  for (const r of chosen) args.push("--region", `${r.label}=${r.box.map(Math.round).join(",")}`);
  task = spawn(findUv(), args, { cwd: ROOT, stdio: ["ignore", log, log] });
  task.sawDone = () => (finished = true);
  task.on("exit", (code) => {
    fs.closeSync(log);
    task = null;
    if (!finished) {
      island.webContents.send("avo", { type: "done", text: `AVO stopped (exit ${code}). See runs/island.log`, ok: false });
    }
  });
}

// ---- bubble ----

ipcMain.on("bubble-size", (_e, { width, height }) => {
  if (!pendingAsk) return;
  const wa = screen.getPrimaryDisplay().workArea;
  const { l, t, r, b } = pendingAsk.rect;
  const w = Math.ceil(width);
  const h = Math.ceil(height);
  let x = Math.round((l + r) / 2 - w / 2);
  x = Math.max(wa.x + 12, Math.min(x, wa.x + wa.width - w - 12));
  let y = b + BUBBLE_GAP;
  if (y + h > wa.y + wa.height - 8) y = Math.max(wa.y + 8, t - BUBBLE_GAP - h); // flip above near the Dock
  bubble.setBounds({ x, y: Math.round(y), width: w, height: h });
  bubble.invalidateShadow();
  if (!bubble.isVisible()) bubble.showInactive();
});

function answer(fields) {
  if (!pendingAsk) return;
  const msg = { type: "answer", id: pendingAsk.id, ...fields };
  pendingAsk = null;
  pointer.setIgnoreMouseEvents(true);
  pointer.webContents.send("avo", { type: "answered" });
  bubble.hide();
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
      return;
    case "done":
      paused = false;
      task?.sawDone();
      island.webContents.send("avo", msg);
      break;
    case "hide":
      island.webContents.send("avo", msg);
      break;
    case "confirm": {
      // The bubble sits next to the target, or next to all the candidates.
      const boxes = msg.candidates ? msg.candidates.map((c) => c.box) : [msg.box || [msg.x - 28, msg.y - 18, msg.x + 28, msg.y + 18]];
      const [l, t, r, b] = [0, 1, 2, 3].map((i) => (i < 2 ? Math.min : Math.max)(...boxes.map((x) => x[i])));
      pendingAsk = { id: msg.id, rect: { l, t, r, b } };
      pointer.setIgnoreMouseEvents(false); // the corrected point is clicked on the pointer window
      bubble.webContents.send("avo", msg);
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
        pointer.setIgnoreMouseEvents(true);
        pointer.webContents.send("avo", { type: "cancel" });
        bubble.hide();
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
  createPointer();
  createIsland();
  createBubble();
  startServer();
  if (!globalShortcut.register(SUMMON_KEY, summon)) console.log(`could not register ${SUMMON_KEY}`);
});
app.on("will-quit", () => globalShortcut.unregisterAll());
