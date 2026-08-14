#!/usr/bin/env bash
# Production-grade Daemon Launcher for ACP REST Bridge Server (v2.2.1)
# Featuring Setsid Detachment (-f), Atomic Locking, Strict PID & Executable Verification,
# Lock FD Isolation (200>&- 201>&-), Deadlock-Free SIGTERM Graceful Shutdown with 65s Buffer,
# and Killing Unhealthy NEW_PID with Exit 1 on Readiness Failure.

LOCK_FILE="/home/codex/.codex/acp_bridge.lock"
PID_FILE="/home/codex/.codex/acp_bridge.pid"
LOG_FILE="/home/codex/.codex/acp_bridge.log"
SCRIPT_PATH="/workspace/scripts/acp_server.py"
HEALTH_URL="http://127.0.0.1:8765/health"

mkdir -p /home/codex/.codex
chmod 700 /home/codex/.codex

exec 200>"$LOCK_FILE"
chmod 600 "$LOCK_FILE" 2>/dev/null

if ! flock -n 200; then
    # Another instance is holding lock / launching, exit silently
    exit 0
fi

is_healthy() {
    local response
    response=$(curl -fsS --connect-timeout 2 -m 3 "$HEALTH_URL" 2>/dev/null)
    if [[ "$response" == *"Antigravity REST Bridge Server"* ]]; then
        return 0
    fi
    return 1
}

# If server is not responding or not healthy, restart it
if ! is_healthy; then
    
    # 1. Strict Process Verification (/proc/$pid/exe + argv[1] matching)
    if [ -f "$PID_FILE" ]; then
        old_pid=$(cat "$PID_FILE")
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            exe_target=$(readlink -f "/proc/$old_pid/exe" 2>/dev/null || true)
            exe_base=$(basename "$exe_target" 2>/dev/null || true)
            
            # Read NUL-delimited cmdline arguments
            if [ -f "/proc/$old_pid/cmdline" ]; then
                readarray -d '' args < "/proc/$old_pid/cmdline" 2>/dev/null || args=()
            else
                args=()
            fi

            arg1="${args[1]:-}"

            if [[ "$exe_base" == python* ]] && [ "$arg1" == "$SCRIPT_PATH" ]; then
                # Send SIGTERM for deadlock-free graceful shutdown (allow up to 65s for 60s in-flight tasks)
                kill -15 "$old_pid" 2>/dev/null
                for i in {1..650}; do
                    if ! kill -0 "$old_pid" 2>/dev/null; then
                        break
                    fi
                    sleep 0.1
                done
                # Force SIGKILL if still alive after 65s
                if kill -0 "$old_pid" 2>/dev/null; then
                    kill -9 "$old_pid" 2>/dev/null
                fi
            fi
        fi
        rm -f "$PID_FILE"
    fi

    # 2. Launch detached background daemon process
    touch "$LOG_FILE" "$PID_FILE"
    chmod 600 "$LOG_FILE" "$PID_FILE"
    
    setsid -f python3 "$SCRIPT_PATH" </dev/null 201>&- 200>&- >> "$LOG_FILE" 2>&1
    
    # 3. Synchronous Readiness Polling: Wait for service to become healthy while holding the lock (up to 6s)
    healthy_ready=0
    for i in {1..60}; do
        if is_healthy; then
            healthy_ready=1
            break
        fi
        sleep 0.1
    done

    # 4. If readiness succeeds, find the new PID
    if [ "$healthy_ready" -eq 1 ]; then
        pgrep -f "$SCRIPT_PATH" | head -n 1 > "$PID_FILE"
        chmod 600 "$PID_FILE"
    else
        echo "[!] Error: ACP REST Bridge failed to become healthy within 6 seconds." >&2
        pkill -f "$SCRIPT_PATH" 2>/dev/null || true
        rm -f "$PID_FILE"
        exit 1
    fi
fi

exit 0
