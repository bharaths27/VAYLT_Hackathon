"""HTTP API for the iPad page (Carther ticket EC12).

Run on the Mac (or the Jetson) with:
    uvicorn --factory engram.api:create_app --host 0.0.0.0 --port 8000

Environment:
    ENGRAM_DB      path of the SQLite memory file (default engram.db)
    ENGRAM_ARENA   arena file (default fixtures/lab/arena.json)
    ENGRAM_MODELS  folder of model files to fingerprint (optional)
    ENGRAM_UI      folder with the built Lovable page to serve at / (optional)
    ENGRAM_ROBOT_URL  robot agent on the MasterPi, e.g. http://192.168.1.50:8100.
                      Leave unset to use the fake robot.
    ENGRAM_ROBOT_API  masterpi (default, robot_agent on 8100) or brian (Brian's engram_robot server on 8200)
    ENGRAM_ROBOT_KEY_FILE  for brian: file holding ENGRAM_ROBOT_SECRET, e.g. ~/engram_robot/robot.env
    ENGRAM_LLM_URL    llama.cpp server completion URL, e.g. http://127.0.0.1:8080/completion.
                      Leave unset to use the rule planner. Any model problem falls back to rules.
    ENGRAM_LLM_TIMEOUT  seconds to wait for the model (default 60; a Pi 4 is slow)
    ENGRAM_VOICE_PRESETS  1 (default) lets "Open the gripper", "Close the gripper" and "Look around"
                      plan as fixed steps (engram/spark/presets.py); 0 turns that off. Either way a
                      person approves every plan by digest before anything moves.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .capabilities import describe_all
from .db import connect
from .engine import Engine, EngineError
from .robot.mock import MockRobot
from .skills import SkillError
from .spark.presets import register_presets

ROOT = Path(__file__).resolve().parent.parent


def voice_presets_enabled() -> bool:
    return os.environ.get("ENGRAM_VOICE_PRESETS", "1").strip().lower() not in ("0", "false", "no", "off")


class SourceIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=200_000)
    kind: str = "document"


class QueryIn(BaseModel):
    goal: str = Field(min_length=1, max_length=500)


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    who: str = "user"


class PlanIn(BaseModel):
    request: str = Field(min_length=1, max_length=500)


class ApproveIn(BaseModel):
    digest: str = Field(min_length=64, max_length=64)
    approved_by: str = Field(min_length=1, max_length=80)


class TagIn(BaseModel):
    tag_id: int
    zone: str


class RestIn(BaseModel):
    zone: str = Field(default="home", min_length=1, max_length=80)


class TriggerIn(BaseModel):
    trigger: str = Field(min_length=1, max_length=200)


def default_engine() -> Engine:
    arena_path = Path(os.environ.get("ENGRAM_ARENA", ROOT / "fixtures/lab/arena.json"))
    arena = json.loads(arena_path.read_text())
    planner = None
    llm_url = os.environ.get("ENGRAM_LLM_URL")
    if llm_url:
        from .planner import LlamaCppPlanner

        planner = LlamaCppPlanner(set(arena["zones"]), llm_url, float(os.environ.get("ENGRAM_LLM_TIMEOUT", "60")))
    engine = Engine(connect(os.environ.get("ENGRAM_DB", "engram.db")), MockRobot(arena), set(arena["zones"]),
                    planner=planner, models_dir=os.environ.get("ENGRAM_MODELS"))
    robot_url = os.environ.get("ENGRAM_ROBOT_URL")
    if robot_url and os.environ.get("ENGRAM_ROBOT_API", "masterpi") == "brian":
        from .robot.brian8200 import BrianRobot, read_secret

        zone_tags = {zone: int(tag) for tag, zone in arena.get("zone_tags", {}).items()}
        secret = read_secret(os.environ["ENGRAM_ROBOT_KEY_FILE"])
        engine.set_robot(BrianRobot(robot_url, engine.memory, secret, zone_tags))
    elif robot_url:
        from .robot.masterpi import MasterPiRobot

        engine.set_robot(MasterPiRobot(robot_url, engine.memory))
    engine.load_arena(arena)
    if not robot_url:
        engine.overhead_scan()  # the fake robot doubles as a fake camera
    return engine


def create_app(engine: Engine | None = None) -> FastAPI:
    engine = engine or default_engine()
    if voice_presets_enabled():
        register_presets(engine)   # same checker and digest-bound approval as every other plan
    app = FastAPI(title="ENGRAM Cortex", version="0.1.0")
    app.state.engine = engine
    # The iPad page is served from the same laptop on the local network.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def fail(e: Exception) -> HTTPException:
        return HTTPException(status_code=409, detail=str(e))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "robot": engine.robot.state().get("adapter")}

    @app.get("/capabilities")
    def capabilities() -> list[dict[str, Any]]:
        return describe_all()

    @app.post("/memory/sources")
    def add_source(body: SourceIn) -> dict[str, Any]:
        return engine.teach(body.title, body.text, body.kind)

    @app.post("/memory/query")
    def memory_query(body: QueryIn) -> dict[str, Any]:
        return engine.memory.retrieve_memory_bundle(body.goal, engine.skills.approved_names()).to_dict()

    @app.get("/memory/objects")
    def memory_objects() -> list[dict[str, Any]]:
        return [{**o.__dict__, "statement": o.describe()} for o in engine.memory.objects()]

    @app.get("/memory/facts")
    def memory_facts() -> list[dict[str, Any]]:
        return engine.memory.current_facts()

    @app.post("/observations/tag")
    def observe_tag(body: TagIn) -> dict[str, Any]:
        return {"object_id": engine.memory.observe_tag(body.tag_id, body.zone)}

    @app.post("/observations/scan")
    def scan() -> dict[str, Any]:
        return {"tags_seen": engine.overhead_scan()}

    @app.get("/evidence/{chunk_id}")
    def evidence(chunk_id: str) -> dict[str, Any]:
        chunk = engine.memory.chunk(chunk_id)
        if not chunk:
            raise HTTPException(404, "no such evidence")
        return chunk

    @app.post("/commands")
    def commands(body: CommandIn) -> dict[str, Any]:
        return engine.command(body.text, body.who)

    @app.post("/plan")
    def plan(body: PlanIn) -> dict[str, Any]:
        return engine.propose(body.request)

    @app.post("/plan/rest")
    def plan_rest(body: RestIn = RestIn()) -> dict[str, Any]:
        """Propose "return to the resting position". Still needs approval before it runs."""
        return engine.propose_rest(body.zone)

    @app.get("/operations/pending")
    def operations_pending() -> list[dict[str, Any]]:
        """Plans waiting for approval that the iPad did not ask for (gestures, fired triggers)."""
        return engine.pending()

    @app.post("/operations/{operation_id}/approve")
    def approve(operation_id: str, body: ApproveIn) -> dict[str, Any]:
        try:
            return engine.approve_and_run(operation_id, body.digest, body.approved_by)
        except EngineError as e:
            raise fail(e)

    @app.post("/stop")
    def stop() -> dict[str, Any]:
        return engine.stop()

    @app.post("/robot/clear_stop")
    def clear_stop() -> dict[str, Any]:
        engine.robot.reset_stop()
        return engine.robot.state()

    @app.get("/robot/state")
    def robot_state() -> dict[str, Any]:
        return engine.robot.state()

    @app.get("/skills")
    def skills() -> list[dict[str, Any]]:
        return engine.skills.list()

    @app.post("/skills/{skill_id}/test")
    def test_skill(skill_id: str) -> dict[str, Any]:
        try:
            return engine.test_skill(skill_id)
        except SkillError as e:
            raise fail(e)

    @app.post("/skills/{skill_id}/approve")
    def approve_skill(skill_id: str, body: ApproveIn) -> dict[str, Any]:
        try:
            engine.skills.approve(skill_id, body.digest, body.approved_by)
        except SkillError as e:
            raise fail(e)
        return engine.skills.get(skill_id)

    @app.post("/skills/{skill_id}/revoke")
    def revoke_skill(skill_id: str) -> dict[str, Any]:
        try:
            engine.skills.revoke(skill_id)
        except SkillError as e:
            raise fail(e)
        return engine.skills.get(skill_id)

    @app.post("/dream/compile")
    def dream() -> dict[str, Any]:
        return {"candidates": engine.dream()}

    @app.get("/prospective")
    def prospective() -> list[dict[str, Any]]:
        return engine.prospective.list()

    @app.post("/events/trigger")
    def trigger(body: TriggerIn) -> list[dict[str, Any]]:
        return engine.fire_trigger(body.trigger)

    @app.get("/events")
    def events(limit: int = 200) -> list[dict[str, Any]]:
        return engine.events(limit)

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        return engine.metrics()

    ui = os.environ.get("ENGRAM_UI")
    if ui and Path(ui).is_dir():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")

    return app
