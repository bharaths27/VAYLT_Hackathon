# ENGRAM at CDS: the runbook

Written for someone who has never touched this robot. Do the steps in order. Every command is copy-paste.
If a step does not give the result it says, stop and use the Troubleshooting table at the end; do not improvise
on the robot.

Repo: **github.com/mrrCarter/VAYLT_Hackathon**, branch **main** (this file is `docs/CDS_RUNBOOK.md`).

---

## 0. What you are working with (two minutes)

**The robot** is a **Hiwonder MasterPi**: a four-wheel rover with a five-servo arm ending in a claw (gripper) and
a camera, driven by a **Raspberry Pi 5 named `star-pi`** (user `starrasp`). It has **one forward ultrasonic
sensor**: it stops before driving into something in front of it. It has **no LiDAR, no rear sensor, no side
sensor**: reversing and turning in place are blind. Only one program may talk to its motor board at a time: Brian's
robot server.

Everything runs on star-pi:

| Service | Port | What it is | Owner |
|---|---|---|---|
| Robot server | 8200 | talks to the motors, claw, camera; enforces the safety stop | Brian |
| Brain | 8001 | understands commands, memory, plans, approval | Bharath |
| Console | 8443 (+ 8000) | Verna's iPad screen: push-to-talk, plans, approve, STOP | Verna |
| Model | 8080 | Qwen3 1.7B, optional; the brain's rules plan most things without it | Bharath |
| Memory inspector | 8010 | the new Cortex memory engine, read-only sidecar, optional | warden |

How a command flows: **you speak on the iPad → console turns speech into text → brain makes a plan → the plan
appears on the iPad → you press Approve → the brain sends signed steps to the robot server → the robot server
runs them under its safety supervisor and reports what its sensors confirmed.**

Nothing moves without your Approve. A spoken or typed **"Stop"** always stops.

---

## 1. Safety rules (read before powering anything)

1. **Mock first.** Steps 5 to 8 use a pretend robot. The real robot cannot move until step 9.
2. **Wheels lifted** for the first real tests: put the rover on a box or book so the wheels spin in the air.
3. **A hand on the power.** Before any real motion, one person stands ready to cut the motor power (unplug the
   motor battery or flip its switch). Ask Brian where that is. The Pi's own power button is **not** an
   emergency stop (it shuts the Pi down, which kills the safety supervisor).
4. **Read before you approve.** Every plan shows its steps. Approve only steps you expect.
5. **Clear floor, low speed.** Nothing within 1.5 m in front, nothing behind or beside it, and no one's feet near
   the wheels.
6. **If anything surprises you: say "Stop", press STOP on the iPad, and cut the motor power.** Then post what
   happened in the room before trying again.

---

## 2. Bring this

- [ ] Your laptop (charged), with an SSH client (Windows: open PowerShell and type `ssh -V`; if it says "not
      recognized", Settings > Apps > Optional features > add "OpenSSH Client").
- [ ] The iPad (charged), Safari.
- [ ] Your phone (its hotspot is the backup network).
- [ ] The robot, its motor battery (charged) and the Pi's power supply (Pi 5 needs a **5 V / 5 A USB-C**
      supply; a weaker one browns out the camera and motors).
- [ ] **Printed tags, 40 mm.** Print `docs/tags-40mm.pdf` from the repo (on GitHub: open the file, Download) at
      **100% scale / "actual size"** (not "fit to page"). With a ruler, the black square must measure **40 mm**: the
      robot's distance math assumes it. Objects: **Sample A = 1, Sample B = 2, Safety goggles = 3, Beaker = 4.**
      Zones: **home = 10, shelf A = 11, shelf B = 12, PPE station = 13, bench = 14.** Old tags printed at 50 mm make
      every distance about 25% wrong: reprint.
- [ ] The objects: something to stand in for Sample A, Sample B, goggles and a beaker, each with its tag on top or
      on the side where the camera still sees it when the claw is closed; a box or two for zones.
- [ ] The SSH password or key for `starrasp@star-pi`: **get it from Brian directly, never through the room.**
- [ ] **The network:** the robot last ran on **Brian's phone hotspot**, so the Pi joins it by itself. Bring Brian's
      phone, or ask Brian for that hotspot's exact name and password and set **your** phone's hotspot to the same
      name and password: the Pi will join yours instead. Use a hotspot with a password, never open Wi-Fi: anyone
      on the robot's network can approve a plan.
- [ ] A USB-C to Ethernet adapter and an Ethernet cable (backup if Wi-Fi fails, see step 4).
- [ ] Ideally Brian in person or on a call for step 9 (the real robot).

---

## 3. Power on

1. Motor battery **on**, then plug in the Pi's power. Wait **one minute** for it to boot.
2. Do not press any buttons on the robot.

---

## 4. Connect your laptop to the robot

Turn on the hotspot (Brian's, or yours with the same name and password; see section 2). Wait a minute: the Pi
shows up in the hotspot's connected devices. Put your laptop on **the same hotspot**. (BU Wi-Fi needs a personal
login the Pi does not have; do not count on it.)

```bash
ssh starrasp@star-pi.local
```

- First time: it asks "Are you sure you want to continue connecting?" Check the fingerprint with Brian, then type
  `yes`.
- Type the password (it does not show as you type) and press Enter.
- You are in when the prompt reads `starrasp@star-pi:~ $`.

If `star-pi.local` is not found: have Brian (or anyone with a screen on the Pi) run `hostname -I`, then use that
address: `ssh starrasp@<that address>`. More fixes: Troubleshooting, "Cannot connect".

**Backup 1, a cable:** plug the Ethernet cable from your laptop's USB-C adapter into the Pi's Ethernet jack.
Try `ssh starrasp@star-pi.local` again. If it is still not found, the Pi's Ethernet probably has the fixed robot-link
address: set your laptop's Ethernet adapter to IPv4 address `10.0.0.1`, mask `255.255.255.0`, no gateway
(Windows: Settings > Network > Ethernet > IP assignment > Edit > Manual), then `ssh starrasp@10.0.0.2`.

**Backup 2, the Pi's own Wi-Fi** (once you are in over SSH, and after step 5, which needs internet):
`bash ~/engram_hub/console/scripts/hotspot.sh` makes the Pi its own Wi-Fi called **ENGRAM-LAB** and prints its
password (made on this Pi the first time and kept in `~/.engram-lab-wifi`; never post it: anyone on this network
can approve a plan). The iPad joins it and the robot is at `10.42.0.1`. Your SSH session over Wi-Fi drops when it
switches; reconnect over the cable or by joining ENGRAM-LAB.

---

## 5. Update the robot's software to `main` (one time)

This switches the Pi's code to the integrated build. **Nothing on the Pi is lost:** the commit it was on is kept
as branch `pre-cds-<date>`, and anything a teammate changed on the Pi is saved first (branch `pi-local-<date>`,
plus a patch file and a copy of new files in `~/cds-backup/<date>/`). Paste this as **one line**:

```bash
cd ~/engram_hub && git fetch https://github.com/mrrCarter/VAYLT_Hackathon.git main && git show FETCH_HEAD:scripts/cds_update.sh > /tmp/cds_update.sh && bash /tmp/cds_update.sh
```

Expected: it ends with these two lines (the dates differ):

```
done: on main. next: bash /home/starrasp/engram_hub/scripts/cds_up.sh --restart   (--restart: services already running still run the old code)
to undo (back from main):  cd /home/starrasp/engram_hub && git checkout pre-cds-20261025-...
```

(If it also prints `ignored files that main replaces, copied first: ...`, those local files were saved in
`~/cds-backup/<date>/` and the undo line puts them back.)

Keep the undo line (it is also saved in `~/cds-backup/<date>/UNDO.txt`). If the Pi has no internet, the `git
fetch` fails and **nothing on the Pi changes**: switch the laptop and the Pi to your phone's hotspot and paste the
line again (the update needs internet once; everything after this works offline).

---

## 6. Start everything (pretend robot: nothing can move)

```bash
bash ~/engram_hub/scripts/cds_up.sh --restart
```

Expected (the last block it prints):

```
  OK    brain      ...   plans + memory (mock robot)
  OK    console    ...   Verna's console (iPad); robot view: SIMULATOR
  OK    cert       ...   certificate download for the iPad
  OK    robot      ...   Brian's robot server (hardware + safety)   <- OK or DOWN: both fine for now
  DOWN  model      ...   Qwen3 1.7B (optional)
  DOWN  cortex     ...   memory inspector sidecar (optional)

  iPad console:      https://star-pi.local:8443
  iPad console:      https://172.20.10.6:8443      <- the same console by number; yours may differ
```

If it also says it renewed the console certificate, that is normal on a new network. Check the status any time:
`bash ~/engram_hub/scripts/cds_status.sh`. Everything keeps running after you close the laptop (if a service shows
DOWN when you come back, run `cds_up.sh` again).

---

## 7. Open the console on the iPad

Put the iPad on the **same network** as the robot.

**Once per iPad, install the robot's certificate** (Safari only allows the microphone on a secure page):
1. In Safari open `http://star-pi.local:8000/ca.crt` (or `http://<robot address>:8000/ca.crt`). Tap **Allow**.
2. Settings > General > **VPN & Device Management** > ENGRAM Local CA > **Install** (twice).
3. Settings > General > About > **Certificate Trust Settings** > turn **ENGRAM Local CA** on.

**Open the console:** `https://star-pi.local:8443` in Safari (or `https://<robot address>:8443`; the iPad often
needs the number form, e.g. `https://172.20.10.6:8443`, which `cds_status.sh` prints). Allow the microphone when
asked. **How to tell the pretend robot from the real one:** during start-up the console lists
`robot  SIMULATOR (not real hardware)`, and afterwards a small **SIM** control with a "drop obstacle" button stays
on screen. On the real robot the start-up line shows the robot's address and there is no SIM control.
**Approving is press-and-hold:** hold the Approve button for about a second.

Verna's 3D rover front page is optional (it embeds this same console): see section 12.

---

## 8. Test with the pretend robot (nothing moves)

Do these in order. Hold push-to-talk, speak, release. (If voice does not work, type the same words.)
Write down what happened for each; you will post it (section 13).

| # | Say | Expected |
|---|---|---|
| T1 | "Open the gripper." | A plan card with one step (open gripper). Tap **Approve** → success (mock) and a spoken reply. |
| T2 | "Stop." | Stops immediately, no plan, status shows **stopped**. Clear it with the console's clear/re-arm control. |
| T3 | "Don't open the claw." | Refused or a question back. **No plan.** |
| T4 | "Open the claw in two days." / "Open the claw after calibration finishes." | It asks you to rephrase: "... Please clarify." **No plan.** (Said while a plan is running, it first stops the robot like STOP: "I stopped. ... Please clarify.") |
| T5 | "Can the beaker go in the drying oven?" | An **answer** from memory (or "I have no record"). **No plan.** |
| T6 | "Remember that safety goggles must be at the bench before handling Sample B." | "Taught" / rule saved. |
| T7 | "Bring Sample B to the bench." | Plan moves the **goggles to the bench first**, then Sample B. (Do not approve on the real robot yet.) |
| T8 | "What did I say about the goggles?" | An answer quoting T6. No plan. |
| T9 | "Look around." | A plan with one observe step. |
| T10 | Time it: stopwatch from the end of "Open the gripper" to the plan card appearing. Do it 3 times. | Write the 3 times down (on the warden's PC this was about 0.7 s; the Pi is slower). |

If T1 to T5 behave differently from the table, **stop here** and post what you saw: those are the safety behaviors.

---

## 9. The robot server and the witnessed stop test (real hardware, wheels lifted)

**Do not skip this. Ideally Brian runs it with you.** Put the rover on a box so **the wheels are in the air**,
hand on the motor power.

**9a. Is the robot server running?**

```bash
bash ~/engram_hub/scripts/cds_status.sh
```

If `robot` is DOWN, start Brian's server:

```bash
sudo systemctl start engram-robot && sleep 5 && bash ~/engram_hub/scripts/cds_status.sh
```

(If that service does not exist: `cd ~/engram_robot && .venv/bin/python -m robot.server --driver real --config configs/ultrasonic.json`
in a second SSH window, and ask Brian.)

**9b. Is the shared secret set?** The brain signs every step with a secret that must match the robot's:

```bash
grep -c '^ENGRAM_ROBOT_SECRET=' ~/engram_robot/robot.env
```

Expected: `1`. If `0` or "No such file": stop and ask Brian (`robot/engram_robot/README.md`, "Two-Pi setup",
step 2). The line must start with `ENGRAM_ROBOT_SECRET=` (not `export ENGRAM_ROBOT_SECRET=`). Never paste the
secret anywhere.

**9c. Switch the brain to the real robot:**

```bash
bash ~/engram_hub/scripts/cds_up.sh --real
```

Expected: `brain ... plans + memory (real robot)`, `console ... robot view: LIVE http://...:8200`, `robot OK` and
`robot stop latch: off`. If the brain line says `real robot, NOT reaching it`, stop and post the status. On the
iPad, reload the console: the start-up line now shows the robot's address instead of SIMULATOR, and the SIM
control is gone.

**9d. The three stop tests (wheels in the air).** The brain plans fetch tasks ("Bring X to Y") and three
fixed commands (open the gripper, close the gripper, look around); it does not take "turn left" or "drive
forward". So the motion for these tests is a fetch whose tag the camera cannot see: the robot turns in place,
searching for it.
1. **Console STOP:** cover or hide the Sample A tag (1). Say "Bring Sample A to the bench." Read the plan, tap
   Approve, and while the wheels turn press **STOP** on the iPad. Wheels must stop at once. Re-arm afterwards.
2. **Spoken stop:** same, but say "Stop." Wheels must stop.
3. **Server death (Brian should do this one):** start the same search, then in the SSH window run
   `sudo systemctl kill -s KILL engram-robot`. **If the wheels keep spinning, cut the motor power now** and write
   it down: it means only a hand on the power can stop the robot if its software dies, so **no driving on the
   floor today**, only stationary tests (10a). Restart it with `sudo systemctl start engram-robot`.

Post the three results (section 13). Without all three passing, stay on 10a.

Brian's own checklist covers what the brain does not drive: straight drives and the ultrasonic stop in front of a
box. He runs it with his tool (`robot/engram_robot/README.md`, "Deniz" steps; `tools/hw_test.py` needs the robot
server stopped first, because only one program can use the motor board).

---

## 10. Real robot tests

**10a. Stationary (robot can stay on the box):**

| # | Say | Expected |
|---|---|---|
| R1 | "Open the gripper." → Approve | The claw opens. The result may say **not sensor checked**: this claw has no sensor, so the robot says so honestly; that is not a failure. **If the claw does not move at all**, its open/close values may still be the placeholders (1500/1500): Brian sets `claw_open_us` / `claw_closed_us` in `~/engram_robot/drive_calib.json`. |
| R2 | "Close the gripper." → Approve | The claw closes. |
| R3 | Hold the Sample A tag (1) **50 cm** in front of the camera (measure). "Look around." → Approve | Tag 1 is listed as seen, at about **0.5 m**. About 0.63 m means the tag was printed at 50 mm, not 40 mm: reprint (section 2) before any motion. |

**10b. Moving (only if all three stop tests passed, floor clear, Brian present):** robot on the floor, 1.5 m
clear in front, nothing behind, hand on the motor power.

| # | Say | Expected |
|---|---|---|
| R4 | Sample A (tag 1) about 0.8 m in front, bench tag 14 on a box about 1 m to the side. "Bring Sample A to the bench." → read the plan (find Sample A, pick it, place it at the bench) → Approve | It turns to find tag 1, approaches, tries to pick, then goes to the bench. Each step reports done, failed or unknown; "not sensor checked" means it ran but no sensor confirmed it. A missed grasp is reported, never claimed. STOP if anything looks wrong. |
| R5 | Teach the rule (T6), then Sample B (tag 2) and the goggles (tag 3) in reach. "Bring Sample B to the bench." | The plan **moves the goggles first**, then Sample B. Approve step by step only if R4 went well. |

If grasping is unreliable, stop at "it finds and approaches the right tag": that is still a real, sensor-verified
result. Say so plainly in the demo.

---

## 11. The memory demo (what judges should see)

1. Teach the rule (T6), ask for the task (T7): the plan includes the prerequisite **because of what it was taught**.
2. Correct the rule: "Remember that safety goggles must be at the PPE station before handling Sample B."
3. Ask the same task again: the plan **changes** to match the corrected rule.
4. Ask "What did I say about the goggles?": it answers from memory without moving.
5. Say clearly which results were simulated, which were the model's suggestion, and which the robot's sensors
   confirmed (a step marked "not sensor checked" ran, but no sensor confirmed it).

---

## 12. Optional extras

**Verna's 3D rover front page** (on your laptop, needs Node.js): `cd console/frontend && npm install && npm run dev`,
open the printed address, and add `?console=https://star-pi.local:8443` to it. The dashboard page embeds the console.

**Qwen3 1.7B** (slow on the Pi; the rules plan the demo tasks without it):
`bash ~/engram_hub/scripts/cds_up.sh --model`. Plans that need the model take tens of seconds.

**Cortex memory inspector** (the new memory engine, never touches the robot):
`bash ~/engram_hub/scripts/cds_up.sh --sidecar`, then on your laptop
`ssh -L 8010:127.0.0.1:8010 starrasp@star-pi.local` and open `http://127.0.0.1:8010/cortex`. Paste the token
from `~/.cortex-sidecar/token` (on the Pi; never post it). See `docs/CORTEX_HANDOFF.md`.

---

## 13. What to post in the room (evidence)

After each block, post one message in the ENGRAM CORTEX room:
- **After step 6:** the output of `bash ~/engram_hub/scripts/cds_status.sh`.
- **After step 8:** T1 to T10: what you said, what happened, the three times from T10.
- **After step 9:** the three stop tests: pass/fail, and for test 3 whether the wheels kept spinning.
- **After step 10:** R1 to R5 results, with a short video if you can.
- Anything that surprised you, even if it worked in the end.

These are the evidence for the board cards **CXV2** (suite on the Pi), **CXV6** (iPad), **CXV7/CXV8** (stop and
live run) and **CXV9** (demo rehearsal).

---

## 14. Troubleshooting

| Symptom | Do this |
|---|---|
| **Cannot connect** (`Could not resolve hostname`) | Use the IP from `hostname -I` on the Pi. Laptop and Pi must be on the same network. |
| `Connection timed out` | Wrong network or address. Check both are on the same Wi-Fi/hotspot. |
| `Permission denied` | Wrong password/key: ask Brian. |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | Stop. Ask Brian before doing anything else. |
| `git fetch` fails in step 5 | No internet on the Pi, or the network gives the Pi only IPv6 (GitHub has no IPv6): put the Pi and laptop on your phone's hotspot. Nothing on the Pi changed. |
| A service shows DOWN after `cds_up.sh` | Look at its log: `tail -50 ~/cds-logs/brain.log` (or `console.log`). Post the last lines. |
| A service stays DOWN but its port is taken | `sudo ss -ltnp 'sport = :8001'` (or 8000 / 8443) shows which program holds it. A copy started by another user or by systemd is invisible to the scripts: stop it (`sudo systemctl stop <service>` or `sudo kill <pid>`), then run `cds_up.sh` again. |
| iPad: "This connection is not private" | Either the certificate is not trusted yet (step 7, all three parts), or the address you typed is not in the certificate (new network). Run `cds_up.sh` again: it renews the certificate with the same CA, so the iPad still trusts it. `cds_status.sh` warns when this is the problem. |
| iPad: microphone does not work | Use `https://` (not `http://`), allow the mic in Safari; or type the commands. |
| Voice understood the wrong words | Type the command instead; post what it heard. |
| `cds_up.sh --real` says REFUSED | Read the reason it prints: secret missing or still the default (9b; a line starting with `export` counts as missing), or robot server not running (9a). Nothing that was running was stopped. |
| Approve gives an error mentioning auth/403 | The brain's and the robot's secrets differ: ask Brian to check `~/engram_robot/robot.env`. |
| Robot stuck in "stopped" | Re-arm in the console, or `curl -s -X POST http://127.0.0.1:8001/robot/clear_stop`. |
| Anything moves unexpectedly | "Stop", STOP button, cut motor power. Post what happened. |

**Undo everything:** `bash ~/engram_hub/scripts/cds_down.sh`, then the undo command printed by step 5.

---

## 15. Shutting down

```bash
bash ~/engram_hub/scripts/cds_down.sh
sudo shutdown -h now
```

Wait until the Pi's green light stops blinking, then unplug the Pi and switch off the motor battery.

---

## 16. Who owns what

| Area | Person |
|---|---|
| Robot hardware, robot server, stop tests, tags | Brian |
| Brain (8001), planning, approval | Bharath |
| Console, iPad, certificate | Verna |
| Memory tests, recall | Alex |
| Integration, these scripts, reviews | warden (post in the room) |

## What is proven and what is not (as of 2026-10-25)

Check it yourself from the repo root, on Linux or macOS (the Pi included). Each should end with no failures:
`python -m pytest -q` (brain and memory), `cd console && python -m pytest -q tests` (console),
`cd robot/engram_robot && python -m pytest -q tests` (robot server) and
`python scripts/cortex_demo.py --out /tmp/cortex-demo.json` (the Cortex checks).

- **Tests at main 5026150, on the warden's PC with a simulated robot:** brain and memory 926 passed and 6 skipped
  (the skipped ones need Linux), console 248 passed, robot server 56 passed, web front page 11 passed. On a Windows
  checkout that converts line endings, one file-checksum test fails; it passes on a normal checkout.
- **The CDS scripts, end to end on a simulated Pi** (Linux, a copy of the old Pi checkout): update with backup and
  exact undo, a failed fetch that changes nothing, mock start, the `--real` refusals, a simulated robot server on
  IPv4 and IPv6, the stop latch, shutdown, and a program that is not ours left running on an ENGRAM port.
- **Observed, not reproducible from this repo yet** (warden's PC, simulated robot; the timing harness is not in
  the repo): about 0.7 s from the end of speech to the plan card.
- **Not yet proven on the Pi:** speed of speech recognition and the spoken reply, Qwen timing, the camera frame
  rate, and every real-hardware step in sections 9 to 11. Today's tests are what prove them.
