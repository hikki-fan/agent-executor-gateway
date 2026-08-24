#!/usr/bin/env python3
"""
Comprehensive Unit, Contract, Route, and Smoke Tests for GrokAdapter and Grok Routes (Phase 4).

Tests:
1. GrokAdapter contract, inheritance (ExecutorAdapter ABC), name == 'grok'
2. Binary resolution (GROK_BIN env var, PATH lookup, standard fallback paths)
3. GrokConfig environment resolution and defaults
4. Command construction: new session (--session-id) vs continuation (--resume), flags ordering, cwd, model, effort
5. JSON parsing robustness (valid JSON, JSON with surrounding text, malformed output)
6. Usage and cost normalization to Section 10 standard 4-key schema
7. Session normalization: UUID generation on new session, preservation on continuation
8. Partial-success classification (non-zero exit code with usable text response)
9. Error classification (zero response or invalid JSON)
10. Non-positive timeout raises subprocess.TimeoutExpired immediately
11. Explicit resume() method validation
12. Health check and capabilities contract
13. HTTP GET /v1/executors/grok/health endpoint delegation
14. HTTP POST /v1/executors/grok/invoke:
    - New session invocation & Section 10 result schema
    - Continuation session invocation (--resume)
    - CWD parameter propagation
    - Model and effort parameter propagation
    - Partial success response (HTTP 200, status partial_success)
    - Adapter execution failure (HTTP 500)
    - Timeout expired (HTTP 504)
    - Per-session concurrency lock (HTTP 409)
15. Safe live disposable smoke test (creates hello.txt, then resumes same session to edit to hello world)
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PROD_TOKEN_PATH = "/home/codex/.codex/acp_token"

TEMP_TOKEN_DIR: str | None = None
TEMP_TOKEN_FILE: str | None = None

if "acp_server" in sys.modules:
    acp_server_mod = sys.modules["acp_server"]
    resolved_token_file = os.path.realpath(getattr(acp_server_mod, "TOKEN_FILE", ""))
    if resolved_token_file == os.path.realpath(PROD_TOKEN_PATH):
        raise RuntimeError(
            "Security violation: acp_server was preloaded with the production token file path. "
            "Refusing to mutate preloaded module state."
        )
    import acp_server
    TEST_TOKEN = acp_server.ACP_AUTH_TOKEN
else:
    TEMP_TOKEN_DIR = tempfile.mkdtemp(prefix="grok_test_token_")
    TEMP_TOKEN_FILE = os.path.join(TEMP_TOKEN_DIR, "test_acp_token")
    TEST_TOKEN = "test_grok_bearer_token_1234567890abcdef"
    with open(TEMP_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(TEST_TOKEN)
    os.chmod(TEMP_TOKEN_FILE, 0o600)

    os.environ["ACP_TOKEN_FILE"] = TEMP_TOKEN_FILE
    os.environ["ACP_PORT"] = "0"
    os.environ["AGY_MAX_CONCURRENCY"] = "1"
    os.environ["ACP_AGENT_TIMEOUT_SEC"] = "10"

    import acp_server

    if os.path.realpath(acp_server.TOKEN_FILE) == os.path.realpath(PROD_TOKEN_PATH):
        raise RuntimeError("Security violation: acp_server imported with production token path.")


def _cleanup_test_tokens() -> None:
    try:
        if TEMP_TOKEN_FILE and os.path.exists(TEMP_TOKEN_FILE):
            os.remove(TEMP_TOKEN_FILE)
        if TEMP_TOKEN_DIR and os.path.exists(TEMP_TOKEN_DIR):
            os.rmdir(TEMP_TOKEN_DIR)
    except Exception:
        pass


atexit.register(_cleanup_test_tokens)

from adapters.base import ExecutorAdapter
from adapters.grok import (
    DEFAULT_GROK_BIN,
    DEFAULT_GROK_MAX_TURNS,
    DEFAULT_GROK_TIMEOUT_SEC,
    INHERITED_SESSION_ENV_KEYS,
    GrokAdapter,
    GrokConfig,
    extract_grok_usage,
    parse_grok_json,
    resolve_grok_bin,
)
from core.process import run_process_group
from core.result import ExecutorResult, normalize_usage


class TestGrokAdapterUnit(unittest.TestCase):
    """Unit and contract tests for GrokAdapter and GrokConfig."""

    def test_01_adapter_contract_and_inheritance(self):
        """GrokAdapter inherits ExecutorAdapter ABC and has name 'grok'."""
        adapter = GrokAdapter()
        self.assertIsInstance(adapter, ExecutorAdapter)
        self.assertEqual(adapter.name, "grok")

    def test_02_binary_resolution_hierarchy(self):
        """Binary resolution respects GROK_BIN env var, PATH lookup, and fallback locations."""
        orig_env = os.environ.copy()
        try:
            # 1. Custom GROK_BIN env var
            os.environ["GROK_BIN"] = "/custom/bin/grok"
            self.assertEqual(resolve_grok_bin(), "/custom/bin/grok")

            # 2. PATH lookup when GROK_BIN is unset
            del os.environ["GROK_BIN"]
            with patch("shutil.which", return_value="/usr/bin/grok"):
                self.assertEqual(resolve_grok_bin(), "/usr/bin/grok")

            # 3. Fallback path when PATH lookup fails
            with patch("shutil.which", return_value=None):
                with patch("os.path.exists", return_value=True), patch("os.access", return_value=True):
                    resolved = resolve_grok_bin()
                    self.assertTrue(resolved.endswith("grok"))
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

    def test_03_grok_config_from_env(self):
        """GrokConfig parses environment settings and applies defaults."""
        orig_env = os.environ.copy()
        try:
            os.environ["GROK_BIN"] = "/opt/grok/bin/grok"
            os.environ["GROK_MODEL"] = "grok-4.6-build"
            os.environ["GROK_EFFORT"] = "high"
            os.environ["GROK_AGENT_TIMEOUT_SEC"] = "450"
            os.environ["GROK_PERMISSION_MODE"] = "bypassPermissions"
            os.environ["GROK_MAX_TURNS"] = "12"

            cfg = GrokConfig.from_env()
            self.assertEqual(cfg.bin_path, "/opt/grok/bin/grok")
            self.assertEqual(cfg.default_model, "grok-4.6-build")
            self.assertEqual(cfg.default_effort, "high")
            self.assertEqual(cfg.default_timeout_sec, 450)
            self.assertEqual(cfg.permission_mode, "bypassPermissions")
            self.assertEqual(cfg.max_turns, 12)
            self.assertEqual(DEFAULT_GROK_MAX_TURNS, 50)
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

    def test_04_command_builder_new_session_vs_continuation(self):
        """build_command generates exact flags for new sessions (--session-id) vs continuation (--resume)."""
        adapter = GrokAdapter(bin_path="/home/codex/.local/bin/grok")

        # Case 1: New session with specified session_id
        cmd_new = adapter.build_command(
            prompt="Build feature",
            cwd="/workspace/repo",
            session_id="00000000-1111-2222-3333-444444444444",
            is_continuation=False,
            model="grok-4.6",
            effort="medium",
        )
        self.assertEqual(cmd_new[0], "/home/codex/.local/bin/grok")
        self.assertIn("--session-id", cmd_new)
        self.assertNotIn("--resume", cmd_new)
        sid_idx = cmd_new.index("--session-id")
        self.assertEqual(cmd_new[sid_idx + 1], "00000000-1111-2222-3333-444444444444")
        self.assertIn("--cwd", cmd_new)
        self.assertEqual(cmd_new[cmd_new.index("--cwd") + 1], "/workspace/repo")
        self.assertIn("--output-format", cmd_new)
        self.assertEqual(cmd_new[cmd_new.index("--output-format") + 1], "json")
        self.assertIn("--permission-mode", cmd_new)
        self.assertIn("--model", cmd_new)
        self.assertEqual(cmd_new[cmd_new.index("--model") + 1], "grok-4.6")
        self.assertIn("--effort", cmd_new)
        self.assertEqual(cmd_new[cmd_new.index("--effort") + 1], "medium")
        self.assertIn("--max-turns", cmd_new)
        self.assertEqual(cmd_new[cmd_new.index("--max-turns") + 1], "50")
        self.assertEqual(cmd_new[-2:], ["-p", "Build feature"])
        self.assertLess(cmd_new.index("--max-turns"), cmd_new.index("-p"))

        # Case 2: Continuation session
        cmd_cont = adapter.build_command(
            prompt="Fix bugs",
            cwd="/workspace/repo",
            session_id="00000000-1111-2222-3333-444444444444",
            is_continuation=True,
        )
        self.assertIn("--resume", cmd_cont)
        self.assertNotIn("--session-id", cmd_cont)
        resume_idx = cmd_cont.index("--resume")
        self.assertEqual(cmd_cont[resume_idx + 1], "00000000-1111-2222-3333-444444444444")
        self.assertEqual(cmd_cont[-2:], ["-p", "Fix bugs"])

    def test_05_json_parsing_and_surrounding_text_handling(self):
        """parse_grok_json successfully extracts JSON objects even when surrounded by text."""
        # 1. Clean JSON
        clean_json = '{"text": "Hello", "sessionId": "sess-1", "stopReason": "end_turn"}'
        self.assertEqual(parse_grok_json(clean_json), {"text": "Hello", "sessionId": "sess-1", "stopReason": "end_turn"})

        # 2. JSON surrounded by log lines
        wrapped_json = 'Starting Grok...\n{"text": "Hello", "sessionId": "sess-2"}\nDone.'
        self.assertEqual(parse_grok_json(wrapped_json), {"text": "Hello", "sessionId": "sess-2"})

        # 3. Invalid or empty JSON
        self.assertIsNone(parse_grok_json(""))
        self.assertIsNone(parse_grok_json("Not a json at all"))
        self.assertIsNone(parse_grok_json("{unclosed json"))

    def test_06_usage_and_cost_normalization(self):
        """extract_grok_usage normalizes Grok token counts and cost to standard Section 10 schema."""
        parsed_grok_output = {
            "text": "Done",
            "usage": {
                "input_tokens": 1200,
                "cache_read_input_tokens": 500,
                "output_tokens": 80,
                "reasoning_tokens": 30,
                "total_tokens": 1780,
            },
            "total_cost_usd": 0.005432,
            "num_turns": 1,
        }
        normalized = extract_grok_usage(parsed_grok_output)
        self.assertEqual(
            normalized,
            {
                "input_tokens": 1200,
                "output_tokens": 80,
                "total_tokens": 1780,
                "cost_usd": 0.005432,
            },
        )
        self.assertNotIn("cache_read_input_tokens", normalized)
        self.assertNotIn("reasoning_tokens", normalized)

    def test_07_invoke_successful_execution(self):
        """invoke() returns a standardized success ExecutorResult."""
        mock_output = {
            "text": "Code refactored successfully.",
            "stopReason": "end_turn",
            "sessionId": "grok-sess-uuid-777",
            "requestId": "req-123",
            "usage": {"input_tokens": 50, "output_tokens": 25, "total_tokens": 75},
            "total_cost_usd": 0.001,
        }
        mock_proc = subprocess.CompletedProcess(
            args=["grok"],
            returncode=0,
            stdout=json.dumps(mock_output),
            stderr="",
        )

        adapter = GrokAdapter(runner=lambda *args, **kwargs: mock_proc)
        result = adapter.invoke(
            prompt="Refactor code",
            cwd="/workspace/test",
            session_id=None,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.executor, "grok")
        self.assertEqual(result.session_id, "grok-sess-uuid-777")
        self.assertEqual(result.response, "Code refactored successfully.")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.usage["input_tokens"], 50)
        self.assertEqual(result.usage["cost_usd"], 0.001)
        self.assertEqual(result.warnings, [])
        self.assertIsNone(result.error)
        self.assertEqual(result.raw["parsed"]["stopReason"], "end_turn")
        self.assertNotIn("cache_read_input_tokens", result.usage)

    def test_07b_invoke_new_session_command_and_env_isolation(self):
        """New sessions pass --session-id, continuations pass --resume, and parent session env is stripped."""
        mock_output = {
            "text": "ok",
            "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
        captured: dict[str, object] = {}

        def recording_runner(cmd, timeout_sec, env=None, cwd=None):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env) if env is not None else None
            captured["cwd"] = cwd
            captured["timeout_sec"] = timeout_sec
            return subprocess.CompletedProcess(cmd, 0, json.dumps(mock_output), "")

        orig_session = os.environ.get("GROK_SESSION_ID")
        orig_agent = os.environ.get("GROK_AGENT")
        try:
            os.environ["GROK_SESSION_ID"] = "parent-session-must-not-leak"
            os.environ["GROK_AGENT"] = "1"
            adapter = GrokAdapter(runner=recording_runner, bin_path="/mock/bin/grok")
            result = adapter.invoke(prompt="Start work", cwd="/tmp/repo", session_id=None, timeout_sec=33)
        finally:
            if orig_session is None:
                os.environ.pop("GROK_SESSION_ID", None)
            else:
                os.environ["GROK_SESSION_ID"] = orig_session
            if orig_agent is None:
                os.environ.pop("GROK_AGENT", None)
            else:
                os.environ["GROK_AGENT"] = orig_agent

        self.assertEqual(result.status, "success")
        cmd = captured["cmd"]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "/mock/bin/grok")
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        generated = cmd[cmd.index("--session-id") + 1]
        uuid.UUID(generated)
        self.assertEqual(cmd[-2:], ["-p", "Start work"])
        self.assertEqual(captured["cwd"], "/tmp/repo")
        self.assertEqual(captured["timeout_sec"], 33.0)
        env = captured["env"]
        self.assertIsInstance(env, dict)
        for key in INHERITED_SESSION_ENV_KEYS:
            self.assertNotIn(key, env)

        captured.clear()
        adapter.invoke(prompt="Continue work", session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        cont_cmd = captured["cmd"]
        self.assertIn("--resume", cont_cmd)
        self.assertNotIn("--session-id", cont_cmd)
        self.assertEqual(cont_cmd[cont_cmd.index("--resume") + 1], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_08_invoke_partial_success_classification(self):
        """invoke() classifies non-zero exit with usable text output as partial_success."""
        mock_output = {
            "text": "Partial code edits applied.",
            "sessionId": "grok-sess-uuid-888",
        }
        mock_proc = subprocess.CompletedProcess(
            args=["grok"],
            returncode=1,
            stdout=json.dumps(mock_output),
            stderr="Warning: tool failed during second turn",
        )

        adapter = GrokAdapter(runner=lambda *args, **kwargs: mock_proc)
        result = adapter.invoke(
            prompt="Edit code",
            session_id="grok-sess-uuid-888",
        )

        self.assertEqual(result.status, "partial_success")
        self.assertEqual(result.executor, "grok")
        self.assertEqual(result.session_id, "grok-sess-uuid-888")
        self.assertEqual(result.response, "Partial code edits applied.")
        self.assertEqual(result.exit_code, 1)
        self.assertGreater(len(result.warnings), 0)
        self.assertIn("Warning: tool failed", result.error or "")

    def test_09_invoke_genuine_failure_classification(self):
        """invoke() classifies zero output or fatal errors as error status."""
        mock_proc = subprocess.CompletedProcess(
            args=["grok"],
            returncode=2,
            stdout="Fatal: failed to connect to backend",
            stderr="Connection refused",
        )

        adapter = GrokAdapter(runner=lambda *args, **kwargs: mock_proc)
        result = adapter.invoke(prompt="Run broken task")

        self.assertEqual(result.status, "error")
        self.assertEqual(result.executor, "grok")
        self.assertIsNone(result.response)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Connection refused", result.error or "")

    def test_09b_invoke_propagates_timeout_expired(self):
        """TimeoutExpired from the process runner is not swallowed by the adapter."""

        def timeout_runner(cmd, timeout_sec, env=None, cwd=None):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)

        adapter = GrokAdapter(runner=timeout_runner, bin_path="/mock/bin/grok")
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.invoke(prompt="Slow task", timeout_sec=4)

    def test_10_non_positive_timeout_raises_immediately(self):
        """Non-positive timeout raises subprocess.TimeoutExpired immediately."""
        adapter = GrokAdapter()
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.invoke(prompt="Test timeout", timeout_sec=0)

        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.invoke(prompt="Test timeout", timeout_sec=-10)

    def test_11_resume_method_validation(self):
        """resume() requires non-empty session_id and delegates to invoke()."""
        mock_output = {"text": "Resumed output", "sessionId": "sess-resume-1"}
        mock_proc = subprocess.CompletedProcess(args=["grok"], returncode=0, stdout=json.dumps(mock_output), stderr="")
        adapter = GrokAdapter(runner=lambda *args, **kwargs: mock_proc)

        # Empty session_id raises ValueError
        with self.assertRaises(ValueError):
            adapter.resume(prompt="Continue", session_id="")

        with self.assertRaises(ValueError):
            adapter.resume(prompt="Continue", session_id="   ")

        # Valid resume succeeds
        res = adapter.resume(prompt="Continue", session_id="sess-resume-1")
        self.assertEqual(res.status, "success")
        self.assertEqual(res.session_id, "sess-resume-1")
        self.assertEqual(res.response, "Resumed output")

    def test_12_health_and_capabilities_contracts(self):
        """health() and capabilities() report accurate executor metadata."""
        adapter = GrokAdapter(bin_path="/home/codex/.local/bin/grok")
        h = adapter.health()
        self.assertIn("status", h)
        self.assertIn("service", h)
        self.assertEqual(h["service"], "Grok Build Agent")
        self.assertIn("available", h)

        caps = adapter.capabilities()
        self.assertTrue(caps.get("supports_session"))
        self.assertTrue(caps.get("supports_cwd"))
        self.assertTrue(caps.get("supports_resume"))
        self.assertIn("grok-4.6", caps.get("models", []))


class TestGrokApiHTTP(unittest.TestCase):
    """HTTP integration tests for /v1/executors/grok/* endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.server = acp_server.ThreadedHTTPServer(("127.0.0.1", 0), acp_server.ACPRequestHandler)
        cls.server_port = cls.server.server_address[1]
        cls.server_url = f"http://127.0.0.1:{cls.server_port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        _cleanup_test_tokens()

    def _http_request(
        self,
        path: str,
        data: dict | list | None = None,
        token: str | None = TEST_TOKEN,
        custom_headers: dict | None = None,
        raw_body: bytes | str | None = None,
        method: str | None = None,
    ) -> tuple[int, dict]:
        url = f"{self.server_url}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if custom_headers:
            headers.update(custom_headers)

        if raw_body is not None:
            body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        elif data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        else:
            body = None

        http_method = method if method else ("POST" if (data is not None or raw_body is not None) else "GET")
        req = urllib.request.Request(url, data=body, headers=headers, method=http_method)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.getcode()
                resp_bytes = resp.read()
                try:
                    resp_data = json.loads(resp_bytes.decode("utf-8"))
                except Exception:
                    resp_data = {"raw": resp_bytes.decode("utf-8")}
                return status_code, resp_data
        except urllib.error.HTTPError as e:
            err_bytes = e.read()
            try:
                parsed = json.loads(err_bytes.decode("utf-8"))
            except Exception:
                parsed = {"raw": err_bytes.decode("utf-8")}
            return e.code, parsed

    def test_13_get_grok_health_endpoint(self):
        """GET /v1/executors/grok/health returns 200 and operational health metadata."""
        code, resp = self._http_request("/v1/executors/grok/health", token=None)
        self.assertEqual(code, 200)
        self.assertEqual(resp.get("service"), "Grok Build Agent")
        self.assertIn("status", resp)
        self.assertIn("available", resp)

    def test_14_post_grok_invoke_new_session_success(self):
        """POST /v1/executors/grok/invoke executes a new turn and returns Section 10 ExecutorResult."""
        mock_output = {
            "text": "Created module in repository.",
            "stopReason": "end_turn",
            "sessionId": "grok-http-session-111",
            "requestId": "req-999",
            "usage": {"input_tokens": 300, "output_tokens": 150, "total_tokens": 450},
            "total_cost_usd": 0.003,
        }
        mock_proc = subprocess.CompletedProcess(
            args=["grok"],
            returncode=0,
            stdout=json.dumps(mock_output),
            stderr="",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc) as mock_run:
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                data={
                    "prompt": "Create new module",
                    "cwd": "/workspace/demo_app",
                    "session_id": None,
                    "model": "grok-4.6",
                    "effort": "high",
                    "timeout_sec": 300,
                },
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)
            self.assertEqual(resp["status"], "success")
            self.assertEqual(resp["executor"], "grok")
            self.assertEqual(resp["session_id"], "grok-http-session-111")
            self.assertEqual(resp["response"], "Created module in repository.")
            self.assertEqual(resp["exit_code"], 0)
            self.assertIsInstance(resp["timing"]["duration_ms"], int)
            self.assertEqual(resp["usage"]["input_tokens"], 300)
            self.assertEqual(resp["usage"]["cost_usd"], 0.003)
            self.assertEqual(resp["warnings"], [])
            self.assertIsNone(resp["error"])

            # Verify arguments passed to subprocess runner
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("--session-id", called_cmd)
            self.assertIn("--cwd", called_cmd)
            self.assertEqual(mock_run.call_args[1].get("cwd"), "/workspace/demo_app")

    def test_15_post_grok_invoke_continuation_session(self):
        """POST /v1/executors/grok/invoke with session_id passes --resume."""
        mock_output = {
            "text": "Refined the module implementation.",
            "sessionId": "grok-http-session-111",
        }
        mock_proc = subprocess.CompletedProcess(
            args=["grok"],
            returncode=0,
            stdout=json.dumps(mock_output),
            stderr="",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc) as mock_run:
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                data={
                    "prompt": "Refine implementation",
                    "session_id": "grok-http-session-111",
                },
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)
            self.assertEqual(resp["status"], "success")
            self.assertEqual(resp["session_id"], "grok-http-session-111")
            self.assertEqual(resp["response"], "Refined the module implementation.")

            called_cmd = mock_run.call_args[0][0]
            self.assertIn("--resume", called_cmd)
            resume_idx = called_cmd.index("--resume")
            self.assertEqual(called_cmd[resume_idx + 1], "grok-http-session-111")

    def test_16_post_grok_invoke_timeout_returns_504(self):
        """Subprocess timeout on grok invocation returns HTTP 504 with structured error."""
        with patch("acp_server.run_agent_command", side_effect=subprocess.TimeoutExpired(cmd=["grok"], timeout=5.0)):
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                data={"prompt": "Long task", "session_id": "grok-timeout-sess", "timeout_sec": 5},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 504)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "grok")
            self.assertEqual(resp["session_id"], "grok-timeout-sess")
            self.assertIn("timed out", resp.get("error", "").lower())

    def test_17_post_grok_invoke_same_session_concurrency_409(self):
        """Concurrent requests to the same Grok session return HTTP 409 Conflict."""
        start_event = threading.Event()
        release_event = threading.Event()

        def slow_runner(cmd, timeout_sec, **kwargs):
            start_event.set()
            release_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"text": "Done", "sessionId": "grok-concurrent-sess"}),
                "",
            )

        with patch("acp_server.run_agent_command", side_effect=slow_runner):
            t1 = threading.Thread(
                target=lambda: self._http_request(
                    "/v1/executors/grok/invoke",
                    data={"prompt": "First turn", "session_id": "grok-concurrent-sess"},
                    token=TEST_TOKEN,
                )
            )
            t1.start()
            self.assertTrue(start_event.wait(timeout=3.0))

            # Colliding turn on same Grok session returns 409
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                data={"prompt": "Second turn colliding", "session_id": "grok-concurrent-sess"},
                token=TEST_TOKEN,
            )
            release_event.set()
            t1.join(timeout=5.0)

            self.assertEqual(code, 409)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "grok")
            self.assertIn("Conflict", resp.get("error", ""))

    def test_17b_post_grok_invoke_error_returns_500(self):
        """Genuine Grok adapter failure returns HTTP 500 with ExecutorResult schema."""
        mock_proc = subprocess.CompletedProcess(
            args=["grok"],
            returncode=2,
            stdout="",
            stderr="authentication required",
        )
        with patch("acp_server.run_agent_command", return_value=mock_proc):
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                data={"prompt": "Failing task"},
                token=TEST_TOKEN,
            )
        self.assertEqual(code, 500)
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["executor"], "grok")
        self.assertIn("authentication required", resp.get("error", ""))

    def test_17c_get_v1_executors_includes_grok(self):
        """GET /v1/executors lists grok alongside agy after Phase 4 registration."""
        code, resp = self._http_request("/v1/executors", token=None)
        self.assertEqual(code, 200)
        names = {entry["name"] for entry in resp.get("executors", [])}
        self.assertIn("agy", names)
        self.assertIn("grok", names)


class TestGrokLiveSmokeIntegration(unittest.TestCase):
    """
    Live Phase 4 Smoke Test:
    Executes actual installed grok CLI in disposable workspace:
    1. Turn 1: create hello.txt containing 'hello'
    2. Turn 2: resume same session to change content to 'hello world'
    Skips cleanly if grok binary is unavailable or unauthenticated.
    """

    def test_18_live_disposable_hello_txt_and_same_session_resume(self):
        if os.environ.get("RUN_LIVE_GROK_SMOKE") != "1":
            self.skipTest("set RUN_LIVE_GROK_SMOKE=1 to run the authenticated disposable smoke test")

        grok_home = os.environ.get("GROK_HOME")
        if not grok_home:
            self.skipTest("set GROK_HOME to an authenticated, writable Grok home for live smoke")

        grok_bin = resolve_grok_bin()
        if not os.path.exists(grok_bin) or not os.access(grok_bin, os.X_OK):
            self.skipTest(f"Grok executable not found at {grok_bin}")

        # Quick check if grok runs
        try:
            probe = subprocess.run(
                [grok_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if probe.returncode != 0:
                self.skipTest("Grok CLI exited with non-zero on --version")
        except Exception as e:
            self.skipTest(f"Failed to probe grok CLI: {e}")

        # Execute safe disposable test in isolated temporary directory
        temp_workspace = tempfile.mkdtemp(prefix="grok_phase4_smoke_")
        live_env = os.environ.copy()
        live_env.setdefault("HOME", "/home/codex")

        def live_runner(cmd, timeout_sec, env=None, cwd=None):
            child_env = dict(live_env)
            if env:
                child_env.update(env)
            return run_process_group(cmd, timeout_sec, env=child_env, cwd=cwd)

        adapter = GrokAdapter(
            runner=live_runner,
            bin_path=grok_bin,
            default_timeout_sec=90,
            permission_mode="bypassPermissions",
        )

        try:
            # Turn 1: Create hello.txt
            res1 = adapter.invoke(
                prompt="Create hello.txt in the current working directory containing exactly hello. Do not modify any other file.",
                cwd=temp_workspace,
                session_id=None,
                timeout_sec=90,
            )

            file_path = os.path.join(temp_workspace, "hello.txt")
            self.assertEqual(res1.status, "success")
            self.assertTrue(res1.session_id)
            session_uuid = res1.session_id
            self.assertTrue(os.path.exists(file_path))
            self.assertEqual(set(os.listdir(temp_workspace)), {"hello.txt"})
            with open(file_path, "r", encoding="utf-8") as f:
                content1 = f.read().strip()
            self.assertEqual(content1, "hello")

            # Turn 2: Resume same session and update hello.txt
            res2 = adapter.resume(
                prompt="change hello to hello world in hello.txt",
                cwd=temp_workspace,
                session_id=session_uuid,
                timeout_sec=90,
            )
            self.assertEqual(res2.status, "success")
            self.assertEqual(res2.session_id, session_uuid)

            with open(file_path, "r", encoding="utf-8") as f:
                content2 = f.read().strip()
            self.assertEqual(content2, "hello world")
            self.assertEqual(set(os.listdir(temp_workspace)), {"hello.txt"})

        finally:
            shutil.rmtree(temp_workspace, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
