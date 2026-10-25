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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brain, config, llm, voice
from .adapters.memory import make_memory
from .adapters.robot import make_camera, make_robot
from .core import Bus, Engine, Ledger, Operation, hash_models, upstream_online
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
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ENGRAM Cortex Console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"ok": True, "robot": config.ROBOT_URL, "memory": config.MEMORY_URL, "uptime_s": int(time.time() - STATUS["boot"])}


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


async def handle_command(text: str, source: str) -> dict:
    t0 = time.time()
    await bus.emit("heard", text=text, source=source)
    ledger.append("command", text=text, source=source)
    if is_stop(text):
        return await _stop_command(text, source, t0)
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
            r.kind, r.plan, r.quip = "plan", plan, quip("plan")
        else:
            r.reason = why
    metrics = {"memory_ms": memory_ms, "parse_ms": parse_ms, "planner_calls": planner_calls,
               "plan_ms": int((time.time() - t0) * 1000)}
    out = {"kind": r.kind, "reason": r.reason, "quip": r.quip, "trace": r.trace, "data": r.data, "metrics": metrics}

    if r.kind == "stop":
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
    ledger.append("parse", text=text, result_kind=r.kind, reason=r.reason, trace=r.trace, metrics=metrics)
    await bus.emit("parsed", **{k: v for k, v in out.items() if k != "operation"})
    if r.quip:
        await bus.emit("say", text=r.quip if r.kind != "abstain" else f"{r.quip} {r.reason}.")
    return out


async def _stop_command(text: str, source: str, t0: float) -> dict:
    """A spoken or typed stop acts before memory, the brain bridge or any planner is asked: none of them
    may delay it or read it as something else. engine.stop() also stops the brain when the bridge is on."""
    await engine.stop(by=f"command:{source}")
    out = {"kind": "stop", "reason": "", "quip": quip("stop"), "trace": ["stop word"], "data": {},
           "metrics": {"plan_ms": int((time.time() - t0) * 1000), "planner_calls": 0}}
    ledger.append("parse", text=text, result_kind="stop", reason="", trace=out["trace"], metrics=out["metrics"])
    await bus.emit("parsed", **out)
    await bus.emit("say", text=out["quip"])
    return out


async def _fire_later(task: dict, delay: float):
    await asyncio.sleep(delay)
    task["status"] = "FIRED"
    await bus.emit("prospective_fired", task=task)
    await bus.emit("say", text="Scheduled task is due. Plan staged for your approval.")
    await handle_command(task["text"], "prospective")


async def _brain_command(text: str, source: str, t0: float) -> dict:
    """EC08: Bharath's brain plans; the console shows it and relays the human approval."""
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
    if kind == "proposal" and p.get("status") == "proposed" and p.get("digest"):
        op = brain.RemoteOperation(p, text, source, metrics)
        await engine.stage(op)
        out["kind"], out["operation"] = "plan", op.public()
        await bus.emit("say", text="Here's my plan. Hold to approve.")
    elif kind == "proposal":
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


async def _brain_pending_loop():
    """Plans the brain made on its own (gestures, fired triggers) also show up for approval."""
    while True:
        for p in await brain.pending():
            oid = p.get("operation_id")
            if oid and oid not in engine.ops and p.get("digest"):
                try:
                    await engine.stage(brain.RemoteOperation(p, (p.get("plan") or {}).get("goal", ""), "brain", {}))
                    await bus.emit("say", text="A new plan is waiting for your approval.")
                except Exception as e:  # noqa: BLE001
                    await bus.emit("log", level="warn", msg=f"pending plan not shown: {e}")
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
    return await engine.stop(by="hud_button")


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
    rec = ledger.find(eid)
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
    p = await voice.synthesize(text)
    if not p:
        raise HTTPException(404, "piper not available; client should use speechSynthesis")
    return FileResponse(p, media_type="audio/wav")


@app.post("/stt")
async def stt(audio: UploadFile = File(...), auto: str = Form("1")):
    data = await audio.read()
    suffix = Path(audio.filename or "a.webm").suffix or ".webm"
    text, why = await voice.transcribe(data, suffix)
    if not text:
        return JSONResponse({"ok": False, "reason": why}, status_code=503)
    res = {"ok": True, "text": text}
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
