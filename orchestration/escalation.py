"""
Escalation and State Machine Module for Agent Executor Gateway (Phase 7).

Implements Goal Prompt Sections 26, 27, 49:
- Multi-attempt retry loop with same-executor self-repair (max_same_executor_attempts, default 2)
- Multi-executor escalation (e.g. AGY -> Grok) up to max_executor_switches (default 1)
- Strict loop prevention (max_replans = 1) triggering REPLAN_REQUIRED
- Structured and redacted Escalation Context passing (Section 27)
- Serializable and auditable decision / history / state tracking
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from orchestration.scope import get_git_changed_and_untracked_files
from orchestration.task import Task
from orchestration.verifier import TaskVerificationResult, redact_sensitive_text

DEFAULT_MAX_SAME_ATTEMPTS = 2
DEFAULT_MAX_SWITCHES = 1
DEFAULT_MAX_REPLANS = 1
MAX_DIFF_CHARS = 10000


@dataclass(frozen=True)
class AttemptRecord:
    """Audit record for an individual executor turn attempt."""
    attempt_number: int
    executor: str
    timestamp: str
    status: str  # "passed", "failed", "timed_out", "scope_violation"
    summary: str
    failure_output: str
    changed_files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttemptRecord:
        return cls(
            attempt_number=int(data.get("attempt_number", 1)),
            executor=str(data.get("executor", "agy")),
            timestamp=str(data.get("timestamp", "")),
            status=str(data.get("status", "failed")),
            summary=str(data.get("summary", "")),
            failure_output=str(data.get("failure_output", "")),
            changed_files=list(data.get("changed_files", [])),
        )


@dataclass
class EscalationState:
    """State tracking for a task's lifecycle across attempts and executor transitions."""
    task_id: str
    current_executor: str
    current_attempt: int = 1
    total_attempts: int = 0
    switches_used: int = 0
    replans_used: int = 0
    status: str = "in_progress"  # "in_progress", "completed", "escalated", "replan_required", "failed"
    history: list[AttemptRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "current_executor": self.current_executor,
            "current_attempt": self.current_attempt,
            "total_attempts": self.total_attempts,
            "switches_used": self.switches_used,
            "replans_used": self.replans_used,
            "status": self.status,
            "history": [a.to_dict() for a in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EscalationState:
        history_list = [AttemptRecord.from_dict(h) for h in data.get("history", [])]
        return cls(
            task_id=str(data.get("task_id", "")),
            current_executor=str(data.get("current_executor", "agy")),
            current_attempt=int(data.get("current_attempt", 1)),
            total_attempts=int(data.get("total_attempts", 0)),
            switches_used=int(data.get("switches_used", 0)),
            replans_used=int(data.get("replans_used", 0)),
            status=str(data.get("status", "in_progress")),
            history=history_list,
        )


@dataclass(frozen=True)
class EscalationContext:
    """
    Structured context required when escalating or handing off a task (Section 27).
    All sensitive values are redacted.
    """
    original_goal: str
    acceptance_criteria: list[str]
    base_commit: str
    current_git_diff: str
    changed_files: list[str]
    verification_commands: list[str]
    failure_output: str
    previous_executor_summary: str
    previous_attempts: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        # Context can be assembled from user/task-controlled strings.  Redact
        # recursively at the serialization boundary so callers cannot
        # accidentally persist a credential embedded in a goal, command, or
        # attempt summary.
        def _redact(value: Any) -> Any:
            if isinstance(value, str):
                return redact_sensitive_text(value)
            if isinstance(value, list):
                return [_redact(item) for item in value]
            if isinstance(value, dict):
                return {key: _redact(item) for key, item in value.items()}
            return value

        return _redact(asdict(self))

    def to_prompt(self, target_executor: str = "grok") -> str:
        """
        Format escalation context into a structured instruction prompt for the incoming executor.
        Instructs the executor to take over existing changes rather than rewrite from scratch.
        """
        serialized = self.to_dict()
        acc_text = "\n".join(f"- {c}" for c in serialized["acceptance_criteria"])
        cmd_text = "\n".join(f"`{cmd}`" for cmd in serialized["verification_commands"])
        files_text = (
            "\n".join(f"- {f}" for f in serialized["changed_files"])
            if serialized["changed_files"]
            else "(No files modified yet)"
        )

        diff_section = ""
        if serialized["current_git_diff"].strip():
            diff_section = (
                f"\n### Current Git Diff (Base Commit: {serialized['base_commit']})\n"
                f"```diff\n{serialized['current_git_diff']}\n```\n"
            )

        failure_section = ""
        if serialized["failure_output"].strip():
            failure_section = (
                f"\n### Previous Verification Failures\n```text\n"
                f"{serialized['failure_output']}\n```\n"
            )

        attempts_section = ""
        if serialized["previous_attempts"]:
            att_lines = []
            for a in serialized["previous_attempts"]:
                num = a.get("attempt_number", "?")
                exc = a.get("executor", "?")
                st = a.get("status", "?")
                summ = a.get("summary", "")
                att_lines.append(f"- Attempt #{num} ({exc}): status={st} | {summ}")
            attempts_section = f"\n### Previous Attempts History\n" + "\n".join(att_lines) + "\n"

        prompt = f"""## Task Escalation Handover ({target_executor.upper()})

### Original Goal
{serialized['original_goal']}

### Acceptance Criteria
{acc_text}

### Modified Files So Far
{files_text}
{diff_section}{failure_section}{attempts_section}
### Required Verification Commands
{cmd_text}

### Previous Executor Summary
{serialized['previous_executor_summary']}

### Instructions for Handover
1. **Take over the existing implementation**: Build upon the current changes in the repository rather than rewriting from scratch.
2. **Diagnose and resolve failures**: Address the specific verification and test errors detailed above.
3. **Verify against acceptance criteria**: Ensure all acceptance criteria pass and verification commands succeed before finishing.
"""
        return prompt.strip()


@dataclass(frozen=True)
class EscalationDecision:
    """Result of evaluating an attempt against escalation policies."""
    action: str  # "retry_same_executor", "switch_executor", "replan_required", "completed", "failed"
    next_executor: str | None
    reason: str
    state: EscalationState
    context: EscalationContext | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "next_executor": self.next_executor,
            "reason": self.reason,
            "state": self.state.to_dict(),
            "context": self.context.to_dict() if self.context else None,
        }


def init_escalation_state(task: Task, initial_executor: str | None = None) -> EscalationState:
    """Initialize a fresh EscalationState for a task."""
    executor = initial_executor or task.execution.executor
    return EscalationState(
        task_id=task.task_id,
        current_executor=executor,
        current_attempt=1,
        total_attempts=0,
        switches_used=0,
        replans_used=0,
        status="in_progress",
        history=[],
    )


def get_git_diff_text(
    repo_path: str,
    base_commit: str | None = None,
    max_chars: int = MAX_DIFF_CHARS,
) -> str:
    """Retrieve sanitized git diff text against base_commit or working tree changes."""
    if not os.path.exists(repo_path):
        return ""

    def _run_git(args: list[str]) -> str:
        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0:
                return res.stdout
            return ""
        except Exception:
            return ""

    diff_out = ""
    if base_commit:
        diff_out = _run_git(["diff", base_commit])
    if not diff_out.strip():
        unstaged = _run_git(["diff"])
        staged = _run_git(["diff", "--cached"])
        diff_out = unstaged + ("\n" + staged if staged else "")

    sanitized = redact_sensitive_text(diff_out)
    if len(sanitized) > max_chars:
        sanitized = sanitized[:max_chars] + "\n... [diff truncated for length]"
    return sanitized


def build_escalation_context(
    task: Task,
    state: EscalationState,
    last_verification: TaskVerificationResult | None = None,
    repo_path: str | None = None,
    previous_executor: str | None = None,
    previous_attempts_count: int | None = None,
) -> EscalationContext:
    """Construct structured Section 27 Escalation Context with sanitized diff and failure logs."""
    effective_repo = repo_path or task.repository.path
    base_commit = task.repository.base_commit

    git_diff = get_git_diff_text(effective_repo, base_commit=base_commit)
    changed_files = get_git_changed_and_untracked_files(effective_repo, base_commit=base_commit)

    failure_output = ""
    if last_verification:
        failed_cmds = [c for c in last_verification.command_results if c.status != "passed"]
        failures = []
        if not last_verification.scope_result.passed:
            failures.append(f"Scope Violation: {last_verification.scope_result.details}")
        for fc in failed_cmds:
            failures.append(f"Command '{fc.command}' failed (exit {fc.exit_code}):\n{fc.relevant_tail}")
        failure_output = redact_sensitive_text("\n\n".join(failures))

    eff_prev_executor = previous_executor or (state.history[-1].executor if state.history else state.current_executor)
    eff_prev_attempts = (
        previous_attempts_count
        if previous_attempts_count is not None
        else (sum(1 for a in state.history if a.executor == eff_prev_executor) or state.current_attempt)
    )

    prev_summary = f"Executor '{eff_prev_executor}' executed {eff_prev_attempts} attempt(s)."
    if state.history:
        prev_summary += f" Last attempt status: {state.history[-1].status} ({state.history[-1].summary})."

    attempts_data = [a.to_dict() for a in state.history]

    return EscalationContext(
        original_goal=task.goal,
        acceptance_criteria=list(task.acceptance),
        base_commit=base_commit,
        current_git_diff=git_diff,
        changed_files=changed_files,
        verification_commands=list(task.verification.commands),
        failure_output=failure_output,
        previous_executor_summary=redact_sensitive_text(prev_summary),
        previous_attempts=attempts_data,
    )


def evaluate_escalation(
    task: Task,
    state: EscalationState,
    verification: TaskVerificationResult,
    repo_path: str | None = None,
) -> EscalationDecision:
    """
    Evaluate verification outcome against state and compute the next escalation step.

    Rules enforced:
    1. If verification passed -> completed.
    2. If verification failed and current_attempt < max_same_executor_attempts -> retry same executor.
    3. If current_attempt >= max_same_executor_attempts and switches_used < max_executor_switches -> switch executor.
    4. If switches_used >= max_executor_switches -> trigger REPLAN_REQUIRED (max_replans=1).
    5. Disallows infinite loops by bounding switches and replans.
    """
    effective_repo = repo_path or task.repository.path
    timestamp = datetime.now(timezone.utc).isoformat()
    state.total_attempts += 1

    changed_files = get_git_changed_and_untracked_files(
        effective_repo, base_commit=task.repository.base_commit
    )

    # 1. Verification passed: success!
    if verification.status == "passed":
        record = AttemptRecord(
            attempt_number=state.total_attempts,
            executor=state.current_executor,
            timestamp=timestamp,
            status="passed",
            summary=verification.summary,
            failure_output="",
            changed_files=changed_files,
        )
        state.history.append(record)
        state.status = "completed"
        return EscalationDecision(
            action="completed",
            next_executor=None,
            reason="Verification passed successfully on all commands and scope checks",
            state=state,
            context=None,
        )

    # 2. Verification failed: build failure record
    failure_details = []
    if not verification.scope_result.passed:
        failure_details.append(f"Scope Violation: {verification.scope_result.details}")
    for cr in verification.command_results:
        if cr.status != "passed":
            failure_details.append(f"{cr.command}: {cr.summary}")

    failure_text = redact_sensitive_text("\n".join(failure_details))
    record = AttemptRecord(
        attempt_number=state.total_attempts,
        executor=state.current_executor,
        timestamp=timestamp,
        status=verification.reason or "failed",
        summary=verification.summary,
        failure_output=failure_text,
        changed_files=changed_files,
    )
    state.history.append(record)

    max_same_attempts = task.execution.max_same_executor_attempts or DEFAULT_MAX_SAME_ATTEMPTS
    max_switches = task.execution.max_executor_switches
    if max_switches is None:
        max_switches = DEFAULT_MAX_SWITCHES
    max_replans = DEFAULT_MAX_REPLANS

    # Check same-executor retry budget
    if state.current_attempt < max_same_attempts:
        state.current_attempt += 1
        state.status = "in_progress"
        return EscalationDecision(
            action="retry_same_executor",
            next_executor=state.current_executor,
            reason=f"Attempt {state.current_attempt - 1}/{max_same_attempts} failed on '{state.current_executor}'. Self-repair retry attempt {state.current_attempt} permitted.",
            state=state,
            context=None,
        )

    # Same executor attempts exhausted; check if executor switch is permitted
    if state.switches_used < max_switches:
        # Determine target fallback executor
        if state.current_executor == "agy":
            target_executor = task.execution.fallback_executor or "grok"
        else:
            target_executor = task.execution.fallback_executor or "agy"

        # Check that target is actually a switch
        if target_executor != state.current_executor:
            prev_executor = state.current_executor
            prev_attempts_count = state.current_attempt
            state.switches_used += 1
            state.current_executor = target_executor
            state.current_attempt = 1
            state.status = "escalated"

            context = build_escalation_context(
                task=task,
                state=state,
                last_verification=verification,
                repo_path=effective_repo,
                previous_executor=prev_executor,
                previous_attempts_count=prev_attempts_count,
            )

            return EscalationDecision(
                action="switch_executor",
                next_executor=target_executor,
                reason=f"Max same-executor attempts ({max_same_attempts}) exhausted on '{prev_executor}'. Escalating to '{target_executor}' (switch {state.switches_used}/{max_switches}).",
                state=state,
                context=context,
            )

    # Switches exhausted; check replan budget
    if state.replans_used < max_replans:
        prev_executor = state.current_executor
        prev_attempts_count = state.current_attempt
        state.replans_used += 1
        state.status = "replan_required"
        context = build_escalation_context(
            task=task,
            state=state,
            last_verification=verification,
            repo_path=effective_repo,
            previous_executor=prev_executor,
            previous_attempts_count=prev_attempts_count,
        )
        return EscalationDecision(
            action="replan_required",
            next_executor=None,
            reason=f"Max executor switches ({max_switches}) reached and verification failed. REPLAN_REQUIRED for Codex architectural intervention.",
            state=state,
            context=context,
        )

    # All budgets fully exhausted
    state.status = "failed"
    return EscalationDecision(
        action="failed",
        next_executor=None,
        reason=f"All attempts, switches ({max_switches}), and replan budgets fully exhausted. Task execution terminated with status FAILED.",
        state=state,
        context=None,
    )
