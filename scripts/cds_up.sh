#!/usr/bin/env bash
# Bring ENGRAM up on star-pi in the right order, then print what is healthy. Run ON the Pi:
#   bash ~/engram_hub/scripts/cds_up.sh             brain on a MOCK robot (nothing can move) + console
#   bash ~/engram_hub/scripts/cds_up.sh --real      brain drives Brian's robot server on :8200
#                                                   (only after the witnessed stop test in docs/CDS_RUNBOOK.md)
#   options:  --model    also Qwen3 1.7B on :8080 (the brain asks it only for tasks its rules cannot plan)
#             --sidecar  also the Cortex memory inspector on 127.0.0.1:8010 (reach it over an ssh tunnel)
#             --restart  stop our services first, so the code just pulled is the code that runs
# Safe to run again: a healthy service is left alone; brain and console restart only when what they should drive
# changes (mock <-> real, the model came or went) or the console certificate had to be renewed.
# It never starts, stops or resets Brian's robot server, and it never moves the robot. On its ports it stops only
# ENGRAM processes (serve_dual.py, run.sh, uvicorn, cortex or engram in the command line); it names anything else.
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
field() { curl -sk -m 3 "$1" 2>/dev/null | grep -o "\"$2\":[^,}]*" | head -1 | cut -d: -f2- | tr -d '"'; }   # one JSON value
wait_for() { for _ in $(seq 1 "${2:-30}"); do healthy "$1" && return 0; sleep 2; done; return 1; }
pids_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
free_ports() { # stop ENGRAM processes on these ports (TERM, then KILL after 5 s: a console streaming video to the
               # iPad ignores TERM); anything else is named and left alone. 0 when nothing else holds them.
  local port p a pids= rc=0
  for port in "$@"; do
    for p in $(pids_on "$port"); do
      a=$(ps -ww -o args= -p "$p" 2>/dev/null) || continue   # already gone
      case $a in
        *serve_dual.py*|*run.sh*|*uvicorn*|*cortex*|*engram*) pids="$pids $p" ;;
        *) echo "port $port is held by pid $p ($a), which these scripts did not start; stop it yourself if it is an old ENGRAM copy, then run again"; rc=1 ;;
      esac
    done
  done
  [ -n "$pids" ] || return $rc
  kill $pids 2>/dev/null
  for p in $pids; do
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$p" 2>/dev/null
  done
  return $rc
}
start() { # name, command...: detached from this ssh session, so closing the laptop does not stop it
  local name=$1; shift
  echo "=== $(date '+%F %T') $*" >> "$LOGS/$name.log"   # appended, so the reason a run died is still there
  setsid nohup "$@" >> "$LOGS/$name.log" 2>&1 < /dev/null &
  echo $! > "$LOGS/$name.pid"
}

# 0. --real: every check comes before anything is stopped, so a refusal leaves the running services alone
ROBOT=
if [ $REAL = 1 ]; then
  ENV_FILE=$HOME/engram_robot/robot.env
  if ! grep -q '^ENGRAM_ROBOT_SECRET=..' "$ENV_FILE" 2>/dev/null; then
    echo "REFUSED --real: no ENGRAM_ROBOT_SECRET in $ENV_FILE (Brian's shared secret; see the runbook)"; exit 1
  fi
  if grep -q 'dev-secret-change-me' "$ENV_FILE"; then
    echo "REFUSED --real: $ENV_FILE still has the default secret"; exit 1
  fi
  # Brian's server binds 0.0.0.0 (IPv4) by default, or :: (IPv6 only): use the address that answers
  for u in http://127.0.0.1:8200 'http://[::1]:8200'; do healthy "$u/robot/state" && { ROBOT=$u; break; }; done
  if [ -z "$ROBOT" ]; then
    echo "REFUSED --real: Brian's robot server answers neither on 127.0.0.1:8200 nor on [::1]:8200 (see the runbook, robot server)"; exit 1
  fi
fi

if [ $RESTART = 1 ]; then
  echo "stopping the brain, console and sidecar so the new code runs"
  free_ports 8010   # the brain and the console are replaced in their own steps below
fi

# 1. model (optional)
if [ $MODEL = 1 ]; then
  bash ai_module/start_model.sh || echo "model did not start (see ~/llama.log); the brain still plans with its rules"
fi

# 2. brain on :8001, mock robot unless --real
KIND=mock
unset ENGRAM_ROBOT_URL ENGRAM_ROBOT_API ENGRAM_ROBOT_KEY_FILE
if [ -n "$ROBOT" ]; then
  KIND=real
  export ENGRAM_ROBOT_API=brian ENGRAM_ROBOT_URL=$ROBOT ENGRAM_ROBOT_KEY_FILE=$ENV_FILE   # the console reads ENGRAM_ROBOT_URL too
fi
export ENGRAM_VOICE_PRESETS=1
if healthy http://127.0.0.1:8080/health; then export ENGRAM_LLM_URL=http://127.0.0.1:8080/completion; fi
settings() { grep -E '^ENGRAM_(ROBOT_API|ROBOT_URL|LLM_URL)=' | sort; }   # what a brain drives, and with which model
running_brain() { local p; p=$(pids_on 8001 | head -1); [ -n "$p" ] && tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | settings; }
if [ $RESTART = 0 ] && healthy http://127.0.0.1:8001/health && [ "$(running_brain)" = "$(env | settings)" ]; then
  echo "brain already up ($KIND robot${ENGRAM_LLM_URL:+, with the model})"
else
  healthy http://127.0.0.1:8001/health && echo "restarting the brain: $KIND robot${ENGRAM_LLM_URL:+, with the model}"
  if free_ports 8001; then   # also clears a brain of ours that holds the port without answering
    start brain .venv/bin/python scripts/serve_dual.py --port 8001
    wait_for http://127.0.0.1:8001/health 40 || echo "brain did not answer; see $LOGS/brain.log"
  fi
fi

# 3. console certificate. Safari refuses an https address the certificate does not name, and the Pi gets a new
#    address on every network. Renewed with the same CA (certs/engram-ca.key), so an iPad that trusts it still does.
cert_misses() { # this Pi's .local name and IPv4 addresses that console/certs/server.crt does not name
  local san n
  san=$(openssl x509 -in console/certs/server.crt -noout -text 2>/dev/null | grep -A1 'Subject Alternative Name' | tail -1)
  for n in "$(hostname -s 2>/dev/null || hostname).local" $(hostname -I 2>/dev/null); do
    case $n in *:*) continue ;; esac   # IPv6: devices on an IPv6-only hotspot use the .local name
    case "$san," in *"DNS:$n,"*|*"IP Address:$n,"*) ;; *) printf '%s ' "$n" ;; esac
  done
}
NEWCERT=0
if [ ! -f console/certs/server.crt ]; then
  (cd console && bash scripts/make_cert.sh) && NEWCERT=1 \
    || echo "no certificate: the iPad gets typed commands only until console/scripts/make_cert.sh works"
elif [ -f console/certs/engram-ca.key ] && [ -n "$(cert_misses)" ]; then
  miss=$(cert_misses)
  (cd console && bash scripts/make_cert.sh > "$LOGS/cert.log" 2>&1) && NEWCERT=1 \
    && echo "renewed the console certificate to cover ${miss% } (same CA: an iPad that trusts it still does)" \
    || echo "could not renew the console certificate (see $LOGS/cert.log): Safari will refuse https://${miss%% *}:8443"
fi

# 4. console on :8443 (https, needed for the iPad microphone) and :8000 (certificate download). Its camera view,
#    distance chip and SIMULATOR label come from its own ENGRAM_ROBOT_URL: mock, or with --real the robot's address.
#    Motion never goes from the console to the robot: in brain mode every plan runs through the brain.
VIEW=${ROBOT:-mock}
console_ok() { # both ports answer (8443 only once there is a certificate) and it shows the robot it should
  healthy http://127.0.0.1:8000/ && { [ ! -f console/certs/server.crt ] || healthy https://127.0.0.1:8443/; } &&
    [ "$(field http://127.0.0.1:8000/status robot_url)" = "$VIEW" ]
}
if [ $RESTART = 0 ] && [ $NEWCERT = 0 ] && console_ok; then
  echo "console already up (robot view: $VIEW)"
else
  healthy http://127.0.0.1:8000/ && echo "restarting the console (robot view: $VIEW)"
  if free_ports 8000 8443; then
    start console env ENGRAM_BRAIN_URL=http://127.0.0.1:8001 bash console/scripts/run.sh
    wait_for http://127.0.0.1:8000/ 30 || echo "console did not answer; see $LOGS/console.log"
  fi
fi

# 5. Cortex memory sidecar (optional; its own database, never the robot)
if [ $SIDECAR = 1 ]; then sh scripts/cortex_sidecar.sh start; fi

echo
bash scripts/cds_status.sh
