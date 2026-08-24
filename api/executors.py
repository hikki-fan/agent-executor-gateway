"""
Generic Executor API and Registry for Agent Executor Gateway.
Provides executor discovery, health inspection, and unified invocation routing.
Upper gateway layers interact solely through ExecutorAdapter abstractions and generic parameters.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from adapters.base import ExecutorAdapter


class ExecutorRegistry:
    """
    Registry managing registered ExecutorAdapters in Agent Executor Gateway.
    Provides uniform discovery and lookup by executor name.
    """

    def __init__(self) -> None:
        self._executors: Dict[str, ExecutorAdapter] = {}

    def register(self, adapter: ExecutorAdapter) -> None:
        """Register an ExecutorAdapter under its adapter.name."""
        if not isinstance(adapter, ExecutorAdapter):
            raise TypeError("Registered executor must be an instance of ExecutorAdapter")
        self._executors[adapter.name] = adapter

    def get(self, name: str) -> Optional[ExecutorAdapter]:
        """Retrieve an ExecutorAdapter by name, or None if not registered."""
        return self._executors.get(name)

    def list_executors(self) -> List[Dict[str, Any]]:
        """
        List all registered executors with operational availability and capabilities.
        Matches the Section 11 discovery schema.
        """
        result: List[Dict[str, Any]] = []
        for name, adapter in self._executors.items():
            health_info = adapter.health()
            caps = adapter.capabilities()
            result.append({
                "name": name,
                "available": bool(health_info.get("available", False)),
                "supports_session": bool(caps.get("supports_session", False)),
            })
        return result

    def registered_names(self) -> List[str]:
        """Return the list of all registered executor names."""
        return list(self._executors.keys())

    def items(self):
        """Return items view of registered executors."""
        return self._executors.items()

    def keys(self):
        """Return keys view of registered executors."""
        return self._executors.keys()

    def values(self):
        """Return values view of registered executors."""
        return self._executors.values()

    def __iter__(self):
        """Iterate over registered executor names."""
        return iter(self._executors)


def validate_invoke_request(payload: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Validate the invoke request JSON payload against Generic API requirements:
    - Top-level JSON body must be an object (dict)
    - 'prompt' is required and must be a non-empty string
    - 'session_id' (if provided and not null) must be a non-empty string
    - 'timeout_sec' (if provided and not null) must be a positive integer (bool rejected)
    - 'cwd', 'model', 'effort' (if provided and not null) must be strings

    Returns (validated_params, None) on success.
    Returns (None, error_message) on validation failure (HTTP 400).
    """
    if not isinstance(payload, dict):
        return None, "Invalid JSON payload: request body must be a JSON object"

    # 1. Validate prompt
    prompt = payload.get("prompt")
    if prompt is None:
        return None, 'Field "prompt" is required'
    if isinstance(prompt, bool) or not isinstance(prompt, str) or not prompt.strip():
        return None, 'Field "prompt" must be a non-empty string'

    # 2. Validate session_id
    session_id = payload.get("session_id")
    if session_id is not None:
        if isinstance(session_id, bool) or not isinstance(session_id, str) or not session_id.strip():
            return None, 'Field "session_id" must be a non-empty string or null'

    # 3. Validate timeout_sec
    timeout_sec = payload.get("timeout_sec")
    if timeout_sec is not None:
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int) or timeout_sec <= 0:
            return None, 'Field "timeout_sec" must be a positive integer or null'

    # 4. Validate cwd
    cwd = payload.get("cwd")
    if cwd is not None:
        if isinstance(cwd, bool) or not isinstance(cwd, str):
            return None, 'Field "cwd" must be a string or null'

    # 5. Validate model
    model = payload.get("model")
    if model is not None:
        if isinstance(model, bool) or not isinstance(model, str):
            return None, 'Field "model" must be a string or null'

    # 6. Validate effort
    effort = payload.get("effort")
    if effort is not None:
        if isinstance(effort, bool) or not isinstance(effort, str):
            return None, 'Field "effort" must be a string or null'

    return {
        "prompt": prompt,
        "cwd": cwd,
        "session_id": session_id,
        "model": model,
        "effort": effort,
        "timeout_sec": timeout_sec,
    }, None
