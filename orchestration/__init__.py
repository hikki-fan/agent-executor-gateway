"""
Orchestration Package for Agent Executor Gateway (Phase 6).

Exports Task schema, verification pipeline, scope control, completion reporting, and metrics.
"""

from __future__ import annotations

from orchestration.task import (
    ALLOWED_COMPLEXITIES,
    ALLOWED_EXECUTORS,
    ALLOWED_RISKS,
    ALLOWED_TASK_TYPES,
    ClassificationSpec,
    ExecutionSpec,
    RepositorySpec,
    ScopeSpec,
    Task,
    VerificationSpec,
    load_and_validate_task,
    validate_task_dict,
)
from orchestration.scope import (
    ScopeCheckResult,
    check_scope,
    get_git_changed_and_untracked_files,
    match_glob_pattern,
)
from orchestration.verifier import (
    CommandVerificationResult,
    TaskVerificationResult,
    redact_sensitive_text,
    run_command_safe,
    verify_task,
)
from orchestration.report import (
    generate_completion_report,
    get_git_diff_stats,
    get_git_head_commit,
    record_task_metrics,
)

__all__ = [
    "ALLOWED_COMPLEXITIES",
    "ALLOWED_EXECUTORS",
    "ALLOWED_RISKS",
    "ALLOWED_TASK_TYPES",
    "ClassificationSpec",
    "ExecutionSpec",
    "RepositorySpec",
    "ScopeSpec",
    "Task",
    "VerificationSpec",
    "load_and_validate_task",
    "validate_task_dict",
    "ScopeCheckResult",
    "check_scope",
    "get_git_changed_and_untracked_files",
    "match_glob_pattern",
    "CommandVerificationResult",
    "TaskVerificationResult",
    "redact_sensitive_text",
    "run_command_safe",
    "verify_task",
    "generate_completion_report",
    "get_git_diff_stats",
    "get_git_head_commit",
    "record_task_metrics",
]
