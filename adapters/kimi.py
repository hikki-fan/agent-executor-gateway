"""Kimi Code CLI adapter for the Agent Executor Gateway."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from adapters.base import ExecutorAdapter
from core.process import run_process_group
from core.result import ExecutorResult, normalize_usage

DEFAULT_KIMI_BIN = "/home/codex/.kimi-code/bin/kimi"
DEFAULT_KIMI_HOME = "/home/codex/.kimi-code"
DEFAULT_KIMI_TIMEOUT_SEC = 900
DEFAULT_KIMI_MAX_CONCURRENCY = 10


def resolve_kimi_bin() -> str:
    """Resolve the Kimi CLI executable using env, PATH, then its known install path."""
    return os.environ.get("KIMI_BIN") or shutil.which("kimi") or DEFAULT_KIMI_BIN


@dataclass(frozen=True)
class KimiConfig:
    """Kimi-specific paths and resource limits."""

    bin_path: str = DEFAULT_KIMI_BIN
    code_home: str = DEFAULT_KIMI_HOME
    default_model: str | None = None
    default_timeout_sec: int = DEFAULT_KIMI_TIMEOUT_SEC
    max_concurrency: int = DEFAULT_KIMI_MAX_CONCURRENCY

    @classmethod
    def from_env(cls) -> KimiConfig:
        return cls(
            bin_path=resolve_kimi_bin(),
            code_home=os.environ.get("KIMI_CODE_HOME", DEFAULT_KIMI_HOME),
            default_model=os.environ.get("KIMI_MODEL"),
            default_timeout_sec=int(os.environ.get("KIMI_AGENT_TIMEOUT_SEC", DEFAULT_KIMI_TIMEOUT_SEC)),
            max_concurrency=int(os.environ.get("KIMI_MAX_CONCURRENCY", DEFAULT_KIMI_MAX_CONCURRENCY)),
        )


class KimiAdapter(ExecutorAdapter):
    """Invoke Kimi Code in bounded non-interactive JSONL mode."""

    name = "kimi"

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        config: KimiConfig | None = None,
    ) -> None:
        self.config = config or KimiConfig.from_env()
        self.runner = runner or run_process_group
        self.bin_path = self.config.bin_path
        self.default_timeout_sec = self.config.default_timeout_sec
        self.total_process_timeout = self.default_timeout_sec

    def build_command(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
    ) -> list[str]:
        command = [self.bin_path]
        if session_id:
            command.extend(["--session", str(session_id)])
        effective_model = model or self.config.default_model
        if effective_model:
            command.extend(["--model", str(effective_model)])
        command.extend(["--prompt", str(prompt), "--output-format", "stream-json"])
        return command

    def _execution_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["KIMI_CODE_HOME"] = self.config.code_home
        # Avoid interactive update notices or prompts in a supervised service.
        env["KIMI_CLI_NO_AUTO_UPDATE"] = "1"
        return env

    def _run(
        self,
        command: Sequence[str],
        timeout_sec: float,
        cwd: str | None,
    ) -> subprocess.CompletedProcess:
        kwargs: dict[str, Any] = {"env": self._execution_env()}
        if cwd is not None:
            kwargs["cwd"] = cwd
        return self.runner(command, timeout_sec, **kwargs)

    @staticmethod
    def _parse_stream(stdout_text: str) -> tuple[str | None, str | None, int]:
        """Return final assistant text, the CLI resume hint, and parsed line count."""
        assistant_messages: list[tuple[str, bool]] = []
        session_id: str | None = None
        parsed_lines = 0
        for line in stdout_text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            parsed_lines += 1
            if event.get("role") == "meta" and event.get("type") == "session.resume_hint":
                candidate = event.get("session_id") or event.get("sessionId") or event.get("id")
                if isinstance(candidate, str) and candidate.strip():
                    session_id = candidate.strip()
            elif event.get("role") == "assistant":
                content = event.get("content")
                if isinstance(content, str) and content.strip():
                    assistant_messages.append((content.strip(), bool(event.get("tool_calls"))))

        final_messages = [content for content, has_tool_calls in assistant_messages if not has_tool_calls]
        response = final_messages[-1] if final_messages else None
        return response, session_id, parsed_lines

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
        start = time.monotonic()
        effective_timeout = float(
            self.default_timeout_sec if timeout_sec is None else timeout_sec
        )
        if effective_timeout <= 0:
            raise subprocess.TimeoutExpired(cmd=[self.bin_path], timeout=effective_timeout)

        command = self.build_command(prompt=prompt, session_id=session_id, model=model)
        process = self._run(command, effective_timeout, cwd)
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout_text = process.stdout or ""
        response, resumed_session_id, parsed_lines = self._parse_stream(stdout_text)
        warnings = []
        if effort:
            warnings.append("Kimi Code CLI does not expose a reasoning-effort flag; the requested effort was ignored")

        if process.returncode == 0 and response:
            return ExecutorResult(
                status="success",
                executor=self.name,
                session_id=resumed_session_id or session_id,
                response=response,
                exit_code=process.returncode,
                timing={"duration_ms": duration_ms},
                usage=normalize_usage(None),
                warnings=warnings,
                raw={"jsonl_events": parsed_lines},
            )

        # Keep provider stdout/stderr out of the Gateway result: tool transcripts
        # may contain private repository content, and provider diagnostics are not
        # required to identify a failed CLI invocation.
        safe_error = (
            "Kimi returned no assistant response"
            if process.returncode == 0
            else f"Kimi CLI exited with code {process.returncode}"
        )
        return ExecutorResult(
            status="error",
            executor=self.name,
            session_id=resumed_session_id or session_id,
            response=None,
            exit_code=process.returncode,
            timing={"duration_ms": duration_ms},
            usage=normalize_usage(None),
            warnings=warnings,
            error=safe_error,
            raw={"jsonl_events": parsed_lines},
        )

    def health(self) -> dict[str, Any]:
        """Report availability without running a model request or exposing credentials."""
        binary_ok = os.path.isfile(self.bin_path) and os.access(self.bin_path, os.X_OK)
        credentials = os.path.join(self.config.code_home, "credentials", "kimi-code.json")
        auth_ok = os.path.isfile(credentials) and not os.path.islink(credentials)
        available = binary_ok and auth_ok
        return {
            "status": "online" if available else "unavailable",
            "service": "Kimi Code",
            "available": available,
            "binary_installed": binary_ok,
            "authentication_configured": auth_ok,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "supports_session": True,
            "supports_model": True,
            "supports_effort": False,
            "supports_cwd": True,
            "supports_resume": True,
            "output_format": "stream-json",
        }
