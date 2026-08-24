# Phase 7 Report — Routing + Escalation
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 7 — Routing + Escalation`
* **Lifecycle Status**: `Phase 7 Candidate — Implementation Complete, Awaiting Codex Independent Review`
* **Baseline Commit**: `dd7e500af039d54c16b961e922d82ba95acedf7d`
* **Upstream Reference**: `/workspace/antigravity-rest-bridge` (Unmodified, Clean, Zero changes)
* **Scope Discipline**: Phase 7 implements deterministic rule-based task routing (`orchestration/router.py`), multi-attempt and multi-executor escalation state machine (`orchestration/escalation.py`), structured and credential-redacted handover context generation (`EscalationContext`), and `agentctl task route` / `agentctl task plan` CLI subcommands. Worktree isolation (Phase 8), autonomous DAG execution (`task run`), and production cutover remain strictly future phases.

---

## 2. Architecture & Design

### 2.1 Rule-Based Router (`orchestration/router.py`)
* Implements deterministic routing per Goal Prompt Sections 22 & 49:
  * **S (Small) Tasks**: Low or high risk -> `agy` (high/critical flagged for review).
  * **M (Medium) Tasks**:
    * `type in ("feature", "bugfix", "refactor", "test", "config", "migration", "architecture")` -> `agy`
    * `type in ("debug", "investigation")` -> `grok`
  * **L / XL (Large / Extra-Large) Tasks**:
    * Automatic execution is blocked (`status: "override_required"`, `executor: null`).
    * Returns explicit `requires_human_review = True` and requires manual decomposition or explicit Codex override.
  * **Explicit Override Priority**:
    * Any explicit `executor_override` (e.g. `--override grok` or `--override agy`) takes absolute precedence over automatic rules.
    * Target executor is strictly validated against `ALLOWED_EXECUTORS` (`"agy"`, `"grok"`); invalid overrides return `status: "invalid_override"` and exit non-zero.

### 2.2 Escalation State Machine (`orchestration/escalation.py`)
* **Bounded Turn Attempts & Self-Repair**:
  * Tracks `current_attempt` (attempts on current executor) against `max_same_executor_attempts` (default `2`).
  * On failure: if `current_attempt < max_same_executor_attempts`, allows same-executor self-repair (`action: "retry_same_executor"`).
* **Executor Escalation & Loop Prevention**:
  * If `current_attempt >= max_same_executor_attempts`:
    * If `switches_used < max_executor_switches` (default `1`): switches executor (e.g. `agy -> grok`) and resets `current_attempt = 1` (`action: "switch_executor"`).
    * If `switches_used >= max_executor_switches`: transitions to `REPLAN_REQUIRED` (`action: "replan_required"`, `max_replans = 1`) for Codex architectural intervention.
  * **Infinite Loop Prevention**: Strict bounds on `max_executor_switches` and `max_replans` forbid `AGY <-> Grok` infinite switching loops.
* **Auditability & Serialization**:
  * `EscalationState` and `AttemptRecord` support lossless JSON serialization (`to_dict()` / `from_dict()`) capturing attempt counts, timestamps, statuses, failure outputs, and changed files.

### 2.3 Structured Handover Context & Redaction
* Implements Goal Prompt Section 27 handover protocol:
  * `original_goal`: original user requirement.
  * `acceptance_criteria`: list of criteria.
  * `base_commit`: git baseline hash.
  * `current_git_diff`: working tree / committed diff against `base_commit`.
  * `changed_files`: modified and untracked files discovered via Git.
  * `verification_commands`: declared test commands.
  * `failure_output`: sanitized tail / summary of verification failures.
  * `previous_executor_summary`: summary accurately attributing the failed previous executor (e.g. `agy`) and its attempt count prior to handover.
  * `previous_attempts`: list of structured attempt history records.
* **Sensitive Token Redaction**:
  * All diffs, failure outputs, and summaries are automatically sanitized via `redact_sensitive_text` (`Bearer <TOKEN>`, keys, passwords -> `[REDACTED]`).
* **Handover Prompt Generator (`to_prompt()`)**:
  * Formats instructions directing incoming executor (e.g. Grok) to take over existing implementation, including the full `Previous Attempts History`, rather than starting from scratch.

### 2.4 `agentctl` CLI Additions (`./agentctl`)
* `agentctl task route TASK.json [--override EXECUTOR] [--json]`: Evaluates routing rules and prints decision (exit 0 on routed, exit 1 if manual override required or invalid override).
* `agentctl task plan TASK.json [--override EXECUTOR] [--json]`: Displays full lifecycle execution plan including primary executor, fallback chain, attempt budgets, scope bounds, and verification commands.
* `agentctl executors`: Lists registered executors (`name`, `available`, `supports_session`).
* `agentctl health`: Probes server and executor health statuses.
* Preserves all existing `validate`, `verify`, `executors`, `health`, and `invoke` subcommands without regression.

---

## 3. Verification & Test Coverage Results

### 3.1 Test Suite Breakdown

```bash
python3 -m unittest discover -s . -v
```

| Suite | Tests | Result | Focus |
| :--- | :---: | :---: | :--- |
| `tests/test_routing_escalation.py` | 14 | 14 Passed | Section 22 rule router, S/M/L/XL routing, override priority, multi-attempt escalation lifecycle, loop prevention, context redaction, previous executor attribution, `agentctl task route`/`plan` CLI |
| `tests/test_task_verification.py` | 28 | 28 Passed | Task schema, verifier, scope control, reports, metrics, agentctl CLI |
| `tests/test_concurrency.py` | 10 | 10 Passed | Global gateway & per-executor semaphores, session locks, 409/429 |
| `tests/test_grok_adapter.py` | 22 | 21 Passed, 1 Skipped | GrokBuild runtime adapter, JSON parsing, process groups, opt-in live smoke |
| `tests/test_executor_api.py` | 27 | 27 Passed | Generic Executor API endpoints (`/v1/executors/*`), 1:1 sessions |
| `tests/test_legacy_compatibility.py` | 38 | 38 Passed | Legacy `/acp/v1/*` contracts, 0-turn EOF retries, partial-success |
| `tests/test_agy_adapter.py` | 20 | 20 Passed | Antigravity adapter unit and integration tests |
| `tests/test_core.py` | 19 | 19 Passed | Core neutrality (AST checked), data models, deadline timer, process groups |
| `test_acp_bridge.py` | 18 | 18 Passed | Root legacy ACP bridge tests |

* **Total Tests in Workspace**: **196 tests** (195 passed, 1 skipped [opt-in live Grok smoke]).
* **Core Neutrality**: AST inspector verified zero provider-specific identifiers in `core/`.
* **Bytecode Compilation**: `python3 -m compileall .` succeeded with exit code 0.
* **Diff Formatting**: `git diff --check` clean with exit code 0.
* **External Repositories**: `/workspace/antigravity-rest-bridge` is 100% clean and untouched.

---

## 4. Known Preserved Constraints & Status

1. **Phase 8 Worktree Isolation**: Autonomous worktree branching and multi-task workspace cloning remain deferred to Phase 8.
2. **Autonomous DAG Execution**: `agentctl task run` is intentionally rejected in Phase 7 per Goal Prompt design.
3. **Candidate Status**: All modifications reside cleanly in the working tree awaiting Codex independent review and sign-off.
