# Agent Executor Gateway

**English** | [中文文档](./README_CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.4.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Phase%206%20Candidate-green.svg)]()

High-reliability, executor-neutral **Agent Executor Gateway** providing unified REST API orchestration, session management, and process lifecycle controls for AI coding agents.

> [!NOTE]
> **Phase 6 Status**: Task Schema validation, Machine Verification pipeline (`orchestration/verifier.py`), Scope Control (`orchestration/scope.py`), Completion Report & Metrics (`orchestration/report.py`), and `agentctl` CLI commands (`task validate`, `task verify`) are operational. Task routing/escalation (Phase 7) and production gateway cutover remain future phases.

---

## 🌟 Key Features

- 📋 **Task Schema & Validation (Phase 6)**: Unified, executor-neutral Task JSON model (`orchestration/task.py`) per Goal Prompt Section 18, validating goals, classifications (S/M/L/XL complexity, risk, type), execution params, scopes, acceptance criteria, and verification commands.
- 🧪 **Machine Verification Pipeline (Phase 6)**: Safe declared command runner (`orchestration/verifier.py`) with `shell=False` execution, `cwd` containment, process-group timeout termination (`os.killpg`), log sanitization/redaction, and concise tail output extraction.
- 🛡️ **Strict Scope Control (Phase 6)**: Git-based scope checking (`orchestration/scope.py`) validating committed, staged, unstaged, and untracked files against `allowed_paths` and `forbidden_paths` globs.
- 📊 **Standardized Completion Report & Metrics (Phase 6)**: Section 30 JSON Completion Reports with Git diff statistics and Section 38 `.agent/metrics.jsonl` structured metric append.
- 🛠️ **`agentctl` CLI Tool (Phase 6)**: Unified CLI supporting `agentctl task validate`, `agentctl task verify`, `agentctl executors`, `agentctl health`, and `agentctl invoke`.
- 🌐 **Unified Generic Executor API (Phase 2 & 4)**: Standardized executor discovery (`GET /v1/executors`), health checks (`GET /v1/executors/{executor}/health`), and invocation (`POST /v1/executors/{executor}/invoke`).
- 🤖 **Multi-Provider Support**: Supports both Google Antigravity (`agy`) and Grok Build (`grok`) headless CLI runtimes.
- 📊 **Section 10 Standardized Result Contract**: Uniform `ExecutorResult` schema across all executors (`status`, `executor`, `session_id`, `response`, `exit_code`, `timing`, `usage`, `warnings`, `error`, `raw`).
- 🎯 **Explicit 1:1 Session Isolation**: Stateless gateway routing where clients hold `session_id` (or legacy `conversation_id`). Zero global `agy -c` preemption.
- 🔒 **Per-Session Concurrency Lock**: Rejects concurrent turns within the same `(executor, session_id)` with `HTTP 409 Conflict` across both Generic and Legacy endpoints.
- 🛑 **Unified Admission Control (HTTP 429)**: Global `GATEWAY_MAX_CONCURRENCY` plus independent `AGY_MAX_CONCURRENCY` / `GROK_MAX_CONCURRENCY` semaphores reject saturated work immediately with `HTTP 429`.
- ⏱️ **Flexible Timeout Budgets**: Per-request `timeout_sec` overrides determine both execution deadline and future waiting windows (+5s transport margin), with automatic process group termination (`SIGKILL via os.killpg`).
- 🔁 **Intelligent Pre-execution Retry**: Retries transient 0-turn startup errors (EOF/network) up to 3 times on new AGY sessions while preserving in-flight errors.
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
                                    └───────┬────────────────┬───────┘
                                            │                │
                                            ▼                ▼
                     ┌────────────────────────────┐    ┌───────────────────────────┐
                     │  AntigravityAdapter (agy)  │    │     GrokAdapter (grok)    │
                     │  - 1:1 session_id mapping  │    │  - Headless CLI (-p)      │
                     │  - Flags ordering before -p│    │  - --session-id / --resume│
                     │  - Pre-execution retry     │    │  - JSON parsing & usage   │
                     │  - Uniform ExecutorResult  │    │  - Uniform ExecutorResult │
                     └──────────────┬─────────────┘    └─────────────┬─────────────┘
                                    │                                │
                                    └───────────────┬────────────────┘
                                                    ▼
                                    ┌────────────────────────────────┐
                                    │      core/process.py           │
                                    │  - Process group setsid        │
                                    │  - os.killpg cleanup           │
                                    └────────────────────────────────┘
```

---

## 📡 REST API Reference

### 1. Generic Executor API (Phase 2 & 4)

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
    },
    {
      "name": "grok",
      "available": true,
      "supports_session": true
    }
  ]
}
```

#### Executor Health Probes (`GET /v1/executors/{executor}/health`)
No authentication required.
```bash
curl -s http://127.0.0.1:8765/v1/executors/agy/health
curl -s http://127.0.0.1:8765/v1/executors/grok/health
```

#### Invoke Executor Task (`POST /v1/executors/{executor}/invoke`)
Requires Bearer Token authentication. Supported executors: `agy`, `grok`.

- **Start New AGY Task**:
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

- **Start New Grok Task**:
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/grok/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Create new data processing pipeline",
    "cwd": "/workspace/project",
    "session_id": null,
    "model": "grok-4.6",
    "effort": "high",
    "timeout_sec": 900
  }'
```

- **Continue Existing Grok Session**:
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/grok/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Add unit tests for the pipeline",
    "cwd": "/workspace/project",
    "session_id": "01a0315c-0241-74b0-beb0-ff058535d5d6"
  }'
```

#### Unified ExecutorResult Response Format
All generic invocations return the Section 10 standard schema:
```json
{
  "status": "success",
  "executor": "grok",
  "session_id": "01a0315c-0241-74b0-beb0-ff058535d5d6",
  "response": "Created data processing pipeline with comprehensive unit tests.",
  "exit_code": 0,
  "timing": {
    "duration_ms": 5210
  },
  "usage": {
    "input_tokens": 11144,
    "output_tokens": 145,
    "total_tokens": 14136,
    "cost_usd": 0.00408816
  },
  "warnings": [],
  "error": null,
  "raw": {
    "parsed": {"text": "...", "stopReason": "end_turn"},
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

## 🛠️ `agentctl` CLI Reference (Phase 6)

The repository provides the `agentctl` command-line utility for task validation, verification, and executor inspection:

### 1. Task Validation
Validate Task JSON schema without executing commands:
```bash
./agentctl task validate .agent/tasks/TASK-001.json
```
Output:
```text
Task validation PASSED: '.agent/tasks/TASK-001.json'
  Task ID:        TASK-001
  Goal:           增加 Telegram 下载任务取消功能
  Executor:       agy
  Complexity:     M
  Risk:           medium
  Repository:     /workspace/project (base_commit=abc1234)
  Scope:          allowed=2, forbidden=1
  Verification:   2 commands declared
```

### 2. Task Verification Pipeline
Run machine verification commands and check Git scope boundaries:
```bash
./agentctl task verify .agent/tasks/TASK-001.json --json
```

### 3. Executor & Health Probe
```bash
./agentctl executors
./agentctl health
```

---

## 🛡️ Safety & Limit Specifications

| Metric / Guard | Value / Strategy | Description |
| :--- | :--- | :--- |
| **Session Model** | Explicit `session_id` / `conversation_id` | Client-held stateless routing; zero `agy -c` preemption |
| **Session Lock** | `HTTP 409 Conflict` | Protects in-flight turns from concurrent collisions across Generic and Legacy |
| **Gateway Concurrency** | `GATEWAY_MAX_CONCURRENCY` (Default `2`) | Global bounded semaphore across all executors; overflow returns `HTTP 429` |
| **Executor Concurrency** | `AGY_MAX_CONCURRENCY=1`, `GROK_MAX_CONCURRENCY=1` | Independent per-executor bounded semaphores; overflow returns `HTTP 429` |
| **Scope Control** | Git status & diff vs allowed/forbidden globs | Rejects boundary violations with `scope_violation` failure |
| **Machine Verification** | `shell=False` execution in repo `cwd` | Process-group SIGKILL cleanup, credential redaction, and concise tail logging |
| **Timeout Budget** | Request `timeout_sec` or provider defaults | Monotonic budget; killed via `os.killpg(pgid, SIGKILL)` on expiration |
| **Transport Margin**| `+5.0 seconds` | Outer Future wait margin over task timeout |
| **Max HTTP Sockets**| `50` | `45 POST` cap plus `5` general HTTP slots; the final slots are not health-exclusive |
| **Socket Idle Timeout** | `10.0 seconds` | Prevents Slowloris socket starvation |
| **Max Request Body** | `2 MB` | Rejects payloads exceeding 2MB (`HTTP 413`) |

---

## 📄 License

MIT License © 2026 hikki-fan
