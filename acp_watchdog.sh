#!/usr/bin/env bash
# Continuous Background Supervisor / Watchdog for ACP REST Bridge & Language Server (v2.2.1)
# Featuring Singleton File Lock, TTY FD Cleanup, and Auto-Recovery for both Bridge & agy LS.

LOCK_FILE="/home/codex/.codex/acp_watchdog.lock"
WATCHDOG_LOG="/home/codex/.codex/acp_watchdog.log"
SCRIPT_LAUNCHER="/workspace/scripts/ensure_acp_bridge.sh"
STATUS_URL="http://127.0.0.1:8765/acp/v1/status"
AGY_SESSION="agy"
LS_FAILURE_LIMIT=3
AGY_START_GRACE_SECONDS=60

# Close all extra inherited file descriptors (from 3 to 255)
exec 0</dev/null
for fd in $(seq 3 255); do
    eval "exec $fd>&-" 2>/dev/null || true
done

mkdir -p /home/codex/.codex
chmod 700 /home/codex/.codex

exec 201>"$LOCK_FILE"
chmod 600 "$LOCK_FILE" 2>/dev/null

# Singleton Lock: Exit if another watchdog instance is active
if ! flock -n 201; then
    exit 0
fi

touch "$WATCHDOG_LOG"
chmod 600 "$WATCHDOG_LOG"

echo "[$(date)] ACP Watchdog Supervisor (Singleton) started" >> "$WATCHDOG_LOG"

agy_process_running() {
    local pid exe arg1
    while read -r pid; do
        [ -n "$pid" ] || continue
        exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
        arg1=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | sed -n '2p')
        if [ "$(basename "$exe")" = "agy" ] && [ "$arg1" = "-c" ]; then
            return 0
        fi
    done < <(pgrep -x agy 2>/dev/null || true)
    return 1
}

language_server_connected() {
    curl -fsS --connect-timeout 2 -m 3 "$STATUS_URL" 2>/dev/null |
        jq -e '.language_server.status == "connected" and (.language_server.address | type == "string")' >/dev/null 2>&1
}

start_agy_session() {
    tmux kill-session -t "$AGY_SESSION" 2>/dev/null || true
    sleep 0.2
    tmux new-session -d -s "$AGY_SESSION" \
        'cd /workspace && while true; do agy -c; sleep 5; done'
    agy_started_at=$(date +%s)
}

stop_stale_agy_processes() {
    local pid exe arg1
    while read -r pid; do
        [ -n "$pid" ] || continue
        exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
        arg1=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | sed -n '2p')
        if [ "$(basename "$exe")" = "agy" ] && [ "$arg1" = "-c" ]; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done < <(pgrep -x agy 2>/dev/null || true)
}

ls_failures=0
agy_started_at=$(date +%s)

while true; do
    # Recover the REST wrapper independently. LS health is queried through this
    # wrapper, so a Bridge outage must never be counted as an agy failure.
    bridge_healthy=1
    if ! curl -fsS --connect-timeout 2 -m 3 "http://127.0.0.1:8765/health" 2>/dev/null | grep -q "Antigravity REST Bridge Server"; then
        bridge_healthy=0
        echo "[$(date)] Bridge down or unresponsive. Triggering recovery..." >> "$WATCHDOG_LOG"
        "$SCRIPT_LAUNCHER" 201>&- 200>&- >> "$WATCHDOG_LOG" 2>&1 || true
        if curl -fsS --connect-timeout 2 -m 3 "http://127.0.0.1:8765/health" 2>/dev/null | grep -q "Antigravity REST Bridge Server"; then
            bridge_healthy=1
        fi
    fi

    # A process check catches crashes immediately; the deep status check also catches
    # a wedged agy process whose Language Server port is no longer usable.
    if ! agy_process_running; then
        echo "[$(date)] agy process offline. Recreating tmux session..." >> "$WATCHDOG_LOG"
        start_agy_session
        ls_failures=0
    elif [ "$bridge_healthy" -eq 1 ] && language_server_connected; then
        ls_failures=0
    elif [ "$bridge_healthy" -eq 1 ]; then
        now=$(date +%s)
        if [ $((now - agy_started_at)) -ge "$AGY_START_GRACE_SECONDS" ]; then
            ls_failures=$((ls_failures + 1))
            echo "[$(date)] agy process exists but Language Server is offline ($ls_failures/$LS_FAILURE_LIMIT)." >> "$WATCHDOG_LOG"
            if [ "$ls_failures" -ge "$LS_FAILURE_LIMIT" ]; then
                echo "[$(date)] Language Server remained offline. Restarting agy session..." >> "$WATCHDOG_LOG"
                stop_stale_agy_processes
                sleep 1
                start_agy_session
                ls_failures=0
            fi
        fi
    else
        # Preserve the failure counter while Bridge recovery is pending, but do
        # not punish agy for an unavailable status endpoint.
        echo "[$(date)] Bridge unavailable; skipping Language Server failure accounting." >> "$WATCHDOG_LOG"
    fi
    sleep 5
done
