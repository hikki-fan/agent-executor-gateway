# Phase 1 Report — AGY Adapter & Core Extraction
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 1 — AGY Adapter & Core Extraction`
* **Lifecycle Status**: `Phase 1 PASS with Codex independent acceptance`
* **Scope Limitation**: This certification is explicitly limited to Phase 1. The overall multi-phase roadmap (Phase 2 through Phase 11) remains ahead and incomplete.
* **Baseline Commit**: `3d2643f83b1b455b1e461a50dce03480f725d8f7` (`chore: bootstrap agent executor gateway from stable agy bridge`)
* **Upstream Reference**: `https://github.com/hikki-fan/antigravity-rest-bridge.git` (Unmodified, Clean)
* **Scope Discipline**: Phase 1 is strictly limited to architectural refactoring and adapter extraction. No Grok integration, Generic Executor endpoints (`/v1/executors`), router, task schemas, or worktree logic were introduced. Phase 2 was intentionally not entered.

---

## 2. Verification & Provenance

* **AGY Worker Responsibility**:
  * Extracted core modules (`core/auth.py`, `core/config.py`, `core/concurrency.py`, `core/session_lock.py`, `core/process.py`, `core/timeout.py`, `core/result.py`).
  * Implemented `adapters/base.py` (`ExecutorAdapter`) and `adapters/antigravity.py` (`AntigravityAdapter`, `AntigravityConfig`).
  * Rewired `acp_server.py` to delegate to `AntigravityAdapter` while retaining legacy compatibility patch points.
  * Added unit test suites `tests/test_core.py` and `tests/test_agy_adapter.py`.
* **Codex Independent Verification & Iteration Evidence**:
  * Initial candidate passed discovery (90 tests), but was rejected during architecture review due to 10 specific defects:
    1. Exception-driven runner signature fallback in `_run` and `_adapter_runner_dispatch` could cause duplicate turn executions if a task raised `TypeError`.
    2. `core/config.py` improperly parsed AGY provider environment variables (`AGY_MAX_CONCURRENCY`, `ACP_AGENT_TIMEOUT_SEC`, `ACP_AUTH_GRACE_SEC`).
    3. Core neutrality gate did not inspect AST identifier names, attribute names, or string constants for provider terms.
    4. `tests/test_agy_adapter.py` lacked token file isolation during standalone execution.
    5. Adapter command builder stripped whitespace, altering legacy truthiness behavior for whitespace flag values.
    6. Non-positive timeouts in `execute_with_retry` raised `ValueError` instead of `subprocess.TimeoutExpired`.
    7. `ExecutorResult.usage` did not strictly normalize to the 4 standard Section 10 keys (`input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`).
    8. `ConversationLockManager` compatibility wrapper required constructor arguments rather than being 0-argument constructible.
    9. Inaccurate comments claimed the final 5 connection slots were exclusive to `/health`.
    10. Report prematurely claimed full verification instead of candidate status.
  * All 10 defects were resolved with deterministic runner dispatch, dual configuration objects (`GatewayConfig` and `AntigravityConfig`), AST identifier/attribute neutrality checks, token isolation, exact flag truthiness, immediate timeout expiration, strict usage normalization, and 0-argument lock constructor compatibility.
  * Codex independent verification after correction round 1:
    * **Command A** (`test_acp_bridge.py`): 18 tests in 1.461s **PASS**
    * **Command B** (`tests.test_legacy_compatibility`): 38 tests in 2.099s **PASS**
    * **Command C** (`tests.test_core tests.test_agy_adapter`): 39 tests in 1.780s **PASS**
    * **Command D** (`unittest discover -s . -v`): 95 tests in 5.299s **PASS**
    * **Command E** (`compileall`): Exited with code `0`
    * **Command F** (`git diff --check`): Exited with code `0`
    * **Command G** (`rg -n -i grok ...`): `0` matches found
    * **Core Provider Scan**: `0` provider identifiers or terms found in `core/`
    * **Old Repository**: `/workspace/antigravity-rest-bridge` remains clean and unmodified
  * Live end-to-end smoke test completed against test server on port `18766` with zero surviving processes (see [PHASE-1-SMOKE-TEST-LOG.md](./PHASE-1-SMOKE-TEST-LOG.md)).

---

## 3. Module Ownership & System Architecture

Phase 1 establishes a clean boundary between executor-neutral gateway primitives and provider-specific CLI behavior:

```text
                                [ Codex Control Plane ]
                             (Planner / Router / Reviewer)
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │         acp_server.py (HTTP)            │
                      │  - Strict Bearer Auth (core.auth)       │
                      │  - Admission Control (core.concurrency) │
                      │  - Session Turn Lock (core.session_lock)│
                      │  - GatewayConfig & AntigravityConfig    │
                      │  - Legacy Response Compatibility Mapper │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │    adapters/antigravity.py (Adapter)    │
                      │  - AntigravityConfig (Provider Env Vars)│
                      │  - CLI Command Builder (Exact Flags)    │
                      │  - 1:1 session_id <-> conversation_id   │
                      │  - Pre-execution Transient Retries      │
                      │  - Monotonic Budget (core.timeout)      │
                      │  - Output JSON Parsing & Partial Success│
                      │  - Diagnostic Normalization             │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │          core/process.py (Core)         │
                      │  - Process Group Spawning (setsid=True) │
                      │  - Pipe Isolation (stdin=DEVNULL)       │
                      │  - Process Group SIGKILL (os.killpg)    │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │        Google Antigravity CLI           │
                      │        (`agy --conversation ID`)        │
                      └─────────────────────────────────────────┘
```

### 3.1 Module Ownership Matrix

| Module Path | Ownership & Purpose | Provider Dependencies |
| :--- | :--- | :--- |
| `core/result.py` | Defines `ExecutorResult` data model matching Section 10 with legal statuses (`success`, `partial_success`, `error`), timing, strictly normalized 4-key usage (`input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`), warnings, error, and raw payload. | Neutral (Zero provider logic) |
| `core/auth.py` | Encapsulates token file initialization (`0600` permissions), 24-byte hex token generation, and constant-time Bearer token verification. | Neutral (Zero provider logic) |
| `core/config.py` | Centralizes transport and network server defaults (ports, connection limits, socket timeouts) via `GatewayConfig`. | Neutral (Zero provider logic) |
| `core/session_lock.py` | Implements `SessionLockManager` keyed strictly by `(executor, session_id)` ensuring multi-executor isolation without cross-executor session collisions. | Neutral (Zero provider logic) |
| `core/concurrency.py` | Implements `AdmissionController` managing bounded semaphores for HTTP sockets (50), POST capacity (45), and active worker concurrency (1). | Neutral (Zero provider logic) |
| `core/process.py` | Implements `run_process_group` with `start_new_session=True`, `stdin=DEVNULL`, stdout/stderr capture, and `os.killpg(pgid, signal.SIGKILL)` on timeout. | Neutral (Zero provider logic) |
| `core/timeout.py` | Implements `DeadlineTimer` for monotonic deadline tracking and aggregate budget enforcement across execution steps and retries. | Neutral (Zero provider logic) |
| `adapters/base.py` | Defines abstract base class `ExecutorAdapter` requiring `invoke()`, `health()`, and `capabilities()`. | Neutral (Abstract Interface) |
| `adapters/antigravity.py` | Concrete `AntigravityAdapter(ExecutorAdapter)` and `AntigravityConfig` implementing AGY CLI binary resolution, exact Phase 0 flag truthiness and ordering before `-p`, session-to-conversation mapping, pre-execution retry predicates, partial-success classification, usage normalization, and diagnostics. | Antigravity (AGY Provider) |
| `acp_server.py` | Direct executable server providing backward-compatible HTTP transport on port `8765`, translating incoming requests to `AntigravityAdapter.invoke()` and formatting legacy JSON responses. | Legacy ACP Compatibility |

---

## 4. Dependency Direction & Core Neutrality

A strict unidirectional dependency architecture is enforced across the codebase:

```text
acp_server.py  ───>  adapters/antigravity.py  ───>  adapters/base.py  ───>  core/*
     │                                                     │
     └─────────────────────────────────────────────────────┘
```

### 4.1 Architectural Neutrality Rules
1. **No Downward Pollution**: Modules under `core/` strictly **never** import `adapters`, `acp_server`, or external frameworks.
2. **Provider Isolation**: Provider command construction, CLI flag ordering, JSON output parsing, pre-execution retries, partial-success detection, and AGY environment variable parsing (`AGY_BIN`, `AGY_MAX_CONCURRENCY`, `ACP_AGENT_TIMEOUT_SEC`, `ACP_AUTH_GRACE_SEC`) are strictly owned by `adapters/antigravity.py`. Legacy compatibility field names remain in `acp_server.py` for client translation.
3. **Automated Verification**: AST analysis test `tests.test_core.TestCoreNeutrality.test_19_core_dependency_and_identifier_neutrality` inspects imports, identifier names, attribute names, and string constants in all Python files under `core/` to verify zero forbidden provider terms (`agy`, `antigravity`, `conversation_id`, `--conversation`, `--output-format`, AGY env vars).

---

## 5. Legacy Compatibility Wiring & Preservation

### 5.1 Internal Compatibility Patch Point
To preserve all 56 baseline Phase 0 tests without modification:
* `acp_server.run_agent_command` is retained as a public module-level function delegating to `core.process.run_process_group`.
* `AntigravityAdapter` accepts an injected runner dispatch callable that routes process executions through `acp_server.run_agent_command`.
* When unit tests patch `acp_server.run_agent_command`, HTTP requests through `acp_server` invoke `AntigravityAdapter.invoke()`, which calls the patched runner deterministically.

### 5.2 Re-Exported Compatibility Wrappers
The following thin wrappers are exposed in `acp_server.py` and delegate directly to `agy_adapter` with zero duplicated logic:
* `build_agy_command` -> `agy_adapter.build_command`
* `execute_with_retry` -> `agy_adapter.execute_with_retry`
* `is_retryable_pre_execution_error` -> `agy_adapter.is_retryable_pre_execution_error`
* `is_partial_success_result` -> `agy_adapter.is_partial_success_result`
* `has_cli_response` -> `agy_adapter.has_cli_response`
* `cli_error_detail` -> `agy_adapter.cli_error_detail`
* `conv_lock_mgr` -> `ConversationLockCompatibility(session_lock_manager, "agy")`
* `ConversationLockManager` -> `ConversationLockCompatibility` (supports 0-arg instantiation)

### 5.3 Preserved Runtime Contracts
* **Port**: Binds to `127.0.0.1:8765` (configurable via `ACP_PORT`).
* **Endpoints**: `/health`, `/acp/v1/status`, `/acp/v1/invoke`, `/acp/v1/new-conversation`, `/acp/v1/send-message`, `/acp/v1/metadata` (501).
* **Auth**: Strict Bearer Token from `/home/codex/.codex/acp_token` (`0600`).
* **HTTP Status Codes**: Exact 200, 400, 401, 404, 409, 413, 429, 500, 501, 503, 504 status codes and error bodies.
* **Admission & Limits**: Max 50 HTTP connections, Max 45 POST connections, 10s socket timeout. POST requests cannot consume the final 5 permits, ensuring connection slots remain available for fast probes and other requests (though these 5 slots are general HTTP connection slots, not exclusive to `/health`).

---

## 6. Test Verification & Timings

All 95 unit, contract, and integration tests across 4 test suites pass with 100% success rate under Codex independent execution:

| Test Suite | Test Count | Execution Time | Coverage Scope |
| :--- | :--- | :--- | :--- |
| `test_acp_bridge.py` | 18 | 1.461s | Baseline HTTP contracts, lock enforcement, flag ordering, CLI parsing, codebase cleanliness audit. |
| `tests.test_legacy_compatibility` | 38 | 2.099s | Comprehensive legacy endpoints, auth rejection, 409/429 concurrency, retry backoff, partial success, real Linux process group integration. |
| `tests.test_core` | 19 | 0.880s | `ExecutorResult` validation/serialization, strict 4-key usage normalization, `core.auth` 0600 permissions, `GatewayConfig`, `SessionLockManager` multi-executor isolation, `AdmissionController`, `DeadlineTimer` (including non-positive timeouts), `run_process_group`, strengthened AST neutrality gate (imports, names, attributes, constants). |
| `tests.test_agy_adapter` | 20 | 0.900s | Token isolation, `ExecutorAdapter` ABC contract, `AntigravityConfig`, exact flag truthiness & whitespace preservation, deterministic dispatch (TypeError single execution), first-turn extraction, continuation, partial success, retry eligibility, non-positive timeout handling, 0-arg `ConversationLockManager` compatibility, timeout/cwd propagation, HTTP delegation. |
| **Combined Discovery (`unittest discover -s .`)** | **95** | **5.299s** | **Full repository test discovery executing all suites end-to-end.** |

### 6.1 Real Linux Process-Tree Timeout Verification
`test_38_real_process_tree_cleanup_integration_linux` executes a real multi-process tree on Linux without mocks:
* Spawns a parent Python process that forks a long-sleeping background child process.
* Executes with a 0.5s timeout via `run_agent_command`.
* Verifies `subprocess.TimeoutExpired` is raised and `os.killpg(pgid, signal.SIGKILL)` successfully terminates both parent and child processes (verified via `/proc/$pid/status`).

---

## 7. Known Edge Cases & Remaining Risks

### 7.1 Unresolved Lifecycle Mismatch (High Risk — Preserved from Phase 0)
* **Issue**: The server execution timeout defaults to `300s` plus `30s` auth grace (`TOTAL_PROCESS_TIMEOUT = 330s`). However, `ensure_acp_bridge.sh` waits only **65 seconds** after `SIGTERM` before sending `SIGKILL` (`kill -9`).
* **Preservation Justification**: Per Phase 1 requirements, this phase is pure refactoring and preserves all existing runtime defaults and supervisor behaviors. This lifecycle mismatch is tracked for resolution in subsequent lifecycle unification phases.

---

## 8. Scope Discipline — Why Phase 2 Was Not Entered

Per Goal Prompt Sections 43, 56, and 58:
1. **Phase 1 Goal**: Strictly extract `core/` and `adapters/antigravity.py` while maintaining 100% backward compatibility with legacy endpoints.
2. **Non-Goals Excluded**:
   * No Generic Executor endpoints (`GET /v1/executors`, `POST /v1/executors/{name}/invoke`).
   * No Grok runtime or adapter code (`GrokAdapter`).
   * No Task DAG, routing, or complexity classifier.
   * No Worktree management.
   * No third-party dependencies.
   * No `app.py` or `api/` modules.

All Phase 1 goals have been achieved with zero scope creep.
