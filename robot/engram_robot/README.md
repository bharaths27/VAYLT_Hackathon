# ENGRAM Cortex — robot adapter (Brian: R01–R05)

Typed, allowlisted, safety-gated robot primitives behind the contract in your packet. Runs today on a
simulator (real ArUco detection, ray-cast LiDAR, a claw that can miss), so Verna and Bharath can
integrate before the hardware is identified. After R01 you implement one file,
`drivers/real_template.py`, and everything else stays the same.

```bash
pip install fastapi uvicorn opencv-python-headless numpy pytest httpx
python -m robot.server --sim                      # http://<pi>:8200
pytest -v tests/                                  # 17 acceptance checks, ~80 s
export ENGRAM_ROBOT_SECRET=...                    # same value in Bharath's approval gate
```

| Ticket | Where |
|---|---|
| R02 primitives | `robot/service.py` (registry + `p_*` functions) |
| R03 LiDAR veto, watchdog, e-stop button | `robot/safety.py`, `attach_estop_button` in `robot/server.py` |
| R04 arena calibration | `tools/make_markers.py`, `tools/calibrate.py`, `calib.json` → `--config` |
| R05 postconditions, failure codes, timing | every `/robot/execute` result; `robot/journal.py` |
| Real hardware | `drivers/real_template.py` (only hardware-specific file) |

---

## R01 — first 45 minutes (do this before writing any code)

Record answers in the evidence log. **Gate: if you cannot demonstrate a reliable stop, no autonomous motion.**

On the Pi 5, start with:
```bash
bash tools/setup_pi5.sh                       # apt deps, groups, venv with --system-site-packages
. .venv/bin/activate
bash tools/r01_probe.sh | tee r01_probe.txt   # read-only inventory: power, USB, serial, cameras, I2C, GPIO
```

Pi 5 gotchas:
- **Power:** the Pi 5 wants a 5 V / 5 A supply. On anything weaker, all USB devices together get 600 mA, and a LiDAR plus webcam can brown out. `vcgencmd get_throttled` must read `0x0` while the motors run.
- **CSI camera:** OpenCV can't open it; the driver uses picamera2 automatically. USB webcams go through OpenCV.
- **GPIO:** `RPi.GPIO` does not work on the Pi 5 (new RP1 chip). gpiozero with the lgpio backend does, and is what the e-stop button uses.
- **Pi's own power button** (on the board) shuts the Pi down. Don't use it as the e-stop: shutting down kills the safety supervisor, and the wheels stop only if the motor firmware has a timeout.
- **Cooling:** if the LLM and vision also run on this Pi, use the active cooler; it throttles at 85 °C.

- [ ] Board: what drives the motors? (Arduino/ESP32 over serial, motor HAT, ROS 2 `/cmd_vel`, vendor SDK). Find the existing control script.
- [ ] Pi 5 RAM (`free -h`), OS, Python version.
- [ ] Power: separate battery for motors vs Pi? Motor stall browning out the Pi = safety failure. Note voltages.
- [ ] Camera: USB or CSI? `python -m drivers.real_template` prints the frame shape.
- [ ] LiDAR model + port (`ls /dev/ttyUSB*`), and which raw angle is the robot's front (point a box at the front, read the angle).
- [ ] Motors: wheels lifted, run existing script: forward, reverse, turn left (does + mean CCW?), **stop**.
- [ ] Does the motor firmware stop on its own if commands stop arriving? If you can edit it, add a 300 ms timeout — it is the backstop if the Pi crashes.
- [ ] Claw: open, close. Any feedback (limit switch, servo position, current)? If not, `gripper_state()` stays `"unknown"`.
- [ ] Encoders? If none, `odometry()` returns `None` and drive/turn honestly report `UNKNOWN`.
- [ ] Both buttons: which GPIO pins? One becomes the latched e-stop (`ENGRAM_ESTOP_GPIO=<BCM pin>`).

Then fill the TODOs in `drivers/real_template.py` and start with `--driver real`.

### DC wheel motors (this robot)
`drivers/motors.py` turns `(v, w)` into left/right PWM duty, with a calibrated deadband, speed scale,
per-side direction and trim, and skid-steer turn slip. Choose the backend in `RealDriver`: `GpioMotors`
for H-bridge drivers on GPIO, or `VendorBoardMotors` wrapping the kit's own SDK. Then calibrate, with
the server stopped:

```bash
python tools/calibrate_motors.py sides      # wheels lifted: which way is forward per side
python tools/calibrate_motors.py deadband   # on floor, tag 7 ~0.8 m ahead: lowest duty that moves
python tools/calibrate_motors.py speed      # m/s per duty, measured with the camera + tag
python tools/calibrate_motors.py turn       # skid-steer slip, you measure the angle
```

No encoders, so `drive_bounded`/`turn_bounded` report `UNKNOWN`, and that's honest.
`approach_tag`, `pick_tagged` and `place_at` close the loop on the camera, so they still report real
SUCCEEDED/FAILED. The planner should move with camera-verified primitives, not dead-reckoned drives.

**Critical stop test:** with wheels lifted and turning, `kill -9` the server. If the wheels keep
spinning, the last PWM value is latched in hardware or on the board. Then the watchdog can't help
when Python dies, and you need either a board-side timeout or a physical power cut-off on the motor
supply. Record the result; it's the first line of the safety story.

---

## Safety design (R03)

All motion goes through `SafetySupervisor.command()`. A separate supervisor thread at 20 Hz reads the
LiDAR and stops the wheels by itself when:

| Trip | Condition | Result status |
|---|---|---|
| `E_LIDAR_VETO` | return in ±30° cone of travel closer than `margin + v·latency + v²/2a` (0.115 m at creep, 0.19 m at 0.2 m/s) | `VETOED` |
| `E_WATCHDOG` | control loop stopped renewing its 0.5 s motion lease | `ABORTED` |
| `E_LIDAR_STALE` | no fresh scan for 0.5 s → fail closed, no motion | `ABORTED` |
| `E_ESTOP` | `/robot/stop` or physical button; **latched** until `POST /robot/reset {operator}` | `ABORTED` / `REJECTED` |
| `E_UNSENSED` | moving in a direction no range sensor covers (rear, with a forward ultrasonic) faster than 0.05 m/s | `VETOED` |

**This robot has a forward ultrasonic sensor, not a LiDAR.** Run with `--config configs/ultrasonic.json`.
The driver returns the ultrasonic reading as a one-beam "scan" at angle 0, and the same veto logic
applies. Consequences, which must also go in the pitch and to the integration owner:
- Only the front is protected, and only within the ultrasonic beam (~±12–15°). Angled, thin or soft obstacles can give no echo.
- Reversing is blind, so it is capped at 0.05 m/s and 0.20 m per operation (enough for the grasp check). Longer reverses are refused.
- In-place turns are unprotected; keep the arena clear around the robot.
- The spec's "LiDAR stop" acceptance check becomes "ultrasonic forward stop"; say so rather than claiming LiDAR.

The planner is a separate process and can only request typed primitives, so a stalled planner cannot
keep the wheels turning (`test_A1_supervisor_stops_robot_even_if_controller_ignores_obstacles`).

Known limits, say these out loud to judges:
- The supervisor is a thread in the robot process. If the whole process dies, only the motor firmware timeout stops the robot — that is why R01 asks about it.
- Small grasp objects usually sit below the LiDAR plane, so they don't trigger the veto during the final approach. If yours does see them, the speed-dependent envelope at creep (0.115 m) must be less than the LiDAR-to-object distance at grasp; Deniz measures this.
- In-place turns are not vetoed (the robot does not advance). Rear motion uses the rear cone.

---

## Handoffs

### Verna (by hour 2) — live view
- `GET /robot/frame?annotate=true` → JPEG with tag boxes, id, distance, bearing. Poll at 5–10 Hz from Safari.
- `GET /robot/state` → pose, gripper, `held_tag_verified`, busy op, safety block (e-stop, LiDAR front/rear minimum, recent trips).
- `GET /robot/journal?limit=50` → evidence timeline. Put a big STOP button on `POST /robot/stop`.

### Bharath — primitive contract
`GET /robot/capabilities` is the machine-readable version (args, bounds, defaults, effect, postcondition, codes).

```
POST /robot/execute {"op", "args", "operation_id" (8–128 chars, unique), "authorization"}
```

| op | args | physical | postcondition (what SUCCEEDED means) |
|---|---|---|---|
| `stop` | – | no | stationary over 0.3 s (odometry) |
| `drive_bounded` | `distance_m` ±1.5, `speed_mps` 0.02–0.2 (0.15) | yes | odometry within 2 cm |
| `turn_bounded` | `angle_deg` ±180 (+ = left), `rate_deg_s` (45) | yes | odometry within 3° |
| `observe` | – | no | frame captured, `data.tags` |
| `detect_tag` | `tag_id` 0–49 | no | seen in ≥2/3 frames, `data.detection` |
| `search_tag` | `tag_id`, `max_turn_deg` (360) | yes | tag visible after 30° steps |
| `approach_tag` | `tag_id`, `stop_distance_m` 0.12–1.0 (0.30), `speed_mps` | yes | tag at distance ±3 cm, centred ±6° |
| `gripper_open` / `gripper_close` | – | yes | gripper sensor agrees (close does **not** mean holding) |
| `pick_tagged` | `tag_id` | yes | tag stays at constant distance while robot reverses 10 cm → object travels with robot |
| `place_at` | `zone_tag_id`, `object_tag_id` (default: verified held) | yes | object tag recedes as robot reverses → it stayed at the zone |

**Statuses — only `SUCCEEDED` means a sensor confirmed it.** `FAILED` (sensor says no), `UNKNOWN`
(can't tell — never assume success, never auto-retry a physical op), `VETOED`, `ABORTED`, `REJECTED`
(never started; HTTP 400/403/409/422/423).

**Authorization** — physical ops need a token for exactly that `{operation_id, op, args}`, max 60 s:
```python
from robot.auth import sign
token = sign(os.environ["ENGRAM_ROBOT_SECRET"], operation_id, op, args, ttl_s=30)
```
Sign the args dict exactly as you send it. Same `operation_id` twice returns the stored result with
`"replayed": true` and does nothing. Ops interrupted by a crash come back as `UNKNOWN/E_INTERRUPTED`
after restart and are never replayed.

Every result has `timing.started_at/ended_at/duration_ms` and `postcondition.evidence` (raw sensor
numbers) for the episode log and the Experience Compiler.

### Deniz — manual stop test + calibration data
1. Server running, robot on the floor, clear 1.5 m ahead. Hand on the physical e-stop.
2. **Button:** send `drive_bounded 1.0`; press the button at ~0.3 m. Expect `ABORTED/E_ESTOP`, robot still within ~2 cm, next command `423` until `POST /robot/reset`.
3. **API stop:** same, using `POST /robot/stop` from the iPad.
4. **LiDAR veto:** box 1.0 m ahead, `drive_bounded 1.5 @ 0.2`. Expect `VETOED`. Tape-measure LiDAR-to-box gap. Repeat 5×, also at 0.1 and 0.05 m/s.
5. **Watchdog:** during a drive, `kill -STOP` the server process → wheels must stop (firmware timeout); `kill -CONT` after. If they don't stop, that is a blocker — report it.
6. **Stale LiDAR:** unplug LiDAR USB, try `drive_bounded 0.2` → `ABORTED/E_LIDAR_STALE`, no motion.

Record for each run: speed, measured stop gap, result code. Set `safety_decel` from the worst run so
every measured gap ≥ `safety_margin_m`.

---

## R04 arena + R05 fallback

1. `python tools/make_markers.py 7 8 --mm 40 -o objects.png` and `... 20 21 --mm 60 -o zones.png`. Print at 100%, measure, update `default_tag_size_m` / `tag_sizes_m`.
2. Tag on the object's top/side so it's still visible to the camera when the claw is closed (otherwise every pick is honestly `UNKNOWN`).
3. `tools/calibrate.py focal --tag 7 --true-m 0.50`, then place the object where the claw grasps reliably and run `tools/calibrate.py standoff --tag 7`. Put both values in `calib.json`.
4. Run `pick_tagged` 10× and record hits/misses honestly.

**Hour-7 kill switch:** if grasping isn't reliable, the demo becomes `search_tag` → `approach_tag(zone, 0.30)` → `observe`, which needs no claw and still produces verified postconditions.

---

## Evidence log

`ticket ID | commit | input fixture | procedure | observed result | limitation | next owner`

```
R03 | <commit> | sim, obstacle at 0.8 m | pytest test_A1_lidar_vetoes_drive_into_obstacle | VETOED, no collision | sim only; repeat on hardware (Deniz step 4) | Deniz
R03 | <commit> | sim | test_A1_watchdog_stops_when_control_loop_dies | wheels zero after lease | in-process; firmware timeout untested | Brian
R05 | <commit> | sim, tag 7 @0.7 m, miss prob 1.0 | test_A3_missed_grasp_is_reported_failed | FAILED/E_GRASP_FAILED | sim claw model | Bharath
R05 | <commit> | sim, tag occluded after close | test_A3_unverifiable_grasp_is_unknown_not_success | UNKNOWN, held_tag None | — | Bharath
```

---

## Two-Pi setup: brain Pi + bot Pi

```
 iPad ──Wi-Fi──► BRAIN Pi 10.0.0.1             BOT Pi 10.0.0.2 (star-pi)
                 memory, LLM, planner,  ─eth─►  robot.server: hardware, safety,
                 approval gate, client.py       ultrasonic stop, watchdog, journal
```

The brain never touches motors. It sends typed primitives; the bot validates, checks the
authorization, runs the primitive under the safety supervisor, and returns sensor-checked results.

1. **Cable** the two Pis' Ethernet ports together, then run `bash deploy/link_setup.sh bot` on the
   bot and `bash deploy/link_setup.sh brain` on the brain. Test with `ping 10.0.0.2` from the brain.
2. **Shared secret:** generate one with `python3 -c "import secrets; print(secrets.token_hex(16))"` and
   put `ENGRAM_ROBOT_SECRET=<it>` in `~/engram_robot/robot.env` on the bot and in the brain's
   environment. Same value on both.
3. **Bot auto-start:** `bash tools/setup_pi5.sh` once (needs internet), then install
   `deploy/engram-robot.service` (instructions at the top of that file).
4. **Brain:** copy `robot/client.py` over (standard library only) and start from `tools/brain_example.py`.

Brain rules:
- `approved=True` only after the gate (and the human, for the demo) approved that exact op and args.
- Only `SUCCEEDED` is success. Don't auto-retry `UNKNOWN` physical ops.
- Reuse the same `operation_id` to retry after a network error; the bot returns the stored result instead of moving twice.
- **Heartbeat:** the client pings every 0.4 s. If the brain crashes or the cable is pulled, the bot stops within 1.5 s (`E_LINK_LOST`). Call `close()` to leave cleanly.
