/* Turn-generation ("epoch") speech controller for the console.

   Every push-to-talk press starts a new turn. newTurn() silences the robot on
   the spot (on-device speech, Piper audio, in-flight /tts downloads) and moves
   the epoch on. Every playback carries the epoch it belongs to and re-checks
   it after each await, so a /tts download or a play() promise that settles
   late can never start, resume, or cut into audio for a newer turn.

   Playback calls resolve to one outcome string:
     "ended"        played to the end
     "stale"        superseded by newTurn()/stop(); the caller does nothing more
     "failed"       could not start (download failed, play() refused)
     "error"        broke off after it started
     "unsupported"  no on-device speech here

   All browser APIs are injectable so Node tests can drive it with fakes. */

export function createSpeechController(deps = {}) {
  const g = globalThis;
  const synth = "speechSynthesis" in deps ? deps.speechSynthesis : g.speechSynthesis;
  const createAudio = deps.createAudio || ((src) => new g.Audio(src));
  const doFetch = deps.fetch || ((url, init) => g.fetch(url, init));
  const now = deps.now || (() => (g.performance ? g.performance.now() : Date.now()));
  const createUtterance = deps.createUtterance || ((text) => new g.SpeechSynthesisUtterance(text));
  const objectUrls = deps.objectUrls || {
    create: (blob) => g.URL.createObjectURL(blob),
    revoke: (url) => g.URL.revokeObjectURL(url),
  };
  // Server speech and the HTTP reply travel on different sockets, so a line
  // can land just after its request settles. Keep a settled request on the
  // books this long.
  const graceMs = deps.graceMs == null ? 1500 : deps.graceMs;

  let epoch = 0;        // turn number; only newTurn() moves it
  let gen = 0;          // moves on newTurn() and stop(); work started before it is dead
  let playing = null;   // { audio, finish } of the one Audio element we own
  const fetches = new Set();   // { epoch, ac } for /tts downloads in flight
  const requests = new Set();  // { epoch, settledAt } for turn requests sent

  const alive = (e, g0) => e === epoch && g0 === gen;

  // Forget requests that settled more than graceMs ago. Runs on every
  // track() too, so the set stays bounded even when no line ever arrives.
  function prune(t) {
    for (const r of requests) {
      if (r.settledAt !== null && t - r.settledAt > graceMs) requests.delete(r);
    }
  }

  function halt(audio) {
    try { audio.pause(); } catch (e) { /* already gone */ }
    try { audio.removeAttribute("src"); audio.load(); } catch (e) { /* already gone */ }
  }

  function silence() {
    gen++;
    try { if (synth) synth.cancel(); } catch (e) { /* nothing to cancel */ }
    if (playing) {
      const p = playing;
      playing = null;
      halt(p.audio);
      p.finish("stale");
    }
    for (const f of fetches) f.ac.abort();
    fetches.clear();
  }

  // Plays a blob the caller has just proven live (no await in between).
  function start(e, g0, blob) {
    const src = objectUrls.create(blob);
    const audio = createAudio(src);
    if (playing) { const p = playing; playing = null; halt(p.audio); p.finish("stale"); }
    return new Promise((resolve) => {
      let settled = false, started = false;
      const me = { audio, finish };
      playing = me;
      function finish(outcome) {
        if (settled) return;
        settled = true;
        audio.onended = audio.onerror = null; // the onset guard below stays
        if (playing === me) playing = null;
        try { objectUrls.revoke(src); } catch (err) { /* ignore */ }
        resolve(outcome);
      }
      // Onset guard, kept on the element for good: if it ever starts after its
      // turn ended (a late play()), it is paused in that same event, before
      // any promise continuation could run.
      const onset = () => {
        if (alive(e, g0) && playing === me) return true;
        halt(audio);
        return false;
      };
      audio.onplay = onset;
      audio.onplaying = () => { if (onset()) started = true; };
      audio.onended = () => finish(alive(e, g0) ? "ended" : "stale");
      audio.onerror = () => { halt(audio); finish(!alive(e, g0) ? "stale" : started ? "error" : "failed"); };
      let p;
      try { p = audio.play(); } catch (err) { p = Promise.reject(err); }
      Promise.resolve(p).then(() => {
        // play() can settle after the turn moved on (also without a fresh
        // 'playing' event); never let it keep sounding.
        if (!alive(e, g0) || playing !== me) { halt(audio); finish("stale"); return; }
        started = true;
      }, () => {
        halt(audio);
        finish(!alive(e, g0) ? "stale" : started ? "error" : "failed");
      });
    });
  }

  return {
    current: () => epoch,

    // Push-to-talk start: silence everything now and open a new turn.
    newTurn() {
      epoch++;
      silence();
      return epoch;
    },

    // Silence everything without opening a new turn (mute, E-STOP).
    stop() {
      silence();
    },

    // On-device speech (iPad speechSynthesis).
    speakText(e, text, opts = {}) {
      if (e !== epoch) return Promise.resolve("stale");
      if (!synth || !text) return Promise.resolve("unsupported");
      const g0 = gen;
      return new Promise((resolve) => {
        let u;
        try { u = createUtterance(text); } catch (err) { resolve("unsupported"); return; }
        for (const k of ["voice", "rate", "pitch", "volume"]) if (opts[k] != null) u[k] = opts[k];
        const done = (outcome) => resolve(alive(e, g0) ? outcome : "stale");
        u.onend = () => done("ended");
        u.onerror = () => done("error");
        try { synth.speak(u); } catch (err) { done("error"); }
      });
    },

    // Piper audio from a URL (e.g. /tts?text=...).
    async playUrl(e, url) {
      if (e !== epoch) return "stale";
      const g0 = gen;
      const f = { epoch: e, ac: new AbortController() };
      fetches.add(f);
      try {
        let r = null, blob = null;
        try { r = await doFetch(url, { signal: f.ac.signal }); } catch (err) { r = null; }
        if (!alive(e, g0)) return "stale"; // never read a stale body
        if (!r || !r.ok) return "failed";
        try { blob = await r.blob(); } catch (err) { blob = null; }
        if (!alive(e, g0)) return "stale";
        if (!blob) return "failed";
        return start(e, g0, blob);
      } finally {
        fetches.delete(f);
      }
    },

    // Piper audio already in hand.
    playBlob(e, blob) {
      if (e !== epoch) return Promise.resolve("stale");
      return start(e, gen, blob);
    },

    // Record a request (POST /stt, /commands) as belonging to `turn`: the turn
    // that produced it, captured by the caller when that turn began (a
    // recording can finish uploading after a newer turn has started).
    track(promise, turn) {
      if (!Number.isInteger(turn) || turn < 0 || turn > epoch) {
        throw new TypeError("track(promise, turn): turn must be a turn from current()/newTurn()");
      }
      prune(now());
      const r = { epoch: turn, settledAt: null };
      requests.add(r);
      const settle = () => { r.settledAt = now(); };
      Promise.resolve(promise).then(settle, settle);
      return promise;
    },

    // Requests still on the books (outstanding, or settled within graceMs).
    tracked: () => requests.size,

    // The turn that server speech arriving now belongs to. Server speech has
    // no turn id, so while a request from an older turn is still out (or just
    // settled), credit the line to that older turn: it is stale, not ours.
    // The graceMs window exists only for these untagged lines; requests carry
    // their turn explicitly.
    incomingEpoch() {
      prune(now());
      let owner = epoch;
      for (const r of requests) if (r.epoch < owner) owner = r.epoch;
      return owner;
    },
  };
}
