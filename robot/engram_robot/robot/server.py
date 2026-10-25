"""HTTP contract (Pi 5, FastAPI). Endpoints are plain `def` so /robot/stop is served on its own
worker thread even while a long /robot/execute is running.

    python -m robot.server --sim                 # simulated robot
    python -m robot.server --driver real --config calib.json
    ENGRAM_ESTOP_GPIO=17 python -m robot.server ...   # physical e-stop button (BCM pin)
"""
import argparse
import logging
import os

import cv2
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .config import Config
from .service import RobotService


class ExecuteReq(BaseModel):
    op: str
    args: dict = Field(default_factory=dict)
    operation_id: str
    authorization: str | None = None


class StopReq(BaseModel):
    latch: bool = True
    reason: str = "operator e-stop (API)"


class HeartbeatReq(BaseModel):
    client: str = Field(default="brain", max_length=64)


class ResetReq(BaseModel):
    operator: str = Field(min_length=1)


def create_app(service: RobotService) -> FastAPI:
    app = FastAPI(title="ENGRAM Cortex robot adapter", version="1.0")

    @app.post("/robot/execute")
    def execute(req: ExecuteReq):
        code, body = service.execute(req.op, req.args, req.operation_id, req.authorization)
        return JSONResponse(body, status_code=code)

    @app.post("/robot/stop")
    def stop(req: StopReq | None = None):
        req = req or StopReq()
        service.emergency_stop(req.reason, latch=req.latch)
        return {"stopped": True, "latched": service.safety.estop_latched}

    @app.post("/robot/reset")
    def reset(req: ResetReq):
        was = service.reset(req.operator)
        return {"cleared": was, "operator": req.operator}

    @app.post("/robot/heartbeat")
    def heartbeat(req: HeartbeatReq):
        service.safety.heartbeat(req.client)
        return {"ok": True, "link_timeout_s": service.cfg.link_timeout_s,
                "estop_latched": service.safety.estop_latched}

    @app.post("/robot/link/release")
    def release():
        service.safety.release_link()
        return {"released": True}

    @app.get("/robot/state")
    def state():
        return service.state()

    @app.get("/robot/capabilities")
    def capabilities():
        return service.capabilities()

    @app.get("/robot/frame")
    def frame(annotate: bool = Query(True), quality: int = Query(70, ge=10, le=95)):
        f = service.d.get_frame()
        if f is None:
            return JSONResponse({"error": "E_CAMERA"}, status_code=503)
        if annotate:
            f = service.detector.annotate(f)
        ok, jpg = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return Response(jpg.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/robot/journal")
    def journal(limit: int = Query(50, ge=1, le=500)):
        return service.journal.tail(limit)

    return app


def attach_estop_button(service: RobotService, pin: int):
    """Physical button -> latched e-stop. Wire button between the GPIO pin and GND."""
    from gpiozero import Button  # only needed on the Pi
    btn = Button(pin, pull_up=True, bounce_time=0.02)
    btn.when_pressed = lambda: service.emergency_stop("physical e-stop button")
    return btn


def build_service(driver_name: str, config_path: str | None, journal: str | None) -> RobotService:
    cfg = Config.load(config_path)
    if driver_name == "sim":
        from .sim import default_world
        driver = default_world(cfg)
    else:
        from drivers.real_template import RealDriver
        driver = RealDriver(cfg)
    secret = os.environ.get("ENGRAM_ROBOT_SECRET", "dev-secret-change-me")
    return RobotService(driver, cfg, secret, journal)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--driver", default="sim", choices=["sim", "real"])
    ap.add_argument("--config")
    ap.add_argument("--journal")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8200)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    service = build_service("sim" if a.sim else a.driver, a.config, a.journal)
    if os.environ.get("ENGRAM_ESTOP_GPIO"):
        service._button = attach_estop_button(service, int(os.environ["ENGRAM_ESTOP_GPIO"]))
    import uvicorn
    uvicorn.run(create_app(service), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
