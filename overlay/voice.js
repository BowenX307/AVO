// Voice input: Meta's realtime speech recognition (muse-voice-transcribe-1.0).
// The island page records the microphone (24 kHz, 16-bit mono PCM) and the
// main process streams it here. ENDPOINTING mode: the model decides when a
// sentence is over and sends it as speechComplete.
// Keywords (names on screen, the candidates of a question) bias recognition;
// they're fixed per session, so a new question opens a new session.

const fs = require("fs");
const path = require("path");
const WebSocket = require("ws");

const URL = "wss://api.meta.ai/v1/asr/realtime";
const MODEL = "muse-voice-transcribe-1.0";
// Meta rejects the whole session if any keyword is longer than this (not in the
// docs; 10/31: "ASR keyword 16 exceeds the maximum length of 30 characters").
const MAX_KEYWORD = 30;
// Always among the keywords: what people say to AVO mid-task.
const CONTROL_WORDS = ["AVO", "stop", "yes", "no", "one", "two", "three", "four", "none of these", "resume", "cancel"];

function envValue(root, name) {
  if (process.env[name]) return process.env[name];
  try {
    const source = fs.readFileSync(path.join(root, ".env"), "utf8");
    const raw = new RegExp("^\\s*" + name + "\\s*=\\s*(.*?)\\s*$", "m").exec(source)?.[1];
    if (!raw) return null;
    if (raw.startsWith('"')) return JSON.parse(raw);
    return raw.replace(/^'|'$/g, "");
  } catch { return null; }
}
function apiKey(root) { return envValue(root, "AVO_TRIAL_TOKEN") || envValue(root, "MODEL_API_KEY"); }
function apiURL(root) { return envValue(root, "AVO_TRIAL_TOKEN") ? "wss://avo-reviewer-gateway.springycairn2.chatgpt.site/v1/asr/realtime" : URL; }

// One recognition session. Audio sent before the socket opens is buffered.
class Session {
  constructor({ key, url = URL, keywords = [], onPartial, onFinal, onClose, onError }) {
    this.queue = [];
    this.closed = false;
    this.ws = new WebSocket(url, url === URL ? {} : { headers: { Authorization: `Bearer ${key}` } });
    this.ws.on("open", () => {
      this.ws.send(
        JSON.stringify({
          authorization: { accessToken: `Bearer ${key}` },
          audioEncoding: "PCM_24KHZ",
          model: MODEL,
          mode: "ENDPOINTING",
          emitAudioProgress: false,
          languageBias: ["English"], // a hint: Mandarin still comes through (code-switching)
          keywords: [...new Set([...CONTROL_WORDS, ...keywords])].filter((k) => k && k.length <= MAX_KEYWORD).slice(0, 120),
        }),
      );
      for (const chunk of this.queue) this.ws.send(chunk);
      this.queue = [];
    });
    this.ws.on("message", (data) => {
      let m;
      try {
        m = JSON.parse(data.toString());
      } catch {
        return;
      }
      if (m.type === "transcript" && m.transcript) onPartial?.(m.transcript);
      else if (m.type === "speechComplete") onFinal?.(String(m.transcript || "").trim());
      else if (m.type === "error") onError?.(m.message || "speech recognition error");
    });
    this.ws.on("close", (code, reason) => {
      this.closed = true;
      onClose?.(code, reason.toString());
    });
    this.ws.on("error", (e) => onError?.(e.message));
  }

  send(pcm) {
    if (this.closed) return;
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(pcm);
    else if (this.ws.readyState === WebSocket.CONNECTING) this.queue.push(pcm);
  }

  end() {
    // Half-close: no more audio, but results still in flight arrive.
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "endStream" }));
  }

  close() {
    this.closed = true;
    try {
      this.ws.close();
    } catch {}
  }
}

// The part of each option that tells it apart, as keywords for the answer:
// "Software Engineer Intern, Systems and Infrastructure (PhD)" and "…, Machine
// Learning (PhD)" -> "Systems and Infrastructure", "Machine Learning". A part
// still too long becomes its words.
const STOP = new Set(["the", "and", "for", "with", "into", "from", "that", "this", "your", "you"]);

function distinct(labels = []) {
  const toks = labels.map((l) => String(l).split(/\s+/).filter(Boolean));
  if (!toks.length) return [];
  const same = (i, at) => toks.every((t) => t[at(t, i)] !== undefined && t[at(t, i)].toLowerCase() === toks[0][at(toks[0], i)].toLowerCase());
  let pre = 0;
  while (toks.length > 1 && pre < Math.min(...toks.map((t) => t.length)) - 1 && same(pre, (t, i) => i)) pre++;
  let suf = 0;
  while (toks.length > 1 && suf < Math.min(...toks.map((t) => t.length)) - pre - 1 && same(suf, (t, i) => t.length - 1 - i)) suf++;
  const out = [];
  for (const t of toks) {
    const part = t.slice(pre, t.length - suf).join(" ").replace(/^[\s,;:–—-]+|[\s,;:–—-]+$/g, "");
    if (part.length <= MAX_KEYWORD) out.push(part);
    else out.push(...part.split(/[\s,;:()]+/).filter((w) => w.length >= 3 && w.length <= MAX_KEYWORD && !STOP.has(w.toLowerCase())));
  }
  return [...new Set(out.filter(Boolean))];
}

// ---- understanding a short answer ----

const NUMBER_WORDS = {
  one: 0, first: 0, won: 0, "1": 0, "1st": 0,
  two: 1, second: 1, "2": 1, "2nd": 1, too: 1,
  three: 2, third: 2, "3": 2, "3rd": 2,
  four: 3, fourth: 3, "4": 3, "4th": 3,
  five: 4, fifth: 4, "5": 4, "5th": 4,
};
const CHINESE_NUMBERS = { 一: 0, 二: 1, 两: 1, 三: 2, 四: 3, 五: 4 };
const FILLER = new Set(["the", "a", "an", "one", "that", "this", "please", "prof", "professor", "dr", "mr", "ms", "mrs", "no", "yes", "i", "mean", "meant"]);

const words = (s) => String(s).toLowerCase().match(/[\p{L}\p{N}']+/gu) || [];

// Which of `labels` the words name, e.g. "the physics one" -> "Prof. David Smith (Physics 212)"; -1 if none or a tie.
// Two words match if they're the same, or share their first 5 letters ("physical" said for
// "Physics", 11/01).
const sameWord = (a, b) => a === b || (a.length >= 5 && b.length >= 5 && a.slice(0, 5) === b.slice(0, 5));

function labelIndex(text, labels = []) {
  const said = words(text).filter((w) => !FILLER.has(w));
  const scores = labels.map((l) => words(l).filter((w) => !FILLER.has(w) && said.some((s) => sameWord(s, w))).length);
  const best = Math.max(0, ...scores);
  return best > 0 && scores.filter((s) => s === best).length === 1 ? scores.indexOf(best) : -1;
}

// What a short utterance says: { stop, yes, no, none, resume, index } (index: a number or ordinal said).
function understand(text) {
  const w = words(text);
  const t = w.join(" ");
  const out = { text };
  if (/\b(stop|abort)\b/.test(t) || /停/.test(text)) out.stop = true;
  if (/^(none|neither)\b|\bnone of (these|them|those)\b|都不是|都不对/.test(t + text)) out.none = true;
  if (/^(yes|yeah|yep|yup|sure|ok|okay|correct|right|go|go ahead|do it|send it|send)\b/.test(t) || /^(是|好|对|可以|确定)/.test(text)) out.yes = true;
  if (/^(no|nope|nah|don't|do not|wait|cancel|not that|wrong)\b/.test(t) || /^(不|别|等)/.test(text)) out.no = true;
  if (/\b(resume|continue|go on|keep going|carry on)\b/.test(t) || /继续/.test(text)) out.resume = true;
  if (w.length <= 4) {
    // "one" is usually a pronoun ("the physics one"): a number only when said on its own.
    const core = w.filter((x) => !["the", "number", "option", "choice"].includes(x));
    const hit = core.find((x) => x in NUMBER_WORDS && (x !== "one" || core.length === 1));
    if (hit) out.index = NUMBER_WORDS[hit];
  }
  const zh = /第?([一二两三四五])个/.exec(text) || /^第([一二两三四五])/.exec(text);
  if (out.index === undefined && zh) out.index = CHINESE_NUMBERS[zh[1]];
  return out;
}

module.exports = { apiURL, Session, apiKey, understand, labelIndex, distinct, CONTROL_WORDS, MAX_KEYWORD };
