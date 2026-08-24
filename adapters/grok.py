"""
Grok Adapter Implementation for Agent Executor Gateway.
Encapsulates all Grok Build CLI provider-specific logic:
- Binary resolution (GROK_BIN env var, PATH lookup, standard fallback locations)
- Headless invocation command construction (-p, --cwd, --output-format json, --permission-mode bypassPermissions)
- Session lifecycle: new session UUID creation (--session-id) vs continuation (--resume)
- JSON output parsing, validation, and diagnostic error extraction
- Normalization of Grok usage (tokens, cost) to standard Section 10 ExecutorResult schema
- Process group execution with timeout enforcement and cleanup via os.killpg
- Health inspection and capability reporting
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from adapters.base import ExecutorAdapter
from core.process import run_process_group
from core.result import ExecutorResult, normalize_usage

DEFAULT_GROK_BIN = "/home/codex/.local/bin/grok"
FALLBACK_GROK_PATHS = (
    "/home/codex/.local/bin/grok",
    "/home/codex/.grok/bin/grok",
    "/usr/local/bin/grok",
    "/usr/bin/grok",
)
DEFAULT_GROK_TIMEOUT_SEC = 900
DEFAULT_GROK_PERMISSION_MODE = "bypassPermissions"
DEFAULT_GROK_MAX_TURNS = 50
DEFAULT_GROK_MAX_CONCURRENCY = 1
# Parent Grok sessions export these; inheriting them would attach gateway
# turns to the wrong conversation instead of --session-id / --resume.
INHERITED_SESSION_ENV_KEYS = ("GROK_SESSION_ID", "GROK_AGENT", "GROK_WORKTREE")


def resolve_grok_bin() -> str:
    """
    Resolve the grok executable path via:
    1. Environment variable GROK_BIN
    2. PATH lookup (shutil.which)
    3. Known standard installation fallback paths
    """
    env_path = os.environ.get("GROK_BIN")
    if env_path:
        return env_path
    which_path = shutil.which("grok")
    if which_path:
        return which_path
    for candidate in FALLBACK_GROK_PATHS:
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return DEFAULT_GROK_BIN


@dataclass(frozen=True)
class GrokConfig:
    """Provider configuration for Grok Build CLI execution."""
    bin_path: str = DEFAULT_GROK_BIN
    default_model: str | None = None
    default_effort: str | None = None
    default_timeout_sec: int = DEFAULT_GROK_TIMEOUT_SEC
    permission_mode: str = DEFAULT_GROK_PERMISSION_MODE
    max_turns: int = DEFAULT_GROK_MAX_TURNS
    max_concurrency: int = DEFAULT_GROK_MAX_CONCURRENCY

    @classmethod
    def from_env(cls) -> GrokConfig:
        """Resolve Grok provider configuration from environment variables."""
        bin_path = resolve_grok_bin()
        model = os.environ.get("GROK_MODEL")
        effort = os.environ.get("GROK_EFFORT")
        timeout_sec = int(os.environ.get("GROK_AGENT_TIMEOUT_SEC", DEFAULT_GROK_TIMEOUT_SEC))
        permission_mode = os.environ.get("GROK_PERMISSION_MODE", DEFAULT_GROK_PERMISSION_MODE)
        max_turns = int(os.environ.get("GROK_MAX_TURNS", DEFAULT_GROK_MAX_TURNS))
        max_concurrency = int(os.environ.get("GROK_MAX_CONCURRENCY", DEFAULT_GROK_MAX_CONCURRENCY))
        return cls(
            bin_path=bin_path,
            default_model=model,
            default_effort=effort,
            default_timeout_sec=timeout_sec,
            permission_mode=permission_mode,
            max_turns=max_turns,
            max_concurrency=max_concurrency,
        )


def parse_grok_json(stdout_text: str) -> dict[str, Any] | None:
    """Parse grok CLI JSON stdout, tolerating potential leading/trailing non-JSON log lines."""
    if not stdout_text or not stdout_text.strip():
        return None
    text = stdout_text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Fallback: attempt to locate outermost JSON object { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def extract_grok_usage(parsed: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize Grok usage dictionary strictly to Section 10 standard keys."""
    if not isinstance(parsed, dict):
        return normalize_usage(None)

    usage_dict = parsed.get("usage")
    cost = parsed.get("total_cost_usd")
    if cost is None:
        cost = parsed.get("cost")

    raw_u: dict[str, Any] = {}
    if isinstance(usage_dict, dict):
        raw_u["input_tokens"] = usage_dict.get("input_tokens")
        raw_u["output_tokens"] = usage_dict.get("output_tokens")
        raw_u["total_tokens"] = usage_dict.get("total_tokens")
    raw_u["cost_usd"] = cost

    return normalize_usage(raw_u)


class GrokAdapter(ExecutorAdapter):
    """
    Executor Adapter interfacing with Grok Build CLI (grok).
    Conforms to the unified ExecutorAdapter specification (Goal Prompt Section 7, 46).
    """

    name: str = "grok"

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        bin_path: str | None = None,
        default_timeout_sec: int | None = None,
        permission_mode: str | None = None,
        config: GrokConfig | None = None,
    ) -> None:
        self.config = config or GrokConfig.from_env()
        self.runner = runner if runner is not None else run_process_group
        self.bin_path = bin_path or self.config.bin_path
        self.default_timeout_sec = (
            default_timeout_sec
            if default_timeout_sec is not None
            else self.config.default_timeout_sec
        )
        self.permission_mode = (
            permission_mode
            if permission_mode is not None
            else self.config.permission_mode
        )
        self.max_turns = self.config.max_turns
        self.total_process_timeout = self.default_timeout_sec

    def _run(
        self,
        cmd: Sequence[str],
        timeout_sec: float,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Deterministic runner dispatch delegating to isolated process group runner."""
        if cwd is not None:
            return self.runner(cmd, timeout_sec, env=env, cwd=cwd)
        if env is not None:
            return self.runner(cmd, timeout_sec, env=env)
        return self.runner(cmd, timeout_sec)

    def _execution_env(self) -> dict[str, str]:
        """Copy the process environment without parent Grok session identifiers."""
        env = os.environ.copy()
        for key in INHERITED_SESSION_ENV_KEYS:
            env.pop(key, None)
        return env

    def build_command(
        self,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        is_continuation: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """
        Construct grok CLI command adhering to the verified headless contract:
        - Continuation passes `--resume <SESSION_ID>`
        - New session with explicit ID passes `--session-id <SESSION_ID>`
        - `--cwd <CWD>`
        - `--output-format json`
        - `--permission-mode <MODE>` (verified substitute for non-existent `--yolo`)
        - optional `--model`, `--effort`, `--max-turns`
        - `-p <PROMPT>` is the final argument
        """
        cmd = [self.bin_path]
        if is_continuation and session_id:
            cmd.extend(["--resume", str(session_id)])
        elif session_id:
            cmd.extend(["--session-id", str(session_id)])

        if cwd:
            cmd.extend(["--cwd", str(cwd)])

        cmd.extend(["--output-format", "json"])

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        effective_model = model or self.config.default_model
        if effective_model:
            cmd.extend(["--model", str(effective_model)])

        effective_effort = effort or self.config.default_effort
        if effective_effort:
            cmd.extend(["--effort", str(effective_effort)])

        if self.max_turns and int(self.max_turns) > 0:
            cmd.extend(["--max-turns", str(int(self.max_turns))])

        cmd.extend(["-p", str(prompt)])
        return cmd

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
        Execute a prompt turn against the Grok Build CLI worker.
        Returns a standardized ExecutorResult conforming to Section 10 schema.
        """
        start_time = time.monotonic()
        effective_timeout = (
            float(timeout_sec) if timeout_sec is not None else float(self.default_timeout_sec)
        )
        if effective_timeout <= 0:
            raise subprocess.TimeoutExpired(cmd=[self.bin_path], timeout=effective_timeout)

        is_continuation = session_id is not None
        effective_session_id = session_id if is_continuation else str(uuid.uuid4())

        cmd = self.build_command(
            prompt=prompt,
            cwd=cwd,
            session_id=effective_session_id,
            is_continuation=is_continuation,
            model=model,
            effort=effort,
        )

        res = self._run(
            cmd,
            timeout_sec=effective_timeout,
            env=self._execution_env(),
            cwd=cwd,
        )
        duration_ms = int((time.monotonic() - start_time) * 1000)

        out_text = res.stdout.strip() if res.stdout else ""
        err_text = res.stderr.strip() if res.stderr else ""
        parsed = parse_grok_json(out_text)

        # Normalized session_id from Grok output or generated UUID
        out_sid: str = effective_session_id
        if isinstance(parsed, dict) and parsed.get("sessionId"):
            out_sid = str(parsed["sessionId"]).strip()

        # Extracted response text
        response_text = parsed.get("text") if isinstance(parsed, dict) else None
        has_response = response_text is not None and bool(str(response_text).strip())

        normalized_usage_dict = extract_grok_usage(parsed)

        # 1. Valid Success case
        if res.returncode == 0 and isinstance(parsed, dict) and (has_response or parsed.get("sessionId")):
            return ExecutorResult(
                status="success",
                executor=self.name,
                session_id=out_sid,
                response=response_text or "",
                exit_code=res.returncode,
                timing={"duration_ms": duration_ms},
                usage=normalized_usage_dict,
                warnings=[],
                error=None,
                raw={"parsed": parsed, "stdout": out_text, "stderr": err_text},
            )

        # 2. Partial Success case: non-zero returncode but produced usable assistant response
        if res.returncode != 0 and has_response:
            error_detail = (
                err_text
                if err_text
                else f"Grok process exited with code {res.returncode}"
            )
            return ExecutorResult(
                status="partial_success",
                executor=self.name,
                session_id=out_sid,
                response=response_text,
                exit_code=res.returncode,
                timing={"duration_ms": duration_ms},
                usage=normalized_usage_dict,
                warnings=[
                    "grok exited with non-zero status code after producing output"
                ],
                error=error_detail,
                raw={"parsed": parsed, "stdout": out_text, "stderr": err_text},
            )

        # 3. Genuine Failure / Error case
        if err_text:
            error_detail = err_text
        elif not isinstance(parsed, dict):
            error_detail = f"Grok output is not valid JSON (exit code {res.returncode})"
        elif parsed.get("error"):
            error_detail = str(parsed.get("error"))
        else:
            error_detail = f"Grok execution failed with exit code {res.returncode}"

        return ExecutorResult(
            status="error",
            executor=self.name,
            session_id=out_sid if is_continuation else None,
            response=response_text if has_response else None,
            exit_code=res.returncode,
            timing={"duration_ms": duration_ms},
            usage=normalized_usage_dict,
            warnings=[],
            error=error_detail,
            raw={"parsed": parsed, "stdout": out_text, "stderr": err_text},
        )

    def resume(
        self,
        *,
        prompt: str,
        session_id: str,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout_sec: int | None = None,
    ) -> ExecutorResult:
        """Resume an existing Grok session turn."""
        if not session_id or not str(session_id).strip():
            raise ValueError("session_id is required for resume")
        return self.invoke(
            prompt=prompt,
            session_id=session_id,
            cwd=cwd,
            model=model,
            effort=effort,
            timeout_sec=timeout_sec,
        )

    def health(self) -> dict[str, Any]:
        """Check operational availability of Grok CLI."""
        bin_exists = os.path.exists(self.bin_path) or bool(shutil.which(self.bin_path))
        return {
            "status": "online" if bin_exists else "unavailable",
            "service": "Grok Build Agent",
            "version": "1.0.5",
            "binary": self.bin_path,
            "available": bin_exists,
        }

    def capabilities(self) -> dict[str, Any]:
        """Return functional capabilities supported by Grok adapter."""
        return {
            "supports_session": True,
            "supports_model": True,
            "supports_effort": True,
            "supports_cwd": True,
            "supports_resume": True,
            "models": ["grok-4.6", "grok-4.5"],
            "efforts": ["low", "medium", "high"],
        }
