"""Voice test console for the robot, served by star-pi over HTTPS for the iPad.

    iPad Safari -> https://<star-pi>:8444
      Talk -> audio -> ffmpeg (16 kHz WAV) -> whisper.cpp (offline) -> text -> one allowlisted command
      shown on screen -> you tap Approve -> robot server :8200 (all safety checks still apply)

Voice can only REQUEST. Every motion needs the Approve tap. "stop" is the one exception: it e-stops
immediately (an extra stop, never the main one). Run: python -m voice.server  (see voice/setup_voice.sh)
"""
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from robot.client import RobotClient
from voice.intents import no_speech, parse

HERE = Path(__file__).resolve().parent
NO_APPROVAL_NEEDED = {"stop", "observe", "detect_tag"}


class WhisperTranscriber:
    def __init__(self, binary=None, model=None, threads=2):
        home = Path.home()
        self.binary = binary or os.environ.get("WHISPER_BIN", str(home / "whisper.cpp/build/bin/whisper-cli"))
        self.model = model or os.environ.get("WHISPER_MODEL", str(home / "whisper.cpp/models/ggml-tiny.en.bin"))
        self.threads = threads

    def check(self):
        missing = [p for p in (self.binary, self.model) if not Path(p).exists()]
        if not shutil.which("ffmpeg"):
            missing.append("ffmpeg")
        return missing

    def __call__(self, audio: bytes, filename: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / (Path(filename).name or "audio.bin")
            wav = Path(d) / "audio.wav"
            src.write_bytes(audio)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1",
                            "-c:a", "pcm_s16le", str(wav)], check=True, timeout=30)
            r = subprocess.run(["nice", "-n", "10", self.binary, "-m", self.model, "-f", str(wav),
                                "-t", str(self.threads), "-nt", "-np"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip()[-300:])
            return " ".join(line.strip() for line in r.stdout.splitlines() if line.strip())


class TextReq(BaseModel):
    text: str


class ExecReq(BaseModel):
    op: str
    args: dict = {}
    pin: str | None = None


class ResetReq(BaseModel):
    operator: str = "ipad"
    pin: str | None = None


class BrainApproveReq(BaseModel):
    operation_id: str
    digest: str
    pin: str | None = None


class Brain:
    """Bharath's ENGRAM brain (POST /commands, POST /operations/{id}/approve). It plans from memory,
    checks the plan, and is the only thing that sends motion to the robot server."""
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def _post(self, path: str, body: dict, timeout: float):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return {"error": json.loads(e.read()).get("detail", str(e)), "http": e.code}
            except Exception:
                return {"error": str(e), "http": e.code}

    def command(self, text: str):
        return self._post("/commands", {"text": text, "who": "voice-console"}, timeout=120)

    def approve(self, operation_id: str, digest: str):
        return self._post(f"/operations/{operation_id}/approve",
                          {"digest": digest, "approved_by": "voice-console"}, timeout=180)

    def stop(self):
        return self._post("/stop", {}, timeout=3)


def create_app(bot: RobotClient, transcribe, pin: str | None = None, brain: "Brain | None" = None) -> FastAPI:
    """brain set  -> BRAIN mode (team demo): every request goes to the brain, which plans and executes.
    brain None -> DIRECT mode (bench testing only): one allowlisted command straight to the robot server."""
    app = FastAPI(title="ENGRAM voice console")

    def poses():
        try:
            caps = bot.capabilities()
            for p in caps["primitives"]:
                if p["op"] == "arm_pose":
                    return p["args"][0].get("choices", [])
        except Exception:
            pass
        return []

    def check_pin(given):
        if pin and given != pin:
            raise HTTPException(403, "wrong PIN")

    def busy() -> bool:
        """Is the robot executing something now? Its server says "busy": the operation in progress, or null when
        idle. Only null (or false) means idle: a state without "busy", one it cannot read, or a value it does not
        recognize counts as busy, so an unclear command stops it."""
        try:
            state = bot.state()
        except Exception:
            return True
        if not isinstance(state, dict) or "busy" not in state:
            return True
        return state["busy"] is not None and state["busy"] is not False

    def clarify(text: str, intent: dict) -> dict:
        """Carter's rule: "ambiguous or misheard command: we stop the task and ask user to please clarify."
        For what this console answers itself: in direct mode whatever the parser does not say it understood
        (its classification; a result that does not say counts as unclear), in brain mode only a request with no
        words in it (the brain answers everything else, under the same rule). If the robot is busy it is
        stopped with the latching stop (a reset is needed to move again): "I stopped. <reason> Please
        clarify."; if not, nothing is stopped: "<reason> Please clarify."."""
        if brain is not None:
            if not no_speech(text):
                return intent
        elif intent.get("classification") == "understood":   # a command, or a refusal of one: unchanged
            return intent
        stopped, lead = busy(), ""
        if stopped:
            lead = "I stopped. "
            try:
                bot.stop(latch=True, reason="unclear voice command")
            except Exception:
                lead = "I could not stop the robot: use its STOP button. "
            if brain:
                try:
                    brain.stop()
                except Exception:
                    pass
        return {**intent, "say": f"{lead}{intent['say']} Please clarify.", "clarify": True, "stopped": stopped}

    @app.get("/")
    def index():
        return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def config():
        missing = transcribe.check() if hasattr(transcribe, "check") else []
        return {"pin_required": bool(pin), "speech_ready": not missing, "missing": missing,
                "mode": "brain" if brain else "direct"}

    @app.post("/api/transcribe")
    def do_transcribe(audio: UploadFile = File(...)):
        data = audio.file.read()
        if not data:
            raise HTTPException(400, "empty recording")
        try:
            text = transcribe(data, audio.filename or "audio.m4a")
        except Exception as e:
            return JSONResponse({"error": f"transcription failed: {e}"}, status_code=500)
        return {"text": text, "intent": clarify(text, parse(text, poses()))}

    @app.post("/api/parse")
    def do_parse(req: TextReq):
        return {"text": req.text, "intent": clarify(req.text, parse(req.text, poses()))}

    @app.post("/api/brain/command")
    def brain_command(req: TextReq):
        if not brain:
            raise HTTPException(409, "brain mode is off")
        return brain.command(req.text)

    @app.post("/api/brain/approve")
    def brain_approve(req: BrainApproveReq):
        check_pin(req.pin)
        if not brain:
            raise HTTPException(409, "brain mode is off")
        return brain.approve(req.operation_id, req.digest)

    @app.post("/api/execute")
    def execute(req: ExecReq):
        check_pin(req.pin)
        if req.op == "stop":
            return bot.stop(latch=True, reason="voice console STOP")
        if brain and req.op not in NO_APPROVAL_NEEDED:
            # one motion path for the team: the brain plans, checks and executes
            raise HTTPException(409, "brain mode: motion goes through the brain (/api/brain/command)")
        # the robot server re-validates op and args against its allowlist and bounds
        return bot.execute(req.op, req.args, approved=req.op not in NO_APPROVAL_NEEDED)

    @app.post("/api/stop")
    def stop():  # no PIN: anyone at the screen can always stop the robot
        out = bot.stop(latch=True, reason="voice console STOP button")  # straight to the robot: never via a model
        if brain:
            try:
                brain.stop()
            except Exception:
                pass
        return out

    @app.post("/api/reset")
    def reset(req: ResetReq):
        check_pin(req.pin)
        return bot.reset(req.operator)

    @app.get("/api/state")
    def state():
        try:
            return bot.state()
        except Exception as e:
            return JSONResponse({"error": f"robot unreachable: {e}"}, status_code=502)

    @app.get("/api/stream")
    def stream(q: int = 60, fps: float = 20, limit: int = 0):
        """Continuous MJPEG stream: one long response, no per-frame round trip over the hotspot.
        Frames are fetched from the robot server on localhost. limit>0 stops after N frames (tests)."""
        period = 1.0 / max(1.0, min(fps, 30.0))
        quality = max(30, min(90, q))

        def gen():
            sent = 0
            while limit <= 0 or sent < limit:
                t0 = time.monotonic()
                try:
                    jpg = bot.frame_jpeg(quality=quality)
                except Exception:
                    jpg = b""
                if jpg.startswith(b"\xff\xd8"):
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode()
                           + b"\r\n\r\n" + jpg + b"\r\n")
                    sent += 1
                else:
                    time.sleep(0.5)  # camera unavailable: back off
                time.sleep(max(0.0, period - (time.monotonic() - t0)))

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/frame")
    def frame(q: int = 60):
        try:
            jpg = bot.frame_jpeg(quality=max(30, min(90, q)))
        except Exception:
            jpg = b""
        if not jpg or not jpg.startswith(b"\xff\xd8"):
            return Response(status_code=503)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    return app


def main():
    import uvicorn
    robot_url = os.environ.get("ENGRAM_ROBOT_URL", "http://localhost:8200")
    secret = os.environ.get("ENGRAM_ROBOT_SECRET", "dev-secret-change-me")
    mode = os.environ.get("VOICE_MODE", "brain")
    # in brain mode the brain owns motion (and its heartbeat); only direct mode drives the robot itself
    bot = RobotClient(robot_url, secret, name="voice-console", heartbeat=(mode == "direct"))
    brain = Brain(os.environ.get("ENGRAM_BRAIN_URL", "http://127.0.0.1:8001")) if mode == "brain" else None
    certs = HERE / "certs"
    uvicorn.run(create_app(bot, WhisperTranscriber(), os.environ.get("VOICE_PIN") or None, brain),
                host=os.environ.get("VOICE_HOST", "0.0.0.0"), port=int(os.environ.get("VOICE_PORT", "8444")),
                ssl_keyfile=str(certs / "key.pem"), ssl_certfile=str(certs / "cert.pem"), log_level="info")


if __name__ == "__main__":
    main()
