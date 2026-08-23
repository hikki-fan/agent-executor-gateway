"""
Antigravity Adapter Implementation for Agent Executor Gateway.
Encapsulates all Google Antigravity (AGY) provider-specific logic:
- Binary resolution & CLI command generation (exact legacy flag truthiness & ordering before -p)
- Explicit 1:1 session_id <-> conversation_id mapping
- AGY output JSON parsing & validation
- Pre-execution transient error retry (0-turn EOF / network errors <= 3 times on new sessions)
- Contradictory ERROR + response partial_success classification
- Diagnostic error detail generation
- Health checks and capability introspection
"""

from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from adapters.base import ExecutorAdapter
from core.process import run_process_group
from core.result import ExecutorResult, normalize_usage
from core.timeout import DeadlineTimer

# Regular expression identifying retryable pre-execution transient failures
RETRYABLE_ERROR_PATTERNS = re.compile(
    r'\b(eof|broken pipe|connection reset|connection refused|network|temporary failure|resource temporarily unavailable|timeout before start)\b',
    re.IGNORECASE,
)

DEFAULT_AGY_BIN = "/home/codex/.local/bin/agy"
DEFAULT_AGY_MAX_CONCURRENCY = 1
DEFAULT_AGY_TIMEOUT_SEC = 300
DEFAULT_AGY_AUTH_GRACE_SEC = 30


def resolve_agy_bin() -> str:
    """Resolve the agy binary path using environment variable, PATH, or standard fallback."""
    return os.environ.get("AGY_BIN") or shutil.which("agy") or DEFAULT_AGY_BIN


@dataclass(frozen=True)
class AntigravityConfig:
    """Provider configuration for Antigravity (AGY) execution."""
    bin_path: str = DEFAULT_AGY_BIN
    max_concurrency: int = DEFAULT_AGY_MAX_CONCURRENCY
    subprocess_timeout_sec: int = DEFAULT_AGY_TIMEOUT_SEC
    auth_grace_sec: int = DEFAULT_AGY_AUTH_GRACE_SEC

    @property
    def total_process_timeout_sec(self) -> int:
        return self.subprocess_timeout_sec + self.auth_grace_sec

    @classmethod
    def from_env(cls) -> AntigravityConfig:
        """Resolve AGY provider configuration from environment variables."""
        bin_path = resolve_agy_bin()
        max_concurrency = int(os.environ.get("AGY_MAX_CONCURRENCY", DEFAULT_AGY_MAX_CONCURRENCY))
        subprocess_timeout = int(os.environ.get("ACP_AGENT_TIMEOUT_SEC", DEFAULT_AGY_TIMEOUT_SEC))
        auth_grace = max(0, int(os.environ.get("ACP_AUTH_GRACE_SEC", DEFAULT_AGY_AUTH_GRACE_SEC)))

        return cls(
            bin_path=bin_path,
            max_concurrency=max_concurrency,
            subprocess_timeout_sec=subprocess_timeout,
            auth_grace_sec=auth_grace,
        )


class AntigravityAdapter(ExecutorAdapter):
    """
    Executor Adapter interfacing with Google Antigravity CLI (agy).
    Conforms to the unified ExecutorAdapter specification.
    """

    name: str = "agy"

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        bin_path: str | None = None,
        subprocess_timeout: int | None = None,
        auth_grace_sec: int | None = None,
        max_retries: int = 3,
        config: AntigravityConfig | None = None,
    ) -> None:
        self.config = config or AntigravityConfig.from_env()
        self.runner = runner if runner is not None else run_process_group
        self.bin_path = bin_path or self.config.bin_path
        self.subprocess_timeout = (
            subprocess_timeout
            if subprocess_timeout is not None
            else self.config.subprocess_timeout_sec
        )
        self.auth_grace_sec = (
            auth_grace_sec
            if auth_grace_sec is not None
            else self.config.auth_grace_sec
        )
        self.total_process_timeout = self.subprocess_timeout + self.auth_grace_sec
        self.max_retries = max_retries

    def _run(
        self,
        cmd: Sequence[str],
        timeout_sec: float,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess:
        """
        Deterministic runner dispatch without exception catching.
        Dispatches two positional args when env and cwd are None,
        keyword env when env is provided and cwd is None,
        keyword env and cwd when cwd is provided.
        """
        if cwd is not None:
            return self.runner(cmd, timeout_sec, env=env, cwd=cwd)
        if env is not None:
            return self.runner(cmd, timeout_sec, env=env)
        return self.runner(cmd, timeout_sec)

    def build_command(
        self,
        prompt: str,
        conversation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """
        Construct agy CLI command with exact Phase 0 flag truthiness and ordering:
        All configuration flags precede `-p <prompt>`. The prompt is strictly the final argument.
        """
        cmd = [self.bin_path]
        if conversation_id:
            cmd.extend(["--conversation", str(conversation_id)])
        cmd.extend(["--output-format", "json", "--dangerously-skip-permissions"])
        if model:
            cmd.extend(["--model", str(model)])
        if effort:
            cmd.extend(["--effort", str(effort)])
        cmd.extend(["-p", str(prompt)])
        return cmd

    def is_retryable_pre_execution_error(
        self,
        proc_result: subprocess.CompletedProcess | None,
        parsed_json: Any = None,
        cmd: Sequence[str] | None = None,
    ) -> bool:
        """
        Evaluate if an error is a pre-execution transient error eligible for retry.
        Strict Rules:
        - Resumed turn (--conversation in cmd) -> NEVER retry.
        - parsed_json must be a valid dict with status == "ERROR".
        - conversation_id must NOT exist or be non-empty.
        - num_turns must be strictly 0.
        - usage.total_tokens must be strictly 0.
        - response must be empty / whitespace.
        - error message must match RETRYABLE_ERROR_PATTERNS.
        """
        if cmd and "--conversation" in cmd:
            return False

        if not isinstance(parsed_json, dict):
            return False

        if parsed_json.get("status") != "ERROR":
            return False

        if parsed_json.get("conversation_id"):
            return False

        num_turns = parsed_json.get("num_turns", 0)
        if num_turns != 0:
            return False

        usage = parsed_json.get("usage")
        if isinstance(usage, dict):
            if usage.get("total_tokens", 0) != 0:
                return False
        elif usage is not None:
            return False

        response_content = parsed_json.get("response", "")
        if response_content and str(response_content).strip():
            return False

        err_msg = str(parsed_json.get("error", "")) + " " + str(parsed_json.get("message", ""))
        if RETRYABLE_ERROR_PATTERNS.search(err_msg):
            return True

        return False

    def has_cli_response(self, parsed_json: Any) -> bool:
        """Return True when agy preserved a non-empty assistant response."""
        if not isinstance(parsed_json, dict):
            return False
        response = parsed_json.get("response")
        return response is not None and bool(str(response).strip())

    def is_partial_success_result(self, parsed_json: Any) -> bool:
        """
        Recognize agy print-mode's contradictory terminal result: an ERROR status
        accompanied by a usable assistant response.
        """
        return (
            isinstance(parsed_json, dict)
            and parsed_json.get("status") == "ERROR"
            and self.has_cli_response(parsed_json)
        )

    def cli_error_detail(
        self,
        proc_result: subprocess.CompletedProcess | None,
        parsed_json: Any,
        stderr_text: str,
    ) -> str:
        """Build a stable diagnostic message preserving upstream provider error."""
        if isinstance(parsed_json, dict) and parsed_json.get("error"):
            return str(parsed_json.get("error"))
        if stderr_text:
            return stderr_text
        if not isinstance(parsed_json, dict):
            return "CLI output is not a valid JSON object"
        if parsed_json.get("status") != "SUCCESS":
            return f"CLI returned status: {parsed_json.get('status')}"
        if proc_result is not None:
            return f"CLI process exited with code {proc_result.returncode}"
        return "CLI process failed"

    def execute_with_retry(
        self,
        cmd: Sequence[str],
        total_timeout_sec: float,
        max_retries: int | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess, dict[str, Any] | None]:
        """
        Execute agy command with monotonic total timeout budget across retry attempts.
        Continuation commands (containing --conversation) are strictly single-attempt.
        """
        if total_timeout_sec <= 0:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=total_timeout_sec)

        is_continuation = "--conversation" in cmd
        limit_retries = 1 if is_continuation else (max_retries if max_retries is not None else self.max_retries)

        timer = DeadlineTimer(total_timeout_sec)
        attempts = 0
        last_res: subprocess.CompletedProcess | None = None
        last_parsed: dict[str, Any] | None = None

        while attempts < limit_retries:
            attempts += 1
            remaining = timer.check_or_raise(cmd)

            last_res = self._run(cmd, remaining, env=env, cwd=cwd)

            stdout_text = last_res.stdout.strip() if last_res.stdout else ""
            try:
                raw_parsed = json.loads(stdout_text) if stdout_text else None
                last_parsed = raw_parsed if isinstance(raw_parsed, dict) else None
            except Exception:
                last_parsed = None

            if last_res.returncode == 0 and isinstance(last_parsed, dict) and last_parsed.get("status") == "SUCCESS":
                return last_res, last_parsed

            if attempts < limit_retries and self.is_retryable_pre_execution_error(last_res, last_parsed, cmd=cmd):
                backoff = 0.3 * attempts
                if timer.remaining() > backoff:
                    time.sleep(backoff)
                    continue
                else:
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=total_timeout_sec)

            break

        if last_res is None:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=total_timeout_sec)

        return last_res, last_parsed

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
        Execute a prompt turn using the Antigravity CLI and return a standardized ExecutorResult.
        """
        start_time = time.monotonic()
        effective_timeout = (
            float(timeout_sec) if timeout_sec is not None else float(self.total_process_timeout)
        )

        cmd = self.build_command(
            prompt=prompt,
            conversation_id=session_id,
            model=model,
            effort=effort,
        )

        res, parsed = self.execute_with_retry(
            cmd=cmd,
            total_timeout_sec=effective_timeout,
            max_retries=self.max_retries,
            cwd=cwd,
        )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        out_text = res.stdout.strip() if res.stdout else ""
        err_text = res.stderr.strip() if res.stderr else ""

        is_valid_success = (
            res.returncode == 0
            and isinstance(parsed, dict)
            and parsed.get("status") == "SUCCESS"
        )

        out_cid: str | None = None
        if is_valid_success:
            if session_id:
                out_cid = str(session_id)
            else:
                top_cid = parsed.get("conversation_id")
                if top_cid and str(top_cid).strip():
                    out_cid = str(top_cid).strip()
                else:
                    out_cid = None
                    is_valid_success = False

        is_partial_success = self.is_partial_success_result(parsed)
        partial_cid = session_id or (
            str(parsed.get("conversation_id")).strip()
            if isinstance(parsed, dict) and parsed.get("conversation_id")
            else None
        )

        raw_usage = parsed.get("usage") if isinstance(parsed, dict) else None
        normalized_usage_dict = normalize_usage(raw_usage)

        if is_valid_success and out_cid:
            return ExecutorResult(
                status="success",
                executor=self.name,
                session_id=out_cid,
                response=parsed.get("response", ""),
                exit_code=res.returncode,
                timing={"duration_ms": duration_ms},
                usage=normalized_usage_dict,
                warnings=[],
                error=None,
                raw={"parsed": parsed, "stdout": out_text, "stderr": err_text},
            )

        if is_partial_success and partial_cid:
            upstream_error = self.cli_error_detail(res, parsed, err_text)
            return ExecutorResult(
                status="partial_success",
                executor=self.name,
                session_id=str(partial_cid),
                response=parsed.get("response", ""),
                exit_code=res.returncode,
                timing={"duration_ms": duration_ms},
                usage=normalized_usage_dict,
                warnings=[
                    "agy reported ERROR after producing a non-empty response; review the response before relying on it"
                ],
                error=upstream_error,
                raw={
                    "parsed": parsed,
                    "stdout": out_text,
                    "stderr": err_text,
                    "upstream_status": parsed.get("status"),
                    "upstream_error": upstream_error,
                    "cli_exit_code": res.returncode,
                },
            )

        # Genuine failure or missing ID
        if (
            isinstance(parsed, dict)
            and parsed.get("status") == "SUCCESS"
            and not session_id
            and not parsed.get("conversation_id")
        ):
            error_detail = (
                "CLI returned status: SUCCESS but missing required top-level 'conversation_id' field in JSON"
            )
        elif is_partial_success and not partial_cid:
            error_detail = "CLI returned a response with status ERROR but no conversation_id was available"
        else:
            error_detail = self.cli_error_detail(res, parsed, err_text)

        resp_val = parsed.get("response") if isinstance(parsed, dict) else None
        if resp_val is not None and not str(resp_val).strip():
            resp_val = None

        return ExecutorResult(
            status="error",
            executor=self.name,
            session_id=session_id if session_id else None,
            response=resp_val,
            exit_code=res.returncode if res else None,
            timing={"duration_ms": duration_ms},
            usage=normalized_usage_dict,
            warnings=[],
            error=error_detail,
            raw={"parsed": parsed, "stdout": out_text, "stderr": err_text},
        )

    def health(self) -> dict[str, Any]:
        """Check availability and binary health of Antigravity CLI."""
        bin_exists = os.path.exists(self.bin_path) or bool(shutil.which(self.bin_path))
        return {
            "status": "online" if bin_exists else "unavailable",
            "service": "Antigravity REST Bridge Server",
            "version": "2.4.0",
            "mode": "explicit_conversation_cli",
            "binary": self.bin_path,
            "available": bin_exists,
        }

    def capabilities(self) -> dict[str, Any]:
        """Return supported features and options for Antigravity."""
        return {
            "supports_session": True,
            "supports_model": True,
            "supports_effort": True,
            "supports_cwd": True,
            "models": ["flash", "pro"],
            "efforts": ["low", "medium", "high"],
        }
