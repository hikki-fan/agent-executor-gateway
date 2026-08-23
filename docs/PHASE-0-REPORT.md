# Phase 0 Report — Bootstrap & Baseline Hardening
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Repository Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 0 — Bootstrap & Baseline Hardening`
* **Lifecycle Status**: `Phase 0 PASS with Codex independent acceptance`
* **Stable Baseline Commit**: `b07e5d2533435272fe2dc96073c7fc0d254a5d49` (`fix: preserve agy partial responses`)
* **Git Branches & Remotes**:
  * Active Branch: `master`
  * `origin`: `https://github.com/hikki-fan/agent-executor-gateway.git` (Fetch & Push)
  * `upstream`: `https://github.com/hikki-fan/antigravity-rest-bridge.git` (Fetch & Push)
* **Scope Note**: This report certifies completion of Phase 0 only. The broader multi-phase replacement roadmap (Phase 1 through Phase 11) remains ahead.

---

## 2. Verification Provenance

* **AGY Worker Responsibility**: Implemented the legacy compatibility test harness, dynamic path audits, token isolation safeguards, real process group cleanup integration test, and Phase 0 documentation.
* **Codex Independent Verification**:
  * Baseline test suite: `18/18 PASS` in `1.454s`
  * Standalone compatibility suite with `ACP_TOKEN_FILE` pre-set to production path: `38/38 PASS` in `2.082s`
  * Combined test discovery: `56/56 PASS` in `3.517s`
  * Python byte-compilation: `compileall` exited with code `0`
  * Git whitespace/diff check: `git diff --check` exited with code `0`
  * Real Linux process tree timeout test: Confirmed zero surviving active parent or child processes
  * Source integrity: Runtime source hashes remain identical to stable `b07e5d2`, and the upstream reference repository `/workspace/antigravity-rest-bridge` remains clean and unmodified.

---

## 3. Current Architecture

```
                                  [ Codex Control Plane ]
                               (Planner / Router / Reviewer)
                                             │
                                             ▼
                             ┌───────────────────────────────┐
                             │    Agent Executor Gateway     │
                             │ (127.0.0.1:8765 / Legacy ACP) │
                             └───────────────┬───────────────┘
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       │                                           │
                       ▼                                           ▼
             [ Fast Probe Channel ]                      [ Protected Heavy Channel ]
           GET /health, /acp/v1/status              POST /acp/v1/invoke, /send-message
             (Max 50 TCP Connections)                (Strict Bearer Auth, Max 45 POST)
                       │                                           │
                       │                                           ▼
                       │                             ┌───────────────────────────┐
                       │                             │ Bounded Agent Semaphore   │
                       │                             │ (AGY_MAX_CONCURRENCY = 1) │
                       │                             └─────────────┬─────────────┘
                       │                                           │
                       │                             ┌─────────────▼─────────────┐
                       │                             │ Conversation Lock Manager │
                       │                             │ (Per-ID Concurrency 409)  │
                       │                             └─────────────┬─────────────┘
                       │                                           │
                       │                                           ▼
                       │                             ┌───────────────────────────┐
                       │                             │ ThreadPoolExecutor Worker │
                       │                             │ (max_workers=max(N,4)=4)  │
                       │                             │ (Monotonic Timeout = 330s)│
                       │                             └─────────────┬─────────────┘
                       │                                           │
                       │                                           ▼
                       │                             ┌───────────────────────────┐
                       │                             │   Subprocess Process PG   │
                       │                             │  (`start_new_session=True`│
                       │                             │  `stdin=DEVNULL`, killpg) │
                       │                             └─────────────┬─────────────┘
                       │                                           │
                       │                                           ▼
                       │                             ┌───────────────────────────┐
                       │                             │  Google Antigravity CLI   │
                       │                             │  (`agy --conversation ID`)│
                       │                             └───────────────────────────┘
```

### 3.1 Concurrency, Admission Control & Threading Architecture

1. **Stateless Gateway Design**: The server maintains zero persistent session database or pseudo-mapping tables. The client (Codex) owns and retains the `conversation_id`. On the first turn, the gateway extracts the newly created `conversation_id` from AGY CLI output and returns it to Codex.
2. **Threaded HTTP Server**: Implemented via `socketserver.ThreadingMixIn` and `http.server.HTTPServer` with `allow_reuse_address = True` and `daemon_threads = True`.
3. **Execution Thread Pool vs Admission Semaphore**:
   * **`agent_executor` (ThreadPoolExecutor)**: Initialized with `max_workers = max(AGY_MAX_CONCURRENCY, 4)`. Under default settings (`AGY_MAX_CONCURRENCY=1`), the pool configures 4 worker threads.
   * **`agent_semaphore` (Admission Semaphore)**: A `threading.BoundedSemaphore(AGY_MAX_CONCURRENCY)` (default 1) strictly controls admitted heavy agent tasks. The `/health` response field `max_worker_threads` reflects this admission concurrency limit (`AGY_MAX_CONCURRENCY = 1`), rather than the internal thread pool size.
4. **Connection Permitting & Health Slot Reservations**:
   * `http_connection_semaphore` (Limit: 50): Restricts total concurrent TCP sockets to 50.
   * `post_connection_semaphore` (Limit: 45): Heavy POST operations can consume at most 45 sockets, ensuring POST requests cannot exhaust the final 5 permits. Note that these remaining 5 slots are not exclusive to `/health` because other GET requests or slow/idle connections can also acquire them.
5. **Session Turn Lock Manager (`ConversationLockManager`)**:
   * Prevents overlapping turns against the same upstream AGY conversation, reducing the risk of transcript race conditions and session-state corruption.
   * Concurrent requests for the exact same `conversation_id` are rejected immediately with `HTTP 409 Conflict`.
   * Lock acquisition is exception-safe and released in `finally` blocks upon completion, failure, or timeout.
6. **Supervisor & Daemon Architecture**:
   * Daemon launcher (`ensure_acp_bridge.sh`): Uses `setsid -f`, lock file descriptor isolation (`exec 200>`), process verification via `/proc/$pid/cmdline`, and synchronous readiness polling.
   * Watchdog supervisor (`acp_watchdog.sh`): Observably runs with PPID=1 in the current container environment with a singleton file lock (`exec 201>`), polling `http://127.0.0.1:8765/health` every 5 seconds.

---

## 4. Current API Contracts

### 4.1 Authentication Contract
* **Mechanism**: Strict HTTP Bearer Token.
* **Header**: `Authorization: Bearer <TOKEN>`
* **Storage**: 24-byte hex token stored at `/home/codex/.codex/acp_token` with `0600` file permissions.
* **Scope**: All `POST` endpoints require authentication. `GET /health`, `GET /acp/v1/status`, and `OPTIONS` do not require authentication.
* **Privacy**: Health probe responses do **not** expose the token file path. (Local startup logs do print the token path to server stdout).

### 4.2 Endpoint Specifications

#### 1. `GET /health` & `GET /acp/v1/status`
* **Authentication**: None required.
* **Response Status**: `200 OK`
* **Response Body**:
```json
{
  "status": "online",
  "service": "Antigravity REST Bridge Server",
  "version": "2.4.0",
  "auth_type": "Strict Bearer Token",
  "mode": "explicit_conversation_cli",
  "language_server": {
    "status": "disabled",
    "address": null,
    "mode": "explicit_conversation_cli"
  },
  "limits": {
    "max_payload_bytes": 2097152,
    "subprocess_timeout_sec": 300,
    "auth_grace_sec": 30,
    "total_process_timeout_sec": 330,
    "max_worker_threads": 1,
    "max_http_connections": 50,
    "max_post_connections": 45,
    "reserved_health_slots": 5,
    "socket_timeout_sec": 10.0,
    "admission_control": "HTTP 429 Bounded Semaphore (1)"
  }
}
```

#### 2. `POST /acp/v1/invoke` & `POST /acp/v1/new-conversation`
* **Authentication**: `Bearer <TOKEN>`
* **Request Body**:
```json
{
  "prompt": "Task description string (required)",
  "conversation_id": "UUID string (optional; if omitted, creates new conversation)",
  "model": "flash | pro (optional)",
  "effort": "low | medium | high (optional)"
}
```
* **Success Response (`200 OK`)**:
```json
{
  "status": "success",
  "action": "new-conversation" | "invoke",
  "conversation_id": "11111111-2222-3333-4444-555555555555",
  "mode": "explicit_conversation_cli",
  "output": "Raw CLI stdout string",
  "parsed": {
    "conversation_id": "11111111-2222-3333-4444-555555555555",
    "status": "SUCCESS",
    "response": "Assistant response text",
    "num_turns": 1,
    "usage": { "total_tokens": 150 }
  }
}
```
* **Partial Success Response (`200 OK`)**:
```json
{
  "status": "partial_success",
  "action": "invoke" | "new-conversation",
  "conversation_id": "11111111-2222-3333-4444-555555555555",
  "mode": "explicit_conversation_cli",
  "warning": "agy reported ERROR after producing a non-empty response; review the response before relying on it",
  "upstream_status": "ERROR",
  "upstream_error": "Error description string",
  "cli_exit_code": 1,
  "output": "Raw stdout string",
  "parsed": { ... }
}
```

#### 3. `POST /acp/v1/send-message`
* **Authentication**: `Bearer <TOKEN>`
* **Request Body**:
```json
{
  "recipient_id": "UUID string (required; alias: conversation_id)",
  "content": "Follow-up message prompt (required; alias: prompt)",
  "model": "flash | pro (optional)",
  "effort": "low | medium | high (optional)"
}
```
* **Response**: Equivalent schema to `POST /acp/v1/invoke` with `action: "send-message"`.

#### 4. `POST /acp/v1/metadata`
* **Status**: `501 Not Implemented` (Language server IPC decoupled in stateless CLI mode).

### 4.3 HTTP Status Codes Matrix

| Code | Meaning | Condition |
| :--- | :--- | :--- |
| **200** | Success / Partial Success | Turn completed successfully, or finished with usable response despite late CLI warning |
| **400** | Bad Request | Missing required parameters (`prompt`, `recipient_id`, `content`) or malformed JSON |
| **401** | Unauthorized | Missing or invalid `Authorization: Bearer <TOKEN>` header |
| **404** | Not Found | Route does not match any registered endpoint |
| **409** | Conflict | Another turn is currently executing for the requested `conversation_id` |
| **413** | Payload Too Large | Request payload exceeds `MAX_CONTENT_LENGTH` (2MB) |
| **429** | Too Many Requests | Active agent count reached `AGY_MAX_CONCURRENCY` limit |
| **500** | Internal Error | Subprocess returned ERROR with empty response, missing top-level ID on turn 1, or non-JSON output |
| **501** | Not Implemented | Endpoint disabled (e.g. `/metadata` without Language Server) |
| **503** | Service Busy | POST API connection pool exhausted (45 slots) |
| **504** | Gateway Timeout | Subprocess exceeded total monotonic budget (`300s + 30s = 330s`) |

---

## 5. Current AGY Runtime Contract

### 5.1 CLI Invocation & Flag Ordering
* **Binary Location**: Resolved in priority order:
  1. `$AGY_BIN` environment variable
  2. `shutil.which("agy")`
  3. `/home/codex/.local/bin/agy`
* **Command Construction Structure**:
  ```bash
  agy \
    [--conversation <conversation_id>] \
    --output-format json \
    --dangerously-skip-permissions \
    [--model <model>] \
    [--effort <effort>] \
    -p <prompt>
  ```
* **Critical Flag Ordering Rule**: All options and arguments **must precede** `-p <prompt>`. The prompt is always strictly the final argument.

### 5.2 AGY CLI Output JSON Contract
AGY CLI is invoked with `--output-format json` and produces a single JSON object on `stdout`:
```json
{
  "conversation_id": "UUID-v4-string",
  "status": "SUCCESS" | "ERROR",
  "response": "Text output from assistant",
  "num_turns": 1,
  "usage": {
    "input_tokens": 120,
    "output_tokens": 80,
    "total_tokens": 200
  },
  "error": "Error message if status is ERROR"
}
```

### 5.3 Session Persistence & 1:1 Mapping
* AGY persists session state, tool execution logs, and transcripts on local disk under `/home/codex/.gemini/antigravity-cli/brain/<conversation-id>`.
* Providing `--conversation <id>` restores conversation context across gateway or container restarts.
* The gateway verifies that a newly initiated session returns a valid top-level `conversation_id`. UUIDs found only embedded in markdown text are strictly rejected.

---

## 6. Current Process Lifecycle

### 6.1 Subprocess Group Isolation
* `subprocess.Popen` is invoked with:
  * `start_new_session=True`: Detaches the child process into a brand new process group (`PGID = proc.pid`).
  * `stdin=subprocess.DEVNULL`: Disconnects interactive input streams to prevent CLI hangs.
  * `stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True`.

### 6.2 Monotonic Timeout Budget & Execution
* **Base Task Budget**: `ACP_AGENT_TIMEOUT_SEC` (Default: 300s).
* **Auth Grace Budget**: `ACP_AUTH_GRACE_SEC` (Default: 30s) for silent OAuth token validation.
* **Total Process Timeout**: `TOTAL_PROCESS_TIMEOUT = 330s`.
* **Monotonic Enforcement**: `execute_with_retry` captures `deadline = time.monotonic() + TOTAL_PROCESS_TIMEOUT`. Every individual subprocess run uses `remaining = deadline - now`.

### 6.3 Process Tree Cleanup on Timeout
When `subprocess.TimeoutExpired` is raised:
1. The gateway executes `os.killpg(pgid, signal.SIGKILL)` to terminate the entire process group.
2. Explicitly closes `proc.stdout` and `proc.stderr` file descriptors.
3. Invokes `proc.wait(timeout=0.5)` to attempt reaping the direct child process.
4. The real integration test (`test_38_real_process_tree_cleanup_integration_linux`) provides evidence that both parent and child become non-active after timeout.
5. Returns HTTP 504 Gateway Timeout.

### 6.4 Pre-execution Retry Mechanics
* **Condition for Retry**:
  * Must be a new conversation attempt (no `--conversation` in command line).
  * Output must be a valid JSON dictionary with `status == "ERROR"`.
  * `conversation_id` must be absent / null.
  * `num_turns` must be strictly 0.
  * `usage.total_tokens` must be strictly 0.
  * `response` must be empty / whitespace.
  * Error message matches retryable transient patterns (e.g. `EOF`, `broken pipe`, `connection reset`, `network failure`).
* **Retry Strategy**: Up to 3 attempts with linear stepped backoff (`0.3s * attempt`, i.e., 0.3s then 0.6s).
* **Single Attempt Rule**: Resumed conversations (`--conversation`) or errors occurring after partial token generation are **never** retried.

### 6.5 Graceful Server Shutdown
* Handles `SIGTERM` and `SIGINT` via non-deadlocking signal handler.
* Spawns an asynchronous daemon thread calling `server_instance.shutdown()` to request `serve_forever()` to stop.
* In `finally`, closes the server socket and executes `agent_executor.shutdown(wait=True, cancel_futures=True)`.
* Individual HTTP request handler threads run as daemon threads and are not explicitly joined.

---

## 7. Known Edge Cases & High Risks

### 7.1 Critical Runtime Lifecycle Mismatch Risk (High Risk - Unresolved in Phase 0)
* **Issue**: The server task budget defaults to `300s` plus `30s` auth grace (`TOTAL_PROCESS_TIMEOUT = 330s`). However, `ensure_acp_bridge.sh` (the daemon restart script) waits only **65 seconds** after sending `SIGTERM` (`for i in {1..650}; sleep 0.1`) before force-killing with `SIGKILL` (`kill -9`), accompanied by stale comments referencing 60 seconds.
* **Impact**: During service restarts or deployments, an active, healthy in-flight task taking longer than 65s can be prematurely SIGKILLed by the launcher before completing its allocated 330s budget.
* **Phase 0 Policy**: Runtime behavior remains strictly unmodified in Phase 0. This lifecycle mismatch is tracked as a primary defect to be resolved in Phase 1 / Phase 2 lifecycle unification.

### 7.2 Portability & Path Hardcoding Risk
* Installer (`install.sh`), launcher (`ensure_acp_bridge.sh`), and watchdog (`acp_watchdog.sh`) contain hardcoded `/home/codex` and `/workspace/scripts` paths.
* In Python, several settings already support environment variable overrides (`ACP_PORT`, `AGY_BIN`, `ACP_TOKEN_FILE`, `ACP_AGENT_TIMEOUT_SEC`, `ACP_AUTH_GRACE_SEC`, `AGY_MAX_CONCURRENCY`). Phase 1 will centralize all configuration in `core/config.py`.

### 7.3 Security Hardening Observations
* **Wildcard CORS**: `Access-Control-Allow-Origin: *` is returned on all responses. Because the bridge binds strictly to `127.0.0.1`, this is a local-only configuration, but should be restricted in future hardening.
* **IP Rate Limiting**: Absence of per-IP rate limiting is currently low priority due to `127.0.0.1` binding, while admission control semaphores protect global process limits.

### 7.4 Observability & CI Architecture
* Currently, the system relies on unstructured file logging (`/home/codex/.codex/acp_bridge.log`) and has no automated CI workflow.
* **ASGI Migration Stance**: Migration to ASGI / FastAPI / Starlette is deferred and evidence-driven; modularization, core extraction, and robust configuration take priority before any transport layer changes.

---

## 8. Existing Tests & Compatibility Baseline

### 8.1 Test Suites Breakdown

1. **Baseline Suite (`test_acp_bridge.py` - 18 Tests)**:
   * Mock-based HTTP contract verification.
   * Audits repository-owned files for zero active `agy -c` commands.
   * Execution time: ~1.45s.

2. **Compatibility Suite (`tests/test_legacy_compatibility.py` - 38 Tests)**:
   * **37 Mock-based HTTP & Unit Tests**: Covering Health, Auth, New Conversation, Continuation, Locking, Retries, Partial Success, and Connection Limits.
   * **1 Real Linux Process Group Integration Test (`test_38_real_process_tree_cleanup_integration_linux`)**:
     * Runs without mocks, without live AGY, and without network calls.
     * Spawns a real parent process that forks a real background child process.
     * Invokes `acp_server.run_agent_command` with a short timeout (`0.5s`).
     * Verifies that `os.killpg(pgid, signal.SIGKILL)` successfully terminates both parent and child processes.
     * Defensively cleans up any surviving processes and temp marker files.
     * Skips on non-Linux platforms.
   * Execution time: ~2.10s.

3. **Combined Discovery (`unittest discover -s .`)**:
   * Total 56 tests passing cleanly in ~3.5s.
   * Detects preloaded `acp_server` state: fails fast if pointing to production path without mutating globals, and safely creates isolated temp tokens when running standalone.

---

## 9. Proof That Old Repository Was Not Modified

* Directory: `/workspace/antigravity-rest-bridge`
* Command: `git -C /workspace/antigravity-rest-bridge status`
* Output:
  ```text
  On branch master
  Your branch is up to date with 'origin/master'.
  nothing to commit, working tree clean
  ```

---

## 10. Real AGY End-to-End Smoke Test Evidence

Real end-to-end smoke verification was executed against the live Antigravity runtime (see [SMOKE-TEST-LOG.md](./SMOKE-TEST-LOG.md)).

* **Test Date**: `2026-08-24`
* **Target Gateway**: Local daemon on `http://127.0.0.1:8765` via `acp-cli`
* **Health Probe**: `HTTP 200 OK`, `version: "2.4.0"`, `status: "online"`.
* **Turn 1 (New Conversation)**:
  * Prompt: `Phase 0 live runtime smoke test. Do not use tools, modify files, or reveal environment data. Reply with exactly PHASE0_NEW_OK and nothing else.`
  * Status: `success`
  * Action: `new-conversation`
  * Assigned ID: `c82bb2e7-ef19-429d-ac2c-998371a61bee`
  * Response: `PHASE0_NEW_OK`
  * `num_turns`: `1`
* **Turn 2 (Continuation Turn)**:
  * Prompt: `Phase 0 continuation smoke test. Do not use tools or modify files. Reply with exactly PHASE0_CONTINUE_OK and nothing else.`
  * Status: `success`
  * Action: `invoke`
  * Target ID: `c82bb2e7-ef19-429d-ac2c-998371a61bee` (exact same conversation)
  * Response: `PHASE0_CONTINUE_OK`
  * `num_turns`: `2`

---

## 11. Phase 1 Exact Refactor Plan

### 11.1 Phase 1 Objective
Extract core primitives and `AntigravityAdapter` into a modular package layout. **Strictly NO Grok code** will be implemented in Phase 1. All existing legacy HTTP API endpoints (`/health`, `/acp/v1/*`) must remain 100% backward compatible and pass all 56 compatibility tests.

### 11.2 Target Module Structure for Phase 1
```text
agent-executor-gateway/
├── app.py                     # Main server entrypoint (ThreadedHTTPServer setup)
├── core/
│   ├── __init__.py
│   ├── config.py              # Ports, timeouts, limits, token paths
│   ├── auth.py                # Strict Bearer token verification
│   ├── concurrency.py         # Semaphores (agent, http, post)
│   ├── session_lock.py        # ConversationLockManager / SessionLockManager
│   ├── process.py             # Process group spawning, killpg, monotonic timer
│   ├── result.py              # Standardized ExecutorResult & normalization
│   └── errors.py              # Error classifications & retry predicates
├── adapters/
│   ├── __init__.py
│   ├── base.py                # Abstract ExecutorAdapter class (invoke, health, capabilities)
│   └── antigravity.py         # AntigravityAdapter (command builder, parser, retry)
├── api/
│   ├── __init__.py
│   ├── health.py              # /health & /acp/v1/status handlers
│   └── compatibility.py       # Legacy /acp/v1/invoke & /send-message handlers
├── tests/
│   ├── test_legacy_compatibility.py
│   ├── test_core.py
│   └── test_agy_adapter.py
├── acp-cli
├── ensure_acp_bridge.sh
├── acp_watchdog.sh
├── install.sh
└── docs/
    ├── PHASE-0-REPORT.md
    └── SMOKE-TEST-LOG.md
```

### 11.3 Implementation Sequence
1. **Step 1: Extract `core/` Primitives**:
   * Move configuration constants to `core/config.py`.
   * Move Bearer token authentication logic to `core/auth.py`.
   * Move admission semaphores to `core/concurrency.py`.
   * Move session lock manager to `core/session_lock.py`.
   * Move process group execution and timeout cleanup to `core/process.py`.
   * Move retry predicates to `core/errors.py`.
2. **Step 2: Abstract `ExecutorAdapter` Interface (`adapters/base.py`)**:
   * Define `invoke()`, `health()`, and `capabilities()` abstract methods.
3. **Step 3: Implement `AntigravityAdapter` (`adapters/antigravity.py`)**:
   * Encapsulate `build_agy_command`, AGY CLI JSON parsing, partial-success detection, and 1:1 conversation mapping.
4. **Step 4: Extract API Handlers & Assemble `app.py`**:
   * Create `api/compatibility.py` and `api/health.py` delegating to `AntigravityAdapter`.
   * Assemble `app.py` as top-level server.
5. **Step 5: Verification Gate**:
   * Run full test suite: `python3 -m unittest discover -s . -v`.
   * Guarantee 100% pass rate across all 56 tests.

---
*Report generated on 2026-08-24. Phase 0 PASS with Codex independent acceptance.*
