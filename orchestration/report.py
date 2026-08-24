"""
Completion Report and Metrics Module for Agent Executor Gateway (Phase 6).

Implements Goal Prompt Section 30 (Completion Report) & Section 38 (Metrics JSONL):
- Standardized Completion Report structure matching Section 30.
- Automatic Git diff stats computation (changed files, insertions, deletions).
- Appending structured execution metrics to repository/.agent/metrics.jsonl.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from orchestration.scope import get_git_changed_and_untracked_files
from orchestration.task import Task
from orchestration.verifier import TaskVerificationResult


def get_git_head_commit(repo_path: str) -> str:
    """Retrieve the current HEAD commit hash from Git repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "UNKNOWN_HEAD"


def get_git_diff_stats(repo_path: str, base_commit: str | None = None) -> tuple[list[str], int, int]:
    """
    Compute changed files, total insertions, and total deletions against base_commit.

    Returns:
        tuple (files_list, total_insertions, total_deletions)
    """
    changed_files = get_git_changed_and_untracked_files(repo_path, base_commit=base_commit)
    insertions = 0
    deletions = 0

    if not os.path.exists(repo_path):
        return changed_files, 0, 0

    # Query numstat against base_commit
    cmd = ["git", "diff", "--numstat"]
    if base_commit:
        cmd.append(base_commit)

    try:
        res = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 3:
                    ins_str, del_str, _ = parts[0], parts[1], parts[2]
                    if ins_str.isdigit():
                        insertions += int(ins_str)
                    if del_str.isdigit():
                        deletions += int(del_str)
    except Exception:
        pass

    return changed_files, insertions, deletions


def generate_completion_report(
    task: Task,
    verification: TaskVerificationResult,
    executor: str | None = None,
    session_id: str | None = None,
    summary: str | None = None,
    known_issues: list[str] | None = None,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """
    Generate unified Completion Report matching Section 30 JSON schema:

    {
      "task_id": "TASK-001",
      "status": "completed" | "failed",
      "executor": "agy",
      "session_id": "xxx",
      "git": {
        "base_commit": "abc123",
        "head_commit": "def456"
      },
      "changes": {
        "files": ["backend/tasks.py"],
        "insertions": 80,
        "deletions": 20
      },
      "verification": {
        "status": "passed" | "failed",
        "commands": [
          {
            "command": "pytest",
            "exit_code": 0
          }
        ]
      },
      "summary": "...",
      "known_issues": []
    }
    """
    effective_repo = repo_path or task.repository.path
    effective_executor = executor or task.execution.executor
    effective_session_id = session_id

    # Status evaluation: only "completed" if verification passed; otherwise "failed"
    report_status = "completed" if verification.status == "passed" else "failed"

    head_commit = get_git_head_commit(effective_repo)
    changed_files, insertions, deletions = get_git_diff_stats(
        effective_repo, base_commit=task.repository.base_commit
    )

    verif_commands_summary = [
        {
            "command": c.command,
            "exit_code": c.exit_code,
        }
        for c in verification.command_results
    ]

    report_summary = summary or verification.summary or f"Task {task.task_id} execution {report_status}"

    return {
        "task_id": task.task_id,
        "status": report_status,
        "executor": effective_executor,
        "session_id": effective_session_id,
        "git": {
            "base_commit": task.repository.base_commit,
            "head_commit": head_commit,
        },
        "changes": {
            "files": changed_files,
            "insertions": insertions,
            "deletions": deletions,
        },
        "verification": {
            "status": verification.status,
            "commands": verif_commands_summary,
        },
        "summary": report_summary,
        "known_issues": list(known_issues or []),
    }


def record_task_metrics(
    task: Task,
    verification: TaskVerificationResult,
    duration_ms: int = 0,
    attempts: int = 1,
    fallback_used: bool = False,
    review_pass: bool = True,
    repo_path: str | None = None,
    metrics_file_path: str | None = None,
) -> str:
    """
    Append a structured execution metric entry to .agent/metrics.jsonl per Section 38:

    {
      "task_id": "TASK-001",
      "task_type": "feature",
      "complexity": "M",
      "risk": "medium",
      "executor": "agy",
      "duration_ms": 183000,
      "attempts": 1,
      "first_pass_success": true,
      "fallback_used": false,
      "verification_pass": true,
      "review_pass": true,
      "changed_files": 4,
      "changed_lines": 230
    }
    """
    effective_repo = repo_path or task.repository.path
    if metrics_file_path:
        out_path = metrics_file_path
    else:
        metrics_dir = os.path.join(effective_repo, ".agent")
        os.makedirs(metrics_dir, exist_ok=True)
        out_path = os.path.join(metrics_dir, "metrics.jsonl")

    changed_files, insertions, deletions = get_git_diff_stats(
        effective_repo, base_commit=task.repository.base_commit
    )
    changed_lines = insertions + deletions
    verification_pass = (verification.status == "passed")
    first_pass_success = (attempts == 1 and verification_pass and not fallback_used)

    metric_entry = {
        "task_id": task.task_id,
        "task_type": task.classification.type,
        "complexity": task.classification.complexity,
        "risk": task.classification.risk,
        "executor": task.execution.executor,
        "duration_ms": duration_ms,
        "attempts": attempts,
        "first_pass_success": first_pass_success,
        "fallback_used": fallback_used,
        "verification_pass": verification_pass,
        "review_pass": review_pass,
        "changed_files": len(changed_files),
        "changed_lines": changed_lines,
    }

    metrics_dir = os.path.dirname(out_path)
    if metrics_dir:
        os.makedirs(metrics_dir, exist_ok=True)

    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(metric_entry, ensure_ascii=False) + "\n")

    return out_path
