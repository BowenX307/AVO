// AVO Remote: a phone on the same Wi-Fi drives AVO through a web page.
//
// Off by default; main.js turns it on (Control-Option-R). Then this serves
// remote/ over HTTP on the Mac's local network address (port 8770) and one
// WebSocket. Pairing: the island shows a QR code with a one-time code; the
// phone trades it for a session token (12 hours, or until Remote is turned
// off), and every WebSocket connection must carry the token. One phone at a
// time: a new pairing disconnects the old one. Only private-network addresses
// (10.x, 172.16-31.x, 192.168.x) may connect at all.
//
// The phone can do only what's on WHITELIST: send a task, answer the question
// on screen, confirm or cancel, resume, stop. Nothing here runs commands,
// reads files, or sends the screen: the only image a phone gets is the crop the
// agent attaches to a question (avo/remote.py).

const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const QRCode = require("qrcode");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.AVO_REMOTE_PORT || 8770);
const TOKEN_TTL_MS = 12 * 60 * 60 * 1000;
const ROOT = path.resolve(__dirname, "..");
const STATIC = { "/": "remote/index.html", "/app.js": "remote/app.js", "/remote.css": "remote/remote.css" };
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml" };
const WHITELIST = new Set(["task", "answer", "resume", "stop", "hello"]);

// 10.x, 172.16-31.x, 192.168.x (IPv4, or IPv4 mapped into IPv6).
function isPrivate(address) {
  const a = String(address || "").replace(/^::ffff:/, "");
  const m = a.match(/^(\d+)\.(\d+)\.\d+\.\d+$/);
  if (!m) return false;
  const [x, y] = [Number(m[1]), Number(m[2])];
  return x === 10 || (x === 172 && y >= 16 && y <= 31) || (x === 192 && y === 168);
}

// The Mac's address on the local network (Wi-Fi or Ethernet), or null.
function lanAddress() {
  for (const list of Object.values(os.networkInterfaces())) {
    for (const i of list || []) if (i.family === "IPv4" && !i.internal && isPrivate(i.address)) return i.address;
  }
  return null;
}

const code = () => crypto.randomBytes(5).toString("hex"); // 10 hex characters

class Remote {
  // hooks: { onMessage(msg), onPaired(replaced), onDisconnect(), log(line) }
  constructor(hooks) {
    this.hooks = hooks;
    this.server = null;
    this.wss = null;
    this.phone = null; // the one connected socket
    this.session = null; // { token, until }
    this.pairCode = null;
    this.address = null;
    this.last = null; // the last state sent, replayed when the phone (re)connects
  }

  get on() {
    return !!this.server;
  }

  get connected() {
    return !!this.phone;
  }

  async start() {
    if (this.server) return this.info();
    this.address = lanAddress();
    if (!this.address) throw new Error("No local network: connect the Mac to Wi-Fi first");
    this.pairCode = code();
    this.server = http.createServer((req, res) => this.http(req, res));
    this.wss = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 });
    this.server.on("upgrade", (req, socket, head) => this.upgrade(req, socket, head));
    await new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(PORT, this.address, resolve);
    });
    this.hooks.log?.(`remote on at http://${this.address}:${PORT}`);
    return this.info();
  }

  stop() {
    this.phone?.close(4002, "Remote turned off");
    this.phone = null;
    this.session = null; // the token dies with it
    this.pairCode = null;
    this.wss?.close();
    this.server?.close();
    this.server = this.wss = null;
    this.hooks.log?.("remote off");
  }

  url() {
    return `http://${this.address}:${PORT}/?pair=${this.pairCode}`;
  }

  async info() {
    return { url: this.url(), qr: await QRCode.toDataURL(this.url(), { margin: 1, width: 360 }) };
  }

  refuse(res, code, why) {
    res.writeHead(code, { "Content-Type": "text/plain" });
    res.end(why);
  }

  http(req, res) {
    if (!isPrivate(req.socket.remoteAddress)) return this.refuse(res, 403, "local network only");
    const url = new URL(req.url, "http://x");
    if (req.method === "POST" && url.pathname === "/pair") return this.pair(req, res);
    if (url.pathname === "/session") return this.refuse(res, this.valid(url.searchParams.get("t")) ? 204 : 403, "");
    let file = STATIC[url.pathname];
    if (!file && /^\/brand\/(tokens\.css|fonts\/[\w.-]+\.woff2|logo\.svg)$/.test(url.pathname)) file = url.pathname.slice(1);
    if (!file) return this.refuse(res, 404, "not found");
    fs.readFile(path.join(ROOT, file), (err, data) => {
      if (err) return this.refuse(res, 404, "not found");
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-store" });
      res.end(data);
    });
  }

  // POST /pair {code}: a one-time code for a session token. A used code is gone;
  // the island shows a fresh one for the next phone.
  pair(req, res) {
    let body = "";
    req.on("data", (c) => (body += c).length > 1000 && req.destroy());
    req.on("end", async () => {
      let sent;
      try {
        sent = JSON.parse(body).code;
      } catch {
        sent = null;
      }
      if (!this.pairCode || typeof sent !== "string" || sent !== this.pairCode) {
        this.hooks.log?.(`remote: pairing refused from ${req.socket.remoteAddress}`);
        return this.refuse(res, 403, "This code was already used or has expired. Scan the code on your Mac again.");
      }
      const replaced = !!this.session;
      this.phone?.close(4001, "Another phone paired");
      this.phone = null;
      this.session = { token: crypto.randomBytes(24).toString("hex"), until: Date.now() + TOKEN_TTL_MS };
      this.pairCode = code();
      this.hooks.log?.(`remote: paired ${req.socket.remoteAddress}${replaced ? " (replaced the previous phone)" : ""}`);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ token: this.session.token }));
      this.hooks.onPaired?.(replaced, await this.info());
    });
  }

  valid(token) {
    const s = this.session;
    if (!s || typeof token !== "string" || token.length !== s.token.length) return false;
    if (Date.now() > s.until) {
      this.session = null;
      return false;
    }
    const supplied = Buffer.from(token);
    const expected = Buffer.from(s.token);
    return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
  }

  upgrade(req, socket, head) {
    const url = new URL(req.url, "http://x");
    if (url.pathname !== "/ws" || !isPrivate(req.socket.remoteAddress) || !this.valid(url.searchParams.get("t"))) {
      this.hooks.log?.(`remote: connection refused from ${req.socket.remoteAddress}`);
      socket.write("HTTP/1.1 403 Forbidden\r\n\r\n");
      return socket.destroy();
    }
    this.wss.handleUpgrade(req, socket, head, (ws) => {
      this.phone?.close(4001, "Another connection took over");
      this.phone = ws;
      this.hooks.log?.("remote: phone connected");
      if (this.last) ws.send(JSON.stringify(this.last));
      ws.on("message", (data) => {
        let msg;
        try {
          msg = JSON.parse(data.toString());
        } catch {
          return;
        }
        if (!this.valid(url.searchParams.get("t"))) return ws.close(4003, "Session expired");
        if (!msg || !WHITELIST.has(msg.type)) return; // nothing else is possible from the phone
        this.hooks.log?.(`remote: ${msg.type}${msg.choice ? " " + msg.choice : ""}`);
        this.hooks.onMessage?.(msg);
      });
      ws.on("close", () => {
        if (this.phone !== ws) return;
        this.phone = null;
        this.hooks.log?.("remote: phone disconnected");
        this.hooks.onDisconnect?.();
      });
    });
  }

  // To the phone. `keep`: replay it when the phone reconnects (the state, not one-off events).
  send(msg, keep = true) {
    if (keep) this.last = msg;
    if (this.phone?.readyState === 1) this.phone.send(JSON.stringify(msg));
  }
}

module.exports = { Remote, isPrivate, lanAddress };
