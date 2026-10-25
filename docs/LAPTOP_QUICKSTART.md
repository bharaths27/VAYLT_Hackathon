# ENGRAM on your laptop (no robot needed)

ENGRAM is a lab robot's "brain": you type a command, it checks what it has been taught, shows you a plan, and
waits for you to approve it before anything moves. On a laptop the robot is a pretend one (a mock), so nothing can
move or break: this is the safe place to practice.

You will run two programs, each in its own terminal window, and open one web page:

| Program | Folder | Address | What it is |
|---|---|---|---|
| Brain | `VAYLT_Hackathon` | http://127.0.0.1:8001 | plans, memory, approvals (pretend robot) |
| Console | `VAYLT_Hackathon/console` | http://127.0.0.1:8000 | the screen you type into (Verna's console) |

**How this guide was checked.** On 2026-10-25 every Windows (PowerShell) command was run exactly as written on a
Windows 11 PC, in a fresh clone of `main` (commit `920d9ce`), with Python 3.13 (`py -3.13`). The same steps also
ran with Python 3.11.9 on an earlier commit that day. Ports 18001 and 18000 were used instead of 8001 and 8000,
because another test was using those. Later the same day `scripts/serve_dual.py` was fixed for Windows and
`requirements.txt` was opened up for newer Pythons; both were re-checked on Windows. The macOS/Linux commands are the same steps with Mac paths. They were **not tested here**, on a Mac
or on Linux.

**Use this page, not the "Run it on a Mac" part of the README.** That part points at an older copy of the repo.
`scripts/cds_*.sh` and `docs/CDS_RUNBOOK.md` are for the Raspberry Pi on the robot, not for a laptop.

---

## 1. Check your Python (the most common problem)

You need **Python 3.11 or newer; 3.13 is the safest** (everything on this page was checked with it).

- **Python 3.14 and 3.15** install the brain and pass its tests (checked), but the console was not checked on
  them. If you have the choice, use 3.13.
- **The `python3` that comes with a Mac is 3.9: the install works but the brain crashes on start** (seen here
  with Python 3.9).

| macOS / Linux (Terminal) | Windows (PowerShell) |
|---|---|
| `python3.13 --version` | `py --list` |

- **macOS / Linux:** you want `Python 3.13.x`. If you get "command not found", try `python3.12 --version`
  and `python3.11 --version`. If none works, install Python 3.13: on https://www.python.org/downloads/ find
  "Looking for a specific release?", pick the newest **3.13.x**, and take the macOS installer. If you use another
  version, put its number everywhere this guide says `3.13`. (Not tested here.)
- **Windows:** the list must include a line with `3.13`, `3.12` or `3.11`. If it doesn't, or `py` is "not
  recognized", install Python 3.13 the same way (the "Windows installer (64-bit)"); it comes with `py`. If you use
  3.12 or 3.11, put that number everywhere this guide says `3.13`.

You also need **Git** (`git --version` should print a version; if not, install it from https://git-scm.com, not
tested here) and a web browser.

---

## 2. Get the code (once)

Do this in a **new terminal window** (a normal one, not "as administrator"). It starts in your home folder, and
that is where the code will go, so `cd VAYLT_Hackathon` works in every new terminal later. To check, type `pwd`:
it should show something like `/Users/yourname` (Mac) or `C:\Users\yourname` (Windows).

| | macOS / Linux (Terminal) | Windows (PowerShell) |
|---|---|---|
| clone | `git clone https://github.com/mrrCarter/VAYLT_Hackathon.git` | `git clone --config core.autocrlf=false https://github.com/mrrCarter/VAYLT_Hackathon.git` |
| go in | `cd VAYLT_Hackathon` | `cd VAYLT_Hackathon` |

(Windows: `--config core.autocrlf=false` keeps the files exactly as they are on GitHub. Without it, one test
fails later; nothing else changes.)

---

## 3. Install (once, about 2 minutes)

The brain and the console each get their own Python environment (a `.venv` folder), the same way they run on the
robot. You never need to "activate" them: every command below calls the `.venv` Python directly.

Start in the `VAYLT_Hackathon` folder:

| | macOS / Linux (Terminal) | Windows (PowerShell) |
|---|---|---|
| brain environment | `python3.13 -m venv .venv` | `py -3.13 -m venv .venv` |
| brain install | `.venv/bin/python -m pip install -r requirements.txt` | `.venv\Scripts\python -m pip install -r requirements.txt` |
| go to the console folder | `cd console` | `cd console` |
| console environment | `python3.13 -m venv .venv` | `py -3.13 -m venv .venv` |
| console install | `.venv/bin/python -m pip install -r requirements.txt` | `.venv\Scripts\python -m pip install -r requirements.txt` |
| back up | `cd ..` | `cd ..` |

Each install ends with `Successfully installed ...`. A yellow `[notice] A new release of pip is available` is
fine: ignore it.

**Optional check (about 2 minutes):** run the tests. Each should end with a line saying `passed` and no `failed`.

| | macOS / Linux (Terminal) | Windows (PowerShell) |
|---|---|---|
| brain tests (in `VAYLT_Hackathon`) | `.venv/bin/python -m pytest` | `.venv\Scripts\python -m pytest` |
| console tests | `cd console` then `.venv/bin/python -m pytest tests` then `cd ..` | `cd console` then `.venv\Scripts\python -m pytest tests` then `cd ..` |

On Windows this showed `964 passed, 41 skipped` and `360 passed` (the numbers grow as the team adds tests).
Skipped tests need the Pi, Linux or extra tools; that is normal. If you see `1 failed`, look it up in the table at
the end.

---

## 4. Start the brain (terminal 1)

In the `VAYLT_Hackathon` folder:

| macOS / Linux (Terminal) | Windows (PowerShell) |
|---|---|
| `.venv/bin/python -m uvicorn --factory engram.api:create_app --host 127.0.0.1 --port 8001` | `.venv\Scripts\python -m uvicorn --factory engram.api:create_app --host 127.0.0.1 --port 8001` |

It is ready when it prints `Uvicorn running on http://127.0.0.1:8001 (Press CTRL+C to quit)`. Leave this window
open; it keeps running.

(On the Pi the brain is started with `scripts/serve_dual.py`, which listens on every network address for the
iPad. It works on a laptop too, but you don't need it there: plain uvicorn on 127.0.0.1 is simpler.)

## 5. Start the console (terminal 2)

Open a **second** terminal window, then:

| | macOS / Linux (Terminal) | Windows (PowerShell) |
|---|---|---|
| go to the console | `cd VAYLT_Hackathon/console` | `cd VAYLT_Hackathon\console` |
| tell it where the brain is | (part of the next line) | `$env:ENGRAM_BRAIN_URL = "http://127.0.0.1:8001"` |
| start it | `ENGRAM_BRAIN_URL=http://127.0.0.1:8001 .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8000` | `.venv\Scripts\python -m uvicorn server.main:app --host 127.0.0.1 --port 8000` |

It is ready when it prints `Uvicorn running on http://127.0.0.1:8000`. Leave it open too.

**Do not skip `ENGRAM_BRAIN_URL`.** Without it the console still starts and even plans "Open the gripper", but it
uses its own small demo memory instead of the brain, and most of the commands below fail. (Windows: set it again
each time you open a new console window.)

## 6. Check that they are connected

In your browser, open these two addresses:

- http://127.0.0.1:8001/health must show `{"ok":true,"robot":"mock"}` (`mock` = the pretend robot).
- http://127.0.0.1:8000/status must show `"brain_url":"http://127.0.0.1:8001"` and
  `"brain":{"ok":true,"robot":"mock"}`. If it shows `"brain_url":"off"`, see the table at the end.

## 7. Open the console page

In your browser go to **http://127.0.0.1:8000** (tested here with Chrome on Windows; Safari is what the iPad uses).

- It boots for a few seconds, then shows Camera, Plan and Mini map panels.
- The first time, a 4-step tour pops up: click **Next** or **Skip tour**.
- The camera picture and mini map come from the console's own simulator (tags 7, 8, 9 and zones A, B). They do
  not show Sample A or Sample B. That is normal with the pretend robot.

---

## 8. How to give commands

- Click the box under **Tell the robot**, type a command, press **Enter** (or click **SEND**). The **HEARD** line
  under the box then shows what the console received.
- Ignore the grey example in the box and the round quick buttons under it ("prep the bench for calibration",
  "bring the blue block to zone A", and so on). They belong to the console's own demo and the brain answers them
  with "I don't know which object you mean".
- **Approve = press and hold** the orange **HOLD TO APPROVE** button for about a second, until it fills. A quick
  click does nothing. You have 90 seconds (the countdown circle), then the plan expires.
- **STOP** is the red button at the bottom right (or press **Esc** twice). To carry on afterwards, click
  **CLEAR E-STOP** at the top.
- **Answers are spoken, not written.** The console reads its replies aloud with your computer's voice, so turn
  your sound on. (Not tested here: the test browser had no sound.) If you also want to read them: click
  **Simple** at the top right (it changes to **Expert**) and zoom the browser out (Ctrl and minus on Windows, Cmd
  and minus on a Mac, until 80% or smaller). The **Activity** panel at the bottom right then shows each reply as a
  line starting with a speaking-head icon.
- **Microphone: type instead.** Speech-to-text (whisper.cpp) is only set up on the Pi
  (`console/scripts/setup_pi.sh`). On a laptop the mic button can record, but then says "Speech-to-text
  unavailable: type the command." (Server side checked here; the mic itself not tested here.)

## 9. The nine test commands

Type them in this order. They are the pretend-robot checks from section 8 of `docs/CDS_RUNBOOK.md`. Each answer
came back in well under a second here (10 to 40 ms).

| # | Type this | What you should see |
|---|---|---|
| T1 | `Open the gripper.` | Plan panel: "Waiting for your approval.", **Open the gripper.**, PENDING APPROVAL, one step **Release** (Gripper open). ("Missing evidence: no source found for this request" in the **Before you approve** box is normal.) Hold **HOLD TO APPROVE**: a green **VERIFIED** banner, the step shows **SUCCEEDED** and the panel says "Done." (pretend robot). |
| T2 | `Stop.` | No plan. A red **E-STOP** banner ("all motion halted · pending plans cancelled"), a red frame, **Robot Stopped** at the top and a **CLEAR E-STOP** button. Click it: "E-STOP cleared. Re-issue commands to continue." |
| T3 | `Don't open the claw.` | No plan. Plan panel: "I won't do that. You said 'don't', so I will not move anything. To make it a rule, say 'Remember that ...'. Nothing was planned, and nothing moved." |
| T4 | `Open the claw in two days.` | No plan. "I won't do that" with "I don't know which object you mean. Can you name it?" (The runbook's other wording, `Open the claw after calibration finishes.`, gives "That sounds like something to do later ('after'), so I will not move now. Say it as 'When <event>, <task>' and I will schedule it.") |
| T5 | `Can the beaker go in the drying oven?` | No plan. It says out loud "I have no record of that." **The Plan panel does not change**: it still shows the previous command's result. HEARD shows your question. (Answers are spoken only, see section 8.) |
| T6 | `Remember that safety goggles must be at the bench before handling Sample B.` | An orange **RULE LEARNED** banner ("cited as src_... · no retraining"), and it says "Got it. I saved that as a new source." |
| T7 | `Bring Sample B to the bench.` | A plan waiting for approval: first **Look**, **Grab** and **Drive** for `object_id=goggles` to `zone=bench`, then the same three for `sample_b`, then a "Say it out loud" step. **Why: rules it found** shows your goggles rule. You don't need to approve it. |
| T8 | `What did I say about the goggles?` | No plan. It says out loud: verna-ipad said "Remember that safety goggles must be at the bench before handling Sample B." -> taught. As in T5, the Plan panel does not change. |
| T9 | `Look around.` | A plan with one step, **Look** (Observe). It runs by itself straight away, shows **SUCCEEDED** and a **VERIFIED** banner: looking moves nothing, so it does not wait for approval. |

The brain remembers what you teach it in the file `engram.db` in the `VAYLT_Hackathon` folder, even after you
stop it. To start over with an empty memory: stop the brain, delete `engram.db` (and `engram.db-wal` and
`engram.db-shm` if you see them), start the brain again.

---

## 10. Stop everything

1. **Close the console's browser tab first.**
2. In terminal 2 (console) press **Ctrl+C** (also on a Mac: Ctrl, not Cmd). It prints `Finished server process`.
3. In terminal 1 (brain) press **Ctrl+C**.

If the console instead prints `Waiting for connections to close. (CTRL+C to force quit)`, the page is still open
somewhere: close the tab, or press **Ctrl+C** again. After a second Ctrl+C a red block of text ending in
`CancelledError` is normal; it has stopped.

To run it again later: steps 4 to 7 only (no installing).

---

## If X happens, do Y

Everything in this table happened during testing, unless it says "not tested here".

| What you see | Why | What to do |
|---|---|---|
| `pip install` ends with `the configured Python interpreter version (3.14) is newer than PyO3's maximum supported version (3.13)` and `Failed to build pydantic-core` (also 3.15) | An older copy of the repo pinned an old pydantic | `git pull`, delete the `.venv` folder and redo section 3, or use Python 3.13. |
| The brain crashes with `TypeError: Unable to evaluate type annotation 'list[str] \| None'` | Python is too old (3.9, the Mac's own `python3`) | Same: Python 3.13, delete `.venv`, redo section 3. |
| `AttributeError: module 'socket' has no attribute 'AF_UNIX'` | An older copy of `serve_dual.py` on Windows | `git pull`, or use the commands in sections 4 and 5. `console/scripts/run.sh` is for the Pi. |
| `ModuleNotFoundError: No module named 'engram'` | The brain was started in the wrong folder | `cd` to `VAYLT_Hackathon` (not `console`) and start it again. |
| `ModuleNotFoundError: No module named 'server'` | The console was started in the wrong folder | `cd` to `VAYLT_Hackathon/console` and start it again. |
| `[Errno 10048] ... only one usage of each socket address` (Windows) or `Address already in use` (Mac, not tested here) | That port is taken, often by a copy you started earlier | Stop the other copy with Ctrl+C in its window. Or use another port: change `--port` and use the new number in the addresses (for the brain, also in `ENGRAM_BRAIN_URL`). |
| http://127.0.0.1:8000/status shows `"brain_url":"off"`, or T7 says "I couldn't match that to a tagged object in memory" | The console was started without `ENGRAM_BRAIN_URL` | Stop the console (Ctrl+C), then start it again exactly as in section 5. |
| Every command fails with "ENGRAM brain unreachable (http://127.0.0.1:8001): All connection attempts failed" (spoken: "I can't reach the planner right now.") | The brain isn't running | Start it (section 4) and check http://127.0.0.1:8001/health. |
| You type a question and the Plan panel still shows the previous command | Answers are spoken only | Turn the sound on, or use Expert view and zoom out (section 8). |
| The quick buttons or the grey example say "I don't know which object you mean" | They are the console's own demo commands | Type the nine commands from section 9. |
| Ctrl+C on the console prints `Waiting for connections to close` and nothing more | The page is still open | Close the browser tab, or press Ctrl+C again. |
| Windows tests: `1 failed` in `test_seed_sources_match_the_files_they_cite` | The repo was cloned without `--config core.autocrlf=false` | Harmless. To get a clean run, clone again as in section 2. |
| Tests: `1 failed` in `test_propose_cost_and_rows_do_not_grow_with_repeated_commands` with `propose time grew from 3.4 ms to 7.2 ms` | A timing test: it can fail when the laptop is busy | Close other programs and run the tests again (here it then passed 15 times in a row). It does not affect using ENGRAM. |
| Windows: `python3` opens the Microsoft Store, or says "Python was not found" (not tested here) | `python3` is a Windows shortcut, not your Python | Use `py -3.13` as written in this guide. |
| Linux: `ensurepip is not available` when making the `.venv` (not tested here) | Ubuntu/Debian keep `venv` in a separate package | Run the `apt install` command it prints (for example `sudo apt install python3.12-venv`), then try again. |
| Mac: a window asks to install "command line developer tools" when you run `git` (not tested here) | Git is not installed yet | Click Install, wait for it to finish, then run the command again. |
