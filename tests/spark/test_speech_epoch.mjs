// node --test tests/spark/test_*.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { makeController, tick } from "./fakes.mjs";

// Start current-turn audio and let play() resolve: returns the sounding element.
async function playing(h, epoch) {
  const done = h.ctl.playBlob(epoch, { fakeBlob: "wav" });
  const a = h.audios.at(-1);
  a.resolvePlay();
  await tick();
  return { a, done };
}

test("newTurn bumps the epoch, cancels speech synthesis, pauses and detaches the audio", async () => {
  const h = makeController();
  const e1 = h.ctl.newTurn();
  const { a, done } = await playing(h, e1);
  assert.equal(a.paused, false);

  const e2 = h.ctl.newTurn();
  assert.equal(e2, e1 + 1);
  assert.equal(h.ctl.current(), e2);
  assert.equal(h.synth.cancelCalls, 2);
  assert.equal(a.paused, true);
  assert.equal(a.srcRemoved, true);
  assert.equal(a.loadCalls, 1);
  assert.equal(await done, "stale");
  assert.equal(h.revoked.length, 1, "blob URL released");

  h.ctl.newTurn(); // detached: a later turn does not touch it again
  assert.equal(a.pauseCalls, 1);
});

test("a /tts reply that arrives after newTurn never plays", async () => {
  const h = makeController({ fetch: { honorAbort: false } });
  const e1 = h.ctl.newTurn();
  const p = h.ctl.playUrl(e1, "/tts?text=old");
  h.ctl.newTurn();
  h.fetch.respond(0); // the old reply lands anyway
  await tick();
  assert.equal(h.audios.length, 0, "no Audio element was created");
  assert.equal(await p, "stale");
});

test("newTurn aborts every in-flight /tts fetch from older turns", async () => {
  const h = makeController();
  const e1 = h.ctl.newTurn();
  const p1 = h.ctl.playUrl(e1, "/tts?text=a");
  h.ctl.stop(); // stop also kills the in-flight download
  assert.equal(h.fetch.calls[0].signal.aborted, true);
  assert.equal(await p1, "stale", "an abort is not a Piper failure");

  const p2 = h.ctl.playUrl(e1, "/tts?text=b");
  const p3 = h.ctl.playUrl(e1, "/tts?text=c");
  h.ctl.newTurn();
  assert.equal(h.fetch.calls[1].signal.aborted, true);
  assert.equal(h.fetch.calls[2].signal.aborted, true);
  assert.deepEqual([await p2, await p3], ["stale", "stale"]);
  assert.equal(h.audios.length, 0);
});

test("a late play() from an old turn is paused in the very event it starts", async () => {
  const h = makeController();
  const e1 = h.ctl.newTurn();
  const done = h.ctl.playBlob(e1, { fakeBlob: "old" });
  const old = h.audios[0];
  h.ctl.newTurn();
  assert.equal(old.paused, true);
  old.resolvePlay(); // the browser starts it after the pause: 'playing' fires
  assert.equal(old.pausedInOnset, true, "paused synchronously at onset, not a tick later");
  await tick();
  assert.equal(old.paused, true, "late play() was silenced");
  assert.equal(old.srcRemoved, true);
  assert.equal(await done, "stale");
});

test("a late play() that resolves with no 'playing' event is still silenced", async () => {
  const h = makeController();
  const e1 = h.ctl.newTurn();
  h.ctl.playBlob(e1, { fakeBlob: "old" });
  const old = h.audios[0];
  h.ctl.newTurn();
  old.resolvePlay({ events: false });
  await tick();
  assert.equal(old.paused, true);
});

test("an audio that ended normally is paused if it ever starts again", async () => {
  const h = makeController();
  const e = h.ctl.newTurn();
  const { a, done } = await playing(h, e);
  assert.equal(a.pausedInOnset, false, "current-turn audio is not paused at onset");
  a.finishPlaying();
  assert.equal(await done, "ended");
  a.play();
  a.resolvePlay();
  assert.equal(a.pausedInOnset, true);
});

test("a late play() from an old turn does not cut into the current turn's audio", async () => {
  const h = makeController();
  const e1 = h.ctl.newTurn();
  h.ctl.playBlob(e1, { fakeBlob: "old" });
  const old = h.audios[0];
  const e2 = h.ctl.newTurn();
  const { a: cur } = await playing(h, e2);
  old.resolvePlay();
  await tick();
  assert.equal(old.paused, true);
  assert.equal(cur.paused, false, "current turn keeps playing");
  assert.equal(cur.srcRemoved, false);
});

test("calls stamped with an older epoch do nothing and leave current audio alone", async () => {
  const h = makeController();
  const e1 = h.ctl.newTurn();
  const e2 = h.ctl.newTurn();
  const { a: cur } = await playing(h, e2);
  assert.equal(await h.ctl.playBlob(e1, { fakeBlob: "x" }), "stale");
  assert.equal(await h.ctl.playUrl(e1, "/tts?text=x"), "stale");
  assert.equal(await h.ctl.speakText(e1, "x"), "stale");
  assert.equal(h.audios.length, 1);
  assert.equal(h.fetch.calls.length, 0);
  assert.equal(h.synth.spoken.length, 0);
  assert.equal(cur.paused, false);
});

test("current-turn Piper audio downloads, plays to the end, and frees its URL", async () => {
  const h = makeController();
  const e = h.ctl.newTurn();
  const p = h.ctl.playUrl(e, "/tts?text=hi");
  assert.equal(h.fetch.calls[0].url, "/tts?text=hi");
  h.fetch.respond(0);
  await tick();
  const a = h.audios[0];
  assert.equal(a.src, "blob:1");
  a.resolvePlay();
  await tick();
  assert.equal(a.paused, false);
  a.finishPlaying();
  assert.equal(await p, "ended");
  assert.deepEqual(h.revoked, ["blob:1"]);
});

test("Piper failures in the current turn report failed so the caller can fall back", async () => {
  const h = makeController();
  const e = h.ctl.newTurn();
  const p = h.ctl.playUrl(e, "/tts?text=hi");
  h.fetch.respond(0, { ok: false });
  assert.equal(await p, "failed");

  const q = h.ctl.playBlob(e, { fakeBlob: "wav" });
  h.audios[0].rejectPlay("NotAllowedError");
  assert.equal(await q, "failed");
  assert.equal(h.audios[0].paused, true);
});

test("on-device speech: current turn speaks with options, a cancelled line ends stale", async () => {
  const h = makeController();
  const e = h.ctl.newTurn();
  const p = h.ctl.speakText(e, "hello", { rate: 1.05, pitch: 0.95, voice: null });
  const u = h.synth.spoken[0];
  assert.equal(u.text, "hello");
  assert.equal(u.rate, 1.05);
  assert.equal("voice" in u, false);
  u.onend();
  assert.equal(await p, "ended");

  const p2 = h.ctl.speakText(e, "again");
  h.ctl.newTurn();
  h.synth.spoken[1].onend(); // browsers fire end/error on cancel
  assert.equal(await p2, "stale");
});

test("stop() silences everything without starting a turn; the next line still plays", async () => {
  const h = makeController({ fetch: { honorAbort: false } });
  const e = h.ctl.newTurn();
  const { a } = await playing(h, e);
  const pending = h.ctl.playUrl(e, "/tts?text=late");
  h.ctl.stop();
  assert.equal(h.ctl.current(), e, "same turn");
  assert.equal(h.synth.cancelCalls, 2);
  assert.equal(a.paused, true);
  assert.equal(a.srcRemoved, true);
  h.fetch.respond(0); // the download started before stop() lands late
  assert.equal(await pending, "stale");
  assert.equal(h.audios.length, 1, "nothing new started");

  const next = h.ctl.playBlob(e, { fakeBlob: "new" });
  h.audios[1].resolvePlay();
  await tick();
  assert.equal(h.audios[1].paused, false);
  h.audios[1].finishPlaying();
  assert.equal(await next, "ended");
});

test("server speech is credited to an older turn while that turn's request is out", async () => {
  const h = makeController({ graceMs: 1000 });
  const e1 = h.ctl.newTurn();
  let settle;
  h.ctl.track(new Promise((r) => { settle = r; }), e1);
  assert.equal(h.ctl.incomingEpoch(), e1, "own request: speech is current");

  const e2 = h.ctl.newTurn();
  assert.equal(h.ctl.incomingEpoch(), e1, "old request still out: stale");
  settle();
  await tick();
  h.clock.t += 500;
  assert.equal(h.ctl.incomingEpoch(), e1, "just settled: the socket may still be behind");
  h.clock.t += 600;
  assert.equal(h.ctl.incomingEpoch(), e2);

  h.ctl.track(Promise.reject(new Error("network")).catch(() => {}), e2);
  assert.equal(h.ctl.incomingEpoch(), e2, "a current-turn request never makes speech stale");
});

test("a request is credited to the turn passed in, even if sent after a newer turn began", async () => {
  const h = makeController();
  const a = h.ctl.newTurn();      // recording A starts
  const b = h.ctl.newTurn();      // B is pressed before A's upload goes out
  h.ctl.track(new Promise(() => {}), a);
  assert.equal(h.ctl.incomingEpoch(), a, "A's late line is stale in turn B");
  assert.notEqual(h.ctl.incomingEpoch(), b);
});

test("track() refuses a missing or unknown turn instead of guessing the current one", () => {
  const h = makeController();
  const e = h.ctl.newTurn();
  assert.throws(() => h.ctl.track(Promise.resolve()), TypeError);
  assert.throws(() => h.ctl.track(Promise.resolve(), e + 1), TypeError);
  assert.equal(h.ctl.incomingEpoch(), e);
});
