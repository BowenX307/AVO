// Tests for overlay/remote.js: pairing, tokens, one phone, the whitelist, LAN only.
//   cd overlay && node test-remote.js
// Needs the Mac on a local network (it listens on the LAN address, port 8771).

process.env.AVO_REMOTE_PORT = "8771";
const assert = require("assert");
const { WebSocket } = require("ws");
const { Remote, isPrivate } = require("./remote");

const got = [];
let paired = 0, disconnects = 0;
const remote = new Remote({
  onMessage: (m) => got.push(m),
  onPaired: () => paired++,
  onDisconnect: () => disconnects++,
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
setTimeout(() => (console.error("timed out"), process.exit(2)), 20000);
const post = (base, code) => fetch(`${base}/pair`, { method: "POST", body: JSON.stringify({ code }) });
function open(base, token) {
  return new Promise((resolve) => {
    const ws = new WebSocket(`${base.replace("http", "ws")}/ws?t=${token}`);
    ws.inbox = [];
    ws.on("message", (d) => ws.inbox.push(JSON.parse(d)));
    ws.on("open", () => resolve({ ws, ok: true }));
    ws.on("error", () => resolve({ ws, ok: false }));
  });
}

(async () => {
  // Private ranges only.
  for (const a of ["10.0.0.5", "172.16.1.1", "172.31.255.1", "192.168.1.20", "::ffff:192.168.0.2"]) assert(isPrivate(a), a);
  for (const a of ["8.8.8.8", "172.32.0.1", "127.0.0.1", "::1", "100.64.0.1", ""]) assert(!isPrivate(a), a);

  const { url } = await remote.start();
  const base = url.split("/?")[0];
  const code = new URL(url).searchParams.get("pair");

  // The page is served; files outside the list are not.
  assert.strictEqual((await fetch(`${base}/`)).status, 200);
  assert.strictEqual((await fetch(`${base}/brand/tokens.css`)).status, 200);
  assert.strictEqual((await fetch(`${base}/../.env`)).status, 404);
  assert.strictEqual((await fetch(`${base}/overlay/main.js`)).status, 404);

  // A wrong code fails; the right one works once.
  assert.strictEqual((await post(base, "nope")).status, 403);
  const r1 = await post(base, code);
  assert.strictEqual(r1.status, 200);
  const { token: t1 } = await r1.json();
  assert.strictEqual((await post(base, code)).status, 403, "a pairing code works only once");
  assert.strictEqual(paired, 1);

  // No token, or a wrong one: refused.
  assert.strictEqual((await open(base, "")).ok, false);
  assert.strictEqual((await open(base, "x".repeat(48))).ok, false);
  assert.strictEqual((await fetch(`${base}/session?t=${t1}`)).status, 204);

  // With the token: only whitelisted messages get through.
  const a = await open(base, t1);
  assert(a.ok);
  a.ws.send(JSON.stringify({ type: "task", text: "hi" }));
  a.ws.send(JSON.stringify({ type: "exec", cmd: "rm -rf ~" }));
  a.ws.send(JSON.stringify({ type: "read-file", path: "/etc/passwd" }));
  await wait(150);
  assert.deepStrictEqual(got.map((m) => m.type), ["task"]);

  // A second phone pairs with the new code: the first is closed (4001), its token dead.
  const code2 = new URL(remote.url()).searchParams.get("pair");
  assert.notStrictEqual(code2, code);
  const closed = new Promise((r) => a.ws.on("close", (c) => r(c)));
  const { token: t2 } = await (await post(base, code2)).json();
  assert.strictEqual(await closed, 4001);
  assert.strictEqual((await open(base, t1)).ok, false, "the old phone's token no longer works");
  assert.strictEqual((await fetch(`${base}/session?t=${t1}`)).status, 403);
  const b = await open(base, t2);
  assert(b.ok);
  assert.strictEqual(paired, 2);

  // The phone gets what the Mac sends, and the last state again when it reconnects.
  remote.send({ type: "status", text: "Planning…" });
  await wait(150);
  assert.strictEqual(b.ws.inbox.at(-1).text, "Planning…");
  b.ws.close();
  await wait(150);
  assert(disconnects >= 1);
  const c = await open(base, t2);
  await wait(150);
  assert.strictEqual(c.ws.inbox[0]?.text, "Planning…");

  // A same-length non-ASCII token must be rejected, not crash timingSafeEqual.
  assert.strictEqual(remote.valid("é".repeat(t2.length)), false);

  // Turning Remote off ends the session.
  const off = new Promise((r) => c.ws.on("close", (code) => r(code)));
  remote.stop();
  assert.strictEqual(await off, 4002);
  console.log("remote: all tests passed");
  process.exit(0);
})().catch((e) => {
  console.error(e);
  remote.stop();
  process.exit(1);
});
