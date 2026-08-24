"""
Core module for Agent Executor Gateway.
Provides executor-neutral foundations: configuration, authentication,
admission control, session locking, process group isolation,
monotonic timeouts, and standardized execution results.
"""

from __future__ import annotations

from core.auth import load_or_create_token, verify_bearer_token
from core.config import (
    DEFAULT_PORT,
    DEFAULT_TOKEN_FILE,
    DEFAULT_MAX_CONTENT_LENGTH,
    DEFAULT_MAX_HTTP_CONNECTIONS,
    DEFAULT_MAX_POST_CONNECTIONS,
    DEFAULT_GATEWAY_MAX_CONCURRENCY,
    DEFAULT_SOCKET_TIMEOUT,
    GatewayConfig,
)
from core.concurrency import AdmissionController
from core.process import run_process_group
from core.result import ExecutorResult, LEGAL_STATUSES, normalize_usage
from core.session_lock import SessionLockManager
from core.timeout import DeadlineTimer

__all__ = [
    "load_or_create_token",
    "verify_bearer_token",
    "DEFAULT_PORT",
    "DEFAULT_TOKEN_FILE",
    "DEFAULT_MAX_CONTENT_LENGTH",
    "DEFAULT_MAX_HTTP_CONNECTIONS",
    "DEFAULT_MAX_POST_CONNECTIONS",
    "DEFAULT_GATEWAY_MAX_CONCURRENCY",
    "DEFAULT_SOCKET_TIMEOUT",
    "GatewayConfig",
    "AdmissionController",
    "run_process_group",
    "ExecutorResult",
    "LEGAL_STATUSES",
    "normalize_usage",
    "SessionLockManager",
    "DeadlineTimer",
]
