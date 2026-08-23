"""
Monotonic Timeout Budget and Deadline Management for Agent Executor Gateway.
Provides monotonic timer tracking across multi-step or retry executions.
"""

from __future__ import annotations
import subprocess
import time
from typing import Sequence


class DeadlineTimer:
    """
    Monotonic deadline tracker to enforce aggregate execution timeout budgets.
    Supports non-positive timeouts by treating them as immediately expired.
    """

    def __init__(self, timeout_sec: float) -> None:
        self.timeout_sec = float(timeout_sec)
        self.start_time = time.monotonic()
        self.deadline = self.start_time + self.timeout_sec

    def remaining(self) -> float:
        """Return the remaining seconds before deadline expiration."""
        return self.deadline - time.monotonic()

    def is_expired(self) -> bool:
        """Return True if the monotonic deadline has expired."""
        return self.remaining() <= 0

    def elapsed_ms(self) -> int:
        """Return the elapsed time in milliseconds since timer creation."""
        return int((time.monotonic() - self.start_time) * 1000)

    def check_or_raise(self, cmd: Sequence[str] | None = None) -> float:
        """
        Check if the budget is exhausted.
        Raises subprocess.TimeoutExpired if remaining time is <= 0.
        Returns the positive remaining seconds.
        """
        rem = self.remaining()
        if rem <= 0:
            raise subprocess.TimeoutExpired(cmd=cmd or [], timeout=self.timeout_sec)
        return rem
