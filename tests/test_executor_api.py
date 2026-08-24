#!/usr/bin/env python3
"""
Comprehensive Test Suite for Generic Executor API in Agent Executor Gateway (Phase 2).

Tests:
1. ExecutorRegistry and validation unit contracts
2. GET /v1/executors discovery endpoint (returns only registered executors, accurate available & supports_session)
3. GET /v1/executors/agy/health health check delegation (unauthenticated, delegates to adapter.health())
4. Unknown executor 404 handling for health and invoke endpoints
5. Strict Bearer token authentication on POST /v1/executors/{name}/invoke (401 on missing/invalid token)
6. Top-level JSON type and payload validation (HTTP 400 on non-object JSON and malformed syntax)
7. Detailed parameter validation on POST invoke (prompt, session_id, timeout_sec, cwd, model, effort)
8. Keyword argument forwarding to AntigravityAdapter (prompt, cwd, session_id, model, effort, timeout_sec)
9. New session vs continuation session distinction (session_id != null)
10. Strict conformance to Section 10 ExecutorResult schema (top-level fields only, normalized 4-key usage)
11. Partial-success execution handling (HTTP 200, ExecutorResult schema with warnings and error)
12. Adapter error handling (HTTP 500, ExecutorResult schema with error)
13. Subprocess TimeoutExpired handling (HTTP 504 Gateway Timeout with structured ExecutorResult schema)
14. Custom timeout_sec budget propagation and outer Future waiting window (+5s transport margin)
15. Unexpected internal exception handling (HTTP 500, not misreported as 504)
16. Per-session concurrency locking (HTTP 409 Conflict) for both Generic vs Generic and cross Generic vs Legacy
17. Global AGY worker concurrency admission control (HTTP 429 Too Many Requests) shared between Legacy and Generic
18. Exception-safe lock and semaphore release without leaks under all success and error paths
19. Behavioral equivalence between Legacy ACP API and Generic Executor API on identical mock AGY output
20. Security audit: zero token, credential, or full environment leakage in responses
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

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
    TEMP_TOKEN_DIR = tempfile.mkdtemp(prefix="executor_api_test_token_")
    TEMP_TOKEN_FILE = os.path.join(TEMP_TOKEN_DIR, "test_acp_token")
    TEST_TOKEN = "test_generic_bearer_token_1234567890abcdef"
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

from adapters.antigravity import AntigravityAdapter
from adapters.base import ExecutorAdapter
from api.executors import ExecutorRegistry, validate_invoke_request
from core.result import LEGAL_STATUSES, ExecutorResult, normalize_usage


class MockGenericAdapter(ExecutorAdapter):
    """Mock ExecutorAdapter for unit testing the registry and endpoint behavior."""
    name = "mock_adapter"

    def __init__(self, available: bool = True, supports_session: bool = True) -> None:
        self._available = available
        self._supports_session = supports_session

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
        return ExecutorResult(
            status="success",
            executor=self.name,
            session_id=session_id or "mock-session-1234",
            response=f"Mock processed: {prompt}",
            exit_code=0,
            timing={"duration_ms": 50},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "cost_usd": None},
            warnings=[],
            error=None,
            raw={"mock": True},
        )

    def health(self) -> dict:
        return {
            "status": "online" if self._available else "offline",
            "available": self._available,
            "service": "Mock Adapter",
        }

    def capabilities(self) -> dict:
        return {
            "supports_session": self._supports_session,
            "supports_cwd": True,
        }


class TestExecutorApiUnit(unittest.TestCase):
    """Unit tests for ExecutorRegistry and validate_invoke_request."""

    def test_01_executor_registry_operations(self):
        reg = ExecutorRegistry()
        self.assertEqual(reg.registered_names(), [])
        self.assertIsNone(reg.get("agy"))

        # Non-adapter raises TypeError
        with self.assertRaises(TypeError):
            reg.register("not_an_adapter")  # type: ignore

        mock_ad = MockGenericAdapter()
        reg.register(mock_ad)
        self.assertEqual(reg.registered_names(), ["mock_adapter"])
        self.assertIs(reg.get("mock_adapter"), mock_ad)

        exec_list = reg.list_executors()
        self.assertEqual(len(exec_list), 1)
        self.assertEqual(
            exec_list[0],
            {"name": "mock_adapter", "available": True, "supports_session": True},
        )

    def test_02_validate_invoke_request_all_rules(self):
        # 1. Non-dict root
        for invalid_root in [None, [], [1, 2], "string", 123, True, False]:
            params, err = validate_invoke_request(invalid_root)
            self.assertIsNone(params)
            self.assertIn("must be a JSON object", err or "")

        # 2. Missing or invalid prompt
        for bad_prompt in [None, "", "   ", 123, True, False, [], {}]:
            params, err = validate_invoke_request({"prompt": bad_prompt})
            self.assertIsNone(params)
            self.assertIn("prompt", err or "")

        # 3. Valid minimal request
        params, err = validate_invoke_request({"prompt": "Hello world"})
        self.assertIsNone(err)
        self.assertEqual(
            params,
            {
                "prompt": "Hello world",
                "cwd": None,
                "session_id": None,
                "model": None,
                "effort": None,
                "timeout_sec": None,
            },
        )

        # 4. Valid complete request
        valid_full = {
            "prompt": "Refactor code",
            "cwd": "/workspace/project",
            "session_id": "session-uuid-1234",
            "model": "pro",
            "effort": "high",
            "timeout_sec": 600,
        }
        params, err = validate_invoke_request(valid_full)
        self.assertIsNone(err)
        self.assertEqual(params, valid_full)

        # 5. Invalid session_id (empty string, whitespace, non-string, bool)
        for bad_sid in ["", "   ", 123, True, False, [], {}]:
            params, err = validate_invoke_request({"prompt": "Do it", "session_id": bad_sid})
            self.assertIsNone(params)
            self.assertIn("session_id", err or "")

        # Explicit null session_id is valid (indicates new session)
        params, err = validate_invoke_request({"prompt": "Do it", "session_id": None})
        self.assertIsNone(err)
        self.assertIsNone(params["session_id"])

        # 6. Invalid timeout_sec (zero, negative, bool, non-numeric)
        for bad_timeout in [0, -1, -10.5, True, False, "600", [], {}]:
            params, err = validate_invoke_request({"prompt": "Do it", "timeout_sec": bad_timeout})
            self.assertIsNone(params)
            self.assertIn("timeout_sec", err or "")

        # Positive floats are rejected because the public adapter contract uses int | None.
        params, err = validate_invoke_request({"prompt": "Do it", "timeout_sec": 45.5})
        self.assertIsNone(params)
        self.assertIn("positive integer", err or "")

        # Session identifiers are opaque and must not be silently rewritten.
        opaque_session = "  opaque-session/with spaces  "
        params, err = validate_invoke_request({"prompt": "Do it", "session_id": opaque_session})
        self.assertIsNone(err)
        self.assertEqual(params["session_id"], opaque_session)

        # 7. Invalid optional string fields (cwd, model, effort)
        for bad_field, key in [(123, "cwd"), (True, "model"), ([], "effort")]:
            params, err = validate_invoke_request({"prompt": "Do it", key: bad_field})
            self.assertIsNone(params)
            self.assertIn(key, err or "")


class TestExecutorApiHTTP(unittest.TestCase):
    """End-to-end HTTP contract tests for Generic Executor API endpoints."""

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

    # =========================================================================
    # 1. Discovery and Health Check Endpoints
    # =========================================================================

    def test_03_get_v1_executors_discovery(self):
        """GET /v1/executors lists real registered executors without authentication."""
        code, resp = self._http_request("/v1/executors", token=None)
        self.assertEqual(code, 200)
        self.assertIn("executors", resp)
        executors = resp["executors"]
        self.assertIsInstance(executors, list)

        # Phase 2: strictly only 'agy' registered; zero fake grok
        self.assertEqual(len(executors), 1)
        agy_entry = executors[0]
        self.assertEqual(agy_entry["name"], "agy")
        self.assertIn("available", agy_entry)
        self.assertIn("supports_session", agy_entry)
        self.assertTrue(agy_entry["supports_session"])
        self.assertNotIn("grok", [e["name"] for e in executors])

    def test_04_get_v1_executors_agy_health_delegation(self):
        """GET /v1/executors/agy/health delegates directly to AntigravityAdapter.health()."""
        adapter = acp_server.executor_registry.get("agy")
        sentinel = {"status": "sentinel", "available": False, "delegated": True}
        with patch.object(adapter, "health", return_value=sentinel) as health_mock:
            code, resp = self._http_request("/v1/executors/agy/health", token=None)
        self.assertEqual(code, 200)
        self.assertEqual(resp, sentinel)
        health_mock.assert_called_once_with()

    def test_04b_legacy_query_path_semantics_are_unchanged(self):
        """Phase 2 routing must not change the legacy server's literal path matching."""
        code, _ = self._http_request("/health?probe=1", token=None)
        self.assertEqual(code, 404)

    def test_05_unknown_executor_health_returns_404(self):
        """GET /v1/executors/{unknown}/health returns HTTP 404."""
        code, resp = self._http_request("/v1/executors/grok/health", token=None)
        self.assertEqual(code, 404)
        self.assertIn("not found", resp.get("error", "").lower())

        code, resp = self._http_request("/v1/executors/nonexistent_executor/health", token=None)
        self.assertEqual(code, 404)
        self.assertIn("not found", resp.get("error", "").lower())

    def test_06_unknown_executor_invoke_returns_404(self):
        """POST /v1/executors/{unknown}/invoke returns HTTP 404."""
        code, resp = self._http_request(
            "/v1/executors/grok/invoke",
            data={"prompt": "Task for grok"},
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 404)
        self.assertIn("not found", resp.get("error", "").lower())

        code, resp = self._http_request(
            "/v1/executors/foo/invoke",
            data={"prompt": "Task for foo"},
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 404)
        self.assertIn("not found", resp.get("error", "").lower())

    # =========================================================================
    # 2. Authentication and Transport Protections
    # =========================================================================

    def test_07_post_invoke_requires_strict_bearer_auth(self):
        """POST /v1/executors/agy/invoke rejects missing, empty, or wrong Bearer tokens with 401."""
        # Missing auth header
        code, resp = self._http_request("/v1/executors/agy/invoke", data={"prompt": "test"}, token=None)
        self.assertEqual(code, 401)
        self.assertIn("Strict Bearer token required", resp.get("error", ""))

        # Invalid token
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            data={"prompt": "test"},
            token="invalid_token_999999",
        )
        self.assertEqual(code, 401)

    def test_08_post_invoke_payload_size_limit_413(self):
        """POST /v1/executors/agy/invoke rejects bodies exceeding 2MB limit with HTTP 413."""
        # Do not upload a multi-megabyte body: the server rejects from
        # Content-Length before reading it, so uploading it races the close.
        conn = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=5)
        try:
            conn.putrequest("POST", "/v1/executors/agy/invoke")
            conn.putheader("Authorization", f"Bearer {TEST_TOKEN}")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(acp_server.MAX_CONTENT_LENGTH + 100))
            conn.endheaders()
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 413)
            self.assertIn("Payload Too Large", body.get("error", ""))
        finally:
            conn.close()

    # =========================================================================
    # 3. Input Validation (HTTP 400)
    # =========================================================================

    def test_09_invalid_json_body_returns_400(self):
        """POST /v1/executors/agy/invoke returns HTTP 400 on malformed JSON or non-object roots."""
        # Malformed JSON
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            raw_body="{invalid_json:",
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 400)
        self.assertIn("Invalid JSON payload", resp.get("error", ""))

        # Top-level array
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            data=[{"prompt": "test"}],
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 400)
        self.assertIn("must be a JSON object", resp.get("error", ""))

        # Top-level primitive string
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            raw_body='"just a string"',
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 400)
        self.assertIn("must be a JSON object", resp.get("error", ""))

    def test_10_missing_or_invalid_prompt_returns_400(self):
        """POST /v1/executors/agy/invoke returns HTTP 400 when 'prompt' is missing, empty, or non-string."""
        # Missing prompt
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            data={"cwd": "/workspace"},
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 400)
        self.assertIn("prompt", resp.get("error", "").lower())

        # Empty prompt
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            data={"prompt": ""},
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 400)

        # Whitespace prompt
        code, resp = self._http_request(
            "/v1/executors/agy/invoke",
            data={"prompt": "   \n\t  "},
            token=TEST_TOKEN,
        )
        self.assertEqual(code, 400)

        # Non-string prompt
        for bad_p in [123, True, False, [], {}]:
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": bad_p},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 400)

    def test_11_invalid_session_id_returns_400(self):
        """POST /v1/executors/agy/invoke returns HTTP 400 on empty or non-string session_id."""
        for bad_sid in ["", "   ", 12345, True, False, [], {}]:
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Valid prompt", "session_id": bad_sid},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 400)
            self.assertIn("session_id", resp.get("error", "").lower())

    def test_12_invalid_timeout_sec_returns_400(self):
        """POST /v1/executors/agy/invoke returns HTTP 400 on non-positive, bool, or non-numeric timeout_sec."""
        for bad_timeout in [0, -1, -50, -10.5, 45.5, True, False, "600", [], {}]:
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Valid prompt", "timeout_sec": bad_timeout},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 400)
            self.assertIn("timeout_sec", resp.get("error", "").lower())

    def test_13_invalid_optional_string_fields_returns_400(self):
        """POST /v1/executors/agy/invoke returns HTTP 400 when cwd, model, or effort are not strings."""
        for key, bad_val in [("cwd", 123), ("cwd", True), ("model", 100), ("model", []), ("effort", {})]:
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Valid prompt", key: bad_val},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 400)
            self.assertIn(key, resp.get("error", "").lower())

    # =========================================================================
    # 4. Standard Execution and Uniform Schema (Section 10)
    # =========================================================================

    def test_14_new_session_success_returns_200_and_exact_section_10_schema(self):
        """
        POST /v1/executors/agy/invoke starting a new session returns HTTP 200 and
        conforms precisely to the Section 10 ExecutorResult schema.
        """
        mock_cli_json = {
            "status": "SUCCESS",
            "conversation_id": "gen-session-uuid-1111",
            "response": "Created feature successfully.",
            "usage": {
                "input_tokens": 150,
                "output_tokens": 75,
                "total_tokens": 225,
                "cost_usd": 0.002,
                "extra_agy_field": "provider_internal",
            },
        }
        mock_proc = subprocess.CompletedProcess(
            args=["/home/codex/.local/bin/agy"],
            returncode=0,
            stdout=json.dumps(mock_cli_json),
            stderr="",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc) as mock_run:
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={
                    "prompt": "Create new feature",
                    "cwd": "/workspace/demo",
                    "session_id": None,
                    "model": "flash",
                    "effort": "low",
                    "timeout_sec": 60,
                },
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)

            # 1. Exact top-level fields
            expected_keys = {
                "status",
                "executor",
                "session_id",
                "response",
                "exit_code",
                "timing",
                "usage",
                "warnings",
                "error",
                "raw",
            }
            self.assertEqual(set(resp.keys()), expected_keys)

            # 2. Value checks
            self.assertEqual(resp["status"], "success")
            self.assertEqual(resp["executor"], "agy")
            self.assertEqual(resp["session_id"], "gen-session-uuid-1111")
            self.assertEqual(resp["response"], "Created feature successfully.")
            self.assertEqual(resp["exit_code"], 0)
            self.assertIsInstance(resp["timing"], dict)
            self.assertIn("duration_ms", resp["timing"])
            self.assertGreaterEqual(resp["timing"]["duration_ms"], 0)

            # 3. Normalized usage (extra provider keys stripped from top-level usage)
            self.assertEqual(
                resp["usage"],
                {"input_tokens": 150, "output_tokens": 75, "total_tokens": 225, "cost_usd": 0.002},
            )
            self.assertEqual(resp["warnings"], [])
            self.assertIsNone(resp["error"])

            # 4. Raw retains provider payload
            self.assertIn("parsed", resp["raw"])
            self.assertEqual(resp["raw"]["parsed"]["usage"]["extra_agy_field"], "provider_internal")

            # 5. Verify argument forwarding
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            self.assertEqual(called_cmd[-1], "Create new feature")
            self.assertIn("--model", called_cmd)
            self.assertIn("flash", called_cmd)
            self.assertIn("--effort", called_cmd)
            self.assertIn("low", called_cmd)
            self.assertEqual(mock_run.call_args[1].get("cwd"), "/workspace/demo")

    def test_14b_http_forwards_all_fields_and_outer_timeout_budget(self):
        """HTTP passes the six generic fields unchanged and waits timeout_sec + 5s."""
        result = ExecutorResult(
            status="success",
            executor="agy",
            session_id="forwarded-session",
            response="forwarded",
            exit_code=0,
            raw={},
        )
        fake_future = MagicMock()
        fake_future.result.return_value = result
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with patch.object(acp_server, "agent_executor", fake_executor):
            code, response = self._http_request(
                "/v1/executors/agy/invoke",
                data={
                    "prompt": "Forward exactly",
                    "cwd": "/workspace/forward",
                    "session_id": None,
                    "model": "pro",
                    "effort": "high",
                    "timeout_sec": 600,
                },
                token=TEST_TOKEN,
            )
        self.assertEqual(code, 200)
        self.assertEqual(response["response"], "forwarded")
        submit_args, submit_kwargs = fake_executor.submit.call_args
        self.assertIs(submit_args[0].__self__, acp_server.agy_adapter)
        self.assertIs(submit_args[0].__func__, type(acp_server.agy_adapter).invoke)
        self.assertEqual(
            submit_kwargs,
            {
                "prompt": "Forward exactly",
                "cwd": "/workspace/forward",
                "session_id": None,
                "model": "pro",
                "effort": "high",
                "timeout_sec": 600,
            },
        )
        fake_future.result.assert_called_once_with(timeout=605.0)

        # With no request override, the adapter's current total budget is used.
        fake_executor.reset_mock()
        fake_future.reset_mock()
        fake_future.result.return_value = result
        with patch.object(acp_server, "agent_executor", fake_executor):
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Use default budget"},
                token=TEST_TOKEN,
            )
        self.assertEqual(code, 200)
        fake_future.result.assert_called_once_with(
            timeout=float(acp_server.TOTAL_PROCESS_TIMEOUT) + 5.0
        )

    def test_15_continuation_session_success(self):
        """POST /v1/executors/agy/invoke with session_id passes --conversation and returns 200."""
        mock_cli_json = {
            "status": "SUCCESS",
            "response": "Continued turn response.",
            "usage": {"total_tokens": 100},
        }
        mock_proc = subprocess.CompletedProcess(
            args=["/home/codex/.local/bin/agy"],
            returncode=0,
            stdout=json.dumps(mock_cli_json),
            stderr="",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc) as mock_run:
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={
                    "prompt": "Continue next step",
                    "session_id": "existing-sess-2222",
                },
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)
            self.assertEqual(resp["status"], "success")
            self.assertEqual(resp["session_id"], "existing-sess-2222")
            self.assertEqual(resp["response"], "Continued turn response.")

            # Verify --conversation flag in generated command
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("--conversation", called_cmd)
            cid_idx = called_cmd.index("--conversation")
            self.assertEqual(called_cmd[cid_idx + 1], "existing-sess-2222")

    def test_16_partial_success_returns_200_and_warning(self):
        """Contradictory ERROR status with non-empty response returns HTTP 200 partial_success."""
        mock_cli_json = {
            "status": "ERROR",
            "error": "Agent finished with minor warnings.",
            "response": "Here is the partial solution.",
        }
        mock_proc = subprocess.CompletedProcess(
            args=["/home/codex/.local/bin/agy"],
            returncode=1,
            stdout=json.dumps(mock_cli_json),
            stderr="Warning: trace completed with error",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc):
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={
                    "prompt": "Execute task",
                    "session_id": "sess-partial-3333",
                },
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)
            self.assertEqual(resp["status"], "partial_success")
            self.assertEqual(resp["session_id"], "sess-partial-3333")
            self.assertEqual(resp["response"], "Here is the partial solution.")
            self.assertEqual(resp["exit_code"], 1)
            self.assertGreater(len(resp["warnings"]), 0)
            self.assertIn("Agent finished with minor warnings.", resp["error"])

    def test_17_adapter_genuine_error_returns_500(self):
        """Adapter returning status 'error' without response returns HTTP 500."""
        mock_cli_json = {
            "status": "ERROR",
            "error": "Fatal runtime failure in worker.",
        }
        mock_proc = subprocess.CompletedProcess(
            args=["/home/codex/.local/bin/agy"],
            returncode=1,
            stdout=json.dumps(mock_cli_json),
            stderr="Fatal error log",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc):
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={
                    "prompt": "Execute crashing task",
                    "session_id": "sess-err-4444",
                },
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 500)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["session_id"], "sess-err-4444")
            self.assertIn("Fatal runtime failure", resp["error"])
            self.assertIsNone(resp["response"])

    # =========================================================================
    # 5. Timeout and Exception Handling
    # =========================================================================

    def test_18_timeout_expired_returns_504_with_structured_result(self):
        """Subprocess TimeoutExpired returns HTTP 504 Gateway Timeout with structured ExecutorResult."""
        with patch("acp_server.run_agent_command", side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=5.0)):
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Long running task", "session_id": "sess-timeout-5555", "timeout_sec": 5},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 504)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "agy")
            self.assertEqual(resp["session_id"], "sess-timeout-5555")
            self.assertIn("timed out", resp.get("error", "").lower())
            self.assertIn("timing", resp)
            self.assertIn("usage", resp)

    def test_19_unexpected_internal_exception_returns_500(self):
        """Unexpected internal exception in adapter returns HTTP 500 (not misreported as 504)."""
        with patch("acp_server.run_agent_command", side_effect=RuntimeError("Unexpected OS I/O failure")):
            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Task causing internal error", "session_id": "sess-internal-6666"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 500)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "agy")
            self.assertEqual(resp["session_id"], "sess-internal-6666")
            self.assertIn("Internal executor failure", resp.get("error", ""))

    # =========================================================================
    # 6. Concurrency, Locking, and Shared Resources
    # =========================================================================

    def test_20_same_session_concurrency_generic_returns_409(self):
        """Concurrent turns on the same session_id via Generic API return HTTP 409 Conflict."""
        start_event = threading.Event()
        release_event = threading.Event()

        def slow_runner(cmd, timeout_sec, **kwargs):
            start_event.set()
            release_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"status": "SUCCESS", "conversation_id": "shared-lock-session", "response": "Done"}),
                "",
            )

        with patch("acp_server.run_agent_command", side_effect=slow_runner):
            t1_res: list[tuple[int, dict]] = []

            def worker():
                res = self._http_request(
                    "/v1/executors/agy/invoke",
                    data={"prompt": "First turn", "session_id": "shared-lock-session"},
                    token=TEST_TOKEN,
                )
                t1_res.append(res)

            t1 = threading.Thread(target=worker)
            t1.start()

            self.assertTrue(start_event.wait(timeout=3.0), "First request failed to start")

            # Second request with identical session_id must immediately receive 409
            code2, resp2 = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Second turn", "session_id": "shared-lock-session"},
                token=TEST_TOKEN,
            )
            release_event.set()
            t1.join(timeout=5.0)

            self.assertEqual(code2, 409)
            self.assertEqual(resp2["status"], "error")
            self.assertEqual(resp2["executor"], "agy")
            self.assertEqual(resp2["session_id"], "shared-lock-session")
            self.assertIn("Conflict", resp2.get("error", ""))

            self.assertEqual(t1_res[0][0], 200)

    def test_21_cross_legacy_generic_session_lock_409(self):
        """
        Cross-API Session Lock:
        1. In-flight Generic turn locks session_id -> Legacy /invoke with same conversation_id gets 409.
        2. In-flight Legacy turn locks conversation_id -> Generic /invoke with same session_id gets 409.
        """
        # Case 1: Generic in-flight -> Legacy gets 409
        start_event_1 = threading.Event()
        release_event_1 = threading.Event()

        def slow_runner_1(cmd, timeout_sec, **kwargs):
            start_event_1.set()
            release_event_1.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"status": "SUCCESS", "conversation_id": "cross-session-1", "response": "Done 1"}),
                "",
            )

        with patch("acp_server.run_agent_command", side_effect=slow_runner_1):
            t1 = threading.Thread(
                target=lambda: self._http_request(
                    "/v1/executors/agy/invoke",
                    data={"prompt": "Generic in-flight", "session_id": "cross-session-1"},
                    token=TEST_TOKEN,
                )
            )
            t1.start()
            self.assertTrue(start_event_1.wait(timeout=3.0))

            # Legacy request with conversation_id="cross-session-1"
            leg_code, leg_resp = self._http_request(
                "/acp/v1/invoke",
                data={"prompt": "Legacy colliding turn", "conversation_id": "cross-session-1"},
                token=TEST_TOKEN,
            )
            release_event_1.set()
            t1.join(timeout=5.0)

            self.assertEqual(leg_code, 409)
            self.assertIn("Conflict", leg_resp.get("error", ""))

        # Case 2: Legacy in-flight -> Generic gets 409
        start_event_2 = threading.Event()
        release_event_2 = threading.Event()

        def slow_runner_2(cmd, timeout_sec, **kwargs):
            start_event_2.set()
            release_event_2.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"status": "SUCCESS", "conversation_id": "cross-session-2", "response": "Done 2"}),
                "",
            )

        with patch("acp_server.run_agent_command", side_effect=slow_runner_2):
            t2 = threading.Thread(
                target=lambda: self._http_request(
                    "/acp/v1/invoke",
                    data={"prompt": "Legacy in-flight", "conversation_id": "cross-session-2"},
                    token=TEST_TOKEN,
                )
            )
            t2.start()
            self.assertTrue(start_event_2.wait(timeout=3.0))

            # Generic request with session_id="cross-session-2"
            gen_code, gen_resp = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Generic colliding turn", "session_id": "cross-session-2"},
                token=TEST_TOKEN,
            )
            release_event_2.set()
            t2.join(timeout=5.0)

            self.assertEqual(gen_code, 409)
            self.assertEqual(gen_resp["status"], "error")
            self.assertIn("Conflict", gen_resp.get("error", ""))

    def test_22_shared_worker_semaphore_saturation_429(self):
        """Global worker semaphore saturation (AGY_MAX_CONCURRENCY=1) returns HTTP 429 across Generic and Legacy."""
        start_event = threading.Event()
        release_event = threading.Event()

        def slow_runner(cmd, timeout_sec, **kwargs):
            start_event.set()
            release_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"status": "SUCCESS", "conversation_id": "sess-sem-1", "response": "Done"}),
                "",
            )

        with patch("acp_server.run_agent_command", side_effect=slow_runner):
            t1 = threading.Thread(
                target=lambda: self._http_request(
                    "/v1/executors/agy/invoke",
                    data={"prompt": "Task holding worker slot", "session_id": "sess-sem-1"},
                    token=TEST_TOKEN,
                )
            )
            t1.start()
            self.assertTrue(start_event.wait(timeout=3.0))

            # 1. Concurrent Generic request on different session -> 429
            code_gen, resp_gen = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "Task on different session", "session_id": "sess-sem-2"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code_gen, 429)
            self.assertEqual(resp_gen["status"], "error")
            self.assertIn("Too Many Requests", resp_gen.get("error", ""))

            # 2. Concurrent Legacy request on different session -> 429
            code_leg, resp_leg = self._http_request(
                "/acp/v1/invoke",
                data={"prompt": "Legacy task on different session", "conversation_id": "sess-sem-3"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code_leg, 429)
            self.assertIn("Too Many Requests", resp_leg.get("error", ""))

            release_event.set()
            t1.join(timeout=5.0)

    def test_23_locks_and_permits_released_without_leaks(self):
        """Success, error, and timeout paths release session locks and worker permits."""
        self.assertEqual(acp_server.session_lock_manager.active_count(), 0)

        success_proc = subprocess.CompletedProcess(
            args=["agy"],
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "conversation_id": "leak-test-session", "response": "OK"}),
            stderr="",
        )

        # A normal request still releases its lock.
        with patch("acp_server.run_agent_command", return_value=success_proc):
            self._http_request("/v1/executors", token=None)
            self._http_request("/v1/executors/agy/health", token=None)
            self._http_request("/v1/executors/unknown/invoke", data={"prompt": "hi"}, token=TEST_TOKEN)
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "hi", "session_id": "leak-test-session"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)

        # An adapter exception must not poison either the same-session lock or
        # the shared AGY worker permit.
        with patch("acp_server.run_agent_command", side_effect=RuntimeError("synthetic failure")):
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "fail", "session_id": "release-error"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 500)
        with patch("acp_server.run_agent_command", return_value=success_proc):
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "recover", "session_id": "release-error"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "different session", "session_id": "release-other"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)

        # A subprocess timeout follows the same release contract.
        with patch(
            "acp_server.run_agent_command",
            side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1),
        ):
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "timeout", "session_id": "release-timeout", "timeout_sec": 1},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 504)
        with patch("acp_server.run_agent_command", return_value=success_proc):
            code, _ = self._http_request(
                "/v1/executors/agy/invoke",
                data={"prompt": "recover timeout", "session_id": "release-timeout"},
                token=TEST_TOKEN,
            )
            self.assertEqual(code, 200)

        # Session lock count must be exactly 0
        self.assertEqual(acp_server.session_lock_manager.active_count(), 0)

    # =========================================================================
    # 7. Behavioral Equivalence (Generic vs Legacy)
    # =========================================================================

    def test_24_generic_and_legacy_behavioral_equivalence(self):
        """Legacy ACP API and Generic Executor API produce equivalent behavioral outcomes for identical CLI results."""
        test_cases = [
            # 1. Success case
            {
                "cli_json": {"status": "SUCCESS", "conversation_id": "equiv-cid-1", "response": "Equivalence response 1"},
                "returncode": 0,
                "expected_legacy_status": 200,
                "expected_generic_status": 200,
                "expected_generic_result_status": "success",
            },
            # 2. Partial success case
            {
                "cli_json": {"status": "ERROR", "error": "Late warning", "response": "Equivalence response 2"},
                "returncode": 1,
                "expected_legacy_status": 200,
                "expected_generic_status": 200,
                "expected_generic_result_status": "partial_success",
            },
            # 3. Error case
            {
                "cli_json": {"status": "ERROR", "error": "Fatal execution failure"},
                "returncode": 1,
                "expected_legacy_status": 500,
                "expected_generic_status": 500,
                "expected_generic_result_status": "error",
            },
        ]

        for i, tc in enumerate(test_cases):
            mock_proc = subprocess.CompletedProcess(
                args=["agy"],
                returncode=tc["returncode"],
                stdout=json.dumps(tc["cli_json"]),
                stderr="",
            )

            with patch("acp_server.run_agent_command", return_value=mock_proc):
                # Call Legacy
                leg_code, leg_resp = self._http_request(
                    "/acp/v1/invoke",
                    data={"prompt": f"Equiv prompt {i}", "conversation_id": f"equiv-sess-{i}"},
                    token=TEST_TOKEN,
                )
                self.assertEqual(leg_code, tc["expected_legacy_status"])

                # Call Generic
                gen_code, gen_resp = self._http_request(
                    "/v1/executors/agy/invoke",
                    data={"prompt": f"Equiv prompt {i}", "session_id": f"equiv-sess-{i}"},
                    token=TEST_TOKEN,
                )
                self.assertEqual(gen_code, tc["expected_generic_status"])
                self.assertEqual(gen_resp["status"], tc["expected_generic_result_status"])

                # Verify extracted response equivalence
                if tc["cli_json"].get("response"):
                    self.assertEqual(gen_resp["response"], tc["cli_json"]["response"])

    def test_25_security_audit_zero_secret_leakage(self):
        """Audit all response JSON payloads to verify zero token, secret, or environment leakage."""
        mock_proc = subprocess.CompletedProcess(
            args=["agy"],
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "conversation_id": "sec-cid", "response": "No secrets"}),
            stderr="",
        )

        with patch("acp_server.run_agent_command", return_value=mock_proc):
            endpoints = [
                ("/v1/executors", None, None),
                ("/v1/executors/agy/health", None, None),
                ("/v1/executors/agy/invoke", {"prompt": "Check secrets", "session_id": "sec-cid"}, TEST_TOKEN),
            ]

            for path, data, tok in endpoints:
                code, resp = self._http_request(path, data=data, token=tok)
                resp_str = json.dumps(resp)
                self.assertNotIn(TEST_TOKEN, resp_str)
                self.assertNotIn("PATH=", resp_str)
                self.assertNotIn("SHELL=", resp_str)


if __name__ == "__main__":
    unittest.main()
