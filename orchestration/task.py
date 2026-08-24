"""
Task Schema and Validation Module for Agent Executor Gateway (Phase 6).

Implements the unified Task JSON schema per Goal Prompt Sections 18, 19, 20, 26, 48:
- version, task_id, parent_task_id, goal
- repository: path, base_commit
- classification: complexity (S|M|L|XL), risk (low|medium|high|critical), type
- execution: executor (agy|grok), fallback_executor, max_same_executor_attempts, max_executor_switches, isolated_worktree
- scope: allowed_paths, forbidden_paths
- acceptance: list[str]
- verification: commands list[str]
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

ALLOWED_COMPLEXITIES = ("S", "M", "L", "XL")
ALLOWED_RISKS = ("low", "medium", "high", "critical")
ALLOWED_EXECUTORS = ("agy", "grok")
ALLOWED_TASK_TYPES = (
    "feature",
    "bugfix",
    "debug",
    "refactor",
    "test",
    "config",
    "architecture",
    "migration",
    "investigation",
)


@dataclass(frozen=True)
class RepositorySpec:
    """Repository specification for a task."""
    path: str
    base_commit: str


@dataclass(frozen=True)
class ClassificationSpec:
    """Classification heuristic and metadata for a task."""
    complexity: str
    risk: str
    type: str


@dataclass(frozen=True)
class ExecutionSpec:
    """Execution policy and routing parameters for a task."""
    executor: str
    fallback_executor: str | None = None
    max_same_executor_attempts: int = 2
    max_executor_switches: int = 1
    isolated_worktree: bool = False


@dataclass(frozen=True)
class ScopeSpec:
    """Scope restriction patterns for repository changes."""
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationSpec:
    """Automated machine verification commands."""
    commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Task:
    """Unified Task specification model."""
    version: str
    task_id: str
    parent_task_id: str | None
    goal: str
    repository: RepositorySpec
    classification: ClassificationSpec
    execution: ExecutionSpec
    scope: ScopeSpec
    acceptance: list[str]
    verification: VerificationSpec

    def to_dict(self) -> dict[str, Any]:
        """Serialize task to dictionary matching Section 18 JSON specification."""
        return {
            "version": self.version,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "goal": self.goal,
            "repository": asdict(self.repository),
            "classification": asdict(self.classification),
            "execution": asdict(self.execution),
            "scope": asdict(self.scope),
            "acceptance": list(self.acceptance),
            "verification": asdict(self.verification),
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize task to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Construct a validated Task instance from dictionary."""
        task, errors = validate_task_dict(data)
        if errors or task is None:
            raise ValueError(f"Task validation failed: {'; '.join(errors)}")
        return task

    @classmethod
    def load_file(cls, filepath: str) -> Task:
        """Load and validate Task from a JSON file."""
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Task file not found: {filepath}")
        with open(filepath, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in task file '{filepath}': {e}") from e
        return cls.from_dict(data)


def validate_task_dict(data: Any) -> tuple[Task | None, list[str]]:
    """
    Validate a task dictionary structure against the Phase 6 Task specification.

    Returns:
        tuple (Task | None, list_of_error_strings)
        If valid: (Task instance, [])
        If invalid: (None, ["error description 1", "error description 2", ...])
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return None, ["Task payload must be a JSON object (dictionary)"]

    # 1. version
    version = data.get("version")
    if version is None:
        errors.append("Missing required field 'version'")
    elif not isinstance(version, str) or version.strip() != "1":
        errors.append("Field 'version' must be the string '1'")

    # 2. task_id
    task_id = data.get("task_id")
    if task_id is None:
        errors.append("Missing required field 'task_id'")
    elif not isinstance(task_id, str) or not task_id.strip():
        errors.append("Field 'task_id' must be a non-empty string")

    # 3. parent_task_id
    parent_task_id = data.get("parent_task_id")
    if parent_task_id is not None:
        if not isinstance(parent_task_id, str) or not parent_task_id.strip():
            errors.append("Field 'parent_task_id' must be null or a non-empty string")

    # 4. goal
    goal = data.get("goal")
    if goal is None:
        errors.append("Missing required field 'goal'")
    elif not isinstance(goal, str) or not goal.strip():
        errors.append("Field 'goal' must be a non-empty string")

    # 5. repository
    repo_data = data.get("repository")
    repo_spec: RepositorySpec | None = None
    if repo_data is None:
        errors.append("Missing required field 'repository'")
    elif not isinstance(repo_data, dict):
        errors.append("Field 'repository' must be a JSON object")
    else:
        repo_path = repo_data.get("path")
        base_commit = repo_data.get("base_commit")

        if repo_path is None:
            errors.append("Missing required field 'repository.path'")
        elif not isinstance(repo_path, str) or not repo_path.strip():
            errors.append("Field 'repository.path' must be a non-empty string")

        if base_commit is None:
            errors.append("Missing required field 'repository.base_commit'")
        elif not isinstance(base_commit, str) or not base_commit.strip():
            errors.append("Field 'repository.base_commit' must be a non-empty string")

        if repo_path and isinstance(repo_path, str) and base_commit and isinstance(base_commit, str):
            repo_spec = RepositorySpec(path=repo_path.strip(), base_commit=base_commit.strip())

    # 6. classification
    class_data = data.get("classification")
    class_spec: ClassificationSpec | None = None
    if class_data is None:
        errors.append("Missing required field 'classification'")
    elif not isinstance(class_data, dict):
        errors.append("Field 'classification' must be a JSON object")
    else:
        complexity = class_data.get("complexity")
        risk = class_data.get("risk")
        task_type = class_data.get("type")

        if complexity is None:
            errors.append("Missing required field 'classification.complexity'")
        elif complexity not in ALLOWED_COMPLEXITIES:
            errors.append(
                f"Field 'classification.complexity' must be one of {list(ALLOWED_COMPLEXITIES)}, got '{complexity}'"
            )

        if risk is None:
            errors.append("Missing required field 'classification.risk'")
        elif risk not in ALLOWED_RISKS:
            errors.append(
                f"Field 'classification.risk' must be one of {list(ALLOWED_RISKS)}, got '{risk}'"
            )

        if task_type is None:
            errors.append("Missing required field 'classification.type'")
        elif not isinstance(task_type, str) or not task_type.strip():
            errors.append("Field 'classification.type' must be a non-empty string")
        elif task_type.strip() not in ALLOWED_TASK_TYPES:
            errors.append(
                f"Field 'classification.type' must be one of {list(ALLOWED_TASK_TYPES)}, got '{task_type}'"
            )

        if (
            complexity in ALLOWED_COMPLEXITIES
            and risk in ALLOWED_RISKS
            and isinstance(task_type, str)
            and task_type.strip() in ALLOWED_TASK_TYPES
        ):
            class_spec = ClassificationSpec(
                complexity=complexity,
                risk=risk,
                type=task_type.strip(),
            )

    # 7. execution
    exec_data = data.get("execution")
    exec_spec: ExecutionSpec | None = None
    if exec_data is None:
        errors.append("Missing required field 'execution'")
    elif not isinstance(exec_data, dict):
        errors.append("Field 'execution' must be a JSON object")
    else:
        executor = exec_data.get("executor")
        fallback = exec_data.get("fallback_executor")
        attempts = exec_data.get("max_same_executor_attempts", 2)
        switches = exec_data.get("max_executor_switches", 1)
        worktree = exec_data.get("isolated_worktree", False)

        if executor is None:
            errors.append("Missing required field 'execution.executor'")
        elif not isinstance(executor, str) or not executor.strip():
            errors.append("Field 'execution.executor' must be a non-empty string")
        elif executor.strip() not in ALLOWED_EXECUTORS:
            errors.append(
                f"Field 'execution.executor' must be one of {list(ALLOWED_EXECUTORS)}, got '{executor}'"
            )

        if fallback is not None:
            if not isinstance(fallback, str) or not fallback.strip():
                errors.append("Field 'execution.fallback_executor' must be null or a non-empty string")
            elif fallback.strip() not in ALLOWED_EXECUTORS:
                errors.append(
                    f"Field 'execution.fallback_executor' must be one of {list(ALLOWED_EXECUTORS)}, got '{fallback}'"
                )

        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts <= 0:
            errors.append(
                f"Field 'execution.max_same_executor_attempts' must be a positive integer (>= 1), got '{attempts}'"
            )

        if isinstance(switches, bool) or not isinstance(switches, int) or switches < 0:
            errors.append(
                f"Field 'execution.max_executor_switches' must be a non-negative integer (>= 0), got '{switches}'"
            )

        if not isinstance(worktree, bool):
            errors.append(f"Field 'execution.isolated_worktree' must be a boolean, got '{worktree}'")

        if (
            isinstance(executor, str)
            and executor.strip() in ALLOWED_EXECUTORS
            and (fallback is None or (isinstance(fallback, str) and fallback.strip() in ALLOWED_EXECUTORS))
            and isinstance(attempts, int)
            and not isinstance(attempts, bool)
            and attempts >= 1
            and isinstance(switches, int)
            and not isinstance(switches, bool)
            and switches >= 0
            and isinstance(worktree, bool)
        ):
            exec_spec = ExecutionSpec(
                executor=executor.strip(),
                fallback_executor=fallback.strip() if fallback else None,
                max_same_executor_attempts=attempts,
                max_executor_switches=switches,
                isolated_worktree=worktree,
            )

    # 8. scope
    scope_data = data.get("scope")
    scope_spec: ScopeSpec | None = None
    if scope_data is None:
        errors.append("Missing required field 'scope'")
    elif not isinstance(scope_data, dict):
        errors.append("Field 'scope' must be a JSON object")
    else:
        allowed = scope_data.get("allowed_paths", [])
        forbidden = scope_data.get("forbidden_paths", [])

        if not isinstance(allowed, list):
            errors.append("Field 'scope.allowed_paths' must be a list of string patterns")
        elif not all(isinstance(p, str) for p in allowed):
            errors.append("All items in 'scope.allowed_paths' must be strings")
        elif not all(p.strip() for p in allowed):
            errors.append("All items in 'scope.allowed_paths' must be non-empty strings")

        if not isinstance(forbidden, list):
            errors.append("Field 'scope.forbidden_paths' must be a list of string patterns")
        elif not all(isinstance(p, str) for p in forbidden):
            errors.append("All items in 'scope.forbidden_paths' must be strings")
        elif not all(p.strip() for p in forbidden):
            errors.append("All items in 'scope.forbidden_paths' must be non-empty strings")

        if (
            isinstance(allowed, list)
            and isinstance(forbidden, list)
            and all(isinstance(p, str) and p.strip() for p in allowed)
            and all(isinstance(p, str) and p.strip() for p in forbidden)
        ):
            scope_spec = ScopeSpec(
                allowed_paths=[p.strip() for p in allowed],
                forbidden_paths=[p.strip() for p in forbidden],
            )

    # 9. acceptance
    acceptance = data.get("acceptance")
    if acceptance is None:
        errors.append("Missing required field 'acceptance'")
    elif not isinstance(acceptance, list):
        errors.append("Field 'acceptance' must be a list of strings")
    elif not acceptance:
        errors.append("Field 'acceptance' list cannot be empty")
    elif not all(isinstance(item, str) and item.strip() for item in acceptance):
        errors.append("All items in 'acceptance' must be non-empty strings")

    # 10. verification
    verif_data = data.get("verification")
    verif_spec: VerificationSpec | None = None
    if verif_data is None:
        errors.append("Missing required field 'verification'")
    elif not isinstance(verif_data, dict):
        errors.append("Field 'verification' must be a JSON object")
    else:
        commands = verif_data.get("commands")
        if commands is None:
            errors.append("Missing required field 'verification.commands'")
        elif not isinstance(commands, list):
            errors.append("Field 'verification.commands' must be a list of command strings")
        elif not commands:
            errors.append("Field 'verification.commands' list cannot be empty")
        elif not all(isinstance(c, str) and c.strip() for c in commands):
            errors.append("All items in 'verification.commands' must be non-empty strings")
        else:
            verif_spec = VerificationSpec(commands=[c.strip() for c in commands])

    if errors or not repo_spec or not class_spec or not exec_spec or not scope_spec or not verif_spec:
        return None, errors

    task = Task(
        version=str(version).strip(),
        task_id=str(task_id).strip(),
        parent_task_id=str(parent_task_id).strip() if parent_task_id else None,
        goal=str(goal).strip(),
        repository=repo_spec,
        classification=class_spec,
        execution=exec_spec,
        scope=scope_spec,
        acceptance=[str(item).strip() for item in acceptance],
        verification=verif_spec,
    )
    return task, []


def load_and_validate_task(filepath_or_data: str | dict[str, Any]) -> tuple[Task | None, list[str]]:
    """Convenience helper to load from file path or dictionary and validate."""
    if isinstance(filepath_or_data, str):
        if not os.path.exists(filepath_or_data):
            return None, [f"Task file not found: {filepath_or_data}"]
        try:
            with open(filepath_or_data, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return None, [f"Invalid JSON syntax in task file: {e}"]
        except Exception as e:
            return None, [f"Failed to read task file: {e}"]
    else:
        data = filepath_or_data

    return validate_task_dict(data)
