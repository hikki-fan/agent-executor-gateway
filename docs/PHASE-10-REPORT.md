# Phase 10 Report — Production Migration Tooling & Preflight Infrastructure
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Status

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 10 — Production Migration Tooling & Preflight Infrastructure`
* **Lifecycle Status**: `Phase 10 Candidate Tooling Independently Validated and Committed — Production Cutover Authorization Pending`
* **Production Status**: **NOT MIGRATED (Dry-Run / Preflight Tooling Mode)**
  * Active legacy production bridge on port `8765` is **100% ONLINE, ACTIVE, and UNTOUCHED**.
  * The legacy watchdog process was independently checked and is currently **INACTIVE**; no watchdog was stopped or modified by this phase.
  * Legacy repository at `/workspace/antigravity-rest-bridge` is **100% CLEAN and UNMODIFIED**.
* **Baseline Commit**: `27c1c0e` (Phase 9 candidate-validation parent); **Phase 10 validation commit**: `65c605e` (pushed to `origin/master`).
* **Deliverables Added in Phase 10**:
  1. [`scripts/migrate_production.sh`](file:///workspace/agent-executor-gateway/scripts/migrate_production.sh): Production migration CLI with mandatory two-factor confirmation gates, watchdog safety overrides, strict NUL `argv[1]` script verification for both server and watchdog, PGID leader verification before group signals, configured runtime path inspections, in-memory syntax verification (zero `__pycache__` writes), fail-closed process resolution, flock concurrency protection, and automated emergency rollback.
  2. [`tests/test_phase10_migration.py`](file:///workspace/agent-executor-gateway/tests/test_phase10_migration.py): Deterministic test suite covering preflight zero mutations (filesystem and `__pycache__`), fail-closed process inspection, two-factor safety refusal gates, exact watchdog path matching, NUL `argv[1]` script path validation for both server and watchdog (rejecting `bash -c ...` and `python -c ...`), PGID leader vs non-leader signaling safety, configured runtime path inspections, rollback refusal on occupied/unknown ports, and sandbox cutover/rollback lifecycle on isolated ports.
  3. [`docs/PHASE-10-REPORT.md`](file:///workspace/agent-executor-gateway/docs/PHASE-10-REPORT.md): This report detailing migration architecture, safety gates, and operational procedures.

---

## 2. Multi-Factor Safety Confirmation Gates

To prevent accidental, unconfirmed, or destructive execution against live production, `scripts/migrate_production.sh` implements strict authorization gates:

```
                                 [ Operator Invocation ]
                                            │
                             ┌──────────────┴──────────────┐
                             │                             │
                     [ Action = cutover ]          [ Action = rollback ]
                             │                             │
              ┌──────────────┴──────────────┐       ┌──────┴──────────────┐
              │ Has CLI --confirm-cutover?  │       │ Has CLI --confirm-? │
              │   AND                       │       │   AND               │
              │ CONFIRM_PRODUCTION_CUTOVER=1│       │ CONFIRM_PRODUCTION_ │
              │   AND (if watchdog active): │       │   ROLLBACK=1        │
              │ --handle-watchdog           │       └──────┬──────────────┘
              │ CONFIRM_WATCHDOG_OVERRIDE=1 │              │
              └──────────────┬──────────────┘              │
                     YES     │      NO                     │ NO
              ┌──────────────┴──────────────┐       ┌──────┴──────────────┐
              │                             │       │                     │
              ▼                             ▼       ▼                     ▼
      [ Preflight & Cutover ]       [ SAFETY REFUSAL: Exit 1, ZERO Changes ]
```

### 2.1 Confirmation Matrix

| Action | Required CLI Flags | Required Environment Variables | Default (Omitted) Behavior |
| :--- | :--- | :--- | :--- |
| **`preflight`** | None (Default) | None | **Strictly Read-Only**: Zero mutations, zero signals, zero lock file creation, zero `__pycache__` disk writes. |
| **`status`** | None | None | **Strictly Read-Only**: Displays port 8765 health, watchdog status, candidate status, and state record. |
| **`cutover`** | `--confirm-cutover`<br>(+ `--handle-watchdog` if watchdog active) | `CONFIRM_PRODUCTION_CUTOVER=1`<br>(+ `CONFIRM_WATCHDOG_OVERRIDE=1` if watchdog active) | **SAFETY REFUSAL**: Exits with code 1; sends no signals, modifies no state. |
| **`rollback`** | `--confirm-rollback` | `CONFIRM_PRODUCTION_ROLLBACK=1` | **SAFETY REFUSAL**: Exits with code 1; sends no signals, modifies no state. |

---

## 3. Preflight & Verification Checklist

Before any cutover is permitted, `scripts/migrate_production.sh preflight` executes a 7-point read-only inspection:

1. **Active Production Port Check**: Probes `http://127.0.0.1:8765/health` to verify that the active service is online and healthy.
2. **Active Process Identity Inspection (Fail-Closed & Exact `argv[1]` Match)**: Inspects `/proc` socket table and process table to verify the process listening on port 8765 is an authentic Python instance with `argv[1]` strictly matching `/workspace/scripts/acp_server.py`. If the process cannot be strictly resolved and verified, preflight **fails closed**.
3. **Active Watchdog Supervisor Detection (Exact `argv[1]` / `argv[0]` Match)**: Inspects `/proc` for running `acp_watchdog.sh` matching `$WATCHDOG_SCRIPT_REAL` at `argv[1]` or `argv[0]`. If detected without explicit override authorization (`--handle-watchdog` + `CONFIRM_WATCHDOG_OVERRIDE=1`), preflight marks cutover as **BLOCKED** to prevent race conditions during cutover.
4. **Legacy Bridge Repository Cleanliness**: Runs `git -C /workspace/antigravity-rest-bridge status --porcelain`. If any unstaged or uncommitted changes exist, cutover is **strictly blocked**.
5. **New Gateway Repository & Syntax Verification (In-Memory)**: Verifies syntax of `/workspace/agent-executor-gateway/acp_server.py` via `PYTHONDONTWRITEBYTECODE=1 compile(open(...).read(), ...)`, guaranteeing zero `__pycache__` writes.
6. **Candidate Deployment Status**: Checks candidate port 8766 status for coexistence readiness.
7. **Runtime Directory & Security Verification**: Read-only inspection of configured runtime paths (`$PROD_LOCK_FILE`, `$PROD_TOKEN_FILE`, `$PROD_PID_FILE`, `$PROD_LOG_FILE`, `$MIGRATION_STATE_FILE`), verifying mode `0700` directory and `0600` files without modifying existing permissions.

---

## 4. Reversible Cutover & Rollback Lifecycle

### 4.1 Production Cutover Sequence
1. **Preflight Gate**: Verifies all 7 preflight checks pass (including watchdog authorization if active).
2. **Pre-Cutover Snapshot**: Records current legacy PID, watchdog PID, git commit hash, port, and timestamp into `migration_state.json` (written atomically with `0600` permissions).
3. **Watchdog Management**: If active and authorized, cleanly stops `acp_watchdog.sh` before server transition.
4. **Graceful Legacy Shutdown**: Signals legacy process safely (verifies `PGID == PID` before group signal, otherwise signals PID only); verifies port 8765 is freed.
5. **New Gateway Launch**: Launches `agent-executor-gateway` on port 8765 using the persistent detached Python reaper wrapper.
6. **Readiness & Strict Identity Probe**: Polls `http://127.0.0.1:8765/health` up to 10 seconds AND verifies `is_acp_server_process` (`argv[1]` check), PGID, and listening socket on port 8765.
7. **Emergency Automated Rollback**: If the new gateway fails health check or strict identity verification within 10s, the script safely signals the candidate gateway, restarts the legacy bridge to `$MIGRATION_RUN_DIR/emergency_rollback.log` (mode `0600`) with explicit `ACP_PORT=PROD_PORT`, and returns an error.
8. **State Recording**: Updates `migration_state.json` to `CUTOVER_COMPLETE`.

### 4.2 Production Rollback Sequence
1. **Confirmation Gate**: Validates `--confirm-rollback` and `CONFIRM_PRODUCTION_ROLLBACK=1`.
2. **Occupied/Unknown Port Rejection**: If the port is occupied by an unverified process or unknown service, rollback strictly **refuses** to kill or restore into an occupied port and exits `1`.
3. **Gateway Verification & Teardown**: Confirms the process on port 8765 is strictly the new gateway (`GATEWAY_SERVER_SCRIPT_REAL` at `argv[1]`) before safely delivering `SIGTERM` (group signal only if PGID leader), verifying process exit.
4. **Legacy Restoration**: Launches legacy bridge from `$OLD_BRIDGE_SERVER_SCRIPT_REAL` with explicit `ACP_PORT=PROD_PORT` logging to `$MIGRATION_RUN_DIR/restored_legacy.log` (mode `0600`).
5. **Health Confirmation**: Polls `http://127.0.0.1:8765/health` to verify legacy service is restored online.
6. **State Update**: Updates `migration_state.json` to `ROLLED_BACK`.

---

## 5. Container PID 1 Lifecycle & Zombie Boundaries

* **Active Server Process**: The active server process (`acp_server.py`) is parented to the persistent Python reaper wrapper (`PPid != 1`). When stopped via `SIGTERM`, the reaper parent calls `proc.wait()` and immediately reaps the server process from the kernel process table. The server PID completely disappears from `/proc` and releases all ports immediately.
* **Container PID 1 Boundary**: In containerized environments where PID 1 is `tail -f /dev/null` (or a non-subreaper container entrypoint), PID 1 does not invoke `wait()` on orphaned background processes. When the reaper wrapper process itself exits following child shutdown, its terminal entry remains in the kernel process table under container PID 1 until container destruction.
* **Preservation**: The legacy repository `/workspace/antigravity-rest-bridge` is never deleted or modified.

---

## 6. Verification & Test Suite Results

```bash
python3 -m unittest discover -s . -v
```

| Suite | Tests | Result | Focus |
| :--- | :---: | :---: | :--- |
| `tests/test_phase10_migration.py` | 23 | 23 Passed | Preflight read-only zero mutations (asserts run dir not created), zero `__pycache__` writes verification (directory snapshot), fail-closed process resolution when legacy PID cannot be strictly verified, `--confirm-cutover` + `CONFIRM_PRODUCTION_CUTOVER=1` safety refusal gates, `--confirm-rollback` + `CONFIRM_PRODUCTION_ROLLBACK=1` safety refusal gates, watchdog supervisor detection & cutover blockage without override, foreign watchdog same-name script rejection, exact `argv[1]` / `argv[0]` watchdog validation (rejecting `bash -c ...`), exact `argv[1]` script path validation (rejecting `python -c ...`), configured artifact path inspections (`PROD_*_FILE`), safe PGID leader vs non-leader signaling verification, rollback refusal on unverified process, rollback refusal on occupied/unknown port, dirty legacy repository detection, invalid port validation, symlink rejection on lock/token files, flock singleton concurrency contention, status state record formatting, end-to-end sandbox cutover and rollback lifecycle on dynamic isolated ports |
| `tests/test_phase9_migration.py` | 21 | 19 Passed, 2 Skipped | Candidate port defaults & env isolation, candidate script start/status/stop lifecycle, port range validation (1024..65535), symlink rejection, forged PID rejection, foreign directory rejection, zombie PID status & stop handling, stdin detachment (`< /dev/null`), runtime server PPID verification (`PPid != 1`), server reap without lingering server PID, flock singleton, permissions (0700 dir, 0600 files), routing & escalation regression matrix, multi-executor concurrency locks, Grok JSON parsing & contract, opt-in live smoke harness (probe & AGY cwd creation) |
| `tests/test_agy_adapter.py` | 23 | 23 Passed | Antigravity adapter unit and integration tests, `--add-dir` workspace flag construction, `cwd` propagation to runner and command line, retry mechanics |
| `tests/test_worktree_dag.py` | 20 | 20 Passed | Worktree creation, base commit checkout, isolation, duplicate rejection, non-destructive branch conflict handling, repo mismatch rejection, root boundary containment, path traversal prevention, cleanup safety for manager worktrees only, non-agent worktree preservation, scope checking, DAG topology & cycle detection, missing dependency blocked handling, concurrent AGY+Grok worktree execution without auto-merge, `agentctl worktree` & `agentctl task ready/graph` CLI |
| `tests/test_routing_escalation.py` | 14 | 14 Passed | Section 22 rule router, S/M/L/XL routing, override priority, multi-attempt escalation lifecycle, loop prevention, context redaction, previous executor attribution, `agentctl task route`/`plan` CLI |
| `tests/test_task_verification.py` | 28 | 28 Passed | Task schema, verifier, scope control, reports, metrics, agentctl CLI |
| `tests/test_concurrency.py` | 10 | 10 Passed | Global gateway & per-executor semaphores, session locks, 409/429 |
| `tests/test_grok_adapter.py` | 22 | 21 Passed, 1 Skipped | GrokBuild runtime adapter, JSON parsing, process groups, opt-in live smoke |
| `tests/test_executor_api.py` | 27 | 27 Passed | Generic Executor API endpoints (`/v1/executors/*`), 1:1 sessions |
| `tests/test_legacy_compatibility.py` | 38 | 38 Passed | Legacy `/acp/v1/*` contracts, 0-turn EOF retries, partial-success |
| `tests/test_core.py` | 19 | 19 Passed | Core neutrality (AST checked), data models, deadline timer, process groups |
| `test_acp_bridge.py` | 18 | 18 Passed | Root legacy ACP bridge tests |

* **Total Tests in Workspace**: **263 tests** (260 passed, 3 skipped [opt-in live Grok smoke + 2 opt-in Phase 9 live smoke]).
* **Core Neutrality**: AST inspector verified zero provider-specific identifiers in `core/`.
* **Bytecode Compilation**: `python3 -m compileall .` succeeded with exit code 0.
* **Diff Formatting**: `git diff --check` clean with exit code 0.
* **Live verification**: port `8765` returned the legacy `Antigravity REST Bridge Server` health payload; legacy PID `119` remained running; candidate port `8766` remained stopped; no active legacy watchdog process was detected.
* **External Repositories**: `/workspace/antigravity-rest-bridge` is 100% clean and untouched.

---

## 7. Future Phases & Next Steps

1. **Production Cutover Execution**: Performing actual production cutover on port 8765 requires explicit manual invocation with `--confirm-cutover` and `CONFIRM_PRODUCTION_CUTOVER=1` following Codex independent review.
2. **Phase 11 — Legacy Bridge Decommissioning & Retirement**: Archiving and decommissioning `/workspace/antigravity-rest-bridge` remains deferred to Phase 11 and requires separate explicit authorization.
