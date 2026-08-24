# Agent Executor Gateway

**English** | [中文文档](./README_CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.4.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Phase%202%20Candidate-yellow.svg)]()

High-reliability, executor-neutral **Agent Executor Gateway** providing unified REST API orchestration, session management, and process lifecycle controls for AI coding agents.

> [!NOTE]
> **Phase 2 Status**: Generic Executor API is introduced with `agy` registered. Legacy ACP API endpoints remain backward compatible under the tested contracts. Additional executors (such as Grok) and full production migration will occur in subsequent phases.

---

## 🌟 Key Features

- 🌐 **Unified Generic Executor API (Phase 2)**: Standardized executor discovery (`GET /v1/executors`), health checks (`GET /v1/executors/{executor}/health`), and invocation (`POST /v1/executors/{executor}/invoke`).
- 📊 **Section 10 Standardized Result Contract**: Uniform `ExecutorResult` schema across all executors (`status`, `executor`, `session_id`, `response`, `exit_code`, `timing`, `usage`, `warnings`, `error`, `raw`).
- 🎯 **Explicit 1:1 Session Isolation**: Stateless gateway routing where clients hold `session_id` (or legacy `conversation_id`). Zero global `agy -c` preemption.
- 🔒 **Per-Session Concurrency Lock**: Rejects concurrent turns within the same `(executor, session_id)` with `HTTP 409 Conflict` across both Generic and Legacy endpoints.
- 🛑 **Global Admission Control (HTTP 429)**: Shared executor concurrency semaphore with non-blocking `HTTP 429 Too Many Requests` rejection when saturated.
- ⏱️ **Flexible Timeout Budgets**: Per-request `timeout_sec` overrides determine both execution deadline and future waiting windows (+5s transport margin), with automatic process group termination (`SIGKILL via os.killpg`).
- 🔁 **Intelligent Pre-execution Retry**: Retries transient 0-turn startup errors (EOF/network) up to 3 times on new sessions while preserving in-flight errors.
- 🟡 **Partial-success Preservation**: Contradictory `ERROR` status with usable output is returned as HTTP 200 `partial_success` with warnings and diagnostic details.
- 🔐 **Strict Bearer Token Auth**: All POST operations require `Authorization: Bearer <TOKEN>` authentication (`0600` restricted permissions).
- ⚡ **General Probe Capacity**: The independent 45-POST cap leaves five general HTTP connection slots available for probes and other requests; these slots are not exclusive to `/health`.
- 🛡️ **Slowloris & Connection Protections**: Socket read timeouts (10s), request body limits (2MB), and max HTTP connection limits (50).
- 🔄 **Full Legacy ACP Compatibility**: Retains all `/acp/v1/*` endpoints without breaking existing Codex workflows.

---

## 🏗️ Architecture Overview

```
[ Codex / API Clients ]
         │
         ├─── (Generic API: /v1/executors/*) ───────┐
         │                                          │
         └─── (Legacy API: /acp/v1/*, /health) ─────┤
                                                    ▼
                                    ┌────────────────────────────────┐
                                    │     acp_server.py (HTTP)       │
                                    │  - Strict Bearer Auth          │
                                    │  - Admission Controller (429)  │
                                    │  - Session Lock Manager (409)  │
                                    │  - ExecutorRegistry            │
                                    └───────────────┬────────────────┘
                                                    │
                                                    ▼
                                    ┌────────────────────────────────┐
                                    │      ExecutorAdapter (ABC)     │
                                    └───────────────┬────────────────┘
                                                    │
                                                    ▼
                                    ┌────────────────────────────────┐
                                    │     AntigravityAdapter (agy)   │
                                    │  - 1:1 session_id mapping      │
                                    │  - Flags ordering before -p    │
                                    │  - Pre-execution retry         │
                                    │  - Uniform ExecutorResult      │
                                    └───────────────┬────────────────┘
                                                    │
                                                    ▼
                                    ┌────────────────────────────────┐
                                    │      core/process.py           │
                                    │  - Process group setsid        │
                                    │  - os.killpg cleanup           │
                                    └────────────────────────────────┘
```

---

## 📡 REST API Reference

### 1. Generic Executor API (Phase 2)

#### Executor Discovery (`GET /v1/executors`)
No authentication required.
```bash
curl -s http://127.0.0.1:8765/v1/executors
```
Response:
```json
{
  "executors": [
    {
      "name": "agy",
      "available": true,
      "supports_session": true
    }
  ]
}
```

#### Executor Health Probe (`GET /v1/executors/{executor}/health`)
No authentication required.
```bash
curl -s http://127.0.0.1:8765/v1/executors/agy/health
```
Response:
```json
{
  "status": "online",
  "service": "Antigravity REST Bridge Server",
  "version": "2.4.0",
  "mode": "explicit_conversation_cli",
  "binary": "/home/codex/.local/bin/agy",
  "available": true
}
```

#### Invoke Executor Task (`POST /v1/executors/{executor}/invoke`)
Requires Bearer Token authentication.

- **Start New Task / Session**:
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/agy/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Implement task cancellation feature",
    "cwd": "/workspace/project",
    "session_id": null,
    "model": "flash",
    "effort": "medium",
    "timeout_sec": 600
  }'
```

- **Continue Existing Session**:
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/agy/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Fix test failures from previous run",
    "cwd": "/workspace/project",
    "session_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b"
  }'
```

#### Unified ExecutorResult Response Format
All generic invocations return the Section 10 standard schema:
```json
{
  "status": "success",
  "executor": "agy",
  "session_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b",
  "response": "Refactoring completed successfully.",
  "exit_code": 0,
  "timing": {
    "duration_ms": 4520
  },
  "usage": {
    "input_tokens": 240,
    "output_tokens": 120,
    "total_tokens": 360,
    "cost_usd": null
  },
  "warnings": [],
  "error": null,
  "raw": {
    "parsed": {"status": "SUCCESS"},
    "stdout": "...",
    "stderr": ""
  }
}
```

---

### 2. Legacy ACP API Compatibility

The Gateway maintains full backward compatibility for legacy clients:

| Endpoint | Method | Auth | Description |
| :--- | :--- | :--- | :--- |
| `/health` | GET | None | Legacy health probe with system limits |
| `/acp/v1/status` | GET | None | Alias for `/health` |
| `/acp/v1/invoke` | POST | Bearer | Start or continue legacy conversation |
| `/acp/v1/new-conversation`| POST | Bearer | Start new legacy conversation |
| `/acp/v1/send-message` | POST | Bearer | Send message with `recipient_id` |
| `/acp/v1/metadata` | POST | Bearer | Returns HTTP 501 Not Implemented |

---

## 🛡️ Safety & Limit Specifications

| Metric / Guard | Value / Strategy | Description |
| :--- | :--- | :--- |
| **Session Model** | Explicit `session_id` / `conversation_id` | Client-held stateless routing; zero `agy -c` preemption |
| **Session Lock** | `HTTP 409 Conflict` | Protects in-flight turns from concurrent collisions across Generic and Legacy |
| **Max Concurrency** | `AGY_MAX_CONCURRENCY` (Default `1`)| Enforced by shared bounded semaphore (`HTTP 429` on overflow) |
| **Timeout Budget** | Request `timeout_sec` or `ACP_AGENT_TIMEOUT_SEC` | Monotonic budget; killed via `os.killpg(pgid, SIGKILL)` on expiration |
| **Transport Margin**| `+5.0 seconds` | Outer Future wait margin over task timeout |
| **Max HTTP Sockets**| `50` | `45 POST` cap plus `5` general HTTP slots; the final slots are not health-exclusive |
| **Socket Idle Timeout** | `10.0 seconds` | Prevents Slowloris socket starvation |
| **Max Request Body** | `2 MB` | Rejects payloads exceeding 2MB (`HTTP 413`) |

---

## 📄 License

MIT License © 2026 hikki-fan
