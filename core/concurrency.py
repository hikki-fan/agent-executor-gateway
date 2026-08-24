"""
Admission Control and Concurrency Management for Agent Executor Gateway.
Provides bounded semaphores for HTTP sockets, POST capacity, global gateway workers, and per-executor concurrency.
"""

from __future__ import annotations

import threading
from typing import Mapping, Optional


class AdmissionController:
    """
    Admission Controller managing:
    1. Global HTTP connection permits (max_http_connections)
    2. Heavy POST endpoint slots (max_post_connections)
    3. Global gateway worker execution concurrency (max_worker_concurrency / max_gateway_concurrency)
    4. Per-executor independent concurrency semaphores
    """

    def __init__(
        self,
        max_http_connections: int = 50,
        max_post_connections: int = 45,
        max_worker_concurrency: int = 2,
        executor_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.max_http_connections = max_http_connections
        self.max_post_connections = max_post_connections
        self.max_worker_concurrency = max_worker_concurrency
        self.max_gateway_concurrency = max_worker_concurrency

        self.http_semaphore = threading.BoundedSemaphore(max_http_connections)
        self.post_semaphore = threading.BoundedSemaphore(max_post_connections)
        self.gateway_semaphore = threading.BoundedSemaphore(max_worker_concurrency)
        self.worker_semaphore = self.gateway_semaphore  # Backward compatibility alias

        self._lock = threading.Lock()
        self._executor_semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._executor_limits: dict[str, int] = {}
        if executor_limits:
            for name, limit in executor_limits.items():
                self.register_executor(name, limit)

    def register_executor(self, executor: str, max_concurrency: int) -> None:
        """Register a bounded concurrency semaphore for a named executor."""
        with self._lock:
            self._executor_limits[executor] = max_concurrency
            self._executor_semaphores[executor] = threading.BoundedSemaphore(max_concurrency)

    def acquire_http(self, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """Acquire a general HTTP connection slot."""
        if timeout is not None:
            return self.http_semaphore.acquire(blocking=blocking, timeout=timeout)
        return self.http_semaphore.acquire(blocking=blocking)

    def release_http(self) -> None:
        """Release a general HTTP connection slot."""
        self.http_semaphore.release()

    def acquire_post(self, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """
        Acquire a heavy POST connection slot.
        The independent POST cap prevents POST requests from consuming the final five
        of 50 total connection permits, keeping general shared slots available for other requests.
        """
        if timeout is not None:
            return self.post_semaphore.acquire(blocking=blocking, timeout=timeout)
        return self.post_semaphore.acquire(blocking=blocking)

    def release_post(self) -> None:
        """Release a heavy POST connection slot."""
        self.post_semaphore.release()

    def acquire_gateway(self, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """Acquire a global gateway worker permit."""
        if timeout is not None:
            return self.gateway_semaphore.acquire(blocking=blocking, timeout=timeout)
        return self.gateway_semaphore.acquire(blocking=blocking)

    def release_gateway(self) -> None:
        """Release a global gateway worker permit."""
        self.gateway_semaphore.release()

    def acquire_worker(self, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """Backward compatibility alias for acquire_gateway."""
        return self.acquire_gateway(blocking=blocking, timeout=timeout)

    def release_worker(self) -> None:
        """Backward compatibility alias for release_gateway."""
        self.release_gateway()

    def acquire_executor(self, executor: str, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """Acquire an executor-specific worker permit."""
        with self._lock:
            sem = self._executor_semaphores.get(executor)
        if sem is None:
            return True
        if timeout is not None:
            return sem.acquire(blocking=blocking, timeout=timeout)
        return sem.acquire(blocking=blocking)

    def release_executor(self, executor: str) -> None:
        """Release an executor-specific worker permit."""
        with self._lock:
            sem = self._executor_semaphores.get(executor)
        if sem is None:
            return
        sem.release()

    def acquire_execution_permits(self, executor: str, blocking: bool = False) -> tuple[bool, str | None]:
        """
        Acquire both global gateway worker permit and executor-specific worker permit.
        Returns:
            (True, None) on success.
            (False, "gateway") if global gateway capacity is saturated.
            (False, executor_name) if specific executor capacity is saturated.
        """
        if not self.acquire_gateway(blocking=blocking):
            return False, "gateway"

        try:
            executor_acquired = self.acquire_executor(executor, blocking=blocking)
        except BaseException:
            # The gateway permit was acquired first; never strand it if a
            # provider semaphore implementation raises unexpectedly.
            self.release_gateway()
            raise

        if not executor_acquired:
            self.release_gateway()
            return False, executor

        return True, None

    def release_execution_permits(self, executor: str) -> None:
        """Safely release both executor-specific permit and global gateway permit."""
        try:
            self.release_executor(executor)
        finally:
            self.release_gateway()

    def get_executor_limit(self, executor: str) -> int | None:
        """Get the configured concurrency limit for an executor."""
        with self._lock:
            return self._executor_limits.get(executor)
