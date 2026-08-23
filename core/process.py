"""
Isolated Process Group Execution for Agent Executor Gateway.
Manages process lifecycle, session detachment (start_new_session=True),
pipe isolation (stdin=DEVNULL), and full process group termination (killpg SIGKILL) on timeout.
"""

from __future__ import annotations
import os
import signal
import subprocess
from typing import Sequence, Mapping


def run_process_group(
    cmd: Sequence[str],
    timeout_sec: float,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """
    Execute a command in an isolated Process Group with standard input disconnected.
    Ensures complete process tree cleanup (SIGKILL via os.killpg) on timeout.
    """
    run_env = os.environ.copy() if env is None else dict(env)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=run_env,
        cwd=cwd,
        start_new_session=True,  # Spawns isolated process group; proc.pid is PGID
    )
    pgid = proc.pid
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        raise
