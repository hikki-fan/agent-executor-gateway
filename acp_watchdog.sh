#!/usr/bin/env bash
# Continuous Background Supervisor / Watchdog for ACP REST Bridge Server
# Featuring Singleton File Lock, TTY FD Cleanup, and Lock FD Isolation.

LOCK_FILE="/home/codex/.codex/acp_watchdog.lock"
WATCHDOG_LOG="/home/codex/.codex/acp_watchdog.log"
SCRIPT_LAUNCHER="/workspace/scripts/ensure_acp_bridge.sh"

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

while true; do
    if ! curl -fsS --connect-timeout 2 -m 3 "http://127.0.0.1:8765/health" 2>/dev/null | grep -q "Antigravity REST Bridge Server"; then
        echo "[$(date)] Service down or unresponsive. Triggering recovery..." >> "$WATCHDOG_LOG"
        "$SCRIPT_LAUNCHER" 201>&- 200>&- >> "$WATCHDOG_LOG" 2>&1
    fi
    sleep 5
done
