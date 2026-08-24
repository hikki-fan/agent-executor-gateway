"""
Orchestration Package for Agent Executor Gateway (Phase 6, 7, 8).

Exports Task schema, verification pipeline, scope control, completion reporting, metrics,
rule-based router, multi-executor escalation state machine, worktree isolation, and DAG infrastructure.
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
from orchestration.router import (
    RouteDecision,
    route_task,
)
from orchestration.escalation import (
    AttemptRecord,
    EscalationContext,
    EscalationDecision,
    EscalationState,
    build_escalation_context,
    evaluate_escalation,
    get_git_diff_text,
    init_escalation_state,
)
from orchestration.worktree import (
    DEFAULT_WORKTREE_DIR_NAME,
    WorktreeInfo,
    WorktreeManager,
    get_default_worktree_root,
    is_path_inside_root,
    sanitize_task_id,
)
from orchestration.dag import (
    STATE_BLOCKED,
    STATE_CANCELLED,
    STATE_CREATED,
    STATE_DONE,
    STATE_FAILED,
    STATE_INTEGRATING,
    STATE_READY,
    STATE_REVIEWING,
    STATE_RUNNING,
    STATE_VERIFYING,
    TaskDAG,
    TaskNode,
    run_dag_parallel,
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
    "RouteDecision",
    "route_task",
    "AttemptRecord",
    "EscalationContext",
    "EscalationDecision",
    "EscalationState",
    "build_escalation_context",
    "evaluate_escalation",
    "get_git_diff_text",
    "init_escalation_state",
    "DEFAULT_WORKTREE_DIR_NAME",
    "WorktreeInfo",
    "WorktreeManager",
    "get_default_worktree_root",
    "is_path_inside_root",
    "sanitize_task_id",
    "STATE_BLOCKED",
    "STATE_CANCELLED",
    "STATE_CREATED",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_INTEGRATING",
    "STATE_READY",
    "STATE_REVIEWING",
    "STATE_RUNNING",
    "STATE_VERIFYING",
    "TaskDAG",
    "TaskNode",
    "run_dag_parallel",
]
