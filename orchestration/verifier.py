"""
Machine Verification Module for Agent Executor Gateway (Phase 6).

Implements Goal Prompt Section 28 & Section 48:
- Executes declared verification commands with shell=False safe parsing.
- Restricts cwd strictly to repository.path.
- Enforces per-command timeout and process-group SIGKILL cleanup.
- Saves full sanitized logs to repository/.agent/logs/.
- Redacts Bearer tokens and sensitive credentials from logs and summaries.
- Returns concise verification summary, exit code, relevant tail, and log path for Codex.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from core.process import run_process_group
from orchestration.scope import ScopeCheckResult, check_scope
from orchestration.task import Task

DEFAULT_COMMAND_TIMEOUT_SEC = 120.0
DEFAULT_TAIL_LINES = 30
MAX_TAIL_CHARS = 4000

# Patterns to redact from logs and summaries
SENSITIVE_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(r"(token[=:\s]+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(r"(password[=:\s]+)[^\s&]+", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{20,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.IGNORECASE),
]


def redact_sensitive_text(text: str) -> str:
    """Redact bearer tokens, API keys, and credentials from log output."""
    if not text:
        return ""
    sanitized = text

    def _replacement(match: re.Match[str]) -> str:
        # Label-based patterns capture a prefix (for example ``Bearer ``),
        # while standalone key patterns such as ``ghp_...`` do not.
        prefix = match.group(1) if match.lastindex else ""
        return f"{prefix}[REDACTED]"

    for pattern in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(_replacement, sanitized)
    return sanitized


def extract_relevant_tail(output: str, max_lines: int = DEFAULT_TAIL_LINES, max_chars: int = MAX_TAIL_CHARS) -> str:
    """Extract relevant tail lines from command output for concise inspection."""
    if not output:
        return ""
    lines = output.splitlines()
    if len(lines) > max_lines:
        tail_lines = lines[-max_lines:]
    else:
        tail_lines = lines
    tail = "\n".join(tail_lines)
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


@dataclass(frozen=True)
class CommandVerificationResult:
    """Result of an individual verification command execution."""
    command: str
    exit_code: int
    status: str  # "passed", "failed", "timed_out"
    summary: str
    relevant_tail: str
    log_path: str
    duration_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaskVerificationResult:
    """Overall verification report for a task."""
    task_id: str
    status: str  # "passed", "failed"
    scope_result: ScopeCheckResult
    command_results: list[CommandVerificationResult]
    reason: str | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "scope": self.scope_result.to_dict(),
            "commands": [c.to_dict() for c in self.command_results],
            "reason": self.reason,
            "summary": self.summary,
        }


def run_command_safe(
    cmd_str: str,
    cwd: str,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    log_dir: str | None = None,
    cmd_index: int = 1,
) -> CommandVerificationResult:
    """
    Execute a single verification command safely:
    - shell=False with shlex.split() to prevent shell injection.
    - cwd bound strictly to repository directory.
    - process group SIGKILL cleanup on timeout.
    - full log written to .agent/logs/.
    """
    if not os.path.exists(cwd):
        raise ValueError(f"Verification working directory does not exist: {cwd}")
    if not os.path.isdir(cwd):
        raise ValueError(f"Verification working directory is not a directory: {cwd}")

    # Safe parsing without shell
    try:
        cmd_args = shlex.split(cmd_str)
    except Exception as e:
        return CommandVerificationResult(
            command=cmd_str,
            exit_code=1,
            status="failed",
            summary=f"Failed to parse command syntax: {e}",
            relevant_tail=f"SyntaxError in command '{cmd_str}': {e}",
            log_path="",
            duration_ms=0,
        )

    if not cmd_args:
        return CommandVerificationResult(
            command=cmd_str,
            exit_code=1,
            status="failed",
            summary="Empty command string",
            relevant_tail="Empty command string",
            log_path="",
            duration_ms=0,
        )

    if log_dir is None:
        log_dir = os.path.join(cwd, ".agent", "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    cmd_slug = re.sub(r"[^A-Za-z0-9_]+", "_", os.path.basename(cmd_args[0]))[:24]
    log_filename = f"verify_{timestamp}_{cmd_index:02d}_{cmd_slug}.log"
    log_path = os.path.join(log_dir, log_filename)

    start_time = time.monotonic()
    timed_out = False
    exit_code = 1
    raw_stdout = ""
    raw_stderr = ""

    try:
        proc = run_process_group(cmd_args, timeout_sec=timeout_sec, cwd=cwd)
        exit_code = proc.returncode
        raw_stdout = proc.stdout or ""
        raw_stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = 124  # Standard timeout exit code
        raw_stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        raw_stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        raw_stderr += f"\n[ERROR] Command timed out after {timeout_sec:.1f}s and was terminated via SIGKILL."
    except Exception as e:
        exit_code = 1
        raw_stderr = f"[ERROR] Execution failed: {e}"

    duration_ms = int((time.monotonic() - start_time) * 1000)

    # Combine and redact output
    combined_output = raw_stdout
    if raw_stderr:
        if combined_output and not combined_output.endswith("\n"):
            combined_output += "\n"
        combined_output += raw_stderr

    sanitized_output = redact_sensitive_text(combined_output)

    # Write log file
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"=== Verification Command Log ===\n")
            f.write(f"Command: {redact_sensitive_text(cmd_str)}\n")
            f.write(f"CWD: {cwd}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Duration: {duration_ms}ms\n")
            f.write(f"Exit Code: {exit_code}\n")
            f.write(f"=== Output ===\n")
            f.write(sanitized_output)
            if not sanitized_output.endswith("\n"):
                f.write("\n")
        os.chmod(log_path, 0o600)
    except Exception as e:
        log_path = f"[Failed to write log: {e}]"

    if timed_out:
        status = "timed_out"
        summary = f"Command timed out after {timeout_sec:.1f}s"
    elif exit_code == 0:
        status = "passed"
        summary = "Command completed successfully (exit code 0)"
    else:
        status = "failed"
        summary = f"Command failed with exit code {exit_code}"

    tail = extract_relevant_tail(sanitized_output)

    return CommandVerificationResult(
        command=cmd_str,
        exit_code=exit_code,
        status=status,
        summary=summary,
        relevant_tail=tail,
        log_path=log_path,
        duration_ms=duration_ms,
    )


def verify_task(
    task: Task,
    repo_path: str | None = None,
    command_timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    log_dir: str | None = None,
    skip_scope: bool = False,
) -> TaskVerificationResult:
    """
    Run full verification pipeline for a Task:
    1. Check scope boundaries against changed files.
    2. Execute all declared verification commands in task repository path.
    3. Determine overall pass/fail status.
    """
    effective_repo = repo_path or task.repository.path
    if not os.path.exists(effective_repo):
        raise ValueError(f"Task repository path does not exist: {effective_repo}")

    # 1. Scope boundary verification
    if skip_scope:
        scope_result = ScopeCheckResult(
            passed=True,
            changed_files=[],
            violating_files=[],
            reason=None,
            details="Scope check skipped",
        )
    else:
        scope_result = check_scope(
            repo_path=effective_repo,
            allowed_paths=task.scope.allowed_paths,
            forbidden_paths=task.scope.forbidden_paths,
            base_commit=task.repository.base_commit,
        )

    # Do not execute arbitrary verification commands when the initial scope is
    # already invalid. This keeps a task from using its own verifier commands
    # to obscure or mutate an out-of-scope change.
    if not scope_result.passed:
        return TaskVerificationResult(
            task_id=task.task_id,
            status="failed",
            scope_result=scope_result,
            command_results=[],
            reason=scope_result.reason or "scope_violation",
            summary=f"Verification failed: {scope_result.details}",
        )

    # 2. Command executions
    command_results: list[CommandVerificationResult] = []
    for idx, cmd_str in enumerate(task.verification.commands, start=1):
        res = run_command_safe(
            cmd_str=cmd_str,
            cwd=effective_repo,
            timeout_sec=command_timeout_sec,
            log_dir=log_dir,
            cmd_index=idx,
        )
        command_results.append(res)

    # Re-check scope after commands: a verifier command can itself create or
    # modify files, and those changes must obey the same task boundaries.
    if not skip_scope:
        scope_result = check_scope(
            repo_path=effective_repo,
            allowed_paths=task.scope.allowed_paths,
            forbidden_paths=task.scope.forbidden_paths,
            base_commit=task.repository.base_commit,
        )

    # 3. Overall pass/fail evaluation
    any_cmd_failed = any(c.status != "passed" for c in command_results)
    scope_failed = not scope_result.passed

    if scope_failed:
        overall_status = "failed"
        reason = scope_result.reason or "scope_violation"
        summary = f"Verification failed: Scope violation detected ({len(scope_result.violating_files)} violating files)"
    elif any_cmd_failed:
        failed_count = sum(1 for c in command_results if c.status != "passed")
        overall_status = "failed"
        reason = "command_failure"
        summary = f"Verification failed: {failed_count}/{len(command_results)} commands failed"
    else:
        overall_status = "passed"
        reason = None
        summary = f"Verification passed: All {len(command_results)} commands completed with exit code 0 and scope valid"

    return TaskVerificationResult(
        task_id=task.task_id,
        status=overall_status,
        scope_result=scope_result,
        command_results=command_results,
        reason=reason,
        summary=summary,
    )
