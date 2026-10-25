# ENGRAM Cortex: iPad Command Center (Verna)

The control room for ENGRAM Cortex. It runs on the Raspberry Pi 5, and you open it in Safari on the iPad.
No internet needed. From it you can:

- watch the robot's camera, with boxes drawn around the ArUco tags it sees
- type or say a command and see exactly what the robot plans to do, and which rule told it to
- hold to approve before anything moves, or hit the big STOP
- teach a new rule live, then approve a new reusable skill the system learned from a run

## How it fits the team
```
iPad Safari ──> this console (Verna) ──> memory API (Bharath)
                                     └─> robot API (Brian) ──> wheels, claw, camera, LiDAR
                       Deniz runs the tests and scoreboard against all of it
```
Until Brian's and Bharath's APIs are running, the console uses a **built-in simulator** and **sample lab memory**,
so you can build, practice and screenshot right now. Everything simulated is labeled SIMULATED on screen.

## Run it on your laptop right now (2 minutes)
```bash
pip install -r requirements.txt
./scripts/run.sh            # then open http://localhost:8000
```
Tap ENGAGE, then try the quick buttons under the command box.

## Run it on the Pi for the real demo
See `docs/DEMO.md`. Short version:
```bash
./scripts/setup_pi.sh       # once, while the Pi still has internet
./scripts/hotspot.sh        # Pi becomes its own Wi-Fi, no internet behind it
ENGRAM_ROBOT_URL=http://127.0.0.1:8100 ENGRAM_MEMORY_URL=http://127.0.0.1:8200 ./scripts/run.sh
```
Change the ports to whatever Brian and Bharath actually use.

## Settings (environment variables)
| Variable | Default | What it does |
|---|---|---|
| `ENGRAM_ROBOT_URL` | `mock` | Brian's robot API, or `mock` for the simulator |
| `ENGRAM_MEMORY_URL` | `mock` | Bharath's memory API, or `mock` for sample SOP memory |
| `ENGRAM_CAMERA` | `robot` | `opencv:0` reads a USB camera directly if the robot frame isn't ready |
| `ENGRAM_LLM_URL` | `off` | local llama.cpp server; only turn on after testing it on the Pi |
| `ENGRAM_APPROVAL_TTL_S` | `90` | seconds an approval stays valid |

## Tests
```bash
python3 -m pytest -q          # 8 tests, including property-based safety tests (hypothesis)
python3 scripts/smoke.py      # live end-to-end run against a running console
```

## Safety rules built in
- Only 10 allowlisted moves, each with hard limits (max 50 cm drive, max 180° turn). Anything else is refused.
- Unsafe requests are refused. Unclear or misheard ones are answered with a question that ends "Please clarify.";
  if a task is running, it is stopped first, latched like STOP ("I stopped. ... Please clarify."), and it never
  resumes on its own. With nothing running, nothing is stopped. No plan is made either way.
- Approval is a 0.8 second hold, tied to a fingerprint (digest) of the exact plan shown, and it expires.
- The console checks LiDAR before every forward move, on top of Brian's own veto.
- Success is only shown when a sensor confirms it, or when you confirm it. A timeout is never retried.
- STOP cancels everything pending and blocks new approvals until cleared.
- Every command, plan, approval and result is written to `data/ledger.jsonl` (append only).

Files: `server/` (Python backend), `web/` (the iPad screen), `tests/`, `docs/CONTRACTS.md` (send to teammates),
`docs/DEMO.md` (your recording script), `docs/KIRO.md` (write-up draft), `.kiro/` (specs, steering, hooks).
