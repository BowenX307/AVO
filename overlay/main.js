// AVO overlay: a transparent, always-on-top, click-through window over the
// main display. The Python agent connects over a local WebSocket and sends
// events (cursor moves, clicks, keys, status text); we draw them.

const path = require("path");
const { app, BrowserWindow, screen } = require("electron");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.AVO_OVERLAY_PORT || 8765);
// By default the overlay is hidden from screen capture so the model never
// sees our own cursor in its screenshots. Set to 1 to capture it anyway.
const VISIBLE_IN_CAPTURE = process.env.AVO_OVERLAY_VISIBLE_IN_CAPTURE === "1";

let win = null;

function createWindow() {
  const { bounds } = screen.getPrimaryDisplay();
  win = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    movable: false,
    focusable: false, // never takes keyboard focus from the app below
    skipTaskbar: true,
    enableLargerThanScreen: true,
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true);
  win.setContentProtection(!VISIBLE_IN_CAPTURE);
  win.setBounds(bounds); // macOS may push a new window below the menu bar
  win.loadFile("index.html");
  win.showInactive();
}

// Events carry screen point coordinates; the renderer wants window coordinates.
function toWindow(msg) {
  const origin = win.getBounds();
  const shift = (p) => ({ ...p, x: p.x - origin.x, y: p.y - origin.y });
  if (typeof msg.x === "number" && typeof msg.y === "number") msg = shift(msg);
  return msg;
}

function startServer() {
  const wss = new WebSocketServer({ host: "127.0.0.1", port: PORT });
  wss.on("connection", (ws) => {
    ws.on("message", (data) => {
      let msg;
      try {
        msg = JSON.parse(data.toString());
      } catch {
        return;
      }
      if (win) win.webContents.send("avo", toWindow(msg));
    });
  });
  console.log(`AVO overlay listening on ws://127.0.0.1:${PORT}`);
}

app.dock?.hide(); // no Dock icon, not in Cmd-Tab
app.whenReady().then(() => {
  createWindow();
  startServer();
});
