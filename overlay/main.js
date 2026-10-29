// AVO overlay. Two click-through, never-focused windows over the main display:
//   - pointer: full-screen and transparent; draws the agent cursor, click
//     ripples and key/typing chips (index.html)
//   - caption: a small native frosted-glass panel above the Dock with what the
//     agent is doing (caption.html). CSS can't blur the desktop behind a
//     transparent window, so the glass comes from macOS vibrancy instead.
// The Python agent connects over a local WebSocket and sends events.

const path = require("path");
const { app, BrowserWindow, ipcMain, nativeTheme, screen } = require("electron");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.AVO_OVERLAY_PORT || 8765);
// By default the overlay is hidden from screen capture so the model never
// sees our own cursor in its screenshots. Set to 1 to capture it anyway.
const VISIBLE_IN_CAPTURE = process.env.AVO_OVERLAY_VISIBLE_IN_CAPTURE === "1";
const CAPTION_GAP = 18; // px between caption and the Dock / bottom of work area

const CAPTION_EVENTS = new Set(["status", "done", "hide"]);

let pointer = null;
let caption = null;
const clients = new Set(); // connected agents

nativeTheme.themeSource = "light"; // light glass even when macOS is in dark mode

function float(win) {
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true);
  win.setContentProtection(!VISIBLE_IN_CAPTURE);
}

const common = {
  frame: false,
  hasShadow: false,
  resizable: false,
  movable: false,
  focusable: false, // never takes keyboard focus from the app below
  skipTaskbar: true,
  webPreferences: { preload: path.join(__dirname, "preload.js") },
};

function createPointer() {
  const { bounds } = screen.getPrimaryDisplay();
  pointer = new BrowserWindow({
    ...common,
    ...bounds,
    transparent: true,
    enableLargerThanScreen: true,
  });
  float(pointer);
  pointer.setBounds(bounds); // macOS may push a new window below the menu bar
  pointer.loadFile("index.html");
  pointer.showInactive();
}

function createCaption() {
  caption = new BrowserWindow({
    ...common,
    width: 240,
    height: 44,
    show: false,
    hasShadow: true,
    vibrancy: "popover",
    visualEffectState: "active",
    backgroundColor: "#00000000",
    roundedCorners: true,
  });
  float(caption);
  caption.loadFile("caption.html");
}

// ---- caption window: size to content, fade in/out ----

let fadeTimer = null;
function fade(win, to, ms = 180) {
  clearInterval(fadeTimer);
  const from = win.getOpacity();
  const steps = Math.max(1, Math.round(ms / 16));
  let i = 0;
  fadeTimer = setInterval(() => {
    i += 1;
    win.setOpacity(from + ((to - from) * i) / steps);
    if (i >= steps) {
      clearInterval(fadeTimer);
      if (to === 0) win.hide();
    }
  }, 16);
}

ipcMain.on("caption-size", (_e, { width, height }) => {
  const wa = screen.getPrimaryDisplay().workArea;
  caption.setBounds({
    x: Math.round(wa.x + (wa.width - width) / 2),
    y: Math.round(wa.y + wa.height - height - CAPTION_GAP),
    width: Math.ceil(width),
    height: Math.ceil(height),
  });
  if (!caption.isVisible()) {
    caption.setOpacity(0);
    caption.showInactive();
  }
  fade(caption, 1);
});

ipcMain.on("caption-hide", () => {
  if (caption.isVisible()) fade(caption, 0);
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
  if (Array.isArray(msg.box)) {
    const [l, t, r, b] = msg.box;
    out.box = [l - o.x, t - o.y, r - o.x, b - o.y];
  }
  return out;
}

function route(msg) {
  if (CAPTION_EVENTS.has(msg.type)) caption.webContents.send("avo", msg);
  if (msg.type === "confirm") {
    // Take clicks while asking: the bubble's buttons, or the user's corrected point.
    pointer.setIgnoreMouseEvents(false);
  }
  if (msg.type !== "status") pointer.webContents.send("avo", toPointerWindow(msg));
}

// The renderer's answer to a confirm: back to click-through, then tell the agent
// (with any corrected point converted back to screen coordinates).
ipcMain.on("answer", (_e, answer) => {
  pointer.setIgnoreMouseEvents(true);
  const o = pointer.getBounds();
  const msg = { type: "answer", ...answer };
  if (typeof answer.x === "number") {
    msg.x = answer.x + o.x;
    msg.y = answer.y + o.y;
  }
  const data = JSON.stringify(msg);
  for (const ws of clients) ws.send(data);
});

function startServer() {
  const wss = new WebSocketServer({ host: "127.0.0.1", port: PORT });
  wss.on("connection", (ws) => {
    clients.add(ws);
    ws.on("close", () => clients.delete(ws));
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
  createCaption();
  startServer();
});
