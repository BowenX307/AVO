// AVO Remote, the phone side. Pairs with the one-time code from the QR (?pair=),
// keeps the session token in sessionStorage, and mirrors the Mac's island: the
// same messages (status, confirm, paused, done...) drive the same states here.
// It can only send what overlay/remote.js allows: task, answer, resume, stop.

(() => {
  const $ = (s) => document.querySelector(s);
  const body = document.body;
  const store = {
    get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch {} },
    del: (k) => { try { sessionStorage.removeItem(k); } catch {} },
  };
  const recentKey = "avo-recent";
  let ws = null, token = store.get("avo-token"), retryMs = 500, doneTimer = null, lastTask = "";
  let task = { step: 0, total: 0 };

  function view(name) {
    clearTimeout(doneTimer);
    body.dataset.view = name;
    body.classList.toggle("busy", ["working", "ask", "confirm", "paused"].includes(name));
  }

  // ---- pairing ----
  async function pair(code) {
    try {
      const r = await fetch("/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
      if (!r.ok) throw new Error(await r.text());
      token = (await r.json()).token;
      store.set("avo-token", token);
    } catch (e) {
      $("#pairError").textContent = e.message || "Pairing didn't work. Scan the code again.";
      token = null;
    }
    history.replaceState(null, "", "/"); // the code is spent; don't leave it in the address bar
  }

  // ---- the connection ----
  function connect() {
    if (!token) return view("unpaired");
    ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
    ws.onopen = () => {
      retryMs = 500;
      body.classList.add("live");
      send({ type: "hello" });
    };
    ws.onmessage = (e) => {
      try { handle(JSON.parse(e.data)); } catch {}
    };
    ws.onclose = async (e) => {
      body.classList.remove("live");
      ws = null;
      // Another phone paired (4001), Remote was turned off (4002), or the session
      // ended (4003); or the Mac refused this token (the check below).
      let gone = [4001, 4002, 4003].includes(e.code);
      if (!gone) {
        try {
          gone = (await fetch(`/session?t=${encodeURIComponent(token)}`, { cache: "no-store" })).status === 403;
        } catch {} // unreachable: keep trying
      }
      if (gone) {
        store.del("avo-token");
        token = null;
        $("#pairError").textContent = e.reason || "This phone isn't paired any more.";
        return view("unpaired");
      }
      view("connecting");
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
  }

  function send(msg) {
    if (ws?.readyState === 1) ws.send(JSON.stringify(msg));
  }
  const answer = (fields) => send({ type: "answer", ...fields });

  // ---- what the Mac says ----
  function handle(m) {
    switch (m.type) {
      case "idle":
      case "hide":
        return showIdle();
      case "status":
        return showStatus(m);
      case "confirm":
        return showQuestion(m);
      case "answered":
      case "cancel":
        $("#countdown").hidden = true;
        return showWorking(task.step ? `Step ${task.step}` : "", "Working…");
      case "paused":
        $("#pausedText").textContent = String(m.text || "Paused").replace(/\.$/, "");
        return view("paused");
      case "done":
        return showDone(m);
      case "notice":
        $("#failText").textContent = `${m.title || "AVO needs something on the Mac"}. ${m.text || ""}`;
        return view("failed");
      case "busy":
        $("#failText").textContent = m.text;
        return view("failed");
    }
  }

  function showIdle() {
    task = { step: 0, total: 0 };
    view("idle");
    drawRecent();
  }

  function showWorking(label, text) {
    $("#stepLabel").textContent = label;
    $("#workText").textContent = text;
    $("#barFill").style.width = task.total ? `${(100 * task.step) / task.total}%` : "0%";
    $("#taskEcho").textContent = lastTask ? `“${lastTask}”` : "";
    view("working");
  }

  function showStatus(m) {
    const words = String(m.text || "").replace(/…$/, "");
    if (m.task) lastTask = m.task;
    $("#countdown").hidden = true;
    if (/^Starting/.test(words)) task = { step: 0, total: 0 };
    if (m.step) {
      Object.assign(task, { step: m.step, total: m.total || task.total });
      return showWorking(task.total ? `Step ${task.step} / ${task.total}` : `Step ${task.step}`, words);
    }
    if (m.phase === "planning" || /^Planning/.test(words)) return showWorking("", "Planning…");
    showWorking(task.step ? `Step ${task.step}` : "", words ? `${words}…`.replace(/……$/, "…") : "Working…");
  }

  function choice(label, cls, onClick, number) {
    const b = document.createElement("button");
    b.className = cls;
    b.innerHTML = number ? `<kbd>${number}</kbd><span></span>` : "<span></span>";
    b.querySelector("span").textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function showQuestion(m) {
    if (m.kind === "countdown") {
      // About to do an ordinary step: say what, with a No while it counts down.
      $("#aboutText").textContent = `About to: ${m.text || "the next step"}`;
      const run = $("#runOut");
      run.style.setProperty("--secs", `${m.seconds || 2}s`);
      run.classList.remove("go");
      void run.offsetWidth;
      run.classList.add("go");
      $("#countdown").hidden = false;
      if (body.dataset.view !== "working") showWorking(task.step ? `Step ${task.step}` : "", "Working…");
      return;
    }
    if (m.kind === "confirm") {
      $("#confirmTitle").textContent = m.title || "OK to do this?";
      $("#confirmText").textContent = m.intent || m.reason || "";
      $("#preview").textContent = m.detail || "";
      $("#preview").hidden = !m.detail;
      const verb = (m.title || "").split(/\s+[—-]\s+/)[0].trim();
      $("#confirmYes").textContent = verb && verb.length < 24 && !/flagged/i.test(verb) ? `Yes, ${verb.toLowerCase()}` : "Yes";
      return view("confirm");
    }
    const list = $("#choices");
    list.replaceChildren();
    $("#noneBox").hidden = true;
    const shot = $("#askShot");
    if (m.crop) {
      shot.src = m.crop;
      shot.hidden = false;
    } else {
      shot.removeAttribute("src");
      shot.hidden = true;
    }
    if (m.kind === "safety") {
      $("#askTitle").textContent = "Meta flagged this step";
      $("#askText").textContent = [m.intent, m.reason].filter(Boolean).join(" ");
      list.append(choice("Continue", "primary", () => answer({ choice: "yes" })), choice("Stop the task", "secondary", () => answer({ choice: "stop" })));
    } else if (m.kind === "clarify") {
      $("#askTitle").textContent = m.question || "Which did you mean?";
      $("#askText").textContent = m.intent || "";
      (m.options || []).forEach((o, i) => list.append(choice(o, "primary", () => answer({ choice: "pick", index: i }), i + 1)));
    } else if (m.candidates) {
      $("#askTitle").textContent = "Which one did you mean?";
      $("#askText").textContent = m.intent || "";
      m.candidates.forEach((c, i) => list.append(choice(c.label, "primary", () => answer({ choice: "pick", index: i }), i + 1)));
      list.append(choice("None of these", "secondary", () => {
        $("#noneBox").hidden = false;
        $("#noneText").focus();
      }));
    } else {
      // "OK to click here?" There's no clicking the right spot from the phone: No
      // tells AVO not to do it, and it tries again from what you say.
      $("#askTitle").textContent = "OK to click here?";
      $("#askText").textContent = [m.intent, m.reason].filter(Boolean).join(" ");
      list.append(choice("Yes, click it", "primary", () => answer({ choice: "yes" })), choice("No", "secondary", () => {
        $("#noneBox").hidden = false;
        $("#noneText").focus();
      }));
    }
    view("ask");
  }

  function showDone(m) {
    if (m.ok === false) {
      $("#failText").textContent = m.text || "AVO stopped.";
      $("#retry").hidden = !lastTask || /stopped the task|said no/i.test(m.text || "");
      return view("failed");
    }
    $("#doneText").textContent = m.text || "Done";
    view("done");
    doneTimer = setTimeout(showIdle, 3000);
  }

  // ---- recent tasks (this tab only) ----
  function recent() {
    try { return JSON.parse(store.get(recentKey) || "[]"); } catch { return []; }
  }
  function remember(text) {
    store.set(recentKey, JSON.stringify([text, ...recent().filter((t) => t !== text)].slice(0, 3)));
  }
  function drawRecent() {
    const box = $("#recent");
    box.replaceChildren();
    const items = recent();
    if (!items.length) return;
    const l = document.createElement("p");
    l.className = "label";
    l.textContent = "Again";
    box.append(l, ...items.map((t) => choice(t, "secondary", () => sendTask(t))));
  }
  function sendTask(text) {
    text = text.trim();
    if (!text || ws?.readyState !== 1) return;
    lastTask = text;
    remember(text);
    send({ type: "task", text });
    $("#taskText").value = "";
    task = { step: 0, total: 0 };
    showWorking("", "Starting…");
  }

  // ---- buttons ----
  $("#taskForm").addEventListener("submit", (e) => {
    e.preventDefault();
    sendTask($("#taskText").value);
  });
  $("#taskText").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendTask($("#taskText").value);
    }
  });
  $("#cancelStep").addEventListener("click", () => answer({ choice: "no" }));
  $("#noneSend").addEventListener("click", () => answer({ choice: "none", said: $("#noneText").value.trim() }));
  $("#confirmYes").addEventListener("click", () => answer({ choice: "yes" }));
  $("#confirmNo").addEventListener("click", () => answer({ choice: "no" }));
  $("#resume").addEventListener("click", () => send({ type: "resume" }));
  $("#retry").addEventListener("click", () => lastTask && sendTask(lastTask));
  $("#stop").addEventListener("click", () => send({ type: "stop" }));

  // ---- start ----
  const code = new URLSearchParams(location.search).get("pair");
  (code ? pair(code) : Promise.resolve()).then(connect);
})();
