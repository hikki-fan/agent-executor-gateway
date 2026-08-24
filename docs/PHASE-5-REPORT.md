# Phase 5 Report — Unified Concurrency
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 5 — Unified Concurrency`
* **Lifecycle Status**: `Phase 5 Candidate — Agy implementation reviewed and independently verified by Codex; acceptance commit pending at report authoring time`
* **Baseline Commit**: `9e522c62e96ee8d9caed5357946ecb71c94aa4c8` (`feat: add grok executor adapter`)
* **Upstream Reference**: `/workspace/antigravity-rest-bridge` (Unmodified, Clean, Zero changes)
* **Scope Discipline**: Phase 5 strictly implements multi-executor concurrency controls per Goal Prompt Section 47. Task DAGs, complexity routing, and production gateway cutover remain strictly out-of-scope.

---

## 2. Implementation Summary

### 2.1 Concurrency Architecture (`core/concurrency.py`, `core/config.py`)
* **Global Gateway Concurrency Semaphore**:
  * Configured via `GATEWAY_MAX_CONCURRENCY` (default `2`).
  * Enforces an upper bound on total active executor turns running simultaneously across all providers.
* **Per-Executor Concurrency Semaphores**:
  * `AGY_MAX_CONCURRENCY` (default `1`) controls concurrent Antigravity turns.
  * `GROK_MAX_CONCURRENCY` (default `1`) controls concurrent Grok turns.
* **Hierarchical Permit Management**:
  * `AdmissionController.acquire_execution_permits(executor, blocking=False)`: Atomically acquires both the global gateway permit and the provider-specific permit. If the provider limit is saturated, the acquired gateway permit is immediately rolled back to prevent resource leaks.
  * `AdmissionController.release_execution_permits(executor)`: Safely releases both provider-specific and gateway permits in a `finally` block.
* **Per-Session Concurrency Lock (`core/session_lock.py`)**:
  * Partitioned by `(executor, session_id)`.
  * Protects active sessions on the same executor with `HTTP 409 Conflict`.
  * Ensures independent executors (e.g. AGY and Grok) sharing identical session IDs do not collide.

### 2.2 Server Integration (`acp_server.py`)
* Wired unified concurrency admission into both Generic (`/v1/executors/{name}/invoke`) and Legacy (`/acp/v1/*`) endpoints.
* Replaced monolithic single-semaphore with `admission_controller.acquire_execution_permits()` and exception-safe `release_execution_permits()`.
* Configured `ThreadPoolExecutor` with `max_workers = max(GATEWAY_MAX_CONCURRENCY * 2, 8)` to eliminate thread starvation during parallel multi-executor turns.
* Updated `/health` endpoint `limits` to report `gateway_max_concurrency`, `agy_max_concurrency`, and `grok_max_concurrency`.

---

## 3. Concurrency Hierarchy Diagram

```text
                     [ Client HTTP Invocations ]
                                  │
                                  ▼
                ┌───────────────────────────────────┐
                │     Gateway Semaphore (429)       │
                │  GATEWAY_MAX_CONCURRENCY (def: 2) │
                └─────────────────┬─────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │                                 │
                 ▼                                 ▼
   ┌───────────────────────────┐     ┌───────────────────────────┐
   │     AGY Semaphore (429)   │     │    Grok Semaphore (429)   │
   │ AGY_MAX_CONCURRENCY (def:1│     │GROK_MAX_CONCURRENCY (def:1│
   └─────────────┬─────────────┘     └─────────────┬─────────────┘
                 │                                 │
                 ▼                                 ▼
   ┌───────────────────────────┐     ┌───────────────────────────┐
   │ (agy, session_id) (409)   │     │ (grok, session_id) (409)  │
   └─────────────┬─────────────┘     └─────────────┬─────────────┘
                 │                                 │
                 ▼                                 ▼
         Antigravity Worker                   Grok Worker
```

---

## 4. Test Suite Coverage & Verification Results

### 4.1 Concurrency Test Suite (`tests/test_concurrency.py`)

| Test Case | Scenario | Expected | Result |
| :--- | :--- | :---: | :---: |
| `test_01` | AGY × 1 + Grok × 1 parallel execution | HTTP 200 (Both) | **PASS** |
| `test_02` | AGY × 2 concurrent turns (limit=1) | HTTP 429 | **PASS** |
| `test_03` | Grok × 2 concurrent turns (limit=1) | HTTP 429 | **PASS** |
| `test_04` | Same AGY session × 2 concurrent turns | HTTP 409 | **PASS** |
| `test_05` | Same Grok session × 2 concurrent turns | HTTP 409 | **PASS** |
| `test_06` | Cross-executor same session_id | HTTP 200 (No collision) | **PASS** |
| `test_07` | Gateway saturation (2 active, 3rd arrives) | HTTP 429 | **PASS** |
| `test_08` | Exception-safe permit & lock release | No leaks | **PASS** |
| `test_09` | Cross-API (Legacy AGY + Generic Grok) | HTTP 200 (Both) | **PASS** |
| `test_10` | `/health` reports concurrency limits | Accurate values | **PASS** |

### 4.2 Overall Repository Test Summary

* Total tests in repository: **154 tests** (143 pass, 1 opt-in live test skipped during normal discovery)
* `tests/test_concurrency.py`: 10 tests (**100% PASS**)
* `tests/test_grok_adapter.py`: 22 tests (21 deterministic pass, 1 opt-in live smoke skipped in normal discovery)
* `tests/test_executor_api.py`: 27 tests (**100% PASS**)
* `tests/test_legacy_compatibility.py`: 38 tests (**100% PASS**)
* `tests/test_agy_adapter.py`: 20 tests (**100% PASS**)
* `tests/test_core.py`: 19 tests (**100% PASS**)
* `test_acp_bridge.py`: 18 tests (**100% PASS**)
* Zero regressions across all legacy and generic contracts.
* Clean AST inspection verifying `core/` contains no provider-specific terms.
* `/workspace/antigravity-rest-bridge` remains completely clean and untouched.

The acceptance commit is intentionally left for Codex after this independent review; this report records the candidate state before that commit.
