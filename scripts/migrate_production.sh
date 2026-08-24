#!/usr/bin/env bash
#
# migrate_production.sh — Reversible Production Migration Tool (Phase 10)
# Agent Executor Gateway (agent-executor-gateway)
#
# Provides safe, reversible, and verifiable preflight, cutover, and rollback
# controls between the legacy bridge and the new unified gateway.
#
# SAFETY GATES:
#   1. Default execution is read-only preflight (zero mutations, zero signals, zero lock creation, zero bytecode writes).
#   2. Cutover strictly requires BOTH:
#        --confirm-cutover CLI flag AND CONFIRM_PRODUCTION_CUTOVER=1 env var.
#   3. If active watchdog (acp_watchdog.sh) is running, cutover also requires:
#        --handle-watchdog CLI flag AND CONFIRM_WATCHDOG_OVERRIDE=1 env var.
#   4. Rollback strictly requires BOTH:
#        --confirm-rollback CLI flag AND CONFIRM_PRODUCTION_ROLLBACK=1 env var.
#   5. Token alignment: Reuses existing regular non-symlink 0600 legacy token if ACP_TOKEN_FILE is unset;
#      otherwise creates controlled migration token and makes handoff explicit, eliminating client 401s.
#   6. All process signals strictly verify /proc exe and exact cmdline script path at argv[1] before sending.
#   7. Process group signals (-PID) are only sent if PGID == PID (process group leader); otherwise signals verified PID only.
#   8. Graceful legacy shutdown allows configurable budget (default 335s) for in-flight task completion.
#   9. Production cutover is blocked until persistent startup wiring points to the new gateway watchdog.
#  10. The legacy repository is NEVER modified or deleted.
#
# Usage:
#   ./scripts/migrate_production.sh [preflight]
#   ./scripts/migrate_production.sh status
#   ./scripts/migrate_production.sh cutover --confirm-cutover [--handle-watchdog]
#   ./scripts/migrate_production.sh rollback --confirm-rollback
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
GATEWAY_SERVER_SCRIPT="$SCRIPT_DIR/acp_server.py"
GATEWAY_SERVER_SCRIPT_REAL="$(realpath "$GATEWAY_SERVER_SCRIPT" 2>/dev/null || echo "$GATEWAY_SERVER_SCRIPT")"

OLD_BRIDGE_DIR="${OLD_BRIDGE_DIR:-/workspace/antigravity-rest-bridge}"
OLD_BRIDGE_SERVER_SCRIPT="${OLD_BRIDGE_SERVER_SCRIPT:-/workspace/scripts/acp_server.py}"
if [ ! -f "$OLD_BRIDGE_SERVER_SCRIPT" ] && [ -f "$OLD_BRIDGE_DIR/acp_server.py" ]; then
    OLD_BRIDGE_SERVER_SCRIPT="$OLD_BRIDGE_DIR/acp_server.py"
fi
OLD_BRIDGE_SERVER_SCRIPT_REAL="$(realpath "$OLD_BRIDGE_SERVER_SCRIPT" 2>/dev/null || echo "$OLD_BRIDGE_SERVER_SCRIPT")"

WATCHDOG_SCRIPT="${WATCHDOG_SCRIPT:-/workspace/scripts/acp_watchdog.sh}"
if [ ! -f "$WATCHDOG_SCRIPT" ] && [ -f "$OLD_BRIDGE_DIR/acp_watchdog.sh" ]; then
    WATCHDOG_SCRIPT="$OLD_BRIDGE_DIR/acp_watchdog.sh"
fi
WATCHDOG_SCRIPT_REAL="$(realpath "$WATCHDOG_SCRIPT" 2>/dev/null || echo "$WATCHDOG_SCRIPT")"

# Persistent startup handoff is intentionally inspected, never edited, by this tool.
GATEWAY_WATCHDOG_SCRIPT="${GATEWAY_WATCHDOG_SCRIPT:-$SCRIPT_DIR/scripts/gateway_watchdog.sh}"
GATEWAY_WATCHDOG_SCRIPT_REAL="$(realpath "$GATEWAY_WATCHDOG_SCRIPT" 2>/dev/null || echo "$GATEWAY_WATCHDOG_SCRIPT")"
GATEWAY_CLI_PATH="${GATEWAY_CLI_PATH:-$SCRIPT_DIR/acp-cli}"
GATEWAY_CLI_PATH_REAL="$(realpath "$GATEWAY_CLI_PATH" 2>/dev/null || echo "$GATEWAY_CLI_PATH")"
STARTUP_ENTRYPOINT_FILE="${STARTUP_ENTRYPOINT_FILE:-/usr/local/bin/start-codex-container}"
STARTUP_PROFILE_FILE="${STARTUP_PROFILE_FILE-$HOME/.bashrc}"
STARTUP_HANDOFF_MARKER="${STARTUP_HANDOFF_MARKER:-AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF}"

MIGRATION_RUN_DIR="${MIGRATION_RUN_DIR:-$HOME/.agent-executor-gateway/production}"
PROD_PORT="${PROD_PORT:-8765}"
CANDIDATE_PORT="${CANDIDATE_PORT:-8766}"
LEGACY_DEFAULT_TOKEN_FILE="${LEGACY_DEFAULT_TOKEN_FILE:-$HOME/.codex/acp_token}"
PROD_TOKEN_FILE="${ACP_TOKEN_FILE:-}"
PROD_PID_FILE="${PROD_PID_FILE:-$MIGRATION_RUN_DIR/production.pid}"
PROD_LOG_FILE="${PROD_LOG_FILE:-$MIGRATION_RUN_DIR/production.log}"
PROD_LOCK_FILE="${PROD_LOCK_FILE:-$MIGRATION_RUN_DIR/production.lock}"
MIGRATION_STATE_FILE="${MIGRATION_STATE_FILE:-$MIGRATION_RUN_DIR/migration_state.json}"
LEGACY_SUBPROCESS_TIMEOUT_SEC="${LEGACY_SUBPROCESS_TIMEOUT_SEC:-300}"
LEGACY_AUTH_GRACE_SEC="${LEGACY_AUTH_GRACE_SEC:-30}"
LEGACY_GRACEFUL_BUFFER_SEC="${LEGACY_GRACEFUL_BUFFER_SEC:-5}"
# Keep the default literal so malformed operator-provided component values cannot
# be interpreted by Bash arithmetic before validate_legacy_graceful_timeout runs.
LEGACY_GRACEFUL_TIMEOUT_SEC="${LEGACY_GRACEFUL_TIMEOUT_SEC:-335}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# 1. Validation & Safety Helpers

validate_port() {
    local port="$1"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
        echo "Error: Port must be an integer between 1024 and 65535 (got '$port')" >&2
        return 1
    fi
    return 0
}

check_not_symlink() {
    local path="$1"
    local desc="$2"
    if [ -L "$path" ]; then
        echo "Security Error: $desc '$path' is a symbolic link. Symlinks are prohibited for production migration runtime files." >&2
        return 1
    fi
    return 0
}

ensure_run_dir() {
    check_not_symlink "$MIGRATION_RUN_DIR" "Migration run directory" || exit 1
    if [ ! -d "$MIGRATION_RUN_DIR" ]; then
        mkdir -p "$MIGRATION_RUN_DIR"
    fi
    chmod 0700 "$MIGRATION_RUN_DIR"
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

get_process_pgid() {
    local pid="$1"
    if [ -r "/proc/$pid/stat" ]; then
        awk '{print $5}' "/proc/$pid/stat" 2>/dev/null || echo ""
    else
        ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || echo ""
    fi
}

signal_process_safely() {
    local pid="$1"
    local sig="$2"
    local pgid
    pgid=$(get_process_pgid "$pid")
    if [ -n "$pgid" ] && [ "$pgid" -eq "$pid" ] && [ "$pgid" -gt 1 ]; then
        kill "-$sig" -- "-$pid" 2>/dev/null || kill "-$sig" "$pid" 2>/dev/null || true
    else
        kill "-$sig" "$pid" 2>/dev/null || true
    fi
}

validate_legacy_graceful_timeout() {
    if ! [[ "$LEGACY_SUBPROCESS_TIMEOUT_SEC" =~ ^[0-9]+$ ]] || \
       ! [[ "$LEGACY_AUTH_GRACE_SEC" =~ ^[0-9]+$ ]] || \
       ! [[ "$LEGACY_GRACEFUL_BUFFER_SEC" =~ ^[0-9]+$ ]] || \
       ! [[ "$LEGACY_GRACEFUL_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
        echo "Error: legacy graceful timeout settings must be non-negative integers." >&2
        return 1
    fi

    local minimum_timeout=$((LEGACY_SUBPROCESS_TIMEOUT_SEC + LEGACY_AUTH_GRACE_SEC + LEGACY_GRACEFUL_BUFFER_SEC))
    if [ "$LEGACY_GRACEFUL_TIMEOUT_SEC" -lt "$minimum_timeout" ]; then
        echo "Error: LEGACY_GRACEFUL_TIMEOUT_SEC=$LEGACY_GRACEFUL_TIMEOUT_SEC is below the required minimum ${minimum_timeout}s (subprocess=${LEGACY_SUBPROCESS_TIMEOUT_SEC}s + auth_grace=${LEGACY_AUTH_GRACE_SEC}s + buffer=${LEGACY_GRACEFUL_BUFFER_SEC}s)." >&2
        return 1
    fi
    return 0
}

port_has_listener() {
    local port="$1"
    local hex_port
    hex_port=$(printf "%04X" "$port")

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

check_startup_file() {
    local path="$1"
    local label="$2"
    local required="$3"

    if [ -z "$path" ]; then
        echo "  • $label is not configured (explicitly disabled for this fixture)."
        return 0
    fi
    if [ ! -e "$path" ]; then
        if [ "$required" -eq 1 ]; then
            echo "  ✗ Startup handoff missing: $label '$path' does not exist."
            return 1
        fi
        echo "  • Optional $label '$path' is absent; no legacy profile hook can run."
        return 0
    fi
    if [ -L "$path" ]; then
        echo "  ✗ Security Error: $label '$path' is a symbolic link."
        return 1
    fi
    if [ ! -f "$path" ] || [ ! -r "$path" ]; then
        echo "  ✗ Startup handoff invalid: $label '$path' is not a readable regular file."
        return 1
    fi
    if ! "$PYTHON_BIN" -c 'import os, stat, sys; mode=stat.S_IMODE(os.stat(sys.argv[1]).st_mode); raise SystemExit(0 if not mode & 0o022 else 1)' "$path" 2>/dev/null; then
        echo "  ✗ Security Warning: $label '$path' is group/other-writable."
        return 1
    fi
    if grep -Eq '(^|[[:space:]/])(acp_watchdog\.sh|ensure_acp_bridge\.sh)([[:space:]"'"'"'/]|$)' "$path"; then
        echo "  ✗ Legacy startup hook still present in $label '$path' (acp_watchdog.sh/ensure_acp_bridge.sh)."
        return 1
    fi
    if [ "$required" -eq 1 ]; then
        if ! grep -Fq -- "$GATEWAY_CLI_PATH" "$path" && ! grep -Fq -- "$GATEWAY_CLI_PATH_REAL" "$path"; then
            echo "  ✗ Container entrypoint '$path' does not restore the new gateway client '$GATEWAY_CLI_PATH_REAL'."
            return 1
        fi
        if grep -Eq '/workspace/(scripts|antigravity-rest-bridge)/acp-cli([[:space:]"'"'"'/]|$)' "$path"; then
            echo "  ✗ Legacy acp-cli restoration remains in container entrypoint '$path'."
            return 1
        fi
    fi
    if ! grep -Fq -- "$STARTUP_HANDOFF_MARKER" "$path" || \
       { ! grep -Fq -- "$GATEWAY_WATCHDOG_SCRIPT" "$path" && ! grep -Fq -- "$GATEWAY_WATCHDOG_SCRIPT_REAL" "$path"; }; then
        echo "  ✗ Startup handoff incomplete: $label '$path' must contain marker '$STARTUP_HANDOFF_MARKER' and the new watchdog path '$GATEWAY_WATCHDOG_SCRIPT_REAL'."
        return 1
    fi
    echo "  ✓ $label '$path' contains the new gateway watchdog handoff and no legacy hook."
    return 0
}

check_startup_handoff() {
    local passed=1
    echo "  Handoff script: $GATEWAY_WATCHDOG_SCRIPT_REAL"
    if [ -L "$GATEWAY_WATCHDOG_SCRIPT" ] || [ ! -f "$GATEWAY_WATCHDOG_SCRIPT" ] || [ ! -x "$GATEWAY_WATCHDOG_SCRIPT" ]; then
        echo "  ✗ New gateway watchdog is missing, non-executable, or a symbolic link: $GATEWAY_WATCHDOG_SCRIPT"
        passed=0
    else
        echo "  ✓ New gateway watchdog candidate is regular and executable."
    fi
    if [ -L "$GATEWAY_CLI_PATH" ] || [ ! -f "$GATEWAY_CLI_PATH" ] || [ ! -x "$GATEWAY_CLI_PATH" ]; then
        echo "  ✗ New gateway client is missing, non-executable, or a symbolic link: $GATEWAY_CLI_PATH"
        passed=0
    else
        echo "  ✓ New gateway client is regular and executable."
    fi

    check_startup_file "$STARTUP_ENTRYPOINT_FILE" "Container entrypoint" 1 || passed=0
    check_startup_file "$STARTUP_PROFILE_FILE" "Shell profile" 0 || passed=0

    if [ "$passed" -eq 1 ]; then
        return 0
    fi
    return 1
}

# Strict watchdog identity verification with exact script path match at argv[1] (or argv[0])
is_watchdog_process() {
    local pid="$1"
    local expected_watchdog="${2:-$WATCHDOG_SCRIPT_REAL}"

    if [ -z "$expected_watchdog" ]; then
        return 1
    fi
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || [ "$pid" -le 0 ]; then
        return 1
    fi
    if [ ! -d "/proc/$pid" ] || is_zombie_process "$pid"; then
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    local cmdline_file="/proc/$pid/cmdline"
    if [ ! -r "$cmdline_file" ]; then
        return 1
    fi

    local exp_real
    exp_real=$(realpath "$expected_watchdog" 2>/dev/null || echo "$expected_watchdog")

    local args=()
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < "$cmdline_file"

    if [ "${#args[@]}" -lt 1 ]; then
        return 1
    fi

    # Case 1: argv[0] is shell interpreter (bash/sh), argv[1] must equal expected_watchdog
    if [ "${#args[@]}" -ge 2 ]; then
        local exe
        exe=$(realpath "/proc/$pid/exe" 2>/dev/null || echo "")
        local arg0="${args[0]}"
        if [[ "$exe" =~ /(bash|sh|dash|zsh)$ ]] || [[ "$arg0" =~ (bash|sh|dash|zsh)$ ]]; then
            local script_arg="${args[1]}"
            local script_arg_real
            script_arg_real=$(realpath "$script_arg" 2>/dev/null || echo "$script_arg")
            if [ "$script_arg" = "$expected_watchdog" ] || [ "$script_arg_real" = "$exp_real" ]; then
                return 0
            fi
            return 1
        fi
    fi

    # Case 2: argv[0] is the script itself
    local arg0="${args[0]}"
    local arg0_real
    arg0_real=$(realpath "$arg0" 2>/dev/null || echo "$arg0")
    if [ "$arg0" = "$expected_watchdog" ] || [ "$arg0_real" = "$exp_real" ]; then
        return 0
    fi

    return 1
}

find_watchdog_process() {
    local expected_watchdog="${1:-$WATCHDOG_SCRIPT_REAL}"
    for p in /proc/[0-9]*; do
        local num_pid="${p##*/}"
        if is_watchdog_process "$num_pid" "$expected_watchdog"; then
            echo "$num_pid"
            return 0
        fi
    done
    echo ""
}

# Strict process identity verification with ZERO fuzzy fallback.
# expected_script is strictly required.
# NUL argv[1] MUST equal expected absolute/realpath (rejecting python -c ... /path/acp_server.py).
is_acp_server_process() {
    local pid="$1"
    local expected_script="$2"

    if [ -z "$expected_script" ]; then
        return 1
    fi

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

    # Verify cmdline arguments: argv[1] must strictly match expected_script path
    local cmdline_file="/proc/$pid/cmdline"
    if [ ! -r "$cmdline_file" ]; then
        return 1
    fi

    local expected_real
    expected_real=$(realpath "$expected_script" 2>/dev/null || echo "$expected_script")

    local args=()
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < "$cmdline_file"

    if [ "${#args[@]}" -lt 2 ]; then
        return 1
    fi

    local script_arg="${args[1]}"
    local script_arg_real
    script_arg_real=$(realpath "$script_arg" 2>/dev/null || echo "$script_arg")

    if [ "$script_arg" != "$expected_script" ] && [ "$script_arg_real" != "$expected_real" ]; then
        return 1
    fi

    return 0
}

find_process_on_port() {
    local port="$1"
    local expected_script="$2"

    # Hex representation of port in /proc/net/tcp
    local hex_port
    hex_port=$(printf "%04X" "$port")

    # Collect socket inodes listening on this port
    local inodes=()
    if [ -r "/proc/net/tcp" ]; then
        while read -r _ local_addr _ state _ _ _ _ _ inode _; do
            if [[ "$local_addr" =~ :$hex_port$ ]] && [ "$state" = "0A" ]; then
                inodes+=("$inode")
            fi
        done < <(tail -n +2 /proc/net/tcp 2>/dev/null || true)
    fi
    if [ -r "/proc/net/tcp6" ]; then
        while read -r _ local_addr _ state _ _ _ _ _ inode _; do
            if [[ "$local_addr" =~ :$hex_port$ ]] && [ "$state" = "0A" ]; then
                inodes+=("$inode")
            fi
        done < <(tail -n +2 /proc/net/tcp6 2>/dev/null || true)
    fi

    local matching_pids=()

    # Search /proc for processes holding these socket fds
    if [ "${#inodes[@]}" -gt 0 ]; then
        for p in /proc/[0-9]*; do
            local num_pid="${p##*/}"
            for inode in "${inodes[@]}"; do
                if [ -d "/proc/$num_pid/fd" ] 2>/dev/null; then
                    if ls -l "/proc/$num_pid/fd" 2>/dev/null | grep -q "socket:\[$inode\]"; then
                        if is_acp_server_process "$num_pid" "$expected_script"; then
                            matching_pids+=("$num_pid")
                        fi
                        break
                    fi
                fi
            done
        done
    fi

    # Fallback to fuser / lsof inspection if socket scan found no PID
    if [ "${#matching_pids[@]}" -eq 0 ]; then
        local raw_pids=""
        if command -v fuser >/dev/null 2>&1; then
            raw_pids=$(fuser "$port/tcp" 2>/dev/null || echo "")
        fi
        if [ -z "$raw_pids" ] && command -v lsof >/dev/null 2>&1; then
            raw_pids=$(lsof -t -i ":$port" -sTCP:LISTEN 2>/dev/null || echo "")
        fi

        for cand_pid in $raw_pids; do
            if is_acp_server_process "$cand_pid" "$expected_script"; then
                matching_pids+=("$cand_pid")
            fi
        done
    fi

    # Deduplicate matching PIDs
    local unique_pids=($(printf "%s\n" "${matching_pids[@]}" 2>/dev/null | sort -u || true))

    if [ "${#unique_pids[@]}" -eq 1 ]; then
        echo "${unique_pids[0]}"
    elif [ "${#unique_pids[@]}" -gt 1 ]; then
        echo "Error: Multiple processes detected on port $port matching $expected_script: ${unique_pids[*]}" >&2
        echo ""
    else
        echo ""
    fi
}

write_state_file_atomic() {
    local content="$1"
    ensure_run_dir
    check_not_symlink "$MIGRATION_STATE_FILE" "Migration state file" || exit 1

    local tmp_file="$MIGRATION_RUN_DIR/migration_state.json.tmp.$$"
    (
        umask 077
        echo "$content" > "$tmp_file"
    )
    chmod 0600 "$tmp_file"
    mv -f "$tmp_file" "$MIGRATION_STATE_FILE"
    chmod 0600 "$MIGRATION_STATE_FILE"
}

ensure_token() {
    ensure_run_dir

    if [ -n "$PROD_TOKEN_FILE" ]; then
        check_not_symlink "$PROD_TOKEN_FILE" "Configured token file" || exit 1
        if [ ! -f "$PROD_TOKEN_FILE" ]; then
            (
                umask 077
                if command -v openssl >/dev/null 2>&1; then
                    openssl rand -hex 32 > "$PROD_TOKEN_FILE"
                else
                    "$PYTHON_BIN" -c "import secrets; print(secrets.token_hex(32))" > "$PROD_TOKEN_FILE"
                fi
            )
        fi
        chmod 0600 "$PROD_TOKEN_FILE"
        return 0
    fi

    # ACP_TOKEN_FILE was not explicitly provided. Evaluate legacy token alignment strategy.
    local default_migration_token="$MIGRATION_RUN_DIR/production.token"
    check_not_symlink "$default_migration_token" "Default migration token file" || exit 1

    local legacy_usable=0
    if [ -f "$LEGACY_DEFAULT_TOKEN_FILE" ] && [ ! -L "$LEGACY_DEFAULT_TOKEN_FILE" ]; then
        local mode
        mode=$("$PYTHON_BIN" -c "import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$LEGACY_DEFAULT_TOKEN_FILE" 2>/dev/null || echo "")
        if [ "$mode" = "0o600" ]; then
            if [ -s "$LEGACY_DEFAULT_TOKEN_FILE" ]; then
                legacy_usable=1
            fi
        fi
    fi

    if [ "$legacy_usable" -eq 1 ]; then
        echo "Token Alignment: Reusing verified legacy token from $LEGACY_DEFAULT_TOKEN_FILE (mode 0600) to maintain zero-auth disruption for existing clients."
        PROD_TOKEN_FILE="$LEGACY_DEFAULT_TOKEN_FILE"
    else
        echo "Token Alignment Notice: Legacy token file ($LEGACY_DEFAULT_TOKEN_FILE) is missing, symlink, or insecure. Initializing controlled migration token at $default_migration_token."
        PROD_TOKEN_FILE="$default_migration_token"
        if [ ! -f "$PROD_TOKEN_FILE" ]; then
            (
                umask 077
                if command -v openssl >/dev/null 2>&1; then
                    openssl rand -hex 32 > "$PROD_TOKEN_FILE"
                else
                    "$PYTHON_BIN" -c "import secrets; print(secrets.token_hex(32))" > "$PROD_TOKEN_FILE"
                fi
            )
        fi
        chmod 0600 "$PROD_TOKEN_FILE"
    fi
}

# 2. Command Implementations

cmd_preflight() {
    echo "=========================================================="
    echo " Phase 10 Migration Preflight & Verification Inspection"
    echo " (Strictly Read-Only: Zero mutations, Zero signals sent)"
    echo "=========================================================="

    local all_passed=1
    validate_port "$PROD_PORT" || all_passed=0
    validate_port "$CANDIDATE_PORT" || all_passed=0
    validate_legacy_graceful_timeout || all_passed=0

    # 1. Inspect Production Port 8765 Health
    echo ""
    echo "[1/8] Checking Active Production Port ($PROD_PORT)..."
    local prod_health
    prod_health=$(curl -fsS --max-time 2 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")
    if [ -n "$prod_health" ] && [[ "$prod_health" =~ "Antigravity REST Bridge Server" ]] && [[ "$prod_health" =~ "online" ]]; then
        echo "  ✓ Production service is ONLINE on port $PROD_PORT"
        echo "    Payload: $prod_health"
    else
        echo "  ✗ Production service NOT responding or health invalid on port $PROD_PORT"
        all_passed=0
    fi

    # 2. Inspect Active Production Process Identity (Fail closed if not resolved)
    echo ""
    echo "[2/8] Inspecting Active Production Process Identity..."
    local active_pid
    active_pid=$(find_process_on_port "$PROD_PORT" "$OLD_BRIDGE_SERVER_SCRIPT_REAL")
    if [ -n "$active_pid" ]; then
        local exe
        exe=$(realpath "/proc/$active_pid/exe" 2>/dev/null || echo "")
        echo "  ✓ Verified active legacy process PID: $active_pid"
        echo "    Executable: $exe"
        echo "    Script:     $OLD_BRIDGE_SERVER_SCRIPT_REAL"
    else
        echo "  ✗ Active legacy process on port $PROD_PORT could not be strictly resolved and verified against $OLD_BRIDGE_SERVER_SCRIPT_REAL"
        all_passed=0
    fi

    # 3. Check for Active Watchdog Supervisor (Exact match)
    echo ""
    echo "[3/8] Checking for Active Watchdog Supervisor ($WATCHDOG_SCRIPT_REAL)..."
    local watchdog_pid
    watchdog_pid=$(find_watchdog_process "$WATCHDOG_SCRIPT_REAL")
    local watchdog_cli_flag=0
    for arg in "$@"; do
        if [ "$arg" = "--handle-watchdog" ]; then
            watchdog_cli_flag=1
        fi
    done
    local watchdog_env_flag="${CONFIRM_WATCHDOG_OVERRIDE:-0}"

    if [ -n "$watchdog_pid" ]; then
        if [ "$watchdog_cli_flag" -eq 1 ] && [ "$watchdog_env_flag" = "1" ]; then
            echo "  ✓ Watchdog detected (PID: $watchdog_pid) — Watchdog override/handling authorized."
        else
            echo "  ✗ Active Watchdog detected (PID: $watchdog_pid)!"
            echo "    Watchdog script is actively supervising port $PROD_PORT and will race with cutover."
            echo "    Cutover requires: --handle-watchdog AND CONFIRM_WATCHDOG_OVERRIDE=1"
            all_passed=0
        fi
    else
        echo "  ✓ No active watchdog process detected matching $WATCHDOG_SCRIPT_REAL."
    fi

    # 4. Check Legacy Bridge Repository Status
    echo ""
    echo "[4/8] Checking Legacy Bridge Repository ($OLD_BRIDGE_DIR)..."
    if [ -d "$OLD_BRIDGE_DIR" ]; then
        if [ -d "$OLD_BRIDGE_DIR/.git" ]; then
            local git_status
            git_status=$(git -C "$OLD_BRIDGE_DIR" status --porcelain 2>/dev/null || echo "")
            if [ -z "$git_status" ]; then
                echo "  ✓ Legacy repository is CLEAN (unmodified working tree)"
            else
                echo "  ✗ Legacy repository is DIRTY! Unstaged/uncommitted changes detected in $OLD_BRIDGE_DIR:"
                echo "$git_status" | sed 's/^/      /'
                all_passed=0
            fi
        else
            echo "  ✓ Legacy bridge directory exists ($OLD_BRIDGE_DIR)"
        fi
    else
        echo "  ✗ Legacy bridge directory not found: $OLD_BRIDGE_DIR"
        all_passed=0
    fi

    # 5. Check New Gateway Repository & Syntax (in-memory compilation, zero __pycache__ writing)
    echo ""
    echo "[5/8] Checking New Gateway Repository ($SCRIPT_DIR)..."
    if [ -f "$GATEWAY_SERVER_SCRIPT" ]; then
        if PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -c "import sys; compile(open(sys.argv[1], 'rb').read(), sys.argv[1], 'exec')" "$GATEWAY_SERVER_SCRIPT" 2>/dev/null; then
            echo "  ✓ New gateway server script syntax verified: $GATEWAY_SERVER_SCRIPT"
        else
            echo "  ✗ Python syntax error in new gateway script: $GATEWAY_SERVER_SCRIPT"
            all_passed=0
        fi
    else
        echo "  ✗ Gateway server script not found: $GATEWAY_SERVER_SCRIPT"
        all_passed=0
    fi

    # 6. Check Candidate Port (8766) Status
    echo ""
    echo "[6/8] Checking Candidate Deployment Port ($CANDIDATE_PORT)..."
    local cand_health
    cand_health=$(curl -fsS --max-time 1 "http://127.0.0.1:$CANDIDATE_PORT/health" 2>/dev/null || echo "")
    if [ -n "$cand_health" ]; then
        echo "  • Candidate service detected on port $CANDIDATE_PORT (Health: $cand_health)"
    else
        echo "  • Candidate service not currently active on port $CANDIDATE_PORT (Standalone cutover ready)"
    fi

    # 7. Check Migration Run Directory, Token Alignment, and Configured Artifacts (Read-Only Inspection)
    echo ""
    echo "[7/8] Inspecting Migration Runtime Paths and Token Alignment..."

    # Evaluate effective token path in read-only mode
    local effective_token_file="$PROD_TOKEN_FILE"
    if [ -z "$effective_token_file" ]; then
        if [ -f "$LEGACY_DEFAULT_TOKEN_FILE" ] && [ ! -L "$LEGACY_DEFAULT_TOKEN_FILE" ]; then
            local leg_mode
            leg_mode=$("$PYTHON_BIN" -c "import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$LEGACY_DEFAULT_TOKEN_FILE" 2>/dev/null || echo "")
            if [ "$leg_mode" = "0o600" ]; then
                effective_token_file="$LEGACY_DEFAULT_TOKEN_FILE"
                echo "  ✓ Token Alignment: Active legacy token ($LEGACY_DEFAULT_TOKEN_FILE, mode 0600) will be reused for seamless auth."
            fi
        fi
        if [ -z "$effective_token_file" ]; then
            effective_token_file="$MIGRATION_RUN_DIR/production.token"
            echo "  • Token Alignment: Migration token will be initialized at $effective_token_file (Export ACP_TOKEN_FILE for custom clients)."
        fi
    else
        echo "  • Token Alignment: Using explicitly configured ACP_TOKEN_FILE: $effective_token_file"
    fi

    if [ -L "$MIGRATION_RUN_DIR" ]; then
        echo "  ✗ Security Error: Run directory path is a symbolic link."
        all_passed=0
    elif [ -d "$MIGRATION_RUN_DIR" ]; then
        local dir_mode
        dir_mode=$("$PYTHON_BIN" -c "import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$MIGRATION_RUN_DIR" 2>/dev/null || echo "")
        if [ "$dir_mode" != "0o700" ]; then
            echo "  ✗ Security Warning: Run directory mode is $dir_mode (expected 0700)."
            all_passed=0
        else
            echo "  ✓ Runtime directory exists with mode 0700."
        fi

        local configured_files=(
            "$PROD_LOCK_FILE"
            "$effective_token_file"
            "$PROD_PID_FILE"
            "$PROD_LOG_FILE"
            "$MIGRATION_STATE_FILE"
        )
        for target_f in "${configured_files[@]}"; do
            if [ -L "$target_f" ]; then
                echo "  ✗ Security Error: Configured runtime file '$target_f' is a symbolic link."
                all_passed=0
            elif [ -f "$target_f" ]; then
                local f_mode
                f_mode=$("$PYTHON_BIN" -c "import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$target_f" 2>/dev/null || echo "")
                if [ "$f_mode" != "0o600" ]; then
                    echo "  ✗ Security Warning: Configured runtime file '$target_f' mode is $f_mode (expected 0600)."
                    all_passed=0
                else
                    echo "  ✓ Configured runtime file '$target_f' exists with mode $f_mode."
                fi
            fi
        done
    else
        echo "  • Runtime directory not yet created (will be safely initialized with 0700 on cutover)."
    fi

    # 8. Verify persistent startup handoff (read-only; no startup files are edited here)
    echo ""
    echo "[8/8] Checking Persistent Startup Handoff (Read-Only)..."
    if ! check_startup_handoff; then
        echo "  Cutover remains blocked until the container entrypoint/profile explicitly launch the new gateway watchdog."
        all_passed=0
    fi

    echo ""
    echo "=========================================================="
    if [ "$all_passed" -eq 1 ]; then
        echo " Preflight Result: PASSED — System is ready for cutover."
        echo " (To perform cutover: ./scripts/migrate_production.sh cutover --confirm-cutover)"
        echo "=========================================================="
        return 0
    else
        echo " Preflight Result: FAILED — Cutover blocked until issues resolved."
        echo "=========================================================="
        return 1
    fi
}

cmd_status() {
    validate_port "$PROD_PORT" || return 1

    echo "=== Production Gateway Migration Status ==="
    echo "Target Production Port: $PROD_PORT"
    echo "Candidate Port:         $CANDIDATE_PORT"
    echo "New Gateway Root:       $SCRIPT_DIR"
    echo "Legacy Bridge Root:     $OLD_BRIDGE_DIR"
    echo "Legacy Default Token:   $LEGACY_DEFAULT_TOKEN_FILE"
    echo "Configured Token File:  ${PROD_TOKEN_FILE:-[Auto-Align: $LEGACY_DEFAULT_TOKEN_FILE -> $MIGRATION_RUN_DIR/production.token]}"
    echo "Migration State File:   $MIGRATION_STATE_FILE"

    echo ""
    echo "--- Port $PROD_PORT Health ---"
    local prod_health
    prod_health=$(curl -fsS --max-time 2 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")
    if [ -n "$prod_health" ]; then
        echo "Status: ONLINE"
        echo "Payload: $prod_health"
    else
        echo "Status: OFFLINE"
    fi

    echo ""
    echo "--- Watchdog Supervisor ---"
    local watchdog_pid
    watchdog_pid=$(find_watchdog_process "$WATCHDOG_SCRIPT_REAL")
    if [ -n "$watchdog_pid" ]; then
        echo "Watchdog Status: ACTIVE (PID: $watchdog_pid, Script: $WATCHDOG_SCRIPT_REAL)"
    else
        echo "Watchdog Status: INACTIVE"
    fi

    echo ""
    echo "--- Persistent Startup Handoff ---"
    if check_startup_handoff; then
        echo "Startup Handoff: READY"
    else
        echo "Startup Handoff: BLOCKED"
    fi

    echo ""
    echo "--- Migration State Record ---"
    if [ -f "$MIGRATION_STATE_FILE" ]; then
        cat "$MIGRATION_STATE_FILE"
        echo ""
    else
        echo "No prior migration state record found (Pre-cutover baseline)."
    fi
}

cmd_cutover() {
    local confirm_cli=0
    local handle_watchdog_cli=0
    for arg in "$@"; do
        if [ "$arg" = "--confirm-cutover" ]; then
            confirm_cli=1
        elif [ "$arg" = "--handle-watchdog" ]; then
            handle_watchdog_cli=1
        fi
    done

    local confirm_env="${CONFIRM_PRODUCTION_CUTOVER:-0}"
    local confirm_watchdog_env="${CONFIRM_WATCHDOG_OVERRIDE:-0}"

    # Strict Safety Confirmation Gate
    if [ "$confirm_cli" -ne 1 ] || [ "$confirm_env" != "1" ]; then
        echo "==========================================================================" >&2
        echo " SAFETY REFUSAL: Production Cutover Aborted (Zero Changes Made)" >&2
        echo "==========================================================================" >&2
        echo " Cutover permanently switches production port $PROD_PORT to the new gateway." >&2
        echo " To authorize cutover, you must provide BOTH:" >&2
        echo "   1. CLI argument:             --confirm-cutover" >&2
        echo "   2. Environment variable:     export CONFIRM_PRODUCTION_CUTOVER=1" >&2
        echo "" >&2
        echo " Example:" >&2
        echo "   CONFIRM_PRODUCTION_CUTOVER=1 $0 cutover --confirm-cutover" >&2
        echo "==========================================================================" >&2
        return 1
    fi

    local watchdog_pid
    watchdog_pid=$(find_watchdog_process "$WATCHDOG_SCRIPT_REAL")
    if [ -n "$watchdog_pid" ]; then
        if [ "$handle_watchdog_cli" -ne 1 ] || [ "$confirm_watchdog_env" != "1" ]; then
            echo "==========================================================================" >&2
            echo " SAFETY REFUSAL: Active Watchdog Supervisor Detected (Zero Changes Made)" >&2
            echo "==========================================================================" >&2
            echo " Watchdog process $watchdog_pid ($WATCHDOG_SCRIPT_REAL) is actively supervising port $PROD_PORT." >&2
            echo " To authorize handling the watchdog during cutover, you must provide BOTH:" >&2
            echo "   1. CLI argument:             --handle-watchdog" >&2
            echo "   2. Environment variable:     export CONFIRM_WATCHDOG_OVERRIDE=1" >&2
            echo "" >&2
            echo " Example:" >&2
            echo "   CONFIRM_PRODUCTION_CUTOVER=1 CONFIRM_WATCHDOG_OVERRIDE=1 $0 cutover --confirm-cutover --handle-watchdog" >&2
            echo "==========================================================================" >&2
            return 1
        fi
    fi

    echo "Authorization confirmed. Executing preflight inspection..."
    cmd_preflight "$@" || {
        echo "Error: Preflight failed. Aborting cutover without modifying production." >&2
        return 1
    }

    ensure_run_dir
    ensure_token

    local old_pid
    old_pid=$(find_process_on_port "$PROD_PORT" "$OLD_BRIDGE_SERVER_SCRIPT_REAL")

    # Record pre-cutover state for rollback guarantee
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local old_commit="unknown"
    if [ -d "$OLD_BRIDGE_DIR/.git" ]; then
        old_commit=$(git -C "$OLD_BRIDGE_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
    fi

    local state_json
    state_json=$(cat <<EOF
{
  "state": "CUTOVER_IN_PROGRESS",
  "timestamp": "$timestamp",
  "prod_port": $PROD_PORT,
  "old_bridge_pid": "${old_pid:-unknown}",
  "old_bridge_commit": "$old_commit",
  "new_gateway_dir": "$SCRIPT_DIR",
  "old_bridge_dir": "$OLD_BRIDGE_DIR",
  "token_file": "$PROD_TOKEN_FILE",
  "stopped_watchdog_pid": "${watchdog_pid:-none}"
}
EOF
)
    write_state_file_atomic "$state_json"

    echo ""
    echo "=========================================================="
    echo " Beginning Production Cutover on Port $PROD_PORT"
    echo "=========================================================="

    # Step 1: Safely stop watchdog if active and authorized
    if [ -n "$watchdog_pid" ]; then
        if is_watchdog_process "$watchdog_pid" "$WATCHDOG_SCRIPT_REAL"; then
            echo "Stopping watchdog process (PID: $watchdog_pid)..."
            signal_process_safely "$watchdog_pid" "TERM"
            local watchdog_wait=20
            while [ "$watchdog_wait" -gt 0 ]; do
                if ! is_watchdog_process "$watchdog_pid" "$WATCHDOG_SCRIPT_REAL"; then
                    break
                fi
                sleep 0.5
                watchdog_wait=$((watchdog_wait - 1))
            done
            if is_watchdog_process "$watchdog_pid" "$WATCHDOG_SCRIPT_REAL"; then
                echo "Error: Authorized watchdog PID $watchdog_pid did not exit after SIGTERM; refusing to continue cutover." >&2
                return 1
            fi
        fi
    fi

    # Step 2: Terminate legacy production service if running (graceful wait for in-flight tasks)
    if [ -n "$old_pid" ] && is_acp_server_process "$old_pid" "$OLD_BRIDGE_SERVER_SCRIPT_REAL"; then
        echo "Stopping legacy bridge process gracefully (PID: $old_pid, buffer up to ${LEGACY_GRACEFUL_TIMEOUT_SEC}s)..."
        signal_process_safely "$old_pid" "TERM"

        local wait_iterations=$((LEGACY_GRACEFUL_TIMEOUT_SEC * 2))
        while [ "$wait_iterations" -gt 0 ]; do
            if ! kill -0 "$old_pid" 2>/dev/null || is_zombie_process "$old_pid"; then
                break
            fi
            sleep 0.5
            wait_iterations=$((wait_iterations - 1))
        done

        if kill -0 "$old_pid" 2>/dev/null && ! is_zombie_process "$old_pid"; then
            if is_acp_server_process "$old_pid" "$OLD_BRIDGE_SERVER_SCRIPT_REAL"; then
                echo "Warning: Legacy bridge process did not stop within ${LEGACY_GRACEFUL_TIMEOUT_SEC}s. Sending SIGKILL..."
                signal_process_safely "$old_pid" "KILL"
                sleep 0.5
            fi
        fi
    fi

    # Step 3: Ensure port is completely free. A silent/unknown listener is unsafe too.
    local port_wait=20
    while [ "$port_wait" -gt 0 ]; do
        local probe_active
        probe_active=$(curl -fsS --max-time 1 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")
        if [ -z "$probe_active" ] && ! port_has_listener "$PROD_PORT"; then
            break
        fi
        sleep 0.5
        port_wait=$((port_wait - 1))
    done
    if port_has_listener "$PROD_PORT"; then
        echo "Security Error: Port $PROD_PORT is still occupied after verified shutdown; refusing to launch the new gateway." >&2
        return 1
    fi

    # Step 4: Launch new Agent Executor Gateway on production port
    echo "Launching new Agent Executor Gateway on port $PROD_PORT..."
    (
        export ACP_PORT="$PROD_PORT"
        export ACP_TOKEN_FILE="$PROD_TOKEN_FILE"
        "$PYTHON_BIN" -c "
import os, sys, subprocess

try:
    os.close(200)
except OSError:
    pass

try:
    os.setsid()
except PermissionError:
    pass

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
    close_fds=True,
)

with open(pid_path, 'w') as pf:
    pf.write(f'{proc.pid}\n')
os.chmod(pid_path, 0o600)

proc.wait()
sys.exit(0)
" "$GATEWAY_SERVER_SCRIPT" "$SCRIPT_DIR" "$PROD_LOG_FILE" "$PROD_PID_FILE"
    ) 200>&- < /dev/null >> "$PROD_LOG_FILE" 2>&1 &

    # Wait for PID file
    local new_pid=""
    local pid_wait=20
    while [ "$pid_wait" -gt 0 ]; do
        if [ -s "$PROD_PID_FILE" ]; then
            new_pid=$(cat "$PROD_PID_FILE" 2>/dev/null || echo "")
            if [ -n "$new_pid" ]; then
                break
            fi
        fi
        sleep 0.1
        pid_wait=$((pid_wait - 1))
    done

    # Step 5: Health Check & Strict Identity Confirmation
    local health_ready=0
    local health_wait=20
    while [ "$health_wait" -gt 0 ]; do
        local resp
        resp=$(curl -fsS --max-time 1 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")
        if [[ "$resp" =~ "Antigravity REST Bridge Server" ]] && [[ "$resp" =~ "online" ]]; then
            if [ -n "$new_pid" ] && is_acp_server_process "$new_pid" "$GATEWAY_SERVER_SCRIPT_REAL"; then
                local resolved_pid
                resolved_pid=$(find_process_on_port "$PROD_PORT" "$GATEWAY_SERVER_SCRIPT_REAL")
                if [ "$resolved_pid" = "$new_pid" ]; then
                    health_ready=1
                    break
                fi
            fi
        fi
        sleep 0.5
        health_wait=$((health_wait - 1))
    done

    if [ "$health_ready" -eq 1 ]; then
        local success_json
        success_json=$(cat <<EOF
{
  "state": "CUTOVER_COMPLETE",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "prod_port": $PROD_PORT,
  "gateway_pid": "$new_pid",
  "new_gateway_dir": "$SCRIPT_DIR",
  "old_bridge_dir": "$OLD_BRIDGE_DIR",
  "token_file": "$PROD_TOKEN_FILE",
  "log_file": "$PROD_LOG_FILE",
  "stopped_watchdog_pid": "${watchdog_pid:-none}"
}
EOF
)
        write_state_file_atomic "$success_json"

        echo "=========================================================="
        echo " Cutover SUCCESS: Production is now served by Agent Executor Gateway"
        echo "   Port:       $PROD_PORT"
        echo "   PID:        $new_pid"
        echo "   Token File: $PROD_TOKEN_FILE"
        echo "   Log File:   $PROD_LOG_FILE"
        echo "   Rollback:   CONFIRM_PRODUCTION_ROLLBACK=1 $0 rollback --confirm-rollback"
        echo "=========================================================="
        return 0
    else
        echo "Error: New gateway failed health check or identity verification on port $PROD_PORT. Triggering emergency rollback..." >&2
        if [ -n "$new_pid" ] && is_acp_server_process "$new_pid" "$GATEWAY_SERVER_SCRIPT_REAL"; then
            signal_process_safely "$new_pid" "TERM"
            sleep 1
        fi
        rm -f "$PROD_PID_FILE"

        # Restore old bridge logging to controlled directory with explicit PROD_PORT and aligned token
        if [ -f "$OLD_BRIDGE_SERVER_SCRIPT_REAL" ]; then
            local emerg_log="$MIGRATION_RUN_DIR/emergency_rollback.log"
            touch "$emerg_log" && chmod 0600 "$emerg_log"
            (
                export ACP_PORT="$PROD_PORT"
                export ACP_TOKEN_FILE="$PROD_TOKEN_FILE"
                nohup "$PYTHON_BIN" "$OLD_BRIDGE_SERVER_SCRIPT_REAL" < /dev/null >> "$emerg_log" 2>&1 &
            ) 200>&- 201>&-
            local emerg_wait=20
            while [ "$emerg_wait" -gt 0 ]; do
                local em_resp
                em_resp=$(curl -fsS --max-time 1 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")
                if [[ "$em_resp" =~ "Antigravity REST Bridge Server" ]]; then
                    break
                fi
                sleep 0.5
                emerg_wait=$((emerg_wait - 1))
            done
        fi
        return 1
    fi
}

cmd_rollback() {
    local confirm_cli=0
    for arg in "$@"; do
        if [ "$arg" = "--confirm-rollback" ]; then
            confirm_cli=1
        fi
    done

    local confirm_env="${CONFIRM_PRODUCTION_ROLLBACK:-0}"

    # Strict Safety Confirmation Gate
    if [ "$confirm_cli" -ne 1 ] || [ "$confirm_env" != "1" ]; then
        echo "==========================================================================" >&2
        echo " SAFETY REFUSAL: Production Rollback Aborted (Zero Changes Made)" >&2
        echo "==========================================================================" >&2
        echo " Rollback shuts down the new gateway and restores the legacy bridge." >&2
        echo " To authorize rollback, you must provide BOTH:" >&2
        echo "   1. CLI argument:             --confirm-rollback" >&2
        echo "   2. Environment variable:     export CONFIRM_PRODUCTION_ROLLBACK=1" >&2
        echo "" >&2
        echo " Example:" >&2
        echo "   CONFIRM_PRODUCTION_ROLLBACK=1 $0 rollback --confirm-rollback" >&2
        echo "==========================================================================" >&2
        return 1
    fi

    ensure_run_dir
    ensure_token

    echo "=========================================================="
    echo " Authorizing Production Rollback to Legacy Bridge"
    echo "=========================================================="

    # Step 1: Verify current running process on port PROD_PORT
    local current_pid=""
    if [ -f "$PROD_PID_FILE" ]; then
        current_pid=$(cat "$PROD_PID_FILE" 2>/dev/null || echo "")
    fi
    if [ -z "$current_pid" ]; then
        current_pid=$(find_process_on_port "$PROD_PORT" "$GATEWAY_SERVER_SCRIPT_REAL")
    fi

    # Check if port is occupied by an unknown process or health is responding without verified gateway PID
    local port_occupied_pid
    port_occupied_pid=$(find_process_on_port "$PROD_PORT" "$GATEWAY_SERVER_SCRIPT_REAL")
    local probe_occupied
    probe_occupied=$(curl -fsS --max-time 1 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")

    if [ -z "$current_pid" ] || [ "$current_pid" != "$port_occupied_pid" ] || ! is_acp_server_process "$current_pid" "$GATEWAY_SERVER_SCRIPT_REAL"; then
        if [ -n "$probe_occupied" ] || [ -n "$port_occupied_pid" ]; then
            echo "Security Error: Port $PROD_PORT is occupied by an unverified/unknown service. Refusing to terminate unverified process or restore into occupied port." >&2
            return 1
        elif [ -n "$current_pid" ] && [ ! -d "/proc/$current_pid" ]; then
            # PID file was stale and port is free, proceed with restoration
            rm -f "$PROD_PID_FILE"
        else
            echo "Security Error: Could not verify that active process on port $PROD_PORT is the gateway ($GATEWAY_SERVER_SCRIPT_REAL). Rollback aborted." >&2
            return 1
        fi
    else
        echo "Stopping verified gateway process (PID: $current_pid)..."
        signal_process_safely "$current_pid" "TERM"

        local wait_attempts=20
        while [ "$wait_attempts" -gt 0 ]; do
            if ! kill -0 "$current_pid" 2>/dev/null || is_zombie_process "$current_pid"; then
                break
            fi
            sleep 0.5
            wait_attempts=$((wait_attempts - 1))
        done

        if kill -0 "$current_pid" 2>/dev/null && ! is_zombie_process "$current_pid"; then
            if is_acp_server_process "$current_pid" "$GATEWAY_SERVER_SCRIPT_REAL"; then
                signal_process_safely "$current_pid" "KILL"
            fi
        fi
        rm -f "$PROD_PID_FILE"
    fi

    # Step 2: Restart legacy bridge
    local restored_log="$MIGRATION_RUN_DIR/restored_legacy.log"
    touch "$restored_log" && chmod 0600 "$restored_log"

    if [ -f "$OLD_BRIDGE_SERVER_SCRIPT_REAL" ]; then
        echo "Restoring legacy bridge from $OLD_BRIDGE_SERVER_SCRIPT_REAL..."
        (
            export ACP_PORT="$PROD_PORT"
            export ACP_TOKEN_FILE="$PROD_TOKEN_FILE"
            nohup "$PYTHON_BIN" "$OLD_BRIDGE_SERVER_SCRIPT_REAL" < /dev/null >> "$restored_log" 2>&1 &
        ) 200>&- 201>&-
    else
        echo "Warning: Old bridge server script not found at $OLD_BRIDGE_SERVER_SCRIPT_REAL." >&2
    fi

    # Step 3: Verify restored bridge health
    local restored_ready=0
    local check_wait=20
    while [ "$check_wait" -gt 0 ]; do
        local resp
        resp=$(curl -fsS --max-time 1 "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null || echo "")
        if [[ "$resp" =~ "Antigravity REST Bridge Server" ]] && [[ "$resp" =~ "online" ]]; then
            restored_ready=1
            break
        fi
        sleep 0.5
        check_wait=$((check_wait - 1))
    done

    local rollback_json
    rollback_json=$(cat <<EOF
{
  "state": "ROLLED_BACK",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "prod_port": $PROD_PORT,
  "old_bridge_dir": "$OLD_BRIDGE_DIR",
  "token_file": "$PROD_TOKEN_FILE"
}
EOF
)
    write_state_file_atomic "$rollback_json"

    if [ "$restored_ready" -eq 1 ]; then
        echo "=========================================================="
        echo " Rollback SUCCESS: Legacy bridge restored and online on port $PROD_PORT."
        echo "=========================================================="
        return 0
    else
        echo "Warning: Legacy bridge process launched but health probe did not confirm readiness within 10s." >&2
        return 1
    fi
}

# 3. Main Entrypoint Protected by Singleton Lock for Mutating Actions

main() {
    local action="${1:-preflight}"
    shift || true

    case "$action" in
        -h|--help|help)
            echo "Usage: $0 {preflight|status|cutover|rollback} [options]"
            echo ""
            echo "Commands:"
            echo "  preflight (default)  Inspect system readiness (strictly read-only, zero mutations)"
            echo "  status               Show current migration status and health"
            echo "  cutover              Switch production port $PROD_PORT to Agent Executor Gateway"
            echo "                       Requires: --confirm-cutover AND CONFIRM_PRODUCTION_CUTOVER=1"
            echo "                       If watchdog active: --handle-watchdog AND CONFIRM_WATCHDOG_OVERRIDE=1"
            echo "  rollback             Restore legacy bridge on production port $PROD_PORT"
            echo "                       Requires: --confirm-rollback AND CONFIRM_PRODUCTION_ROLLBACK=1"
            exit 0
            ;;
        preflight|status)
            # Strictly read-only operations: Do NOT create directories or lock files
            if [ "$action" = "preflight" ]; then
                cmd_preflight "$@"
            else
                cmd_status "$@"
            fi
            return $?
            ;;
        cutover|rollback)
            # Mutating operations: Acquire flock singleton lock
            ensure_run_dir
            check_not_symlink "$PROD_LOCK_FILE" "Lock file" || exit 1

            exec 200>"$PROD_LOCK_FILE"
            chmod 0600 "$PROD_LOCK_FILE"
            if ! flock -n 200; then
                echo "Error: Another production migration operation is currently in progress (lock held on $PROD_LOCK_FILE)." >&2
                exit 1
            fi

            if [ "$action" = "cutover" ]; then
                cmd_cutover "$@"
            else
                cmd_rollback "$@"
            fi
            return $?
            ;;
        *)
            echo "Error: Unknown action '$action'" >&2
            echo "Usage: $0 {preflight|status|cutover|rollback}" >&2
            exit 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
