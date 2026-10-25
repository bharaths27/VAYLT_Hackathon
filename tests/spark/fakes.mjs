// Fake browser speech APIs for driving console/web/js/speech_epoch.mjs in Node.
import { createSpeechController } from "../../console/web/js/speech_epoch.mjs";

export const tick = () => new Promise((r) => setImmediate(r));

export class FakeAudio {
  constructor(src) {
    this.src = src;
    this.paused = true;
    this.pauseCalls = 0;
    this.loadCalls = 0;
    this.srcRemoved = false;
    this.onplay = this.onplaying = this.onended = this.onerror = null;
    this.pausedInOnset = null;
    this._settle = null;
  }
  // play() stays pending until the test settles it.
  play() {
    return new Promise((resolve, reject) => {
      this._settle = { resolve, reject };
    });
  }
  // The browser starts the element, even if pause() ran meanwhile (a late
  // play()): it sounds, 'play' and 'playing' fire, then the play() promise
  // resolves. pausedInOnset records whether it was paused synchronously inside
  // those events, before any promise continuation could run.
  // events=false: play() resolves with no fresh 'playing' event.
  resolvePlay({ events = true } = {}) {
    this.paused = false;
    if (events) {
      if (this.onplay) this.onplay();
      if (this.onplaying) this.onplaying();
      this.pausedInOnset = this.paused;
    }
    this._settle.resolve();
  }
  rejectPlay(name = "NotAllowedError") { const e = new Error(name); e.name = name; this._settle.reject(e); }
  pause() { this.paused = true; this.pauseCalls++; }
  removeAttribute(name) { if (name === "src") { this.src = ""; this.srcRemoved = true; } }
  load() { this.loadCalls++; }
  finishPlaying() { this.paused = true; if (this.onended) this.onended(); }
}

export class FakeSynth {
  constructor() { this.cancelCalls = 0; this.spoken = []; }
  speak(u) { this.spoken.push(u); }
  cancel() { this.cancelCalls++; }
}

// fetch whose responses the test settles by hand. honorAbort=false models a
// response that was already on its way when the abort landed.
export class FakeFetch {
  constructor({ honorAbort = true } = {}) { this.calls = []; this.honorAbort = honorAbort; }
  fn = (url, init = {}) => new Promise((resolve, reject) => {
    const call = { url, signal: init.signal, body: init.body, resolve, reject };
    this.calls.push(call);
    if (this.honorAbort && init.signal) {
      init.signal.addEventListener("abort", () => {
        const e = new Error("aborted"); e.name = "AbortError"; reject(e);
      });
    }
  });
  respond(i, { ok = true, body = "wav", json = null } = {}) {
    this.calls[i].resolve({ ok, status: ok ? 200 : 404, blob: async () => ({ fakeBlob: body }), json: async () => json });
  }
}

export function makeController(opts = {}) {
  const audios = [];
  const synth = new FakeSynth();
  const fetch = new FakeFetch(opts.fetch);
  const clock = { t: 0 };
  const revoked = [];
  let n = 0;
  const ctl = createSpeechController({
    speechSynthesis: synth,
    createAudio: (src) => { const a = new FakeAudio(src); audios.push(a); return a; },
    fetch: fetch.fn,
    now: () => clock.t,
    createUtterance: (text) => ({ text }),
    objectUrls: { create: () => "blob:" + ++n, revoke: (u) => revoked.push(u) },
    graceMs: opts.graceMs,
  });
  return { ctl, audios, synth, fetch, clock, revoked };
}
