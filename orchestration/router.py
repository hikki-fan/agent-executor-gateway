"""
Rule-Based Task Router for Agent Executor Gateway (Phase 7).

Implements Goal Prompt Section 22 & Section 49:
- S (Low/High) -> agy
- M Feature / Bugfix / Refactor -> agy
- M Debug / Investigation -> grok
- L / XL -> Manual decomposition / Codex override required (no auto-run)
- Explicit Codex / client executor override takes absolute precedence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from orchestration.task import ALLOWED_EXECUTORS, Task


@dataclass(frozen=True)
class RouteDecision:
    """Represents a deterministic routing decision for a Task."""
    task_id: str
    executor: str | None
    status: str  # "routed", "override_required", "invalid_override"
    rule: str
    complexity: str
    risk: str
    task_type: str
    requires_human_review: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def route_task(
    task: Task,
    executor_override: str | None = None,
) -> RouteDecision:
    """
    Determine the appropriate executor for a task using Goal Prompt Section 22 / 49 rules.

    Priority order:
    1. Explicit override (CLI argument or explicit override request) takes absolute precedence.
    2. L / XL complexity requires manual decomposition / Codex override.
    3. M Debug / Investigation routes to Grok.
    4. S and M Feature/Bugfix/Refactor route to Antigravity (agy).
    """
    task_id = task.task_id
    complexity = task.classification.complexity.upper()
    risk = task.classification.risk.lower()
    task_type = task.classification.type.lower()

    # 1. Explicit override takes precedence
    if executor_override is not None:
        target = executor_override.strip().lower()
        if target not in ALLOWED_EXECUTORS:
            return RouteDecision(
                task_id=task_id,
                executor=None,
                status="invalid_override",
                rule="explicit_override_validation_failure",
                complexity=complexity,
                risk=risk,
                task_type=task_type,
                requires_human_review=True,
                reason=f"Invalid executor override '{executor_override}'. Must be one of {list(ALLOWED_EXECUTORS)}",
            )
        requires_review = (complexity in ("L", "XL") or risk in ("high", "critical"))
        return RouteDecision(
            task_id=task_id,
            executor=target,
            status="routed",
            rule="explicit_codex_override",
            complexity=complexity,
            risk=risk,
            task_type=task_type,
            requires_human_review=requires_review,
            reason=f"Explicit Codex/executor override to '{target}' applied",
        )

    # 2. Large and Extra-Large tasks: Block automated execution without explicit override
    if complexity in ("L", "XL"):
        return RouteDecision(
            task_id=task_id,
            executor=None,
            status="override_required",
            rule="large_task_manual_decomposition_required",
            complexity=complexity,
            risk=risk,
            task_type=task_type,
            requires_human_review=True,
            reason=f"{complexity} complexity tasks require manual decomposition or explicit Codex override before execution",
        )

    # 3. Medium Debug / Investigation tasks -> Grok
    if complexity == "M" and task_type in ("debug", "investigation"):
        requires_review = (risk in ("high", "critical"))
        return RouteDecision(
            task_id=task_id,
            executor="grok",
            status="routed",
            rule="medium_debug_investigation_rule",
            complexity=complexity,
            risk=risk,
            task_type=task_type,
            requires_human_review=requires_review,
            reason=f"Medium {task_type} tasks route to Grok for deep diagnostic analysis",
        )

    # 4. Small tasks (S Low / S High) and Medium Feature/Bugfix/Refactor -> Antigravity (agy)
    requires_review = (risk in ("high", "critical"))
    rule_name = "small_task_agy_rule" if complexity == "S" else "medium_feature_agy_rule"
    reason_text = (
        f"Small ({complexity}) tasks default to fast execution on Antigravity (agy)"
        if complexity == "S"
        else f"Medium {task_type} tasks default to Antigravity (agy)"
    )
    if requires_review:
        reason_text += " (Codex Review recommended due to high/critical risk)"

    return RouteDecision(
        task_id=task_id,
        executor="agy",
        status="routed",
        rule=rule_name,
        complexity=complexity,
        risk=risk,
        task_type=task_type,
        requires_human_review=requires_review,
        reason=reason_text,
    )
