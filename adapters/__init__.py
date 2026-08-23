"""
Adapters module for Agent Executor Gateway.
Exposes ExecutorAdapter base abstraction and concrete provider adapters.
"""

from __future__ import annotations

from adapters.base import ExecutorAdapter
from adapters.antigravity import AntigravityAdapter, AntigravityConfig

__all__ = [
    "ExecutorAdapter",
    "AntigravityAdapter",
    "AntigravityConfig",
]
