"""
Comprehensive Unit and Contract Tests for AntigravityAdapter, AntigravityConfig, and HTTP Delegation.
Tests:
- Token isolation (safe isolated ephemeral token setup; fails fast on production token preloading)
- ExecutorAdapter ABC contract conformance and AntigravityConfig resolution
- Command construction and exact legacy flag truthiness (including whitespace values)
- Deterministic runner dispatch: verifies a runner raising TypeError executes exactly once without retry
- Successful first-turn execution with session_id extraction
- Successful continuation turn with explicit session_id
- Missing first-turn conversation_id error classification
- Contradictory ERROR + response partial_success classification
- Genuine failure error classification
- Pre-execution retry eligibility predicate & single-attempt continuation rule
- Non-positive total_timeout_sec raises subprocess.TimeoutExpired immediately
- Normalized usage metadata restricting to standard Section 10 keys (raw retains extra provider keys)
- 0-argument constructibility of legacy ConversationLockManager compatibility class
- Monotonic timeout propagation and cwd handling
- HTTP layer delegation through AntigravityAdapter.invoke
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
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
else:
    TEMP_TOKEN_DIR = tempfile.mkdtemp(prefix="adapter_test_token_")
    TEMP_TOKEN_FILE = os.path.join(TEMP_TOKEN_DIR, "test_acp_token")
    with open(TEMP_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write("test_adapter_bearer_token_1234567890abcdef")
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

from adapters.antigravity import AntigravityAdapter, AntigravityConfig
from adapters.base import ExecutorAdapter
from core.result import ExecutorResult


class TestAntigravityAdapterContract(unittest.TestCase):
    """Verify AntigravityAdapter contract, inheritance, health, and capabilities."""

    def setUp(self):
        self.config = AntigravityConfig(
            bin_path="/usr/bin/agy",
            max_concurrency=2,
            subprocess_timeout_sec=400,
            auth_grace_sec=40,
        )
        self.adapter = AntigravityAdapter(config=self.config)

    def test_01_inherits_executor_adapter(self):
        self.assertIsInstance(self.adapter, ExecutorAdapter)
        self.assertEqual(self.adapter.name, "agy")
        self.assertEqual(self.adapter.bin_path, "/usr/bin/agy")
        self.assertEqual(self.adapter.subprocess_timeout, 400)
        self.assertEqual(self.adapter.auth_grace_sec, 40)
        self.assertEqual(self.adapter.total_process_timeout, 440)

    def test_02_health_contract(self):
        health = self.adapter.health()
        self.assertIn("status", health)
        self.assertIn("service", health)
        self.assertIn("version", health)
        self.assertIn("mode", health)
        self.assertIn("binary", health)
        self.assertIn("available", health)
        self.assertEqual(health["mode"], "explicit_conversation_cli")
        self.assertEqual(health["version"], "2.4.0")

    def test_03_capabilities_contract(self):
        caps = self.adapter.capabilities()
        self.assertTrue(caps.get("supports_session"))
        self.assertTrue(caps.get("supports_model"))
        self.assertTrue(caps.get("supports_effort"))
        self.assertTrue(caps.get("supports_cwd"))
        self.assertIn("flash", caps.get("models", []))
        self.assertIn("pro", caps.get("models", []))
        self.assertIn("medium", caps.get("efforts", []))


class TestAntigravityCommandBuilder(unittest.TestCase):
    """Verify exact Phase 0 CLI flag ordering, truthiness, and whitespace preservation."""

    def setUp(self):
        self.adapter = AntigravityAdapter(bin_path="/mock/bin/agy")

    def test_04_new_conversation_flag_order(self):
        cmd = self.adapter.build_command(
            prompt="Create a database schema",
            conversation_id=None,
            model="flash",
            effort="medium",
        )
        self.assertEqual(cmd[0], "/mock/bin/agy")
        self.assertNotIn("--conversation", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)

        p_idx = cmd.index("-p")
        self.assertEqual(p_idx, len(cmd) - 2)
        self.assertEqual(cmd[-1], "Create a database schema")

        model_idx = cmd.index("--model")
        effort_idx = cmd.index("--effort")
        self.assertLess(model_idx, p_idx)
        self.assertLess(effort_idx, p_idx)
        self.assertEqual(cmd[model_idx + 1], "flash")
        self.assertEqual(cmd[effort_idx + 1], "medium")

    def test_05_continuation_flag_order(self):
        cid = "conv-1111-2222-3333-4444"
        cmd = self.adapter.build_command(
            prompt="Run integration tests",
            conversation_id=cid,
            model="pro",
        )
        self.assertEqual(cmd[0], "/mock/bin/agy")
        self.assertIn("--conversation", cmd)
        c_idx = cmd.index("--conversation")
        self.assertEqual(cmd[c_idx + 1], cid)

        p_idx = cmd.index("-p")
        self.assertGreater(p_idx, c_idx)
        self.assertEqual(cmd[-1], "Run integration tests")

    def test_06_truthiness_and_whitespace_preservation(self):
        # Truthy whitespace values must be passed as-is to preserve exact Phase 0 behavior
        cmd = self.adapter.build_command(
            prompt="Test whitespace flags",
            conversation_id="   ",
            model="   ",
            effort="   ",
        )
        self.assertIn("--conversation", cmd)
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "   ")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "   ")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "   ")


class TestDeterministicRunnerDispatch(unittest.TestCase):
    """Verify that a TypeError raised inside a runner propagates without duplicate execution."""

    def test_07_runner_type_error_executes_exactly_once(self):
        call_count = 0

        def faulty_runner(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise TypeError("Simulated internal TypeError in user task")

        adapter = AntigravityAdapter(runner=faulty_runner, bin_path="agy")

        with self.assertRaises(TypeError) as ctx:
            adapter.invoke(prompt="Trigger single call error")

        self.assertIn("Simulated internal TypeError", str(ctx.exception))
        self.assertEqual(call_count, 1, "Faulty runner must be called exactly once without fallback retry")


class TestAntigravityAdapterInvoke(unittest.TestCase):
    """Verify invoke produces standardized ExecutorResult across outcomes."""

    def test_08_invoke_new_conversation_success(self):
        cid = "new-cid-9999-8888-7777"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Implementation generated.",
            "num_turns": 1,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "thinking_tokens": 30,
            },
        }

        def mock_runner(cmd, timeout, env=None, cwd=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps(mock_output),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=mock_runner, bin_path="agy")
        result = adapter.invoke(
            prompt="Build module",
            model="flash",
            effort="low",
        )

        self.assertIsInstance(result, ExecutorResult)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.executor, "agy")
        self.assertEqual(result.session_id, cid)
        self.assertEqual(result.response, "Implementation generated.")
        self.assertEqual(result.exit_code, 0)
        self.assertGreaterEqual(result.timing["duration_ms"], 0)
        self.assertEqual(
            result.usage,
            {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": None},
        )
        self.assertNotIn("thinking_tokens", result.usage)
        self.assertEqual(result.raw["parsed"]["usage"]["thinking_tokens"], 30)
        self.assertEqual(result.warnings, [])
        self.assertIsNone(result.error)
        self.assertEqual(result.raw["parsed"], mock_output)

    def test_09_invoke_continuation_success(self):
        cid = "existing-cid-5555-6666"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Refactor applied.",
            "num_turns": 2,
            "usage": {"total_tokens": 250},
        }

        def mock_runner(cmd, timeout, env=None, cwd=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps(mock_output),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=mock_runner, bin_path="agy")
        result = adapter.invoke(
            prompt="Refactor",
            session_id=cid,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.session_id, cid)
        self.assertEqual(result.response, "Refactor applied.")

    def test_10_invoke_missing_first_turn_conversation_id_returns_error(self):
        mock_output = {
            "status": "SUCCESS",
            "response": "No top level ID provided",
            "num_turns": 1,
        }

        def mock_runner(cmd, timeout, env=None, cwd=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps(mock_output),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=mock_runner, bin_path="agy")
        result = adapter.invoke(prompt="Start task")

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.session_id)
        self.assertIn("missing required top-level 'conversation_id'", result.error or "")

    def test_11_invoke_partial_success_classification(self):
        cid = "partial-cid-3333-4444"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "Agent execution terminated due to error.",
            "response": "Partial completed work.",
            "num_turns": 3,
        }

        def mock_runner(cmd, timeout, env=None, cwd=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout=json.dumps(mock_output),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=mock_runner, bin_path="agy")
        result = adapter.invoke(prompt="Do work", session_id=cid)

        self.assertEqual(result.status, "partial_success")
        self.assertEqual(result.session_id, cid)
        self.assertEqual(result.response, "Partial completed work.")
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(len(result.warnings) > 0)
        self.assertIn("agy reported ERROR", result.warnings[0])
        self.assertIn("terminated", result.error or "")

    def test_12_invoke_genuine_failure_classification(self):
        cid = "fail-cid-2222-3333"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "Quota exceeded",
            "response": "",
            "num_turns": 0,
        }

        def mock_runner(cmd, timeout, env=None, cwd=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout=json.dumps(mock_output),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=mock_runner, bin_path="agy")
        result = adapter.invoke(prompt="Run query", session_id=cid)

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.response)
        self.assertIn("Quota exceeded", result.error or "")


class TestAntigravityAdapterRetry(unittest.TestCase):
    """Verify pre-execution retry eligibility and execution mechanics."""

    def setUp(self):
        self.adapter = AntigravityAdapter(bin_path="agy")

    def test_13_retry_eligibility_predicates(self):
        valid_eof = {
            "status": "ERROR",
            "error": "connection reset by peer",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": "",
        }
        self.assertTrue(self.adapter.is_retryable_pre_execution_error(None, valid_eof, cmd=["agy", "-p", "hi"]))

        # Ineligible: continuation command
        self.assertFalse(
            self.adapter.is_retryable_pre_execution_error(
                None, valid_eof, cmd=["agy", "--conversation", "123", "-p", "hi"]
            )
        )

        # Ineligible: existing conversation_id in error JSON
        with_cid = dict(valid_eof, conversation_id="cid-123")
        self.assertFalse(self.adapter.is_retryable_pre_execution_error(None, with_cid, cmd=["agy", "-p", "hi"]))

        # Ineligible: num_turns > 0
        with_turns = dict(valid_eof, num_turns=1)
        self.assertFalse(self.adapter.is_retryable_pre_execution_error(None, with_turns, cmd=["agy", "-p", "hi"]))

        # Ineligible: total_tokens > 0
        with_tokens = dict(valid_eof, usage={"total_tokens": 50})
        self.assertFalse(self.adapter.is_retryable_pre_execution_error(None, with_tokens, cmd=["agy", "-p", "hi"]))

        # Ineligible: non-empty response
        with_resp = dict(valid_eof, response="some response")
        self.assertFalse(self.adapter.is_retryable_pre_execution_error(None, with_resp, cmd=["agy", "-p", "hi"]))

        # Ineligible: non-transient error message
        non_transient = dict(valid_eof, error="Syntax error in python script")
        self.assertFalse(self.adapter.is_retryable_pre_execution_error(None, non_transient, cmd=["agy", "-p", "hi"]))

    def test_14_execute_with_retry_retries_transient_failures(self):
        calls = 0
        eof_json = {"status": "ERROR", "error": "network EOF", "num_turns": 0, "usage": {"total_tokens": 0}, "response": ""}
        ok_json = {"conversation_id": "new-cid-1", "status": "SUCCESS", "response": "Success after retry"}

        def runner(cmd, timeout, env=None, cwd=None):
            nonlocal calls
            calls += 1
            if calls < 3:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=json.dumps(eof_json), stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=json.dumps(ok_json), stderr="")

        adapter = AntigravityAdapter(runner=runner, bin_path="agy")
        res, parsed = adapter.execute_with_retry(["agy", "-p", "test"], total_timeout_sec=5.0, max_retries=3)

        self.assertEqual(calls, 3)
        self.assertEqual(parsed.get("status"), "SUCCESS")
        self.assertEqual(parsed.get("conversation_id"), "new-cid-1")

    def test_15_execute_with_retry_single_attempt_for_continuation(self):
        calls = 0
        eof_json = {"status": "ERROR", "error": "network EOF", "num_turns": 0, "usage": {"total_tokens": 0}, "response": ""}

        def runner(cmd, timeout, env=None, cwd=None):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=json.dumps(eof_json), stderr="")

        adapter = AntigravityAdapter(runner=runner, bin_path="agy")
        cmd = ["agy", "--conversation", "cid-existing", "-p", "test"]
        res, parsed = adapter.execute_with_retry(cmd, total_timeout_sec=5.0, max_retries=3)

        self.assertEqual(calls, 1)
        self.assertEqual(parsed.get("status"), "ERROR")

    def test_16_non_positive_total_timeout_raises_timeout_expired(self):
        adapter = AntigravityAdapter(bin_path="agy")
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.execute_with_retry(["agy", "-p", "test"], total_timeout_sec=0)

        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.execute_with_retry(["agy", "-p", "test"], total_timeout_sec=-10.0)


class TestConversationLockManagerCompatibility(unittest.TestCase):
    """Verify that ConversationLockManager can be constructed with zero arguments."""

    def test_17_no_argument_constructor_compatibility(self):
        mgr = acp_server.ConversationLockManager()
        cid = "compat-lock-cid-1"

        self.assertTrue(mgr.acquire(cid))
        self.assertTrue(mgr.is_locked(cid))
        self.assertFalse(mgr.acquire(cid))

        mgr.release(cid)
        self.assertFalse(mgr.is_locked(cid))
        self.assertTrue(mgr.acquire(cid))
        mgr.release(cid)


class TestAntigravityAdapterTimeoutAndCwd(unittest.TestCase):
    """Verify cwd propagation and timeout handling."""

    def test_18_cwd_propagation(self):
        captured_cwd = None

        def runner(cmd, timeout, env=None, cwd=None):
            nonlocal captured_cwd
            captured_cwd = cwd
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "ok"}),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=runner, bin_path="agy")
        adapter.invoke(prompt="test", cwd="/tmp/custom_workspace")
        self.assertEqual(captured_cwd, "/tmp/custom_workspace")

    def test_19_timeout_sec_propagation(self):
        captured_timeout = None

        def runner(cmd, timeout, env=None, cwd=None):
            nonlocal captured_timeout
            captured_timeout = timeout
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "ok"}),
                stderr="",
            )

        adapter = AntigravityAdapter(runner=runner, bin_path="agy")
        adapter.invoke(prompt="test", timeout_sec=120)
        self.assertIsNotNone(captured_timeout)
        self.assertAlmostEqual(captured_timeout, 120.0, delta=1.0)


class TestHTTPDelegationThroughAdapter(unittest.TestCase):
    """Prove HTTP requests delegate through AntigravityAdapter and format responses."""

    def test_20_http_delegation_to_adapter_invoke(self):
        mock_result = ExecutorResult(
            status="success",
            executor="agy",
            session_id="delegated-cid-1111",
            response="Delegated response text",
            exit_code=0,
            timing={"duration_ms": 500},
            usage={"total_tokens": 100},
            warnings=[],
            error=None,
            raw={"parsed": {"conversation_id": "delegated-cid-1111", "status": "SUCCESS"}, "stdout": "raw stdout"},
        )

        with patch.object(acp_server.agy_adapter, "invoke", return_value=mock_result) as mock_invoke:
            server = acp_server.ThreadedHTTPServer(("127.0.0.1", 0), acp_server.ACPRequestHandler)
            server_port = server.server_address[1]
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            try:
                url = f"http://127.0.0.1:{server_port}/acp/v1/invoke"
                req = urllib.request.Request(
                    url,
                    data=json.dumps({"prompt": "Hello delegated"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {acp_server.ACP_AUTH_TOKEN}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    code = resp.getcode()
                    data = json.loads(resp.read().decode("utf-8"))

                self.assertEqual(code, 200)
                self.assertEqual(data.get("status"), "success")
                self.assertEqual(data.get("conversation_id"), "delegated-cid-1111")
                self.assertEqual(data.get("action"), "new-conversation")
                self.assertTrue(mock_invoke.called)
                self.assertEqual(mock_invoke.call_args.kwargs.get("prompt"), "Hello delegated")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
