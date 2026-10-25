#!/usr/bin/env bash
# Bring ENGRAM up on star-pi in the right order, then print what is healthy. Run ON the Pi:
#   bash ~/engram_hub/scripts/cds_up.sh             brain on a MOCK robot (nothing can move) + console
#   bash ~/engram_hub/scripts/cds_up.sh --real      brain drives Brian's robot server on [::1]:8200
#                                                   (only after the witnessed stop test in docs/CDS_RUNBOOK.md)
#   options:  --model    also Qwen3 1.7B on :8080 (the brain asks it only for tasks its rules cannot plan)
#             --sidecar  also the Cortex memory inspector on 127.0.0.1:8010 (reach it over an ssh tunnel)
#             --restart  stop our services first, so the code just pulled is the code that runs
# Safe to run again: a healthy service is left alone, and the brain restarts only to switch mock <-> real.
# It never starts, stops or resets Brian's robot server, and it never moves the robot.
set -uo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
LOGS=$HOME/cds-logs
mkdir -p "$LOGS"
REAL=0 MODEL=0 SIDECAR=0 RESTART=0
for a in "$@"; do
  case $a in
    --real) REAL=1 ;; --model) MODEL=1 ;; --sidecar) SIDECAR=1 ;; --restart) RESTART=1 ;;
    *) echo "unknown option: $a (see the top of this file)"; exit 2 ;;
  esac
done
cd "$HUB" || { echo "no $HUB"; exit 1; }

code() { curl -sk -m 3 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }   # 000 when nothing answers
healthy() { case $(code "$1") in 2*) return 0 ;; *) return 1 ;; esac; }
wait_for() { for _ in $(seq 1 "${2:-30}"); do healthy "$1" && return 0; sleep 2; done; return 1; }
pids_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
stop_port() { for p in $(pids_on "$1"); do kill "$p" 2>/dev/null; done; }
start() { # name, command...: detached from this ssh session, so closing the laptop does not stop it
  local name=$1; shift
  setsid nohup "$@" > "$LOGS/$name.log" 2>&1 < /dev/null &
  echo $! > "$LOGS/$name.pid"
}

if [ $RESTART = 1 ]; then
  echo "stopping the brain, console and sidecar so the new code runs"
  stop_port 8001; stop_port 8443; stop_port 8000; stop_port 8010; rm -f "$LOGS/brain.mode"; sleep 2
fi

# 1. model (optional)
if [ $MODEL = 1 ]; then
  bash ai_module/start_model.sh || echo "model did not start (see ~/llama.log); the brain still plans with its rules"
fi

# 2. brain on :8001, mock robot unless --real
WANT=mock
unset ENGRAM_ROBOT_URL ENGRAM_ROBOT_API ENGRAM_ROBOT_KEY_FILE
if [ $REAL = 1 ]; then
  WANT=real
  ENV_FILE=$HOME/engram_robot/robot.env
  if ! grep -q '^ENGRAM_ROBOT_SECRET=..' "$ENV_FILE" 2>/dev/null; then
    echo "REFUSED --real: no ENGRAM_ROBOT_SECRET in $ENV_FILE (Brian's shared secret; see the runbook)"; exit 1
  fi
  if grep -q 'dev-secret-change-me' "$ENV_FILE"; then
    echo "REFUSED --real: $ENV_FILE still has the default secret"; exit 1
  fi
  if ! healthy 'http://[::1]:8200/robot/state'; then
    echo "REFUSED --real: Brian's robot server does not answer on [::1]:8200 (see the runbook, robot server)"; exit 1
  fi
  export ENGRAM_ROBOT_API=brian ENGRAM_ROBOT_URL='http://[::1]:8200' ENGRAM_ROBOT_KEY_FILE=$ENV_FILE
fi
export ENGRAM_VOICE_PRESETS=1
if healthy http://127.0.0.1:8080/health; then export ENGRAM_LLM_URL=http://127.0.0.1:8080/completion; fi
if healthy http://127.0.0.1:8001/health && [ "$(cat "$LOGS/brain.mode" 2>/dev/null)" = "$WANT" ]; then
  echo "brain already up ($WANT)"
else
  if healthy http://127.0.0.1:8001/health; then echo "restarting the brain to switch to $WANT"; stop_port 8001; sleep 2; fi
  start brain .venv/bin/python scripts/serve_dual.py --port 8001
  echo "$WANT" > "$LOGS/brain.mode"
  wait_for http://127.0.0.1:8001/health 40 || echo "brain did not answer; see $LOGS/brain.log"
fi

# 3. console on :8443 (https, needed for the iPad microphone) and :8000 (certificate download)
if healthy https://127.0.0.1:8443/ || healthy http://127.0.0.1:8000/; then
  echo "console already up"
else
  [ -f console/certs/server.crt ] || (cd console && bash scripts/make_cert.sh) \
    || echo "no certificate: the iPad gets typed commands only until console/scripts/make_cert.sh works"
  start console env ENGRAM_BRAIN_URL=http://127.0.0.1:8001 bash console/scripts/run.sh
  wait_for http://127.0.0.1:8000/ 30 || echo "console did not answer; see $LOGS/console.log"
fi

# 4. Cortex memory sidecar (optional; its own database, never the robot)
if [ $SIDECAR = 1 ]; then sh scripts/cortex_sidecar.sh start; fi

echo
bash scripts/cds_status.sh
