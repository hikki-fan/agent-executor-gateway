"""
Abstract Base Class for Executor Adapters in Agent Executor Gateway.
Matches the interface definition from Goal Prompt Section 7.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any
from core.result import ExecutorResult


class ExecutorAdapter(ABC):
    """
    Unified Executor Adapter interface for coding agents.
    Upper gateway layers interact solely through this abstraction.
    """

    name: str

    @abstractmethod
    def invoke(
        self,
        *,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout_sec: int | None = None,
    ) -> ExecutorResult:
        """
        Execute a prompt against the underlying agent worker.
        Returns a standardized ExecutorResult.
        """
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Check the operational health of the executor."""
        ...

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Return the functional capabilities supported by this executor."""
        ...
