# Antigravity REST Bridge

**English** | [中文文档](./README_CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.4.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Production--Ready-brightgreen.svg)]()

Production-grade, highly reliable, and secure **REST API & IPC Control Bridge** for **Google Antigravity (`agy`) / OpenAI Codex** explicit 1:1 session isolation.

---

## 🌟 Key Features

- 🎯 **Explicit 1:1 Conversation Mapping**: Fully decoupled from global `agy -c` loops and Language Server preemption. Each Codex session explicitly corresponds to an independent Antigravity `conversation_id`.
- 🔒 **Per-Conversation Concurrency Lock**: Rejects concurrent turns within the same `conversation_id` with `HTTP 409 Conflict` to prevent transcript corruption.
- 🛑 **Global Admission Control (HTTP 429)**: Configurable `AGY_MAX_CONCURRENCY` (default `1` for proxy link protection) with non-blocking `HTTP 429 Too Many Requests` rejection when saturated.
- ⏱️ **Configurable Timeout Budget**: `ACP_AGENT_TIMEOUT_SEC` (default `300s`) provides the base task budget, while `ACP_AUTH_GRACE_SEC` (default `30s`) is added as an automatic-login/preflight allowance. The default combined process deadline is `330s` and client timeout is `360s`; process trees are still cleaned up on timeout.
- 🔁 **Intelligent Pre-execution Retry**: Automatically retries transient early errors (EOF/network) up to 3 times strictly on 0-turn errors before conversation initialization, while preserving in-flight errors for Codex decision.
- 🟡 **Partial-success Preservation**: If agy reports `ERROR` after emitting a non-empty response, the bridge returns HTTP 200 with `status: partial_success`, the response, original error, and CLI exit code. Empty-response failures remain HTTP 500.
- 🔐 **Strict Bearer Token Auth**: All POST API operations require `Authorization: Bearer <TOKEN>` authentication (`0600` restricted permissions token file).
- ⚡ **Reserved Capacity Health Checks**: Dedicated `/health` probe slots (0.001s latency) completely isolated from agent execution.
- 🛡️ **Slowloris & Connection Protections**: Socket read timeouts (10s), request body limits (2MB), and max HTTP connection limits (50).

---

## 🏗️ Architecture Overview

```
[ Codex Session A ]        [ Codex Session B ]        [ Other Clients ]
         │                          │                       │
         ▼                          ▼                       ▼
  (conversation_id: A)      (conversation_id: B)     (No conversation_id: New)
         │                          │                       │
         └──────────────────────────┼───────────────────────┘
                                    │
                                    ▼ (Strict Bearer Token Auth)
                    ┌─────────────────────────────────┐
                    │  Antigravity REST Bridge Server │  ── (127.0.0.1:8765)
                    └─────────────────────────────────┘
                           │                   │
                    (Fast GET /health)    (Heavy POST /invoke & /send-message)
                           │                   │
                           ▼                   ▼
                  ┌──────────────────┐ ┌──────────────────────────────┐
                  │ Fast Direct Slot │ │ Bounded Agent Pool (Max N)   │
                  └──────────────────┘ │ Per-Conversation Lock (409)  │
                                       └──────────────────────────────┘
                                                       │
                                                       ▼
                                        ┌─────────────────────────────┐
                                        │ `agy --conversation <id>`   │
                                        │ (Zero agy -c preemption)    │
                                        └─────────────────────────────┘
                                                       ▲
                                                       │
                                   [ Watchdog Supervisor (PPID=1, TTY=?) ]
                                     (Singleton File Lock & /health check)
```

---

## 💡 Conversation Lifecycle & Client Responsibilities

1. **Client Stores `conversation_id`**: The Bridge is a stateless gateway. On the first turn, call `POST /acp/v1/invoke` without a `conversation_id`. The response returns a generated `conversation_id`. The Codex client must record this ID.
2. **Explicit Continuation**: On subsequent turns, provide `conversation_id` in `POST /acp/v1/invoke` or `POST /acp/v1/send-message`.
3. **Restart Resilience**: Because Antigravity stores conversation transcripts on disk, sessions persist across Bridge and container restarts. Supplying the same `conversation_id` seamlessly resumes context.
4. **No Same-Session Concurrency**: The same Codex session must not issue simultaneous turns. The Bridge immediately returns `HTTP 409 Conflict` if another turn is active for that conversation.

---

## 🚀 Quick Start & Installation

### One-line Automated Deploy
```bash
./install.sh
```

### CLI Client (`acp-cli`)
```bash
# Check service status
acp-cli status

# Start a new conversation (returns conversation_id)
acp-cli invoke "Review code in /workspace/processor.py"

# Continue an existing conversation (unambiguous --conversation flag recommended; positional also supported)
acp-cli invoke --conversation <conversation_id> "Continue processing"
acp-cli invoke <conversation_id> "Continue processing"
acp-cli send <conversation_id> "Continue processing"
```

---

## 📡 REST API Reference

### 1. Health & Status Probe (`GET /health` or `GET /acp/v1/status`)
No authentication required.
```bash
curl -s http://127.0.0.1:8765/health
```

### 2. Invoke Agent Task (`POST /acp/v1/invoke`)
Requires Bearer Token.

- **Start New Conversation**:
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Perform code refactoring"}'
```
Response:
```json
{
  "status": "success",
  "action": "new-conversation",
  "conversation_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b",
  "mode": "explicit_conversation_cli",
  "output": "..."
}
```

If agy emits a usable response but its print mode reports a late terminal error, the bridge preserves both facts:
```json
{
  "status": "partial_success",
  "conversation_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b",
  "warning": "agy reported ERROR after producing a non-empty response; review the response before relying on it",
  "upstream_status": "ERROR",
  "upstream_error": "Agent execution terminated due to error.",
  "cli_exit_code": 1,
  "parsed": {"response": "..."}
}
```

- **Continue Existing Conversation**:
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b", "prompt": "Continue with unit tests"}'
```

### 3. Send Conversation Message (`POST /acp/v1/send-message`)
Requires Bearer Token.
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/send-message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recipient_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b", "content": "Proceed to test"}'
```

---

## 🛡️ Safety & Limit Specifications

| Metric / Guard | Value / Strategy | Description |
| :--- | :--- | :--- |
| **Session Model** | Explicit `conversation_id` | Client-held stateless routing; zero `agy -c` preemption |
| **Per-Conversation Lock** | `HTTP 409 Conflict` | Protects in-flight turns from concurrent collision |
| **Max Concurrency** | `AGY_MAX_CONCURRENCY` (Default `1`)| Enforced by bounded semaphore (`HTTP 429` on overflow) |
| **Task Timeout Budget**| `ACP_AGENT_TIMEOUT_SEC` (Default `300s`) | Base task budget; killed via `os.killpg(pgid, SIGKILL)` when the combined deadline expires |
| **Automatic Login Grace** | `ACP_AUTH_GRACE_SEC` (Default `30s`) | Added once to the process deadline for silent login and preflight |
| **Client Timeout** | `ACP_CLIENT_TIMEOUT_SEC` (Default `360s`)| Defaults to task budget + auth grace + 30s transport margin |
| **Max HTTP Connections**| `50` | `45 POST` + `5 Reserved /health` |
| **Socket Idle Timeout** | `10.0 seconds` | Prevents Slowloris socket starvation |
| **Max Request Body** | `2 MB` | Rejects payloads exceeding 2MB (`HTTP 413`) |

---

## 📄 License

MIT License © 2026 hikki-fan
