"""All runtime settings come from environment variables so the same code runs
on a laptop (mocks) and on the Pi 5 (real robot + memory) with no edits."""
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _seconds(name: str, default: float, cap: float) -> float:
    """A finite number of seconds with 0 < t <= cap; anything else (inf, nan, <= 0, too big,
    not a number) gives the default."""
    try:
        t = float(_env(name, str(default)))
    except ValueError:
        return default
    return t if math.isfinite(t) and 0 < t <= cap else default


def _count(name: str, default: int, cap: int) -> int:
    """A whole number with 0 < n <= cap; anything else gives the default."""
    try:
        n = int(_env(name, str(default)))
    except ValueError:
        return default
    return n if 0 < n <= cap else default


def _audio_ctx(name: str, default: int) -> int:
    """0 (whisper's own default, all 1500) or 384..1500; anything else gives the default. Below
    ~384 whisper starts repeating words (256 measured unsafe: multi-second hallucinations)."""
    try:
        n = int(_env(name, str(default)))
    except ValueError:
        return default
    return n if n == 0 or 384 <= n <= 1500 else default


# Where Brian's robot adapter lives. "mock" = built-in simulator.
ROBOT_URL = _env("ENGRAM_ROBOT_URL", "mock")
# Where Bharath's memory API lives. "mock" = built-in sample memory.
MEMORY_URL = _env("ENGRAM_MEMORY_URL", "mock")
# Bharath's ENGRAM brain API (plans + approvals + execution). "off" = console plans by itself.
BRAIN_URL = _env("ENGRAM_BRAIN_URL", "off")
# Where Deniz's metrics / dream compiler lives. "off" = skip.
METRICS_URL = _env("ENGRAM_METRICS_URL", "off")

# Camera source override: "robot" (use robot adapter frame), "opencv:0" (USB cam on Pi)
CAMERA = _env("ENGRAM_CAMERA", "robot")
CAMERA_FPS = float(_env("ENGRAM_CAMERA_FPS", "10"))

# Optional local LLM (llama.cpp llama-server). Off until measured on the board.
LLM_URL = _env("ENGRAM_LLM_URL", "off")  # e.g. http://127.0.0.1:8081

# Voice
PIPER_MODEL = _env("ENGRAM_PIPER_MODEL", str(ROOT / "models" / "en_US-lessac-medium.onnx"))
PIPER_BIN = _env("ENGRAM_PIPER_BIN", "piper")
WHISPER_BIN = _env("ENGRAM_WHISPER_BIN", str(ROOT / "vendor" / "whisper.cpp" / "build" / "bin" / "whisper-cli"))
WHISPER_MODEL = _env("ENGRAM_WHISPER_MODEL", str(ROOT / "models" / "ggml-tiny.en.bin"))
FFMPEG_BIN = _env("ENGRAM_FFMPEG_BIN", "ffmpeg")
# Optional resident whisper.cpp server (keeps the model loaded between requests), e.g.
# http://127.0.0.1:8178. Empty = off. Loopback hosts only (127.0.0.1, localhost, ::1); any other
# host is ignored so audio never leaves the board. Any server failure falls back to WHISPER_BIN.
WHISPER_SERVER_URL = _env("ENGRAM_WHISPER_SERVER_URL", "")
# One overall deadline for the whole server request (connect, upload, answer), at most 120 s.
WHISPER_SERVER_TIMEOUT_S = _seconds("ENGRAM_WHISPER_SERVER_TIMEOUT_S", 30.0, 120.0)
# whisper's audio context (encoder frames; 1500 = 30 s). Our clips are a few seconds, so a smaller
# context is much faster: 0 = whisper's default, else 384..1500. Passed to whisper-cli as -ac N,
# and to the whisper server with every request as its audio_ctx form field (whisper.cpp's
# /inference reads it per request; the server's own -ac start flag is only its default).
WHISPER_AUDIO_CTX = _audio_ctx("ENGRAM_WHISPER_AUDIO_CTX", 512)
# Initial prompt naming the robot's vocabulary, so whisper hears "claw" rather than "cloth" or
# "clock". Passed to whisper-cli as --prompt and to the server as its prompt form field; empty =
# none. A sentence of nouns, not a list of commands: measured, a bare word list did not help and a
# prompt full of command phrases gets copied into transcripts.
WHISPER_PROMPT = _env("ENGRAM_WHISPER_PROMPT", "A lab robot with a claw (its gripper), a beaker, goggles, "
                      "a sample, a bench, a shelf, a tray and a station.").strip()[:500]
# Longest clip STT accepts, measured on the decoded audio (at most 120 s). ffmpeg stops decoding
# just past it, and longer clips are refused before whisper sees them.
STT_MAX_SECONDS = _seconds("ENGRAM_STT_MAX_SECONDS", 30.0, 120.0)
# How long /stt waits for the whole upload to arrive (at most 120 s). A client that stalls
# part way is answered 408 and the room it reserved in the STT queue is given back.
STT_UPLOAD_TIMEOUT_S = _seconds("ENGRAM_STT_UPLOAD_TIMEOUT_S", 20.0, 120.0)
# Speech queues: past these, requests are refused as busy instead of piling up in memory.
# Counts include the job that is running. STT also caps the audio bytes its queued jobs hold.
STT_MAX_PENDING = _count("ENGRAM_STT_MAX_PENDING", 4, 64)
STT_MAX_PENDING_BYTES = _count("ENGRAM_STT_MAX_PENDING_BYTES", 32 * 1024 * 1024, 1024 * 1024 * 1024)
TTS_MAX_PENDING = _count("ENGRAM_TTS_MAX_PENDING", 8, 64)

# Folder whose files are SHA-256 hashed and shown on the HUD (proves weights unchanged)
MODELS_DIR = Path(_env("ENGRAM_MODELS_DIR", str(ROOT / "models")))

# Evidence ledger (append only JSONL)
DATA_DIR = Path(_env("ENGRAM_DATA_DIR", str(ROOT / "data")))
LEDGER_PATH = DATA_DIR / "ledger.jsonl"
TTS_CACHE = DATA_DIR / "tts_cache"

# Governance
APPROVAL_TTL_S = int(_env("ENGRAM_APPROVAL_TTL_S", "90"))
STEP_TIMEOUT_S = float(_env("ENGRAM_STEP_TIMEOUT_S", "75"))  # pick/place can take up to 60 s on the robot

# Brian's robot adapter: shared HMAC secret for physical-effect authorization (robot/auth.py)
ROBOT_SECRET = _env("ENGRAM_ROBOT_SECRET", "dev-secret-change-me")
# Zone name -> zone marker ArUco tag on the arena (Brian's sim: 20 and 21). Format "A=20,B=21"
ZONE_TAGS = {k.strip().upper(): int(v) for k, v in
             (pair.split("=") for pair in _env("ENGRAM_ZONE_TAGS", "A=20,B=21").split(",") if "=" in pair)}
