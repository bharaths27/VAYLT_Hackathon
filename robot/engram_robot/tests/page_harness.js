// Runs the robot voice page's own script (voice/static/index.html) with a small stand-in for the browser, in brain
// mode, and prints as JSON what the page says out loud, what it shows, and what it asks the brain, per scenario.
// usage: node page_harness.js <path to index.html>
"use strict";
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("<script>");
const end = html.lastIndexOf("</script>");
if (start < 0 || end < start) throw new Error("no script in the page");
const code = html.slice(start + "<script>".length, end);

function element() {
  const classes = new Set();
  return {
    textContent: "", innerHTML: "", value: "", src: "", className: "",
    classList: {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    addEventListener() {},
  };
}
const elements = {};
const document = {
  getElementById: (id) => (elements[id] = elements[id] || element()),
  querySelector: () => element(),
};

const spoken = [];
const asked = [];
let brainReply = {};
const routes = {
  "/api/config": () => ({ pin_required: false, speech_ready: true, missing: [], mode: "brain" }),
  "/api/state": () => ({ busy: null, safety: { estop_latched: false, lidar_min_front_m: null }, driver: {} }),
  "/api/brain/command": (body) => { asked.push(body.text); return brainReply; },
};
async function fetch(path, opts) {
  const body = opts && typeof opts.body === "string" ? JSON.parse(opts.body) : undefined;
  const data = (routes[path] || (() => ({})))(body);
  return { ok: true, statusText: "OK", json: async () => data };
}
class SpeechSynthesisUtterance { constructor(text) { this.text = text; } }
const speechSynthesis = { cancel() {}, speak(u) { spoken.push(u.text); } };

const context = vm.createContext({
  document, fetch, SpeechSynthesisUtterance, speechSynthesis, console,
  FormData: class FormData {}, setInterval: () => 0, setTimeout: () => 0,
  navigator: {}, prompt: () => null, confirm: () => false, alert: () => {},
});
vm.runInContext(code, context);

async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
}

async function scenario(run) {
  const result = document.getElementById("result");
  result.innerHTML = ""; result.textContent = "";
  spoken.length = 0; asked.length = 0;
  await run();
  await settle();
  return { spoken: [...spoken], asked: [...asked], html: result.innerHTML, text: result.textContent };
}

(async () => {
  await settle();   // the page's start: /api/config puts it in brain mode
  const unclear = { kind: "proposal", status: "refused", reason: "I don't know which object you mean.",
                    classification: "unclear", needs_clarification: true };
  const out = {};
  brainReply = { ...unclear, stopped: true, reply: "I stopped. I don't know which object you mean. Please clarify." };
  out.stopped = await scenario(() => context.askBrain("bring the thing"));
  brainReply = { ...unclear, stopped: false, reply: "I don't know which object you mean. Please clarify." };
  out.clarify = await scenario(() => context.askBrain("bring the thing"));
  brainReply = { kind: "proposal", status: "refused", classification: "understood", needs_clarification: false,
                 reason: "I will not do that: 'throw' is not something this robot is allowed to do." };
  out.refused = await scenario(() => context.askBrain("throw the beaker"));
  brainReply = { kind: "answer", classification: "understood", needs_clarification: false,
                 reply: "Sample B was last seen on shelf A." };
  out.answer = await scenario(() => context.askBrain("where is sample b"));
  out.misheard = await scenario(() => context.showIntent("[BLANK_AUDIO]",
    { op: null, say: "I stopped. I didn't catch anything. Please clarify.", clarify: true, stopped: true }));
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
