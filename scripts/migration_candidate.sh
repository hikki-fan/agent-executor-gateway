#!/usr/bin/env bash
#
# migration_candidate.sh — Production-Grade Management Helper for Agent Executor Gateway Candidate (Phase 9)
#
# Manages the candidate gateway instance on port 8766 in strict isolation from
# active production services on port 8765.
#
# Usage:
#   ./scripts/migration_candidate.sh start
#   ./scripts/migration_candidate.sh status
#   ./scripts/migration_candidate.sh stop
#   ./scripts/migration_candidate.sh restart
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SERVER_SCRIPT="$SCRIPT_DIR/acp_server.py"
SERVER_SCRIPT_REAL="$(realpath "$SERVER_SCRIPT" 2>/dev/null || echo "$SERVER_SCRIPT")"

CANDIDATE_RUN_DIR="${CANDIDATE_RUN_DIR:-$HOME/.agent-executor-gateway}"
CANDIDATE_PORT="${ACP_PORT:-8766}"
CANDIDATE_TOKEN_FILE="${ACP_TOKEN_FILE:-$CANDIDATE_RUN_DIR/candidate.token}"
CANDIDATE_PID_FILE="${CANDIDATE_PID_FILE:-$CANDIDATE_RUN_DIR/candidate.pid}"
CANDIDATE_LOG_FILE="${CANDIDATE_LOG_FILE:-$CANDIDATE_RUN_DIR/candidate.log}"
CANDIDATE_LOCK_FILE="${CANDIDATE_LOCK_FILE:-$CANDIDATE_RUN_DIR/candidate.lock}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# 1. Validation & Security Helpers

validate_port() {
    local port="$1"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
        echo "Error: CANDIDATE_PORT must be an integer between 1024 and 65535 (got '$port')" >&2
        return 1
    fi
    return 0
}

check_not_symlink() {
    local path="$1"
    local desc="$2"
    if [ -L "$path" ]; then
        echo "Security Error: $desc '$path' is a symbolic link. Symlinks are prohibited for candidate runtime files." >&2
        return 1
    fi
    return 0
}

ensure_run_dir() {
    check_not_symlink "$CANDIDATE_RUN_DIR" "Candidate run directory" || exit 1
    if [ ! -d "$CANDIDATE_RUN_DIR" ]; then
        mkdir -p "$CANDIDATE_RUN_DIR"
    fi
    chmod 0700 "$CANDIDATE_RUN_DIR"
}

is_zombie_process() {
    local pid="$1"
    if [ ! -d "/proc/$pid" ]; then
        return 1
    fi
    if [ -r "/proc/$pid/status" ]; then
        if grep -q "^State:[[:space:]]*Z" "/proc/$pid/status" 2>/dev/null; then
            return 0
        fi
    elif [ -r "/proc/$pid/stat" ]; then
        local stat_content
        stat_content=$(cat "/proc/$pid/stat" 2>/dev/null || echo "")
        if [[ "$stat_content" =~ \)[[:space:]]Z ]]; then
            return 0
        fi
    fi
    return 1
}

is_candidate_process() {
    local pid="$1"

    # Must be a strictly positive integer
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || [ "$pid" -le 0 ]; then
        return 1
    fi

    if [ ! -d "/proc/$pid" ]; then
        return 1
    fi

    if is_zombie_process "$pid"; then
        return 1
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    # Verify executable is a Python interpreter
    local exe
    exe=$(realpath "/proc/$pid/exe" 2>/dev/null || echo "")
    if ! [[ "$exe" =~ /python[0-9.]*$ ]]; then
        return 1
    fi

    # Verify NUL-separated cmdline arguments contain exact acp_server.py path
    local cmdline_file="/proc/$pid/cmdline"
    if [ ! -r "$cmdline_file" ]; then
        return 1
    fi

    local matched=0
    while IFS= read -r -d '' arg; do
        local arg_real
        arg_real=$(realpath "$arg" 2>/dev/null || echo "$arg")
        if [ "$arg" = "$SERVER_SCRIPT" ] || [ "$arg_real" = "$SERVER_SCRIPT_REAL" ]; then
            matched=1
            break
        fi
    done < "$cmdline_file"

    if [ "$matched" -ne 1 ]; then
        return 1
    fi

    return 0
}

ensure_token() {
    ensure_run_dir
    check_not_symlink "$CANDIDATE_TOKEN_FILE" "Token file" || exit 1

    if [ ! -f "$CANDIDATE_TOKEN_FILE" ]; then
        (
            umask 077
            if command -v openssl >/dev/null 2>&1; then
                openssl rand -hex 32 > "$CANDIDATE_TOKEN_FILE"
            else
                "$PYTHON_BIN" -c "import secrets; print(secrets.token_hex(32))" > "$CANDIDATE_TOKEN_FILE"
            fi
        )
    fi
    chmod 0600 "$CANDIDATE_TOKEN_FILE"
}

# 2. Command Implementations

cmd_start() {
    validate_port "$CANDIDATE_PORT" || return 1
    ensure_token

    check_not_symlink "$CANDIDATE_PID_FILE" "PID file" || return 1
    check_not_symlink "$CANDIDATE_LOG_FILE" "Log file" || return 1

    # Check if candidate process is already running
    if [ -f "$CANDIDATE_PID_FILE" ]; then
        local existing_pid
        existing_pid=$(cat "$CANDIDATE_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$existing_pid" ] && is_candidate_process "$existing_pid"; then
            echo "Error: Migration candidate is already running (PID: $existing_pid) on port $CANDIDATE_PORT" >&2
            return 1
        else
            rm -f "$CANDIDATE_PID_FILE"
        fi
    fi

    # Check if candidate port is already bound by another service
    local pre_health
    pre_health=$(curl -fsS --max-time 1 "http://127.0.0.1:$CANDIDATE_PORT/health" 2>/dev/null || echo "")
    if [ -n "$pre_health" ]; then
        echo "Error: Port $CANDIDATE_PORT is already in use by another service. Cannot start candidate." >&2
        return 1
    fi

    # Initialize log file securely
    if [ ! -f "$CANDIDATE_LOG_FILE" ]; then
        ( umask 077 && touch "$CANDIDATE_LOG_FILE" )
    fi
    chmod 0600 "$CANDIDATE_LOG_FILE"

    echo "Starting Agent Executor Gateway Candidate on port $CANDIDATE_PORT..."
    # Launch persistent detached background reaper parent process:
    # 1. Close lock FD 200 so it does not inherit the flock singleton lock.
    # 2. Detach stdin from /dev/null.
    # 3. Setsid in Python, spawns setsid python server, writes server PID to CANDIDATE_PID_FILE.
    # 4. Waits on and reaps the server child process upon exit to ensure server PPID is reaper and not PID 1.
    (
        export ACP_PORT="$CANDIDATE_PORT"
        export ACP_TOKEN_FILE="$CANDIDATE_TOKEN_FILE"
        "$PYTHON_BIN" -c "
import os, sys, subprocess

try:
    os.close(200)
except OSError:
    pass

os.setsid()

server_script = sys.argv[1]
cwd_dir = sys.argv[2]
log_path = sys.argv[3]
pid_path = sys.argv[4]

log_f = open(log_path, 'a')
proc = subprocess.Popen(
    [sys.executable, server_script],
    cwd=cwd_dir,
    stdin=subprocess.DEVNULL,
    stdout=log_f,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)

with open(pid_path, 'w') as pf:
    pf.write(f'{proc.pid}\n')
os.chmod(pid_path, 0o600)

proc.wait()
sys.exit(0)
" "$SERVER_SCRIPT" "$SCRIPT_DIR" "$CANDIDATE_LOG_FILE" "$CANDIDATE_PID_FILE"
    ) 200>&- < /dev/null >> "$CANDIDATE_LOG_FILE" 2>&1 &

    # Wait briefly for background reaper to write server PID
    local candidate_pid=""
    local pid_wait_attempts=20
    while [ "$pid_wait_attempts" -gt 0 ]; do
        if [ -s "$CANDIDATE_PID_FILE" ]; then
            candidate_pid=$(cat "$CANDIDATE_PID_FILE" 2>/dev/null || echo "")
            if [ -n "$candidate_pid" ]; then
                break
            fi
        fi
        sleep 0.1
        pid_wait_attempts=$((pid_wait_attempts - 1))
    done

    if [ -z "$candidate_pid" ]; then
        echo "Error: Failed to write candidate PID file" >&2
        return 1
    fi

    # Poll health endpoint for readiness (up to 10 seconds, checking response payload)
    local max_attempts=20
    local attempt=0
    local is_ready=0

    while [ "$attempt" -lt "$max_attempts" ]; do
        if ! is_candidate_process "$candidate_pid" || is_zombie_process "$candidate_pid"; then
            echo "Error: Candidate process $candidate_pid exited prematurely. Check logs at $CANDIDATE_LOG_FILE" >&2
            rm -f "$CANDIDATE_PID_FILE"
            return 1
        fi

        local health_resp
        health_resp=$(curl -fsS --max-time 1 "http://127.0.0.1:$CANDIDATE_PORT/health" 2>/dev/null || echo "")
        if [[ "$health_resp" =~ "Antigravity REST Bridge Server" ]] && [[ "$health_resp" =~ "online" ]]; then
            is_ready=1
            break
        fi

        sleep 0.5
        attempt=$((attempt + 1))
    done

    if [ "$is_ready" -eq 1 ]; then
        echo "Agent Executor Gateway Candidate started successfully."
        echo "  Status:      ONLINE"
        echo "  PID:         $candidate_pid"
        echo "  Port:        $CANDIDATE_PORT"
        echo "  Token File:  $CANDIDATE_TOKEN_FILE"
        echo "  Log File:    $CANDIDATE_LOG_FILE"
        echo "  Health URL:  http://127.0.0.1:$CANDIDATE_PORT/health"
        return 0
    else
        echo "Error: Candidate failed to respond with valid health payload on port $CANDIDATE_PORT within 10s." >&2
        if is_candidate_process "$candidate_pid" && ! is_zombie_process "$candidate_pid"; then
            kill -TERM -- "-$candidate_pid" 2>/dev/null || kill -TERM "$candidate_pid" 2>/dev/null || true
            sleep 1
            if is_candidate_process "$candidate_pid" && ! is_zombie_process "$candidate_pid"; then
                kill -KILL -- "-$candidate_pid" 2>/dev/null || kill -KILL "$candidate_pid" 2>/dev/null || true
            fi
        fi
        rm -f "$CANDIDATE_PID_FILE"
        return 1
    fi
}

cmd_status() {
    validate_port "$CANDIDATE_PORT" || return 1
    ensure_run_dir

    check_not_symlink "$CANDIDATE_PID_FILE" "PID file" || return 1

    if [ ! -f "$CANDIDATE_PID_FILE" ]; then
        echo "Status: STOPPED (PID file not found)"
        return 1
    fi

    local pid
    pid=$(cat "$CANDIDATE_PID_FILE" 2>/dev/null || echo "")

    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "Status: STOPPED (stale or invalid PID file: $pid)"
        rm -f "$CANDIDATE_PID_FILE"
        return 1
    fi

    if is_zombie_process "$pid"; then
        echo "Status: STOPPED (process $pid exited; defunct/zombie awaiting reap)"
        rm -f "$CANDIDATE_PID_FILE"
        return 1
    fi

    if ! is_candidate_process "$pid"; then
        echo "Status: STOPPED (stale or invalid PID file: $pid)"
        rm -f "$CANDIDATE_PID_FILE"
        return 1
    fi

    local health_resp
    health_resp=$(curl -fsS --max-time 2 "http://127.0.0.1:$CANDIDATE_PORT/health" 2>/dev/null || echo "")

    if [[ "$health_resp" =~ "Antigravity REST Bridge Server" ]] && [[ "$health_resp" =~ "online" ]]; then
        echo "Status: ONLINE"
        echo "  PID:         $pid"
        echo "  Port:        $CANDIDATE_PORT"
        echo "  Token File:  $CANDIDATE_TOKEN_FILE"
        echo "  Log File:    $CANDIDATE_LOG_FILE"
        echo "  Health:      $health_resp"
        return 0
    else
        echo "Status: UNRESPONSIVE (PID $pid running, but port $CANDIDATE_PORT health payload invalid)"
        return 1
    fi
}

cmd_stop() {
    ensure_run_dir
    check_not_symlink "$CANDIDATE_PID_FILE" "PID file" || return 1

    if [ ! -f "$CANDIDATE_PID_FILE" ]; then
        echo "Candidate is already stopped."
        return 0
    fi

    local pid
    pid=$(cat "$CANDIDATE_PID_FILE" 2>/dev/null || echo "")

    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "Candidate PID file was stale. Removed."
        rm -f "$CANDIDATE_PID_FILE"
        return 0
    fi

    if is_zombie_process "$pid"; then
        echo "Candidate process $pid has already exited (defunct/zombie awaiting reap)."
        rm -f "$CANDIDATE_PID_FILE"
        return 0
    fi

    # Strictly re-verify process identity before sending signals
    if ! is_candidate_process "$pid"; then
        echo "Security Error: PID $pid in PID file is not a verified acp_server candidate process. Aborting signal to avoid impacting unrelated processes." >&2
        rm -f "$CANDIDATE_PID_FILE"
        return 1
    fi

    echo "Stopping candidate process group (PID: $pid)..."
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true

    # Graceful shutdown wait loop (up to 10s)
    local max_attempts=20
    local attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if ! kill -0 "$pid" 2>/dev/null || is_zombie_process "$pid"; then
            break
        fi
        sleep 0.5
        attempt=$((attempt + 1))
    done

    # If still alive and not zombie, re-check process identity before SIGKILL
    if kill -0 "$pid" 2>/dev/null && ! is_zombie_process "$pid"; then
        if is_candidate_process "$pid"; then
            echo "Warning: Candidate process did not stop gracefully. Sending SIGKILL to process group..."
            kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
            sleep 0.5
        else
            echo "Warning: Process PID $pid changed identity during shutdown. Skipping SIGKILL." >&2
        fi
    fi

    rm -f "$CANDIDATE_PID_FILE"
    echo "Candidate stopped."
    return 0
}

# 3. Entry Point Protected by Singleton Lock

main() {
    local action="${1:-}"
    if [ -z "$action" ]; then
        echo "Usage: $0 {start|status|stop|restart}" >&2
        exit 1
    fi

    ensure_run_dir
    check_not_symlink "$CANDIDATE_LOCK_FILE" "Lock file" || exit 1

    if [ ! -f "$CANDIDATE_LOCK_FILE" ]; then
        ( umask 077 && touch "$CANDIDATE_LOCK_FILE" )
    fi
    chmod 0600 "$CANDIDATE_LOCK_FILE"

    # Acquire exclusive non-blocking file lock
    exec 200>"$CANDIDATE_LOCK_FILE"
    if ! flock -n 200; then
        echo "Error: Another migration candidate operation is currently in progress (lock held on $CANDIDATE_LOCK_FILE)" >&2
        exit 1
    fi

    case "$action" in
        start)
            cmd_start
            ;;
        status)
            cmd_status
            ;;
        stop)
            cmd_stop
            ;;
        restart)
            cmd_stop
            cmd_start
            ;;
        *)
            echo "Usage: $0 {start|status|stop|restart}" >&2
            exit 1
            ;;
    esac
}

main "$@"
