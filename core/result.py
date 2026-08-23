"""
Standardized ExecutorResult data model for Agent Executor Gateway.
Matches the uniform result contract defined in Goal Prompt Section 10.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

LEGAL_STATUSES = frozenset({"success", "partial_success", "error"})


def normalize_usage(raw_usage: Any) -> dict[str, Any]:
    """
    Normalize usage metadata strictly to standard Section 10 keys:
    input_tokens, output_tokens, total_tokens, cost_usd.
    Provider-specific usage fields remain preserved under raw.parsed.
    """
    if isinstance(raw_usage, dict):
        return {
            "input_tokens": raw_usage.get("input_tokens"),
            "output_tokens": raw_usage.get("output_tokens"),
            "total_tokens": raw_usage.get("total_tokens"),
            "cost_usd": raw_usage.get("cost_usd"),
        }
    return {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }


@dataclass
class ExecutorResult:
    """
    Standardized execution result produced by any ExecutorAdapter.
    Legal status values: 'success', 'partial_success', 'error'.
    """
    status: str
    executor: str
    session_id: str | None = None
    response: str | None = None
    exit_code: int | None = None
    timing: dict[str, Any] = field(default_factory=lambda: {"duration_ms": 0})
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
        }
    )
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.usage, dict):
            self.usage = normalize_usage(self.usage)
        self.validate()

    def validate(self) -> None:
        """Validate that the result status and attributes adhere to the schema."""
        if self.status not in LEGAL_STATUSES:
            raise ValueError(
                f"Invalid ExecutorResult status '{self.status}'. Must be one of: {sorted(LEGAL_STATUSES)}"
            )
        if not isinstance(self.executor, str) or not self.executor.strip():
            raise ValueError("ExecutorResult executor must be a non-empty string")
        if not isinstance(self.timing, dict):
            raise ValueError("ExecutorResult timing must be a dictionary")
        if not isinstance(self.usage, dict):
            raise ValueError("ExecutorResult usage must be a dictionary")
        if not isinstance(self.warnings, list):
            raise ValueError("ExecutorResult warnings must be a list")
        if not isinstance(self.raw, dict):
            raise ValueError("ExecutorResult raw must be a dictionary")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result into the standard Section 10 JSON-compatible dictionary."""
        return {
            "status": self.status,
            "executor": self.executor,
            "session_id": self.session_id,
            "response": self.response,
            "exit_code": self.exit_code,
            "timing": dict(self.timing),
            "usage": normalize_usage(self.usage),
            "warnings": list(self.warnings),
            "error": self.error,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutorResult:
        """Reconstruct an ExecutorResult instance from a dictionary."""
        timing = data.get("timing") or {"duration_ms": 0}
        usage = normalize_usage(data.get("usage"))
        warnings = data.get("warnings") or []
        raw = data.get("raw") or {}

        return cls(
            status=data.get("status", "error"),
            executor=data.get("executor", ""),
            session_id=data.get("session_id"),
            response=data.get("response"),
            exit_code=data.get("exit_code"),
            timing=dict(timing),
            usage=dict(usage),
            warnings=list(warnings),
            error=data.get("error"),
            raw=dict(raw),
        )
