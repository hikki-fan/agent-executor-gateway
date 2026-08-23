"""
Admission Control and Concurrency Management for Agent Executor Gateway.
Provides bounded semaphores for HTTP sockets, POST capacity, and executor admission.
"""

from __future__ import annotations
import threading
from typing import Optional


class AdmissionController:
    """
    Admission Controller managing global HTTP connection permits,
    heavy POST endpoint slots (the independent 45 POST cap prevents POST requests
    from consuming the final five of 50 total permits, but those five remain general
    shared HTTP permits and are not exclusive to health),
    and per-executor admission limits (HTTP 429).
    """

    def __init__(
        self,
        max_http_connections: int = 50,
        max_post_connections: int = 45,
        max_worker_concurrency: int = 1,
    ) -> None:
        self.max_http_connections = max_http_connections
        self.max_post_connections = max_post_connections
        self.max_worker_concurrency = max_worker_concurrency

        self.http_semaphore = threading.BoundedSemaphore(max_http_connections)
        self.post_semaphore = threading.BoundedSemaphore(max_post_connections)
        self.worker_semaphore = threading.BoundedSemaphore(max_worker_concurrency)

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

    def acquire_worker(self, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """Acquire an active worker admission permit."""
        if timeout is not None:
            return self.worker_semaphore.acquire(blocking=blocking, timeout=timeout)
        return self.worker_semaphore.acquire(blocking=blocking)

    def release_worker(self) -> None:
        """Release an active worker admission permit."""
        self.worker_semaphore.release()
