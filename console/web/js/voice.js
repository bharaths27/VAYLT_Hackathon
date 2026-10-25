/* Voice: speaking (Piper on the Pi, else iPad on-device speechSynthesis) and
   hold-to-talk (MediaRecorder -> /stt -> whisper.cpp). Speech never blocks
   motion: everything here is fire and forget.
   All playback goes through window.SPEECH (speech_epoch.mjs, set up in
   index.html before this script runs): pressing talk starts a new turn, which
   silences the robot at once, and a line from an older turn never plays. */
(function () {
  const V = { muted: false, speaking: false, listening: false };
  const ctl = window.SPEECH;
  const q = [];   // { text, epoch }
  let piperOk = true;
  let iosVoice = null;

  function pickVoice() {
    if (!("speechSynthesis" in window)) return;
    const vs = speechSynthesis.getVoices();
    iosVoice = vs.find(v => /en-(GB|US)/.test(v.lang) && /Daniel|Samantha|Karen|Arthur|Aaron/.test(v.name)) || vs.find(v => /^en/.test(v.lang)) || null;
  }
  if ("speechSynthesis" in window) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }

  V.say = function (text) {
    if (V.muted || !text || V.listening) return; // the user has the floor
    const epoch = ctl.incomingEpoch();
    if (epoch !== ctl.current()) return;          // reply to a turn the user already replaced
    q.push({ text, epoch });
    if (q.length > 3) q.splice(0, q.length - 3); // drop stale lines, never lag behind reality
    if (!V.speaking) next();
  };
  V.cancel = function () {
    q.length = 0;
    ctl.stop();
    V.speaking = false;
  };
  // Push-to-talk start: the robot stops talking now, before the mic opens.
  V.newTurn = function () {
    q.length = 0;
    V.speaking = false;
    return ctl.newTurn();
  };
  // Tag each request with the turn that produced it, so its late speech can be
  // told apart. The turn is passed explicitly, never read when the request goes out.
  V.turn = () => ctl.current();
  V.track = (promise, turn) => ctl.track(promise, turn);

  // A "stale" outcome means a newer turn or a stop owns the queue: do nothing.
  function next() {
    const item = q.shift();
    if (!item) { V.speaking = false; return; }
    V.speaking = true;
    if (piperOk) {
      ctl.playUrl(item.epoch, "/tts?text=" + encodeURIComponent(item.text)).then((r) => {
        if (r === "failed") { piperOk = false; browserSay(item); }
        else if (r !== "stale") next();
      });
    } else browserSay(item);
  }
  function browserSay(item) {
    ctl.speakText(item.epoch, item.text, { voice: iosVoice, rate: 1.05, pitch: 0.95 }).then((r) => {
      if (r !== "stale") next();
    });
  }
  V.unlock = function () {
    try { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; speechSynthesis.speak(u); } catch (e) {}
  };

  // ---------------- microphone ----------------
  V.micAvailable = () => !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  // rec = { mr, chunks, turn } for the recording in progress. Each recording
  // keeps its own chunks and turn, so a new press while the last recorder is
  // still stopping cannot mix the two up.
  // opening = the press whose mic is still opening (the permission prompt can
  // take seconds). Only that press may start a recorder.
  let rec = null, opening = null, streamP = null, analyser = null, raf = 0;

  function openStream() {
    if (!streamP) {
      streamP = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      streamP.catch(() => { streamP = null; }); // the next press asks again
    }
    return streamP;
  }
  // Drop any recorder that is open or opening: never two at once.
  function discard() {
    opening = null;
    cancelAnimationFrame(raf);
    if (!rec) return;
    const r = rec;
    rec = null;
    r.mr.ondataavailable = r.mr.onstop = null;
    try { r.mr.stop(); } catch (e) {}
  }

  // Resolves true once recording, false if the press was released or replaced
  // by a newer press while the mic was opening (then nothing records).
  V.startRec = async function (waveCanvas) {
    if (!V.micAvailable()) throw new Error(window.isSecureContext ? "MediaRecorder not supported" : "mic needs HTTPS (trusted cert)");
    discard();
    const turn = V.newTurn();
    const me = { turn };
    opening = me;
    V.listening = true;
    let stream;
    try {
      stream = await openStream();
      if (opening !== me) return false; // released or replaced during the prompt
      const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
      const mr = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      const chunks = [];
      mr.ondataavailable = e => e.data.size && chunks.push(e.data);
      mr.start();
      rec = { mr, chunks, turn };
      opening = null;
    } catch (err) {
      if (opening !== me) return false; // a newer press owns the mic and its errors
      opening = null; V.listening = false; throw err;
    }
    const ctx = FX.ctx();
    if (ctx && waveCanvas) {
      const src = ctx.createMediaStreamSource(stream);
      analyser = ctx.createAnalyser(); analyser.fftSize = 256;
      src.connect(analyser);
      drawWave(waveCanvas);
    }
    return true;
  };
  // Release before the mic opened: latch it, so the opening press never records.
  V.cancelStart = function () {
    if (!opening) return;
    opening = null;
    V.listening = false;
  };
  // Resolves { blob, ext, turn }: turn is the one this recording started.
  V.stopRec = function () {
    V.listening = false;
    cancelAnimationFrame(raf);
    const r = rec;
    rec = null;
    return new Promise((resolve) => {
      if (!r) return resolve(null);
      r.mr.onstop = () => {
        const type = r.mr.mimeType || "audio/mp4";
        resolve({ blob: new Blob(r.chunks, { type }), ext: /mp4|aac|m4a/.test(type) ? ".m4a" : ".webm", turn: r.turn });
      };
      r.mr.stop();
    });
  };
  // Transcribe a stopped recording (auto=0: transcript only, the server runs
  // nothing) and hand the text to runCommand(text, turn) with the turn that
  // recorded it. If a newer push-to-talk turn began while it was being
  // transcribed, the result is dropped: never run, and no error shown over
  // the newer turn. Resolves { status: "sent" | "dropped" | "unavailable",
  // text, reason }; throws only for the current turn's network errors.
  // /stt is not tracked: with auto=0 it broadcasts nothing (pinned by
  // console/tests/test_stt_transcript_only.py).
  V.voiceCommand = async function (a, runCommand) {
    const fd = new FormData();
    fd.append("audio", a.blob, "speech" + a.ext);
    fd.append("auto", "0");
    let j = null, err = null;
    try { j = await (await fetch("/stt", { method: "POST", body: fd })).json(); } catch (e) { err = e; }
    if (a.turn !== ctl.current()) return { status: "dropped", text: j && j.text };
    if (err) throw err;
    if (!j.ok) return { status: "unavailable", reason: j.reason };
    runCommand(j.text, a.turn);
    return { status: "sent", text: j.text };
  };
  function drawWave(c) {
    const g = c.getContext("2d");
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const loop = () => {
      c.width = c.clientWidth * 2; c.height = c.clientHeight * 2;
      analyser.getByteTimeDomainData(buf);
      g.clearRect(0, 0, c.width, c.height);
      g.strokeStyle = "rgba(255,59,78,.8)"; g.lineWidth = 2; g.beginPath();
      for (let i = 0; i < buf.length; i++) {
        const x = (i / buf.length) * c.width, y = (buf[i] / 255) * c.height;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.stroke();
      raf = requestAnimationFrame(loop);
    };
    loop();
  }
  window.VOICE = V;
})();
