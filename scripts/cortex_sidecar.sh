#!/bin/sh
# ENGRAM Cortex as a SIDECAR for the demo (POSIX sh, for the Pi).
#
# The existing brain (engram/api.py on :8001, engram.db) stays the ONLY executor and the
# console keeps pointing at it. This starts Cortex as a separate process with its own
# database, the real lab arena, no robot, no camera, no cloud, no Qwen, idle learning on,
# bound to loopback only:
#     sh scripts/cortex_sidecar.sh start | stop | status
# Inspector, from a laptop:  ssh -L 8010:127.0.0.1:8010 starrasp@star-pi
#                            then open http://127.0.0.1:8010/cortex
# The operator token is generated once into $CORTEX_STATE_DIR/token (mode 0600, outside the
# repository) and never printed. Paste it into the inspector from that file; never post it.
# Rollback = "stop" (kill this process). Nothing else on the Pi is touched.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STATE=${CORTEX_STATE_DIR:-$HOME/.cortex-sidecar}
TOKEN_FILE=$STATE/token
PID_FILE=$STATE/cortex.pid
LOG=$STATE/cortex.log
HOST=${CORTEX_HOST:-127.0.0.1}
PORT=${CORTEX_PORT:-8010}
DB=${CORTEX_DB:-$STATE/cortex.db}
ARENA=$REPO/fixtures/lab/arena.json

die() { echo "cortex-sidecar: $*" >&2; exit 1; }

python_bin() {
    if [ -n "${CORTEX_PYTHON:-}" ]; then echo "$CORTEX_PYTHON"
    elif [ -x "$REPO/.venv/bin/python" ]; then echo "$REPO/.venv/bin/python"
    else echo python3; fi
}

# Running = the pid file names a live process that is this Cortex server (guards against pid reuse).
running() {
    [ -f "$PID_FILE" ] || return 1
    pid=$(cat "$PID_FILE")
    kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -q 'engram.cortex.api:create_app'
}

# Canonical path (symlinks and .. resolved, also for a path that does not exist yet; '/' separators) and file
# identity (device + inode, so a hard link counts), both by the Python that runs Cortex: portable beyond GNU
# realpath/stat. same_file answers with a word, so a failed check can never read as "different".
canon() { "$(python_bin)" -c 'import os,sys;print(os.path.realpath(sys.argv[1]).replace(os.sep,"/"))' "$1"; }
same_file() { "$(python_bin)" -c 'import os,sys;a,b=sys.argv[1:];print("same" if os.path.exists(a) and os.path.exists(b) and os.path.samefile(a,b) else "different")' "$1" "$2"; }

url() { case $HOST in *:*) echo "http://[$HOST]:$PORT" ;; *) echo "http://$HOST:$PORT" ;; esac; }

health() {
    "$(python_bin)" -c 'import sys,urllib.request
try:print(urllib.request.urlopen(sys.argv[1]+"/health",timeout=2).read().decode())
except Exception as e:sys.exit(1)' "$(url)"
}

refuse_unsafe_config() {
    case $HOST in
        127.0.0.1|::1) ;;
        *) die "refusing to bind '$HOST': the sidecar binds loopback only (127.0.0.1 or ::1); reach it with ssh -L" ;;
    esac
    case $PORT in
        ''|*[!0-9]*) die "CORTEX_PORT must be a number" ;;
    esac
    [ "${CORTEX_LIVE:-0}" = 0 ] || die "refusing CORTEX_LIVE=${CORTEX_LIVE}: the sidecar never drives the robot; the brain on :8001 is the only executor"
    [ "${CORTEX_CLOUD:-0}" = 0 ] || die "refusing CORTEX_CLOUD=${CORTEX_CLOUD}: the sidecar is local-only"
    [ "${CORTEX_QWEN:-0}" = 0 ] || die "refusing CORTEX_QWEN=${CORTEX_QWEN}: the sidecar does not call Qwen"
    [ -z "${CORTEX_CAMERA_URL:-}" ] || die "refusing CORTEX_CAMERA_URL: the sidecar runs without a camera"
    db=$(canon "$DB") && repo=$(canon "$REPO") && state=$(canon "$STATE") || die "cannot resolve the configured paths with $(python_bin)"
    for name in "$DB" "$db"; do
        [ "$(basename -- "$name")" != engram.db ] || die "refusing CORTEX_DB=$DB (resolves to $db): Cortex never opens the old brain's database"
    done
    for brain in "$REPO/engram.db" "${ENGRAM_DB:-}"; do
        [ -z "$brain" ] || [ "$(same_file "$DB" "$brain")" = different ] || die "refusing CORTEX_DB=$DB: it is the same file as the old brain's database $brain"
    done
    case $state/ in
        "$repo"/*) die "refusing CORTEX_STATE_DIR=$STATE (resolves to $state): the token must live outside the repository" ;;
    esac
    # From here on only canonical paths: start() changes directory, so a relative path would move.
    STATE=$state; DB=$db; TOKEN_FILE=$STATE/token; PID_FILE=$STATE/cortex.pid; LOG=$STATE/cortex.log
    [ -f "$ARENA" ] || die "lab arena not found at $ARENA"
}

start() {
    refuse_unsafe_config
    if running; then echo "cortex-sidecar: already running (pid $(cat "$PID_FILE")) at $(url)"; return 0; fi
    py=$(python_bin)
    mkdir -p "$STATE" && chmod 700 "$STATE"
    if [ ! -s "$TOKEN_FILE" ]; then
        (umask 077 && "$py" -c 'import secrets;print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
    fi
    chmod 600 "$TOKEN_FILE"
    cd "$REPO"
    # env -i: exactly this configuration; nothing else from the caller's environment leaks in.
    env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
        CORTEX_API_TOKEN="$(cat "$TOKEN_FILE")" CORTEX_OPERATOR="${CORTEX_OPERATOR:-carther}" \
        CORTEX_DB="$DB" ENGRAM_ARENA="$ARENA" \
        CORTEX_LIVE=0 CORTEX_CLOUD=0 CORTEX_QWEN=0 CORTEX_IDLE=1 \
        nohup "$py" -m uvicorn engram.cortex.api:create_app --factory --host "$HOST" --port "$PORT" \
        >>"$LOG" 2>&1 </dev/null &
    echo $! > "$PID_FILE"
    i=0
    while [ $i -lt 30 ]; do
        if health >/dev/null 2>&1; then
            echo "cortex-sidecar: started (pid $(cat "$PID_FILE")) at $(url); db $DB; token file $TOKEN_FILE; log $LOG"
            echo "cortex-sidecar: inspector: ssh -L $PORT:127.0.0.1:$PORT starrasp@star-pi, then http://127.0.0.1:$PORT/cortex"
            return 0
        fi
        kill -0 "$(cat "$PID_FILE")" 2>/dev/null || break
        sleep 1; i=$((i+1))
    done
    tail -n 20 "$LOG" >&2 || true
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
    die "did not become healthy; see $LOG"
}

stop() {
    if ! running; then rm -f "$PID_FILE"; echo "cortex-sidecar: not running"; return 0; fi
    pid=$(cat "$PID_FILE")
    kill "$pid"
    i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 15 ]; do sleep 1; i=$((i+1)); done
    if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; echo "cortex-sidecar: did not exit in 15 s; killed"; fi
    rm -f "$PID_FILE"
    echo "cortex-sidecar: stopped (pid $pid)"
}

status() {
    if running; then
        echo "cortex-sidecar: running (pid $(cat "$PID_FILE")) at $(url); db $DB"
        health || echo "cortex-sidecar: process alive but /health did not answer"
    else
        echo "cortex-sidecar: not running"; return 3
    fi
}

case ${1:-} in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "usage: sh scripts/cortex_sidecar.sh start|stop|status" >&2; exit 2 ;;
esac
