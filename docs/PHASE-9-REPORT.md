# Phase 9 Report — Migration Candidate
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 9 — Migration Candidate`
* **Lifecycle Status**: `Phase 9 Candidate — Codex Independently Validated; Production Cutover Deferred`
* **Baseline Commit**: `04258e9256824b7e764743f4365bc9a5be115ad3` (short `04258e9`)
* **Upstream Reference**: `/workspace/antigravity-rest-bridge` (Unmodified, Clean, Zero changes)
* **Scope Discipline**: Phase 9 implements candidate server lifecycle controls (`scripts/migration_candidate.sh`), isolated candidate configuration on port `8766`, regression verification matrices for AGY and Grok runtimes (including `cwd` / `--add-dir` workspace scoping, persistent background reaper process wrapper, and stdin detachment), and opt-in live smoke harnesses (`tests/test_phase9_migration.py`). Production traffic cutover (Phase 10) and legacy bridge retirement (Phase 11) remain strictly future phases.

---

## 2. Dual-Port Topology & Rollback Guarantee

### 2.1 Coexistence Architecture (Section 51)

```
                       [ Codex / Production Clients ]
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │  antigravity-rest-bridge  │
                        │       PORT :8765          │ (Active Production, Unmodified)
                        └───────────────────────────┘

                        ┌───────────────────────────┐
                        │  agent-executor-gateway   │
                        │       PORT :8766          │ (Migration Candidate)
                        └───────────────────────────┘
```

* **Production Protection**: The active bridge (`antigravity-rest-bridge`) continues serving all production tasks on port `8765` completely untouched.
* **Candidate Port**: `agent-executor-gateway` runs as a candidate deployment on port `8766` (overridable via `ACP_PORT`).
* **Instantaneous Rollback Window**: If any unexpected regression is observed on candidate port `8766`, stopping the candidate instance has zero impact on active production on port `8765`.

---

## 3. Architecture & Candidate Controls

### 3.1 Production-Grade Helper Script (`scripts/migration_candidate.sh`)
* **Singleton Lock Protection**: All operations (`start`, `status`, `stop`, `restart`) run under non-blocking exclusive flock (`candidate.lock`, permissions `0600`). Spawning closes lock FD 200 (`200>&-` and `os.close(200)`) so daemon/reaper processes never inherit the script-level lock.
* **Persistent Background Reaper Process Wrapper**:
  - `cmd_start` spawns a persistent detached background Python reaper wrapper (`$PYTHON_BIN -c "..."`) that runs `os.setsid()`, closes lock FD 200, detaches stdin (`subprocess.DEVNULL` / `< /dev/null`), launches the python server in a new session (`start_new_session=True`), writes the real server PID to `$CANDIDATE_PID_FILE`, and executes `proc.wait()`.
  - **Process Hierarchy & Server Reaping**:
    - The server process's `PPid` is strictly the persistent reaper wrapper process (`PPid != 1`).
    - When `cmd_stop` delivers `SIGTERM` to the server process group (`-$candidate_pid`), the server process exits, and `proc.wait()` in the reaper parent immediately reaps the child from the kernel process table. The active server PID disappears from `/proc` (`os.path.exists(f"/proc/{server_pid}") == False`) and port 8766 is released immediately.
  - **Container PID 1 Boundary**:
    - In containerized environments where PID 1 is `tail -f /dev/null` (or a non-subreaper container entrypoint), PID 1 does not invoke `wait()` on orphaned background processes.
    - While the active server process itself is 100% reaped upon exit by the reaper parent, when the reaper wrapper itself terminates, its terminal entry remains in the kernel process table under container PID 1 until container destruction.
* **Interactive Terminal Detachment**: Background daemon/reaper launches explicitly redirect `stdin` from `/dev/null` (`< /dev/null` and `subprocess.DEVNULL`) alongside `setsid` and `200>&-` to decouple from interactive terminal sessions.
* **Strict Process & Zombie Verification**:
  * PID is verified to be a strictly positive integer.
  * `/proc/$pid/exe` must resolve to a valid Python interpreter.
  * NUL-separated `/proc/$pid/cmdline` arguments are parsed to verify exact match with `$SERVER_SCRIPT` or `$SERVER_SCRIPT_REAL` (zero fuzzy fallbacks to arbitrary directories).
  * Defunct/zombie processes (`State: Z` in `/proc/$pid/status` or `stat`) are explicitly recognized:
    - `cmd_status`: reports status as `STOPPED (process <PID> exited; defunct/zombie awaiting reap)` and cleans up the PID file without reporting spurious security errors.
    - `cmd_stop`: detects that the defunct process has already exited, removes the PID file, and returns 0 without attempting signals or false-positive identity change warnings.
    - Graceful shutdown wait loop immediately exits upon detecting zombie state rather than waiting out full timeouts.
  * Signals (`SIGTERM`, `SIGKILL`) are preceded by re-verification of active process identity to prevent PID reuse races.
* **Process Group Isolation (`setsid`)**: Launches under `setsid` in an independent process group. Stopping terminates the candidate process group (`kill -TERM -- "-$pid"`).
* **Symlink and Permission Safeguards**:
  * Rejects symbolic links for PID file, token file, log file, lock file, and runtime directory.
  * Runtime directory `CANDIDATE_RUN_DIR` is strictly maintained at `0700`.
  * `candidate.token`, `candidate.log`, `candidate.pid`, and `candidate.lock` are strictly maintained at `0600`.
* **Port Validation & Health Verification**: Validates port in range `1024..65535`. Probes `http://127.0.0.1:8766/health` verifying JSON contains `"status": "online"` and `"service": "Antigravity REST Bridge Server"`.

### 3.2 Regression Verification & CWD Workspace Scoping
* **AGY Runtime**:
  * **CWD Workspace Scoping**: When `cwd` is specified in `invoke`/`resume`, `AntigravityAdapter.build_command` injects `--add-dir <realpath(cwd)>` preceding `-p` and executes `subprocess` with `cwd=cwd`. This scopes AGY workspace operations directly to the caller's target working directory rather than defaulting to `~/.gemini/antigravity-cli/scratch`.
  * Verified session UUID generation, multi-turn continuation, 0-turn EOF retry, `partial_success` preservation, process-group timeout enforcement (`SIGKILL via os.killpg`), and single-session concurrency mutex (`HTTP 409 Conflict`).
* **Grok Runtime**: Verified headless `--output-format json` invocation, `--resume` session continuation, custom `cwd` execution, timeout budgeting, and Grok semaphore concurrency limits.
* **Routing & Escalation**: Verified Small/Medium feature routing to AGY, Medium debug routing to Grok, AGY multi-attempt failure escalation to Grok, and concurrent multi-task execution across isolated worktrees.

---

## 4. Verification & Test Suite Results

### 4.1 Test Suite Breakdown

```bash
python3 -m unittest discover -s . -v
```

| Suite | Tests | Result | Focus |
| :--- | :---: | :---: | :--- |
| `tests/test_phase9_migration.py` | 21 | 19 Passed, 2 Skipped | Candidate port defaults & env isolation, candidate script start/status/stop lifecycle, port range validation (1024..65535), symlink rejection on token/PID/log/lock files, forged PID rejection without signal sending, foreign directory `acp_server.py` rejection without signal sending, defunct zombie PID status & stop handling without signal attempts, stdin detachment verification (`< /dev/null`), runtime server PPID verification (`PPid != 1`), server reap without lingering server PID and clean port release, flock singleton lock contention, runtime files permissions (0700 dir, 0600 files), routing & escalation regression matrix, multi-executor concurrency locks, Grok JSON parsing & contract, opt-in live smoke harness (probe & AGY cwd creation) |
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

* **Total Tests in Workspace**: **240 tests** (237 passed, 3 skipped [opt-in live Grok smoke + 2 opt-in Phase 9 live smoke]).
* **Core Neutrality**: AST inspector verified zero provider-specific identifiers in `core/`.
* **Bytecode Compilation**: `python3 -m compileall .` succeeded with exit code 0.
* **Diff Formatting**: `git diff --check` clean with exit code 0.
* **External Repositories**: `/workspace/antigravity-rest-bridge` is 100% clean and untouched.

---

## 5. Opt-in Live Smoke Testing Instructions

To run end-to-end live smoke tests against running AGY and Grok runtime binaries on candidate port 8766:

```bash
# 1. Start the candidate instance on port 8766
./scripts/migration_candidate.sh start

# 2. Execute live smoke tests (including health probe and real AGY cwd file creation)
RUN_PHASE9_LIVE=1 python3 -m unittest tests.test_phase9_migration.TestPhase9LiveSmokeHarness -v

# 3. Stop the candidate instance
./scripts/migration_candidate.sh stop
```

---

## 6. Preserved Constraints & Future Phases

1. **Phase 10 — Production Cutover**: Switching production port 8765 to `agent-executor-gateway` remains deferred to Phase 10 after full Codex review and sign-off.
2. **Phase 11 — Retire Old Bridge**: Decommissioning `/workspace/antigravity-rest-bridge` remains deferred to Phase 11.
3. **Candidate Status**: Phase 9 implementation is complete and independently validated by Codex. Production cutover remains deferred pending explicit Phase 10 authorization; the candidate and rollback window remain available.
