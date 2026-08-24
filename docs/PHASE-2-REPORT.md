# Phase 2 Report — Generic Executor API
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 2 — Generic Executor API`
* **Lifecycle Status**: `Phase 2 Candidate — Codex Independent Verification Complete; Commit Pending`
* **Scope Limitation**: This report certifies the candidate implementation of Phase 2. Grok integration (Phase 3/4), Task DAGs, complexity routing, and production gateway cutover remain strictly out-of-scope and not entered.
* **Baseline Commit**: `899aa647be89e685ef306f8eabd4a661d53da163` (`refactor: extract executor core and antigravity adapter`)
* **Upstream Reference**: `/workspace/antigravity-rest-bridge` (Unmodified, Clean, Zero changes)
* **Scope Discipline**: Phase 2 is strictly focused on introducing the Generic Executor API surface (`GET /v1/executors`, `GET /v1/executors/agy/health`, `POST /v1/executors/agy/invoke`), standardizing responses to Section 10 `ExecutorResult`, and sharing concurrency/session locks with Legacy ACP endpoints.

### 1.1 Work-product provenance

* Agy produced the initial Phase 2 implementation, documentation, and 25-test candidate in conversation `3a16c7d7-f80b-4abf-8607-ae3ffb133627`.
* Codex treated that output as untrusted work product. Independent testing found a standalone oversized-payload test race (`BrokenPipeError`) and then corrected the test transport, tightened integer timeout validation, preserved opaque session identifiers, restored legacy query-path semantics, and added forwarding/timeout/resource-release coverage.
* Agy follow-up was unavailable in this sandbox because the local ACP client could not reach its bridge; the small correction round stayed within Phase 2 scope and was performed and reviewed by Codex.

---

## 2. Implementation Summary

### 2.1 Generic API Layer (`api/` package)
* Created `api/__init__.py` and `api/executors.py`.
* Implemented `ExecutorRegistry`:
  * Dynamic registration and lookup of `ExecutorAdapter` instances.
  * `list_executors()` returns accurate `[{"name": "agy", "available": bool, "supports_session": bool}]` derived directly from adapter `health()` and `capabilities()`.
  * Phase 2 registers strictly only `agy`; zero fake Grok entries.
* Implemented `validate_invoke_request()`:
  * Rejects non-object JSON bodies with HTTP 400.
  * Validates required non-empty string `prompt` (HTTP 400 on empty, missing, or non-string).
  * Validates `session_id` (non-null indicates continuation; rejects empty strings, whitespace, or invalid types with HTTP 400).
  * Validates `timeout_sec` (must be a positive integer; rejects booleans, floats, and non-numeric values with HTTP 400).
  * Validates optional string fields `cwd`, `model`, `effort` (rejects non-string types with HTTP 400).

### 2.2 Server Routing & Lifecycle (`acp_server.py`)
* Wired `executor_registry` at module level registering `agy_adapter`.
* **`GET /v1/executors`**: Unauthenticated executor discovery returning registered executors.
* **`GET /v1/executors/{executor}/health`**: Unauthenticated health probe delegating directly to `adapter.health()`; returns HTTP 404 for unregistered executors.
* **`POST /v1/executors/{executor}/invoke`**:
  * Enforces Strict Bearer Token authentication (HTTP 401 on missing/invalid token).
  * Validates 2MB payload limit (HTTP 413) and JSON syntax (HTTP 400).
  * Validates request fields via `validate_invoke_request` (HTTP 400).
  * Enforces per-session concurrency lock via `session_lock_manager.acquire(executor, session_id)` (HTTP 409 Conflict).
  * Enforces global worker admission control via shared `agent_semaphore.acquire()` (HTTP 429 Too Many Requests).
  * Calculates effective execution timeout and Future waiting window (+5.0s transport margin), preventing premature client aborts when custom `timeout_sec` is provided.
  * Delegates all fields as keyword arguments to `adapter.invoke(prompt=..., cwd=..., session_id=..., model=..., effort=..., timeout_sec=...)`.
  * Standardizes all responses to Section 10 `ExecutorResult.to_dict()`:
    * Returns HTTP 200 on `status == "success"` or `status == "partial_success"`.
    * Returns HTTP 500 on `status == "error"`.
    * Catches `subprocess.TimeoutExpired` / `TimeoutError` and returns HTTP 504 with structured `ExecutorResult` error.
    * Catches unexpected internal exceptions and returns HTTP 500 (not misreported as 504) with structured `ExecutorResult` error.
  * Exception-safely releases `agent_semaphore` and `session_lock_manager` in `finally` blocks without leaks or double-release.
* **Legacy ACP API**: Existing routes remain in place; baseline contracts, status codes, response structures, and literal query-path behavior are covered by the compatibility suites. No blanket claim is made beyond those tested contracts.

---

## 3. Architecture & Dependency Direction

```text
                                [ Codex Control Plane ]
                                           │
                        ┌──────────────────┴──────────────────┐
                        │                                     │
           (Generic API: /v1/executors/*)          (Legacy API: /acp/v1/*)
                        │                                     │
                        └──────────────────┬──────────────────┘
                                           │
                                           ▼
                        ┌─────────────────────────────────────┐
                        │        acp_server.py (HTTP)         │
                        │  - Strict Bearer Auth (core.auth)   │
                        │  - Admission Limit (core.concurrency│
                        │  - Session Lock (core.session_lock) │
                        │  - ExecutorRegistry (api.executors) │
                        └──────────────────┬──────────────────┘
                                           │
                                           ▼
                        ┌─────────────────────────────────────┐
                        │      adapters/base.py (ABC)         │
                        │      ExecutorAdapter Interface      │
                        └──────────────────┬──────────────────┘
                                           │
                                           ▼
                        ┌─────────────────────────────────────┐
                        │  adapters/antigravity.py (Adapter)  │
                        │  - CLI Command Builder              │
                        │  - 1:1 session_id <-> conversation  │
                        │  - Pre-execution Transient Retries  │
                        │  - Output JSON Parsing & Partial    │
                        │  - Uniform ExecutorResult (Section10│
                        └──────────────────┬──────────────────┘
                                           │
                                           ▼
                        ┌─────────────────────────────────────┐
                        │       core/process.py (Core)        │
                        │  - Isolated Process Group (setsid)  │
                        │  - Process Tree SIGKILL (os.killpg) │
                        └─────────────────────────────────────┘
```

### 3.1 Neutrality & Scope Integrity
* **Core Neutrality**: `core/` remains strictly executor-neutral. AST checks confirm zero provider terms and zero downward dependencies.
* **Upper Layer Neutrality**: `api/executors.py` interacts purely with `ExecutorAdapter` and `ExecutorResult`, containing zero provider concepts (`conversation_id`, CLI flags).

---

## 4. Test Suite Coverage & Verification Results

### 4.1 Test Summary
A new test suite `tests/test_executor_api.py` contains 27 unit and integration tests covering all Phase 2 requirements:

| Test Group | Coverage Scope |
| :--- | :--- |
| Registry Unit (`test_01` - `test_02`) | Registry lifecycle, lookup, registration type checks, comprehensive `validate_invoke_request` validation branches. |
| Discovery & Health (`test_03` - `test_06`) | `GET /v1/executors` listing, `GET /v1/executors/agy/health` delegation, 404 for unknown executors. |
| Auth & Transport (`test_07` - `test_08`) | Strict Bearer authentication enforcement (401), 2MB payload limit enforcement (413). |
| Input Validation (`test_09` - `test_13`) | Malformed JSON (400), non-dict JSON roots (400), missing/empty prompt (400), invalid session_id (400), invalid timeout_sec (400), invalid cwd/model/effort (400). |
| Execution & Schema (`test_14` - `test_17`) | New session execution with Section 10 schema, continuation with `--conversation`, partial_success (200), genuine adapter error (500). |
| Timeouts & Failures (`test_18` - `test_19`) | `TimeoutExpired` returning 504 with structured `ExecutorResult`, unexpected internal exception returning 500. |
| Concurrency & Locking (`test_20` - `test_23`) | Same-session concurrency 409 (Generic vs Generic, Generic vs Legacy), shared worker semaphore saturation 429, exception-safe resource release without leaks. |
| Equivalence & Security (`test_24` - `test_25`) | Behavioral equivalence between Legacy ACP and Generic Executor API on identical mock AGY outputs, zero credential/token leakage audit. |

### 4.2 Local Verification Execution

1. **New Test Suite (`tests/test_executor_api.py`)**:
   * Ran 27 tests independently three times: **100% PASS** on every run.
2. **Legacy Root Suite (`test_acp_bridge.py`)**:
   * Ran 18 tests: **100% PASS**.
3. **Legacy compatibility suite (`tests.test_legacy_compatibility`)**:
   * Ran 38 tests: **100% PASS**.
4. **Core + AGY adapter suites (`tests.test_core tests.test_agy_adapter`)**:
   * Ran 39 tests: **100% PASS**.
5. **Full Combined Discovery (`python3 -m unittest discover -s .`)**:
   * Total 122 tests: **100% PASS** (zero regressions across the Phase 0/1 baseline and the new API suite).
6. **Bytecode Compilation (`python3 -m compileall -q acp_server.py api core adapters tests test_acp_bridge.py`)**:
   * Exited with code `0`.
7. **Whitespace & Git Diff Check (`git diff --check`)**:
   * Exited with code `0` (clean, no trailing whitespace or merge conflict markers).
8. **Core Neutrality Verification (`tests.test_core.TestCoreNeutrality`)**:
   * Passed AST inspection verifying zero provider terms in `core/`.

### 4.3 Isolated HTTP smoke test

Codex started the candidate on temporary loopback port `28991` with a temporary 0600 token file, leaving production port `8765` untouched. The smoke log is recorded in [`docs/PHASE-2-SMOKE-TEST-LOG.md`](./PHASE-2-SMOKE-TEST-LOG.md).

* `GET /health` → HTTP 200, online response, expected limits.
* `GET /v1/executors` → HTTP 200, exactly the registered `agy` executor.
* Unauthenticated `POST /v1/executors/agy/invoke` → HTTP 401.
* Authenticated request with `timeout_sec: 0` → HTTP 400.
* SIGINT shutdown → clean `serve_forever()` exit and thread-pool shutdown.

---

## 5. Known Edge Cases & Remaining Risks

### 5.1 Supervisor 65s Lifecycle Mismatch (Preserved Legacy Risk)
* **Status**: Tracked from Phase 0.
* **Detail**: `ensure_acp_bridge.sh` waits 65s after `SIGTERM` before sending `SIGKILL`. Default process budget is 330s.
* **Preservation Justification**: Per Phase 2 instructions, no changes were made to the supervisor script or lifecycle defaults in this phase.

### 5.2 Deployment portability and HTTP hardening (deferred)

* AGY and token defaults remain container-oriented (`AGY_BIN` and `ACP_TOKEN_FILE` environment overrides are supported). Making every deployment path configuration-file driven is deferred to a later hardening phase.
* The server remains on the standard-library threaded HTTP implementation. The current Phase 2 limits and socket timeout are tested, but an ASGI migration, IP-level rate limiting, and narrower CORS policy are intentionally out of scope.

---

## 6. Scope Discipline — What Was Not Done

In strict adherence to Phase 2 guidelines:
1. **No Grok Implementation**: Zero Grok adapter code, runtime detection, or fake registry entries were introduced.
2. **No Multi-Executor Concurrency / Task DAG**: No complex scheduler, task graph, or router was added.
3. **No Production Gateway Cutover**: No signals, restarts, or deployment changes were made to running services.
4. **No Bridge Mutation**: `/workspace/antigravity-rest-bridge` was not modified.

---

## 7. Status & Handoff

The Phase 2 candidate implementation is independently verified and ready for the requested commit/push. Phase 3 and later work remains out of scope.
