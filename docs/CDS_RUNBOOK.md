# ENGRAM at CDS: the runbook

Written for someone who has never touched this robot. Do the steps in order. Every command is copy-paste.
If a step does not give the result it says, stop and use the Troubleshooting table at the end; do not improvise
on the robot.

Repo: **github.com/mrrCarter/VAYLT_Hackathon**, branch **main** (this file is `docs/CDS_RUNBOOK.md`).

---

## 0. What you are working with (two minutes)

**The robot** is a small wheeled rover with a claw (gripper) and a camera, driven by a **Raspberry Pi 5 named
`star-pi`** (user `starrasp`). It has **one forward ultrasonic sensor**: it stops before driving into something
in front of it. It has **no LiDAR, no rear sensor, no side sensor**: reversing and turning in place are blind.

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
- [ ] Printed tags (ArUco 4x4). Objects: **Sample A = 1, Sample B = 2, Safety goggles = 3, Beaker = 4.**
      Zones: **home = 10, shelf A = 11, shelf B = 12, PPE station = 13, bench = 14.** If you need to print them:
      `python scripts/make_tags.py` on any machine with the repo, print at 100% scale.
- [ ] The SSH password or key for `starrasp@star-pi`: **get it from Brian directly, never through the room.**
- [ ] Ideally Brian in person or on a call for step 9 (the real robot).

---

## 3. Power on

1. Motor battery **on**, then plug in the Pi's power. Wait **one minute** for it to boot.
2. Do not press any buttons on the robot.

---

## 4. Connect your laptop to the robot

The Pi joins the team's network (the team hotspot or the room's Wi-Fi). Put your laptop on **the same network**.

```bash
ssh starrasp@star-pi.local
```

- First time: it asks "Are you sure you want to continue connecting?" Check the fingerprint with Brian, then type
  `yes`.
- Type the password (it does not show as you type) and press Enter.
- You are in when the prompt reads `starrasp@star-pi:~ $`.

If `star-pi.local` is not found: have Brian (or anyone with a screen on the Pi) run `hostname -I`, then use that
address: `ssh starrasp@<that address>`. More fixes: Troubleshooting, "Cannot connect".

**Backup network (no Wi-Fi at all):** with a keyboard and screen on the Pi, run
`bash ~/engram_hub/console/scripts/hotspot.sh`; the Pi becomes its own Wi-Fi called **ENGRAM-LAB** (password
`engram2026`), and the robot is at `10.42.0.1`.

---

## 5. Update the robot's software to `main` (one time)

This switches the Pi's code to the integrated build. **Anything changed on the Pi is saved first** (a backup
branch plus a patch file in `~/cds-backup/`), so nothing a teammate left there is lost. Paste all four lines:

```bash
cd ~/engram_hub
git fetch https://github.com/mrrCarter/VAYLT_Hackathon.git main
git show FETCH_HEAD:scripts/cds_update.sh > /tmp/cds_update.sh
bash /tmp/cds_update.sh
```

Expected: it ends with `done. next: bash .../scripts/cds_up.sh` and prints the exact command to undo. If the Pi has
no internet, the `git fetch` fails: switch the laptop and the Pi to your phone's hotspot and repeat (the update
needs internet once; everything after this works offline).

---

## 6. Start everything (pretend robot: nothing can move)

```bash
bash ~/engram_hub/scripts/cds_up.sh --restart
```

Expected (the last block it prints):

```
  OK    brain      ...   plans + memory (mock robot)
  OK    console    ...   Verna's console (iPad)
  OK    cert       ...   certificate download for the iPad
  OK/DOWN robot    ...   Brian's robot server      <- either is fine for now
  DOWN  model      ...   (optional)
  DOWN  cortex     ...   (optional)
  iPad console:      https://star-pi.local:8443
```

Check the status any time: `bash ~/engram_hub/scripts/cds_status.sh`. Everything keeps running after you close
the laptop.

---

## 7. Open the console on the iPad

Put the iPad on the **same network** as the robot.

**Once per iPad, install the robot's certificate** (Safari only allows the microphone on a secure page):
1. In Safari open `http://star-pi.local:8000/ca.crt` (or `http://<robot address>:8000/ca.crt`). Tap **Allow**.
2. Settings > General > **VPN & Device Management** > ENGRAM Local CA > **Install** (twice).
3. Settings > General > About > **Certificate Trust Settings** > turn **ENGRAM Local CA** on.

**Open the console:** `https://star-pi.local:8443` in Safari. Allow the microphone when asked. The screen should
show a **SIMULATED** / mock label (that is the pretend robot) and the push-to-talk button.

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
| T4 | "Open the claw in two days." / "Open the claw after calibration finishes." | It asks you to rephrase. **No plan.** |
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
step 2). Never paste the secret anywhere.

**9c. Switch the brain to the real robot:**

```bash
bash ~/engram_hub/scripts/cds_up.sh --real
```

Expected: `brain ... (real robot)` and `robot OK`. The console label changes from SIMULATED to LIVE.

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
| R1 | "Open the gripper." → Approve | The claw opens; result SUCCEEDED (or UNKNOWN if the claw has no sensor; that is honest, not a failure). |
| R2 | "Close the gripper." → Approve | The claw closes. |
| R3 | Hold the Sample A tag (1) in front of the camera. "Look around." → Approve | The camera looks; tag 1 is listed as seen. |

**10b. Moving (only if all three stop tests passed, floor clear, Brian present):** robot on the floor, 1.5 m
clear in front, nothing behind, hand on the motor power.

| # | Say | Expected |
|---|---|---|
| R4 | Sample A (tag 1) about 0.8 m in front, bench tag 14 on a box about 1 m to the side. "Bring Sample A to the bench." → read the plan (find Sample A, pick it, place it at the bench) → Approve | It turns to find tag 1, approaches, tries to pick, then goes to the bench. Each step reports SUCCEEDED / FAILED / UNKNOWN. A missed grasp is reported, never claimed. STOP if anything looks wrong. |
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
   confirmed (only SUCCEEDED means a sensor confirmed it).

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
| `git fetch` fails in step 5 | No internet on the Pi: put the Pi and laptop on your phone's hotspot. |
| A service shows DOWN after `cds_up.sh` | Look at its log: `tail -50 ~/cds-logs/brain.log` (or `console.log`). Post the last lines. |
| iPad: "This connection is not private" | The certificate is not trusted yet: step 7, all three parts. |
| iPad: microphone does not work | Use `https://` (not `http://`), allow the mic in Safari; or type the commands. |
| Voice understood the wrong words | Type the command instead; post what it heard. |
| `cds_up.sh --real` says REFUSED | Read the reason it prints: secret missing (9b), or robot server not running (9a). |
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

- **Proven on the warden's PC, simulated robot:** all test suites (881 brain, 187 console, 30 web, 56 robot server),
  voice end to end about 0.7 s from end of speech to the plan, stop/negation/deferral behavior, questions never
  become plans.
- **Not yet proven on the Pi:** speed of speech recognition and the spoken reply, Qwen timing, the camera frame
  rate, and every real-hardware step in sections 9 to 11. Today's tests are what prove them.
