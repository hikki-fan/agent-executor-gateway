#!/usr/bin/env bash
# Automated Installer for Antigravity REST Bridge

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[*] Installing Antigravity REST Bridge..."

# 1. Ensure /workspace/scripts directory
mkdir -p /workspace/scripts
cp "$SCRIPT_DIR/acp_server.py" /workspace/scripts/acp_server.py
cp "$SCRIPT_DIR/ensure_acp_bridge.sh" /workspace/scripts/ensure_acp_bridge.sh
cp "$SCRIPT_DIR/acp_watchdog.sh" /workspace/scripts/acp_watchdog.sh
cp "$SCRIPT_DIR/acp-cli" /workspace/scripts/acp-cli

chmod +x /workspace/scripts/acp_server.py
chmod +x /workspace/scripts/ensure_acp_bridge.sh
chmod +x /workspace/scripts/acp_watchdog.sh
chmod +x /workspace/scripts/acp-cli

# 2. Link the persistent CLI into the container PATH. The symlink can be
# recreated after every container rebuild without reinstalling the bridge.
ln -sf /workspace/scripts/acp-cli /usr/local/bin/acp-cli

# 3. Create global symlinks for agy and agentapi if available
if [ -f "/home/codex/.local/bin/agy" ]; then
    ln -sf /home/codex/.local/bin/agy /usr/local/bin/agy
fi

if [ -f "/home/codex/.gemini/antigravity-cli/bin/agentapi" ]; then
    ln -sf /home/codex/.gemini/antigravity-cli/bin/agentapi /usr/local/bin/agentapi
fi

# 4. Install /etc/profile.d hook
if [ -d "/etc/profile.d" ]; then
    cat << 'EOF' > /etc/profile.d/acp_bridge.sh
/workspace/scripts/ensure_acp_bridge.sh >/dev/null 2>&1
EOF
    chown root:root /etc/profile.d/acp_bridge.sh 2>/dev/null || true
    chmod 644 /etc/profile.d/acp_bridge.sh 2>/dev/null || true
fi

# 5. Launch service & watchdog daemon
/workspace/scripts/ensure_acp_bridge.sh
setsid /workspace/scripts/acp_watchdog.sh </dev/null >/dev/null 2>&1 &

echo "[*] Installation completed successfully!"
acp-cli status
