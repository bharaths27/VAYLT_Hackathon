"""ENGRAM Cortex console (Verna). FastAPI app served from the Pi 5 to iPad Safari.

Contract (Verna packet):
  GET /video.mjpg   WS /events   POST /commands   POST /operations/{id}/approve
  POST /stop        GET /evidence/{id}
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import UploadFile as FormFile
from starlette.formparsers import MultiPartParser
from starlette.requests import ClientDisconnect

from . import brain, config, llm, voice
from .adapters.memory import make_memory
from .adapters.robot import make_camera, make_robot
from .core import (Bus, Engine, Ledger, LedgerBusy, Operation, best_effort, hash_models, stop_unconfirmed,
                   upstream_online)
from .parser import is_stop, parse, quip

WEB = config.ROOT / "web"

robot = make_robot()
camera = make_camera(robot)
memory = make_memory()
bus = Bus()
ledger = Ledger(config.LEDGER_PATH)
engine = Engine(robot, memory, bus, ledger)
STATUS = {"upstream_online": None, "models": [], "boot": time.time(), "last_state": {}}


async def _state_loop():
    while True:
        try:
            st = await robot.state()
            STATUS["last_state"] = st
            await bus.emit("state", state=st, stopped=engine.stopped)
        except Exception as e:  # noqa: BLE001
            await bus.emit("state", state={"mode": "error", "connected": False, "error": str(e)}, stopped=engine.stopped)
        await asyncio.sleep(0.25)


async def _net_loop():
    while True:
        STATUS["upstream_online"] = await asyncio.to_thread(upstream_online)
        await bus.emit("net", upstream_online=STATUS["upstream_online"])
        await asyncio.sleep(5)


async def _expiry_loop():
    while True:
        now = time.time()
        for op in list(engine.ops.values()):
            if op.status == "PENDING_APPROVAL" and now > op.plan.expires_at:
                op.status = "EXPIRED"
                await bus.emit("operation", op=op.public())
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    STATUS["models"] = await asyncio.to_thread(hash_models, config.MODELS_DIR)
    ledger.append("boot", robot=config.ROBOT_URL, memory=config.MEMORY_URL, models=STATUS["models"])
    tasks = [asyncio.create_task(f()) for f in (_state_loop, _net_loop, _expiry_loop)]
    if brain.enabled():
        tasks.append(asyncio.create_task(_brain_pending_loop()))
    tasks.append(asyncio.create_task(voice.warm_tts()))  # load Piper now, not on the first reply
    yield
    for t in tasks:
        t.cancel()
    await asyncio.to_thread(voice.close)  # the whisper server's connection pool
    ledger.flush(2.0)  # the ledger writes on its own thread; give queued records a moment to reach the disk


# ------------------------------------------------------------------ /stt upload limits
STT_FORM_OVERHEAD = 16 * 1024  # multipart boundaries, part headers and the small "auto" field


def stt_upload_cap() -> int:
    """Largest /stt request body accepted: one clip at the STT queue's byte limit, plus room
    for the multipart framing around it."""
    return config.STT_MAX_PENDING_BYTES + STT_FORM_OVERHEAD


def _audio_too_large(limit: int) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": f"audio too large: over {limit} bytes"}, status_code=413)


def _declared_length(value: str | bytes | None) -> int | None:
    """The Content-Length as a number, or None when there is none (chunked) or it is not one."""
    if value is None:
        return None
    value = value.decode("latin-1") if isinstance(value, bytes) else value
    return int(value) if value.strip().isdigit() else None


class _UploadTooLarge(Exception):
    pass


async def _read_stt_form(request: Request, deadline: float):
    """Parse the multipart /stt body within `deadline` seconds (asyncio.TimeoutError past it).
    On any way out but success (timeout, client gone, malformed body, the guard's cut-off) the
    partly written spool files are closed here rather than left for the garbage collector."""
    parser = MultiPartParser(request.headers, request.stream(), max_files=1, max_fields=2)
    try:
        return await asyncio.wait_for(parser.parse(), deadline)
    except BaseException:
        for spool in getattr(parser, "_files_to_close_on_error", ()):  # the spool files it opened
            spool.close()
        raise


def _route_path(scope) -> str:
    """The path the router matches: scope["path"] without the mount prefix (root_path), as Starlette
    strips it, so the guard still sees /stt when the app is mounted under a prefix."""
    path, root = scope["path"], scope.get("root_path", "")
    return path[len(root):] if root and path.startswith(root) else path


class SttUploadGuard:
    """Pure ASGI guard for POST /stt; every other request passes straight through. A body over
    stt_upload_cap() is refused with 413 before Starlette's multipart parser parses or spools
    it: a declared Content-Length over the cap is refused without reading any of the body, and
    a chunked (or unknown-length) body is counted as it streams through receive() and cut off
    as soon as the count passes the cap."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or _route_path(scope) != "/stt":
            await self.app(scope, receive, send)
            return
        cap = stt_upload_cap()
        declared = _declared_length(dict(scope["headers"]).get(b"content-length"))
        if declared is not None and declared > cap:
            await _audio_too_large(config.STT_MAX_PENDING_BYTES)(scope, receive, send)
            return
        seen, refused, started = 0, False, False

        async def counted_receive():
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    refused = True
                    raise _UploadTooLarge(f"/stt body passed {cap} bytes")
            return message

        async def send_unless_refused(message):
            nonlocal started
            if refused:
                return  # the app's answer to a refused upload is dropped; the 413 goes out instead
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, send_unless_refused)
        except Exception:
            if not refused:
                raise
        if refused and not started:
            await _audio_too_large(config.STT_MAX_PENDING_BYTES)(scope, receive, send)


app = FastAPI(title="ENGRAM Cortex Console", lifespan=lifespan)
app.add_middleware(SttUploadGuard)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"ok": True, "robot": config.ROBOT_URL, "memory": config.MEMORY_URL, "uptime_s": int(time.time() - STATUS["boot"]),
            "ledger_dropped": ledger.dropped}  # records dropped while the ledger's disk was stalled (Ledger)


@app.get("/status")
async def status():
    return {"upstream_online": STATUS["upstream_online"], "models": STATUS["models"], "robot_url": config.ROBOT_URL,
            "memory_url": config.MEMORY_URL, "llm": config.LLM_URL != "off", "tts": voice.tts_status(),
            "stt": voice.stt_status(), "stopped": engine.stopped, "simulated": config.ROBOT_URL == "mock",
            "brain_url": config.BRAIN_URL, "brain": (await brain.health()) if brain.enabled() else None,
            "boot": STATUS["boot"]}


@app.get("/models/hash")
async def models_hash():
    STATUS["models"] = await asyncio.to_thread(hash_models, config.MODELS_DIR)
    ledger.append("model_hash", models=STATUS["models"])
    return STATUS["models"]


# ------------------------------------------------------------------ camera
@app.get("/frame.jpg")
async def frame():
    try:
        return Response(await camera.frame(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"no frame: {e}")


@app.get("/video.mjpg")
async def video(request: Request):
    async def gen():
        period = 1.0 / config.CAMERA_FPS
        while not await request.is_disconnected():
            t = time.time()
            try:
                jpg = await camera.frame()
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
            await asyncio.sleep(max(0.0, period - (time.time() - t)))
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ events
@app.websocket("/events")
async def events(ws: WebSocket):
    await ws.accept()
    bus.clients.add(ws)
    try:
        await ws.send_json({"type": "hello", "ts": time.time(), "status": await status(),
                            "ops": [o.public() for o in list(engine.ops.values())[-20:]],
                            "skills": await _skills_safe(), "runs": engine.runs[-10:],
                            "log": bus.log[-60:], "prospective": await _prospective_safe()})
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        bus.clients.discard(ws)


async def _skills_safe():
    try:
        return await memory.list_skills()
    except Exception:  # noqa: BLE001
        return []


async def _prospective_safe():
    try:
        return await memory.list_prospective()
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ commands
class Command(BaseModel):
    text: str
    source: str = "typed"   # typed | voice | prospective


def _running_ops() -> list:
    """Operations approved or executing here: the console's own, and the brain's that the console approved."""
    return [op for op in engine.ops.values() if op.status in ("APPROVED", "EXECUTING")]


async def _task_running() -> bool:
    """Is a task running? One of _running_ops(), or, with the brain bridge on, a plan the brain is executing,
    whoever started it. When the brain cannot say, one is assumed: a needless stop costs one Clear Stop."""
    if _running_ops():
        return True
    if not brain.enabled():
        return False
    try:
        return bool(await brain.running())
    except Exception as e:  # noqa: BLE001
        await best_effort(lambda: bus.emit("log", level="warn", msg=f"brain running state unknown ({e}): treated as running"))
        return True


def _sentence(text: str) -> str:
    text = text.strip()
    return text + "." if text and text[-1] not in ".?!" else text


def clarify_reply(reason: str, stopped: bool) -> str:
    """Carter's rule, in words: "I stopped. <reason> Please clarify.", or "<reason> Please clarify." when nothing was
    running (the same wording as engram.engine.clarify_reply)."""
    reason = _sentence(reason)
    return ("I stopped. " if stopped else "") + (reason + " " if reason else "") + "Please clarify."


async def _clarify(out: dict, reason: str, source: str, running: bool | None = None) -> dict:
    """Carter's rule (16:3xZ): "ambiguous or misheard command: we stop the task and ask user to please clarify."
    A task that is running is stopped with the latching stop a stop word uses (engine.stop: Clear Stop is needed
    to move again, and nothing resumes on its own); with nothing running nothing is stopped, so a cough latches
    nothing. `out` becomes the clarification: kind "clarify", the spoken reply, and whether it stopped.
    running: already known (the brain stopped its own plan), or None to check now."""
    if running is None:
        running = await _task_running()
    out.update(kind="clarify", stopped=running)
    if running:
        res = await engine.stop(by=f"clarify:{source}")
        unconfirmed = stop_unconfirmed(res, engine.robot)
        out.update(robot=res, stop_confirmed=not unconfirmed)
        if unconfirmed:  # as a stop word's answer does: the latch holds, but the robot may still be moving
            out["reply"] = ("I could not confirm the robot stopped. Cut the motor power. "
                            + clarify_reply(reason, False))
            return out
    out["reply"] = clarify_reply(reason, running)
    return out


async def handle_command(text: str, source: str) -> dict:
    t0 = time.time()
    if is_stop(text):  # first: not even the "heard" telemetry may delay or skip a stop
        return await _stop_command(text, source, t0)
    if voice.no_speech(text):  # nothing heard but noise: misheard, known at once, so it too acts before telemetry
        return await _misheard_command(text, source, t0)
    await bus.emit("heard", text=text, source=source)
    ledger.append("command", text=text, source=source)
    if brain.enabled():
        return await _brain_command(text, source, t0)
    scene = STATUS.get("last_state") or {}
    tm = time.time()
    try:
        bundle = await memory.query(text, {"detections": scene.get("detections", [])})
    except Exception as e:  # noqa: BLE001
        await bus.emit("log", level="error", msg=f"memory unavailable: {e}")
        bundle = {"facts": [], "bindings": [], "zones": [], "constraints": [], "skills": [], "missing_evidence": ["memory service unreachable"]}
    memory_ms = int((time.time() - tm) * 1000)
    await bus.emit("bundle", bundle=bundle, memory_ms=memory_ms)

    tp = time.time()
    r = parse(text, bundle)
    parse_ms = int((time.time() - tp) * 1000)
    planner_calls = 0
    if r.kind == "abstain" and r.data.get("llm_eligible") and config.LLM_URL != "off":
        plan, why = await llm.propose(text, bundle)
        planner_calls = 1
        r.trace.append(why)
        if plan:
            r.kind, r.plan, r.quip, r.classification = "plan", plan, quip("plan"), "understood"
        else:
            r.reason = why
    metrics = {"memory_ms": memory_ms, "parse_ms": parse_ms, "planner_calls": planner_calls,
               "plan_ms": int((time.time() - t0) * 1000)}
    out = {"kind": r.kind, "reason": r.reason, "quip": r.quip, "trace": r.trace, "data": r.data, "metrics": metrics}
    # A result that does not act follows Carter's rule unless the parser states it was understood (a refusal: unsafe,
    # a limit, "don't", a plan that failed validation, a request that cannot be scheduled as said). Whatever does
    # not say so is unclear: the parser asks, nothing it knows matched and no model plan came of it, or nothing was
    # heard. Stated, never worked out from the kind or the words.
    unclear = r.kind not in ("plan", "rule", "prospective", "stop") and r.classification != "understood"

    if unclear:
        await _clarify(out, r.short or r.reason, source)
    elif r.kind == "stop":
        await engine.stop(by=f"command:{source}")
    elif r.kind == "rule":
        res = await memory.add_rule(r.data["rule_text"])
        ledger.append("rule_ingested", text=r.data["rule_text"], result=res)
        out["rule"] = res
        await bus.emit("rule", result=res, text=r.data["rule_text"])
    elif r.kind == "prospective" and r.plan:
        delay = r.data["delay_s"]
        task = await memory.add_prospective({"text": r.data["inner"], "due_at": time.time() + delay, "trigger": f"timer:{delay:.0f}s"})
        out["prospective"] = task
        await bus.emit("prospective", task=task, tasks=await _prospective_safe())
        asyncio.create_task(_fire_later(task, delay))
    elif r.kind == "plan" and r.plan:
        r.plan.planner_calls = planner_calls
        op = Operation(r.plan, source, text, bundle, metrics, r.trace)
        await engine.stage(op)
        out["operation"] = op.public()
    ledger.append("parse", text=text, result_kind=out["kind"], reason=r.reason, trace=r.trace, metrics=metrics)
    await bus.emit("parsed", **{k: v for k, v in out.items() if k != "operation"})
    if unclear:
        await bus.emit("say", text=out["reply"])
    elif r.kind == "clarify":  # understood, refused as said: its reason, and nothing is stopped
        await bus.emit("say", text=_sentence(r.short or r.reason))
    elif r.quip:
        await bus.emit("say", text=r.quip if r.kind != "abstain" else f"{r.quip} {r.reason}.")
    return out


async def _misheard_command(text: str, source: str, t0: float) -> dict:
    """Nothing was heard but noise (an empty transcript, or only whisper's markers): misheard, so Carter's rule.
    As for a stop word, the stop (if a task is running) comes first and the telemetry after it, best effort."""
    out = {"kind": "clarify", "reason": "I didn't catch that.", "quip": "", "trace": ["misheard: no words in it"],
           "data": {}, "metrics": {"plan_ms": int((time.time() - t0) * 1000), "planner_calls": 0}}
    await _clarify(out, out["reason"], source)
    await best_effort(lambda: ledger.append("command", text=text, source=source),
                      lambda: ledger.append("parse", text=text, result_kind="clarify", reason=out["reason"],
                                            trace=out["trace"], metrics=out["metrics"]),
                      lambda: bus.emit("heard", text=text, source=source),
                      lambda: bus.emit("parsed", **out),
                      lambda: bus.emit("say", text=out["reply"]))
    return out


async def _stop_command(text: str, source: str, t0: float) -> dict:
    """A spoken or typed stop acts before telemetry, memory, the brain bridge or any planner: none of them may
    delay it or read it as something else. engine.stop() latches and stops the robot (and the brain, when the
    bridge is on) before its own telemetry; this command's telemetry comes after that, best effort.
    If a stop call did not confirm, the answer and the spoken text say so: the latch holds, but the robot may
    still be moving."""
    res = await engine.stop(by=f"command:{source}")
    unconfirmed = stop_unconfirmed(res, engine.robot)
    out = {"kind": "stop", "reason": "", "quip": quip("stop"), "trace": ["stop word"], "data": {},
           "robot": res, "stop_confirmed": not unconfirmed,
           "metrics": {"plan_ms": int((time.time() - t0) * 1000), "planner_calls": 0}}
    if unconfirmed:
        out["reason"] = f"I could not confirm the robot stopped ({'; '.join(unconfirmed)}). Cut the motor power."
        out["quip"] = "I could not confirm the robot stopped. Cut the motor power."
        out["trace"].append(f"stop not confirmed: {'; '.join(unconfirmed)}")
    await best_effort(lambda: ledger.append("command", text=text, source=source),
                      lambda: ledger.append("parse", text=text, result_kind="stop", reason=out["reason"],
                                            trace=out["trace"], metrics=out["metrics"]),
                      lambda: bus.emit("heard", text=text, source=source),
                      lambda: bus.emit("parsed", **out),
                      lambda: bus.emit("say", text=out["quip"]))
    return out


async def _fire_later(task: dict, delay: float):
    await asyncio.sleep(delay)
    task["status"] = "FIRED"
    await bus.emit("prospective_fired", task=task)
    await bus.emit("say", text="Scheduled task is due. Plan staged for your approval.")
    await handle_command(task["text"], "prospective")


_declined_brain_plans: set[str] = set()  # brain plans a command's reply did not state understood
_brain_replies_unchecked = 0  # brain commands sent whose reply is not checked yet


def _brain_understood(p: dict) -> bool:
    """Did the brain state that it understood this proposal? Only when it says so both ways it can: classification
    "understood" and needs_clarification false. A missing or contradictory field counts as unclear."""
    return p.get("classification") == "understood" and p.get("needs_clarification") is False


async def _brain_command(text: str, source: str, t0: float) -> dict:
    """EC08: Bharath's brain plans; the console shows it and relays the human approval. Until the reply is checked,
    the pending loop stages nothing: the brain may list this command's plan as waiting before it answers, and only
    the checked reply decides whether that plan is staged or declined."""
    global _brain_replies_unchecked
    _brain_replies_unchecked += 1
    try:
        return await _brain_reply(text, source, t0)
    finally:
        _brain_replies_unchecked -= 1


async def _brain_reply(text: str, source: str, t0: float) -> dict:
    try:
        p = await brain.command(text)
    except Exception as e:  # noqa: BLE001
        msg = f"ENGRAM brain unreachable ({brain.URL}): {e}"
        await bus.emit("log", level="error", msg=msg)
        await bus.emit("say", text="I can't reach the planner right now.")
        return {"kind": "abstain", "reason": msg}
    metrics = {"plan_ms": int((time.time() - t0) * 1000), "planner_calls": p.get("planner_calls", 0)}
    if p.get("memory"):
        await bus.emit("bundle", bundle=brain.to_bundle(p["memory"]), memory_ms=0)
    kind = p.get("kind")
    out = {"kind": kind, "brain": p, "metrics": metrics}
    if kind == "proposal" and not _brain_understood(p):
        # Unclear or misheard (an unknown object, a time that did not parse, nothing heard): Carter's rule. It is
        # checked for every proposal before anything is staged: a plan the brain does not state it understood is
        # never staged, and a refusal it does not state it understood is not an understood refusal.
        reason = p.get("reason") or "I'm not sure what you meant."
        out["reason"] = reason
        if p.get("operation_id") and p.get("digest"):  # a plan declined here is not staged later from the pending list
            _declined_brain_plans.add(p["operation_id"])
        await _clarify(out, reason, source, running=True if p.get("stopped") is True else None)
        trace = [f"brain: {p.get('status')}", f"brain: classification {p.get('classification')!r}, needs_clarification "
                 f"{p.get('needs_clarification')!r}: not stated understood, so unclear"]
        await bus.emit("parsed", kind="clarify", reason=reason, quip="", trace=trace, data={}, metrics=metrics,
                       stopped=out["stopped"])
        await bus.emit("say", text=out["reply"])
    elif kind == "proposal" and p.get("status") == "proposed" and p.get("digest"):
        op = brain.RemoteOperation(p, text, source, metrics)
        await engine.stage(op)
        out["kind"], out["operation"] = "plan", op.public()
        await bus.emit("say", text="Here's my plan. Hold to approve.")
    elif kind == "proposal":  # understood and refused (a prohibited action, "don't"): unchanged
        reason = p.get("reason") or p.get("status") or "refused"
        out.update(kind="abstain", reason=reason)
        await bus.emit("parsed", kind="abstain", reason=reason, quip="", trace=[f"brain: {p.get('status')}"], data={}, metrics=metrics)
        await bus.emit("say", text=reason)
    elif kind == "scheduled":
        task = {"id": p.get("task_id"), "text": p.get("request"), "trigger": p.get("trigger"), "status": "PENDING", "created_at": time.time()}
        out["prospective"] = task
        await bus.emit("prospective", task=task, tasks=[task])
        await bus.emit("say", text=f"Okay. I'll do that when {p.get('trigger')}, and ask you first.")
    elif kind == "taught":
        await bus.emit("rule", result=p, text=text)
        await bus.emit("say", text="Got it. I saved that as a new source.")
    elif kind == "answer":
        await bus.emit("say", text=p.get("reply") or "I have no record of that.")
    ledger.append("brain_command", text=text, source=source, result_kind=kind, operation_id=p.get("operation_id"))
    return out


async def _stage_brain_pending() -> None:
    """Show each new plan the brain lists as waiting (gestures, fired triggers) for approval. Not one the console
    declined when a command's reply brought it: the brain did not state it understood it, so it is never staged.
    And nothing while a command's reply is not checked yet: the next pass shows what is still waiting then."""
    for p in await brain.pending():
        if _brain_replies_unchecked:  # a command's plan may be in this list: its reply decides, never this loop
            return
        oid = p.get("operation_id")
        if oid and oid not in engine.ops and oid not in _declined_brain_plans and p.get("digest"):
            try:
                await engine.stage(brain.RemoteOperation(p, (p.get("plan") or {}).get("goal", ""), "brain", {}))
                await bus.emit("say", text="A new plan is waiting for your approval.")
            except Exception as e:  # noqa: BLE001
                await bus.emit("log", level="warn", msg=f"pending plan not shown: {e}")


async def _brain_pending_loop():
    """Plans the brain made on its own (gestures, fired triggers) also show up for approval."""
    while True:
        await _stage_brain_pending()
        await asyncio.sleep(2)


@app.post("/commands")
async def commands(cmd: Command):
    return await handle_command(cmd.text, cmd.source)


class Decision(BaseModel):
    digest: str = ""


@app.post("/operations/{oid}/approve")
async def approve(oid: str, d: Decision):
    ok, why = await engine.approve(oid, d.digest)
    if not ok:
        raise HTTPException(409, why)
    return {"ok": True}


@app.post("/operations/{oid}/reject")
async def reject(oid: str):
    if not await engine.reject(oid):
        raise HTTPException(409, "not pending")
    return {"ok": True}


class Verify(BaseModel):
    ok: bool
    note: str = ""


@app.post("/operations/{oid}/verify")
async def verify(oid: str, v: Verify):
    if not await engine.verify(oid, v.ok, v.note):
        raise HTTPException(409, "operation not awaiting verification")
    return {"ok": True}


@app.get("/operations")
async def operations():
    return [o.public() for o in engine.ops.values()]


@app.post("/stop")
async def stop():
    res = await engine.stop(by="hud_button")
    if stop_unconfirmed(res, engine.robot):  # 502: the HUD shows "STOP request failed: use the robot's physical button!"
        return JSONResponse(res, status_code=502)
    return res


@app.post("/stop/clear")
async def stop_clear():
    return await engine.clear_stop()


# ------------------------------------------------------------------ memory, evidence, skills
class Rule(BaseModel):
    text: str
    tag_id: int | None = None
    zone: str | None = None
    trigger: str | None = None


@app.post("/rules")
async def add_rule(r: Rule):
    fields = {k: v for k, v in r.model_dump().items() if k != "text" and v not in (None, "")}
    res = await memory.add_rule(r.text, fields)
    ledger.append("rule_ingested", text=r.text, result=res)
    await bus.emit("rule", result=res, text=r.text)
    await bus.emit("say", text=quip("rule"))
    return res


@app.get("/evidence/{eid:path}")
async def evidence(eid: str):
    if eid.startswith("frame:"):
        p = engine.frames_dir / f"{eid[6:]}.jpg"
        if p.exists():
            return FileResponse(p, media_type="image/jpeg")
        raise HTTPException(404)
    ev = await memory.evidence(eid)
    if ev:
        return ev
    try:
        rec = await ledger.find(eid)  # read off the event loop: a stalled disk cannot hold up a stop
    except LedgerBusy as e:  # the disk is not answering: say so, don't pretend the record is missing
        raise HTTPException(503, str(e))
    if rec:
        return {"id": eid, "title": "Ledger record", "passage": rec, "immutable": True}
    raise HTTPException(404, "no such evidence")


@app.get("/skills")
async def skills():
    return await _skills_safe()


@app.post("/skills/{sid}/{decision}")
async def skill_decide(sid: str, decision: str):
    status = {"approve": "APPROVED", "reject": "REJECTED", "revoke": "REVOKED"}.get(decision)
    if not status:
        raise HTTPException(400, "approve | reject | revoke")
    res = await memory.set_skill_status(sid, status)
    if res is None:
        raise HTTPException(404)
    ledger.append("skill_decision", skill_id=sid, status=status, by="operator")
    await bus.emit("skills", skills=await _skills_safe())
    await bus.emit("say", text={"APPROVED": "Skill approved. I'll reuse it next time.",
                                "REJECTED": "Skill rejected. I won't use it.", "REVOKED": "Skill revoked."}[status])
    return res


@app.get("/metrics")
async def metrics():
    return {"runs": engine.runs, "models": STATUS["models"]}


@app.get("/ledger")
async def ledger_tail(n: int = 100):
    if not config.LEDGER_PATH.exists():
        return []
    lines = config.LEDGER_PATH.read_text().splitlines()[-n:]
    import json
    return [json.loads(x) for x in lines]


# ------------------------------------------------------------------ voice
@app.get("/tts")
async def tts(text: str):
    try:
        p = await voice.synthesize(text)
    except voice.VoiceBusy as e:
        raise HTTPException(503, f"{e}; client should use speechSynthesis")
    if not p:
        raise HTTPException(404, "piper not available; client should use speechSynthesis")
    return FileResponse(p, media_type="audio/wav")


@app.post("/stt")
async def stt(request: Request):
    """Multipart form: audio (the clip) and auto ("1": run it as a command). Room in the STT
    queue is reserved BEFORE any of the upload is parsed or read, for the declared length (or
    the queue's byte limit when none is declared): a full queue answers 503 busy, unread. The
    clip is then read bounded by that reservation, which is trimmed to its real size.

    A clip that was transcribed but has no words in it (an empty transcript, or only whisper's
    noise markers) is a misheard command: speech was detected, since the iPad sends only what was
    recorded while the mic was held. It is answered like any command, with "misheard": true, so
    Carter's rule applies (a running task stops, and the reply asks to clarify): run here with
    auto=1; with auto=0 the iPad runs the transcript through /commands as usual, and /stt itself
    still broadcasts nothing. Only a transcription that failed (busy, STT missing, whisper or ffmpeg
    failed) is a 503, as before."""
    limit = config.STT_MAX_PENDING_BYTES
    declared = _declared_length(request.headers.get("content-length"))
    try:
        reservation = voice.reserve_stt(limit if declared is None else min(declared, limit))
    except voice.VoiceBusy as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=503)
    with reservation:  # released on every way out unless the STT job has taken it over
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return JSONResponse({"ok": False, "reason": "no audio file in the upload"}, status_code=422)
        deadline = config.STT_UPLOAD_TIMEOUT_S
        try:
            form = await _read_stt_form(request, deadline)
        except asyncio.TimeoutError:
            return JSONResponse({"ok": False, "reason": f"upload not received within {deadline:g} s"}, status_code=408)
        except ClientDisconnect:
            return Response(status_code=400)  # the client has gone; nobody reads this
        except Exception as e:  # noqa: BLE001 - a malformed (or, past the guard's cap, cut off) upload
            return JSONResponse({"ok": False, "reason": f"unreadable upload: {type(e).__name__}"}, status_code=400)
        try:
            audio, auto = form.get("audio"), form.get("auto", "1")
            if not isinstance(audio, FormFile):
                return JSONResponse({"ok": False, "reason": "no audio file in the upload"}, status_code=422)
            data = await audio.read(reservation.size + 1)  # never more than the room reserved
            if len(data) > reservation.size:
                return _audio_too_large(reservation.size)
            reservation.trim(len(data))
            suffix = Path(audio.filename or "a.webm").suffix or ".webm"
        finally:
            await form.close()
        try:
            text, why = await voice.transcribe(data, suffix, reservation=reservation)
        finally:
            del data  # the queue released its byte reservation: the handler must not keep the clip either
    if text is None:  # the transcription failed: nothing was heard wrong, it was not heard at all
        return JSONResponse({"ok": False, "reason": why}, status_code=503)
    res = {"ok": True, "text": text}
    if voice.no_speech(text):
        res["misheard"] = True
    if auto == "1":
        res["result"] = await handle_command(text, "voice")
    return res


# ------------------------------------------------------------------ simulator + cert
@app.post("/sim/obstacle")
async def sim_obstacle(cm: float | None = None):
    if config.ROBOT_URL != "mock":
        raise HTTPException(400, "simulator only")
    robot.set_obstacle(cm)
    await bus.emit("log", level="info", msg=f"SIM obstacle {'placed at %.0f cm' % cm if cm else 'removed'}")
    return {"ok": True}


@app.get("/ca.crt")
async def ca_cert():
    p = config.ROOT / "certs" / "engram-ca.crt"
    if not p.exists():
        raise HTTPException(404, "run scripts/make_cert.sh first")
    return FileResponse(p, media_type="application/x-x509-ca-cert", filename="engram-ca.crt")
