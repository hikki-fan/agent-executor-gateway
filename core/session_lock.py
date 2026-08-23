"""
Session Lock Manager for Agent Executor Gateway.
Thread-safe session locking keyed by (executor, session_id).
Prevents concurrent execution turns against the same session on the same executor (HTTP 409).
"""

from __future__ import annotations
import threading
from typing import Set, Tuple


class SessionLockManager:
    """
    Thread-safe session lock manager.
    Keys are strictly partitioned by (executor, session_id), ensuring independent
    executors do not conflict even if they share identical session identifiers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_sessions: Set[Tuple[str, str]] = set()

    @staticmethod
    def _normalize_key(executor: str, session_id: str | None) -> Tuple[str, str] | None:
        if not session_id or not str(session_id).strip():
            return None
        return (str(executor).strip(), str(session_id).strip())

    def acquire(self, executor: str, session_id: str | None) -> bool:
        """
        Attempt to acquire a lock for (executor, session_id).
        Returns True if acquired (or if session_id is None/empty).
        Returns False if the session is already active.
        """
        key = self._normalize_key(executor, session_id)
        if key is None:
            return True

        with self._lock:
            if key in self._active_sessions:
                return False
            self._active_sessions.add(key)
            return True

    def release(self, executor: str, session_id: str | None) -> None:
        """
        Release the lock for (executor, session_id).
        Safe to call if session_id is None/empty or not locked.
        """
        key = self._normalize_key(executor, session_id)
        if key is None:
            return

        with self._lock:
            self._active_sessions.discard(key)

    def is_locked(self, executor: str, session_id: str | None) -> bool:
        """
        Check if (executor, session_id) is currently locked.
        Returns False if session_id is None/empty.
        """
        key = self._normalize_key(executor, session_id)
        if key is None:
            return False

        with self._lock:
            return key in self._active_sessions

    def active_count(self) -> int:
        """Return the total number of currently locked sessions across all executors."""
        with self._lock:
            return len(self._active_sessions)

    def active_sessions(self) -> Set[Tuple[str, str]]:
        """Return a copy of the active session keys."""
        with self._lock:
            return set(self._active_sessions)
