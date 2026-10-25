// Runs the real console/web/js/voice.js (a plain browser script) in a Node vm
// with fake browser APIs and the real speech controller, so no DOM is needed.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { makeController, tick, FakeFetch } from "./fakes.mjs";

const ROOT = new URL("../../", import.meta.url);
const VOICE_SRC = readFileSync(new URL("console/web/js/voice.js", ROOT), "utf8");

// autoStop=false: a recorder's stop() finishes only when the test calls flush(),
// like a real MediaRecorder whose onstop fires later.
function loadVoice({ autoStop = true } = {}) {
  const h = makeController();
  const log = [];
  const recorders = [];
  const stt = new FakeFetch(); // the page's own fetch (POST /stt)
  const newTurn = h.ctl.newTurn;
  h.ctl.newTurn = () => { log.push("newTurn"); return newTurn(); };
  class FakeRecorder {
    static isTypeSupported(t) { return t === "audio/mp4"; }
    constructor(stream, opts) { this.mimeType = opts && opts.mimeType; this.id = recorders.length; recorders.push(this); }
    start() { log.push("rec.start"); }
    stop() { log.push("rec.stop"); if (autoStop) this.flush(); }
    flush() { this.ondataavailable({ data: { size: 4000, from: this.id } }); this.onstop(); }
  }
  class FakeFormData {
    constructor() { this.fields = {}; }
    append(k, v) { this.fields[k] = v; }
  }
  const win = {
    SPEECH: h.ctl,
    isSecureContext: true,
    navigator: { mediaDevices: { getUserMedia: async () => { log.push("getUserMedia"); return {}; } } },
    MediaRecorder: FakeRecorder,
    FX: { ctx: () => null },
    Blob: class { constructor(parts, o) { this.parts = parts; this.size = 4000; this.type = o.type; } },
    FormData: FakeFormData,
    fetch: stt.fn,
    cancelAnimationFrame() {},
    requestAnimationFrame() {},
    encodeURIComponent,
  };
  win.window = win;
  vm.runInNewContext(VOICE_SRC, win);
  return { V: win.VOICE, h, log, recorders, stt };
}

test("push-to-talk start silences the robot before the mic opens", async () => {
  const { V, h, log } = loadVoice();
  V.say("talking");
  assert.equal(h.fetch.calls.length, 1, "robot is mid-line");

  const before = h.ctl.current();
  await V.startRec(null);
  assert.deepEqual(log, ["newTurn", "getUserMedia", "rec.start"]);
  assert.equal(h.ctl.current(), before + 1);
  assert.equal(h.fetch.calls[0].signal.aborted, true, "its /tts download was dropped");
  assert.equal(h.synth.cancelCalls, 1);
});

test("speech is held while the user talks, then plays in the current turn", async () => {
  const { V, h } = loadVoice();
  await V.startRec(null);
  V.say("from an older turn");
  assert.equal(h.fetch.calls.length, 0, "nothing plays over the user");

  await V.stopRec();
  V.say("reply");
  assert.equal(h.fetch.calls.length, 1);
  assert.equal(h.fetch.calls[0].url, "/tts?text=reply");
  h.fetch.respond(0);
  await tick();
  h.audios[0].resolvePlay();
  await tick();
  assert.equal(h.audios[0].paused, false);
});

// Stands in for app.js runCommand: POST /commands tracked with the given turn.
function commandRunner(V) {
  const runs = [];
  const settles = [];
  const run = (text, turn) => {
    runs.push([text, turn]);
    return V.track(new Promise((r) => settles.push(r)), turn);
  };
  return { run, runs, settles };
}
const heard = (text) => ({ json: { ok: true, text } });

test("a voice command runs in its own turn; its reply is dropped once the user barges in", async () => {
  const { V, h, stt } = loadVoice();
  const cmd = commandRunner(V);
  await V.startRec(null);
  const turn1 = V.turn();
  const sent = V.voiceCommand(await V.stopRec(), cmd.run);
  assert.equal(stt.calls[0].url, "/stt");
  assert.equal(stt.calls[0].body.fields.auto, "0", "transcript only: the server must not run it");
  stt.respond(0, heard("prep the bench"));
  assert.equal((await sent).status, "sent");
  assert.deepEqual(cmd.runs, [["prep the bench", turn1]]);

  await V.startRec(null);                        // the user barges in
  await V.stopRec();
  V.say("turn 1's plan is ready");               // turn 1's line lands after the barge-in
  assert.equal(h.fetch.calls.length, 0);
  assert.equal(h.synth.spoken.length, 0);

  cmd.settles[0]();                              // turn 1 is over; later lines play
  await tick();
  h.clock.t += 5000;
  V.say("turn 2's reply");
  assert.equal(h.fetch.calls.length, 1, "the dropped line did not wedge the queue");
  assert.equal(h.fetch.calls[0].url, "/tts?text=turn%202's%20reply");
});

test("a transcript that comes back after a newer press is dropped, never run", async () => {
  const { V, h, stt } = loadVoice();
  const cmd = commandRunner(V);
  await V.startRec(null);                        // turn A
  const pendingA = V.voiceCommand(await V.stopRec(), cmd.run);
  await V.startRec(null);                        // turn B starts while A is transcribing
  const turnB = V.turn();
  stt.respond(0, heard("drive forward 20 cm"));  // A's transcript arrives late
  assert.equal((await pendingA).status, "dropped");
  assert.deepEqual(cmd.runs, [], "no /commands request for A");
  assert.equal(h.fetch.calls.length, 0, "nothing plays");
  assert.equal(h.synth.spoken.length, 0);

  const pendingB = V.voiceCommand(await V.stopRec(), cmd.run);
  stt.respond(1, heard("stop"));
  assert.equal((await pendingB).status, "sent");
  assert.deepEqual(cmd.runs, [["stop", turnB]]);
});

test("a late failed or broken STT reply from an older turn is dropped silently", async () => {
  const { V, stt } = loadVoice();
  const cmd = commandRunner(V);
  await V.startRec(null);                                    // turn A
  const failedA = V.voiceCommand(await V.stopRec(), cmd.run);
  await V.startRec(null);                                    // turn B
  const brokenB = V.voiceCommand(await V.stopRec(), cmd.run);
  await V.startRec(null);                                    // turn C is recording
  stt.respond(0, { ok: false, json: { ok: false, reason: "whisper failed" } });
  stt.calls[1].reject(new Error("network down"));
  assert.equal((await failedA).status, "dropped", "no 'STT offline' over turn C");
  assert.equal((await brokenB).status, "dropped", "no 'STT failed' toast over turn C");
  assert.deepEqual(cmd.runs, []);
});

test("the current turn's STT failures are still reported", async () => {
  const { V, stt } = loadVoice();
  const cmd = commandRunner(V);
  await V.startRec(null);
  const a = await V.stopRec();
  const failed = V.voiceCommand(a, cmd.run);
  stt.respond(0, { ok: false, json: { ok: false, reason: "whisper failed" } });
  const v = await failed;
  assert.equal(v.status, "unavailable");
  assert.equal(v.reason, "whisper failed");
  const broken = V.voiceCommand(a, cmd.run);
  stt.calls[1].reject(new Error("network down"));
  await assert.rejects(broken, /network down/);
  assert.deepEqual(cmd.runs, []);
});

test("a recording still stopping when the next press lands keeps its own turn and audio", async () => {
  const { V, h, recorders, stt } = loadVoice({ autoStop: false });
  const cmd = commandRunner(V);
  await V.startRec(null);                        // turn A
  const turnA = V.turn();
  const stoppingA = V.stopRec();                 // release A; its recorder stops later
  await V.startRec(null);                        // press B before A's stop completes
  const turnB = V.turn();
  recorders[0].flush();                          // A's recorder finally stops
  const a = await stoppingA;
  assert.equal(a.turn, turnA);
  assert.deepEqual([...a.blob.parts].map((p) => p.from), [0], "A's audio only");

  const pendingA = V.voiceCommand(a, cmd.run);   // A's upload goes out during turn B
  const stoppingB = V.stopRec();                 // release B
  recorders[1].flush();
  const b = await stoppingB;
  assert.equal(b.turn, turnB);
  assert.deepEqual([...b.blob.parts].map((p) => p.from), [1], "B's audio only");

  stt.respond(0, heard("bring it over there"));
  assert.equal((await pendingA).status, "dropped");
  assert.deepEqual(cmd.runs, []);
  assert.equal(h.fetch.calls.length, 0);
});

test("Piper refusing to play falls back to on-device speech in the same turn", async () => {
  const { V, h } = loadVoice();
  V.say("hello");
  h.fetch.respond(0, { ok: false });
  await tick();
  assert.equal(h.synth.spoken.length, 1);
  assert.equal(h.synth.spoken[0].text, "hello");
  h.synth.spoken[0].onend();
  await tick();
  assert.equal(V.speaking, false);
});

test("index.html loads the controller before voice.js and app.js", () => {
  const html = readFileSync(new URL("console/web/index.html", ROOT), "utf8");
  const tags = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].map(([, attrs, body]) => ({ attrs, body }));
  const ctl = tags.findIndex((t) => /type="module"/.test(t.attrs) && /speech_epoch\.mjs/.test(t.body));
  const voice = tags.findIndex((t) => /voice\.js/.test(t.attrs));
  const app = tags.findIndex((t) => /app\.js/.test(t.attrs));
  assert.ok(ctl >= 0 && ctl < voice && voice < app, "order: controller, voice.js, app.js");
  // A classic script without defer would run before the module and find no window.SPEECH.
  assert.match(tags[voice].attrs, /\bdefer\b/);
  assert.match(tags[app].attrs, /\bdefer\b/);
});
