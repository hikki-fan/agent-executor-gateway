# Antigravity REST Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.1.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Production--Ready-brightgreen.svg)]()

Production-grade, highly reliable, and secure **REST API & IPC Control Bridge** for **Google Antigravity (`agy`) / OpenAI Codex** Agent inter-process communication.

---

## 🌟 Key Features

- 🔐 **Strict Bearer Token Auth**: All POST API operations require `Authorization: Bearer <TOKEN>` authentication (`0600` restricted permissions token file).
- ⚡ **Reserved Capacity Health Checks**: Dedicated `/health` probe slots (0.001s latency) completely isolated from heavy agent execution queues.
- 🛑 **Bounded Admission Control (HTTP 429)**: Rejects overflow tasks with `HTTP 429 Too Many Requests` when max agent task concurrency (10) is reached.
- 🛡️ **Slowloris & Connection Protections**: Socket read timeouts (10s), request body limits (2MB), and max HTTP connection limits (50).
- 🧹 **Process Group Tree Cleanup**: Isolated process group execution (`start_new_session=True` + `os.killpg`) guaranteeing 100% cleanup of subprocess trees on timeout.
- 🔄 **Deadlock-Free SIGTERM & Watchdog**: Async signal handling (`BaseServer.shutdown()` deadlock prevention) + `cancel_futures=True` with a 65s graceful exit window and TTY-detached self-healing Watchdog.

---

## 🏗️ Architecture Overview

```
[ External Client / Codex CLI / Agent ]
                   │
                   ▼
      [ acp-cli / HTTP REST API ]
                   │
        (Strict Bearer Token Auth)
                   │
                   ▼
  ┌─────────────────────────────────┐
  │  Antigravity REST Bridge Server │  ── (127.0.0.1:8765)
  └─────────────────────────────────┘
         │                   │
  (Fast GET /health)    (Heavy Agent Tasks POST)
         │                   │
         ▼                   ▼
┌──────────────────┐ ┌──────────────────────────────┐
│ Fast Direct Thread│ │ Bounded Agent ThreadPool (10)│
└──────────────────┘ └──────────────────────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │ `agy agentapi` Subprocess   │
              │  (Isolated Process Group)   │
              └─────────────────────────────┘
                             ▲
                             │
         [ Watchdog Supervisor (PPID=1, TTY=?) ]
           (Singleton File Lock & Heartbeat)
```

---

## 🚀 Quick Start & Installation

### One-line Automated Deploy
```bash
./install.sh
```

### Manual Service Control
```bash
# Check service & watchdog status
acp-cli status

# Invoke an agent task
acp-cli invoke "Review code in /workspace/processor.py"

# Send a message to an active conversation
acp-cli send <conversation_id> "Continue processing"
```

---

## 📡 REST API Reference

### 1. Health & Status Probe (`GET /health` or `GET /acp/v1/status`)
No authentication required. Dedicated fast thread response.
```bash
curl -s http://127.0.0.1:8765/health
```

### 2. Invoke Agent Task (`POST /acp/v1/invoke`)
Requires Bearer Token.
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Perform code refactoring"}'
```

### 3. Send Conversation Message (`POST /acp/v1/send-message`)
Requires Bearer Token.
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/send-message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recipient_id": "YOUR_CONVERSATION_ID", "content": "Proceed to test"}'
```

---

## 🛡️ Safety & Limit Specifications

| Metric / Guard | Value / Strategy | Description |
| :--- | :--- | :--- |
| **Max Concurrent Agent Tasks** | `10` | Enforced by `BoundedSemaphore(10)` |
| **Overflow Strategy** | `HTTP 429` | Rejects task overflow immediately |
| **Max Total HTTP Connections** | `50` | `45 POST` + `5 Reserved /health` |
| **Socket Idle Timeout** | `10.0 seconds` | Prevents Slowloris socket starvation |
| **Subprocess Timeout** | `60.0 seconds` | Killed via `os.killpg(pgid, SIGKILL)` |
| **Max Request Body** | `2 MB` | Rejects payloads exceeding 2MB (HTTP 413) |
| **SIGTERM Graceful Window** | `65.0 seconds` | Allows active 60s tasks to finish |

---

## 📄 License

MIT License © 2026 hikki-fan
