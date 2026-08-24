#!/usr/bin/env bash
#
# gateway_watchdog.sh — candidate persistent supervisor for Agent Executor Gateway.
#
# This script is deliberately shipped as a candidate handoff target. Installing it
# into a container entrypoint/profile is a separate, explicitly authorized action.
# It never kills an unverified process and never reuses the legacy bridge hooks.
#
# Required startup marker:
#   AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SERVER_SCRIPT="$SCRIPT_DIR/acp_server.py"
SERVER_SCRIPT_REAL="$(realpath "$SERVER_SCRIPT" 2>/dev/null || echo "$SERVER_SCRIPT")"

GATEWAY_PORT="${ACP_PORT:-8765}"
GATEWAY_RUN_DIR="${GATEWAY_RUN_DIR:-${MIGRATION_RUN_DIR:-$HOME/.agent-executor-gateway/production}}"
GATEWAY_TOKEN_FILE="${ACP_TOKEN_FILE:-$HOME/.codex/acp_token}"
GATEWAY_PID_FILE="${GATEWAY_PID_FILE:-$GATEWAY_RUN_DIR/production.pid}"
GATEWAY_LOG_FILE="${GATEWAY_LOG_FILE:-$GATEWAY_RUN_DIR/production.log}"
GATEWAY_LOCK_FILE="${GATEWAY_LOCK_FILE:-$GATEWAY_RUN_DIR/gateway_watchdog.lock}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HEALTH_INTERVAL_SEC="${HEALTH_INTERVAL_SEC:-5}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-3}"

validate_port() {
    [[ "$GATEWAY_PORT" =~ ^[0-9]+$ ]] && [ "$GATEWAY_PORT" -ge 1024 ] && [ "$GATEWAY_PORT" -le 65535 ]
}

port_has_listener() {
    local hex_port
    hex_port=$(printf "%04X" "$GATEWAY_PORT")
    if [ -r "/proc/net/tcp" ] && awk -v expected=":$hex_port" \
        'NR > 1 && $2 ~ (expected "$") && $4 == "0A" { found = 1 } END { exit(found ? 0 : 1) }' \
        /proc/net/tcp 2>/dev/null; then
        return 0
    fi
    if [ -r "/proc/net/tcp6" ] && awk -v expected=":$hex_port" \
        'NR > 1 && $2 ~ (expected "$") && $4 == "0A" { found = 1 } END { exit(found ? 0 : 1) }' \
        /proc/net/tcp6 2>/dev/null; then
        return 0
    fi
    return 1
}

check_not_symlink() {
    local path="$1"
    local label="$2"
    if [ -L "$path" ]; then
        echo "Security Error: $label '$path' is a symbolic link." >&2
        return 1
    fi
}

ensure_runtime() {
    check_not_symlink "$GATEWAY_RUN_DIR" "Gateway runtime directory"
    if [ ! -d "$GATEWAY_RUN_DIR" ]; then
        mkdir -p "$GATEWAY_RUN_DIR"
    fi
    chmod 0700 "$GATEWAY_RUN_DIR"
    check_not_symlink "$GATEWAY_PID_FILE" "Gateway PID file"
    check_not_symlink "$GATEWAY_LOG_FILE" "Gateway log file"
    check_not_symlink "$GATEWAY_LOCK_FILE" "Gateway watchdog lock file"
    if [ ! -f "$GATEWAY_LOG_FILE" ]; then
        (umask 077 && : > "$GATEWAY_LOG_FILE")
    fi
    chmod 0600 "$GATEWAY_LOG_FILE"
}

is_zombie() {
    local pid="$1"
    [ -r "/proc/$pid/status" ] && grep -q '^State:[[:space:]]*Z' "/proc/$pid/status" 2>/dev/null
}

is_gateway_process() {
    local pid="$1"
    [ -n "$pid" ] && [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 0 ] || return 1
    [ -d "/proc/$pid" ] && ! is_zombie "$pid" && kill -0 "$pid" 2>/dev/null || return 1

    local exe
    exe=$(realpath "/proc/$pid/exe" 2>/dev/null || echo "")
    [[ "$exe" =~ /python[0-9.]*$ ]] || return 1

    local args=()
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < "/proc/$pid/cmdline"
    [ "${#args[@]}" -ge 2 ] || return 1
    local script_arg_real
    script_arg_real=$(realpath "${args[1]}" 2>/dev/null || echo "${args[1]}")
    [ "${args[1]}" = "$SERVER_SCRIPT" ] || [ "$script_arg_real" = "$SERVER_SCRIPT_REAL" ]
}

read_pid() {
    local pid=""
    if [ -f "$GATEWAY_PID_FILE" ] && [ ! -L "$GATEWAY_PID_FILE" ]; then
        pid=$(sed -n '1p' "$GATEWAY_PID_FILE" 2>/dev/null || true)
    fi
    printf '%s' "$pid"
}

healthy() {
    local response
    response=$(curl -fsS --max-time "$HEALTH_TIMEOUT_SEC" "http://127.0.0.1:$GATEWAY_PORT/health" 2>/dev/null || true)
    [[ "$response" == *'"status": "online"'* || "$response" == *'"status":"online"'* ]] || return 1
    [[ "$response" == *'Antigravity REST Bridge Server'* ]]
}

start_gateway() {
    check_not_symlink "$GATEWAY_TOKEN_FILE" "Gateway token file"
    [ -f "$GATEWAY_TOKEN_FILE" ] || { echo "Gateway token file is missing: $GATEWAY_TOKEN_FILE" >&2; return 1; }
    if ! "$PYTHON_BIN" -c 'import os, stat, sys; mode=stat.S_IMODE(os.stat(sys.argv[1]).st_mode); raise SystemExit(0 if mode == 0o600 else 1)' "$GATEWAY_TOKEN_FILE" 2>/dev/null; then
        echo "Gateway token file must be a regular 0600 file: $GATEWAY_TOKEN_FILE" >&2
        return 1
    fi
    [ -x "$SERVER_SCRIPT" ] || { echo "Gateway server script is missing or not executable: $SERVER_SCRIPT" >&2; return 1; }

    local existing_pid
    existing_pid=$(read_pid)
    if is_gateway_process "$existing_pid"; then
        echo "Gateway process already running (PID $existing_pid)."
        return 0
    fi
    rm -f "$GATEWAY_PID_FILE"

    if healthy || port_has_listener; then
        echo "Another service already owns port $GATEWAY_PORT; refusing to start a duplicate." >&2
        return 1
    fi

    echo "Starting Agent Executor Gateway on port $GATEWAY_PORT..."
    (
        export ACP_PORT="$GATEWAY_PORT"
        export ACP_TOKEN_FILE="$GATEWAY_TOKEN_FILE"
        "$PYTHON_BIN" -c '
import os, subprocess, sys
try:
    os.setsid()
except PermissionError:
    pass
script, cwd, log_path, pid_path = sys.argv[1:]
with open(log_path, "a", buffering=1) as log_f:
    proc = subprocess.Popen(
        [sys.executable, script],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    with open(pid_path, "w") as pid_f:
        pid_f.write(f"{proc.pid}\n")
    os.chmod(pid_path, 0o600)
    proc.wait()
' "$SERVER_SCRIPT" "$SCRIPT_DIR" "$GATEWAY_LOG_FILE" "$GATEWAY_PID_FILE"
    ) 200>&- 201>&- </dev/null >>"$GATEWAY_LOG_FILE" 2>&1 &
}

main() {
    if ! validate_port; then
        echo "Gateway port must be an integer between 1024 and 65535: $GATEWAY_PORT" >&2
        return 1
    fi
    ensure_runtime
    exec 0</dev/null
    # Keep exactly one supervisor. The descriptor is intentionally closed in the
    # child launcher above so a crashed watchdog cannot retain this flock.
    exec 201>"$GATEWAY_LOCK_FILE"
    chmod 0600 "$GATEWAY_LOCK_FILE"
    flock -n 201 || exit 0

    trap 'exit 0' TERM INT HUP
    while :; do
        local pid
        pid=$(read_pid)
        if ! is_gateway_process "$pid"; then
            start_gateway || echo "Gateway start deferred; will retry after next health interval." >&2
        elif ! healthy; then
            echo "Gateway PID $pid is alive but health is not ready; will not kill an unverified process." >&2
        fi
        sleep "$HEALTH_INTERVAL_SEC"
    done
}

main "$@"
