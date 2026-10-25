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

# The listener on the port is our child: the listening socket's inode is in the child's fd table (Linux /proc).
# Until that is shown no credential goes to whatever answers there; without /proc it cannot be shown: not ours.
owns_port() { "$(python_bin)" -c 'import os,sys
pid,port=sys.argv[1],int(sys.argv[2]);inodes=set()
for t in ("/proc/net/tcp","/proc/net/tcp6"):
    try:
        for line in open(t).read().splitlines()[1:]:
            f=line.split()
            if f[3]=="0A" and int(f[1].rsplit(":",1)[1],16)==port:inodes.add("socket:["+f[9]+"]")
    except OSError:pass
fds="/proc/"+pid+"/fd";mine=False
try:
    for fd in os.listdir(fds):
        try:mine=mine or os.readlink(fds+"/"+fd) in inodes
        except OSError:pass
except OSError:pass
print("ours" if inodes and mine else "not-ours")' "$1" "$PORT"; }
# The responder is OUR Cortex: /health says cortex, our token is accepted and a wrong one refused. Called only once
# owns_port showed the listener is our child; redirects are not followed. The token is read from its file here,
# never placed on a command line.
ours() { "$(python_bin)" -c 'import json,os,sys,urllib.error,urllib.request
class N(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None
def get(path,token=None):
    r=urllib.request.Request(sys.argv[1]+path,headers={"Authorization":"Bearer "+token} if token else {})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),N()).open(r,timeout=2) as x:return x.status,x.read()
    except urllib.error.HTTPError as e:return e.code,b""
t=open(sys.argv[2],encoding="utf-8").read().strip();s,body=get("/health")
ok=s==200 and json.loads(body or b"{}").get("service")=="cortex" and get("/camera/status",t)[0]==200 and get("/camera/status","not-"+t)[0]==401
sys.exit(0 if ok else 1)' "$(url)" "$TOKEN_FILE"; }
# An existing state dir is adopted only if it is a real directory we own with mode 700 (never chmodded here).
dir_ok() { "$(python_bin)" -c 'import os,stat,sys
st=os.lstat(sys.argv[1])
print("ok" if stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid() and stat.S_IMODE(st.st_mode)==0o700 else "bad")' "$1"; }
# The token is created exclusively and without following a symlink: an existing path is never written through.
new_token() { "$(python_bin)" -c 'import os,secrets,sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
os.write(fd,secrets.token_urlsafe(32).encode()+bytes([10]));os.close(fd)' "$1"; }
# The remote end of the ssh tunnel is the address the sidecar binds (bracketed for IPv6).
tunnel_host() { case $HOST in *:*) echo "[$HOST]" ;; *) echo "$HOST" ;; esac; }

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
    # A dedicated state directory: created here with mode 700, or an existing one that already is exactly that.
    if [ -e "$STATE" ] || [ -L "$STATE" ]; then
        [ "$(dir_ok "$STATE")" = ok ] || die "refusing CORTEX_STATE_DIR=$STATE: an existing path must be a directory you own with mode 700, not a shared or symlinked one; name a new path and the sidecar creates it"
    else
        (umask 077 && mkdir -p -- "$STATE") || die "could not create $STATE"
    fi
    [ ! -L "$TOKEN_FILE" ] || die "refusing $TOKEN_FILE: the token path is a symlink"
    if [ ! -e "$TOKEN_FILE" ]; then new_token "$TOKEN_FILE" || die "could not create $TOKEN_FILE"; fi
    [ -f "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ] || die "refusing $TOKEN_FILE: not a non-empty regular file"
    chmod 600 "$TOKEN_FILE"
    cd "$REPO"
    # env -i: exactly this configuration; nothing else from the caller's environment leaks in. The token itself is
    # never an argument (ps, /proc/<pid>/cmdline): the server reads it from the private token file.
    env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
        CORTEX_API_TOKEN_FILE="$TOKEN_FILE" CORTEX_OPERATOR="${CORTEX_OPERATOR:-carther}" \
        CORTEX_DB="$DB" ENGRAM_ARENA="$ARENA" \
        CORTEX_LIVE=0 CORTEX_CLOUD=0 CORTEX_QWEN=0 CORTEX_IDLE=1 \
        nohup "$py" -m uvicorn engram.cortex.api:create_app --factory --host "$HOST" --port "$PORT" \
        >>"$LOG" 2>&1 </dev/null &
    echo $! > "$PID_FILE"
    i=0
    while [ $i -lt 30 ]; do
        kill -0 "$(cat "$PID_FILE")" 2>/dev/null || break      # our process died (e.g. the port was taken): never "started"
        if [ "$(owns_port "$(cat "$PID_FILE")")" = ours ] && ours >/dev/null 2>&1 && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "cortex-sidecar: started (pid $(cat "$PID_FILE")) at $(url); db $DB; token file $TOKEN_FILE; log $LOG"
            echo "cortex-sidecar: inspector: ssh -L $PORT:$(tunnel_host):$PORT starrasp@star-pi, then http://127.0.0.1:$PORT/cortex"
            return 0
        fi
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
