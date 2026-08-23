"""
Unit and Integration Tests for Core Modules of Agent Executor Gateway.
Tests:
- ExecutorResult validation, legal states (success, partial_success, error), Section 10 serialization & deserialization
- Normalized usage dictionary restricting to standard keys (input_tokens, output_tokens, total_tokens, cost_usd)
- Neutral token management (0600 permissions, generation, verification, constant-time compare)
- GatewayConfig transport defaults and environment variable overrides
- SessionLockManager multi-executor isolation (same session_id on different executors does not collide)
- AdmissionController semaphore management
- DeadlineTimer monotonic budget tracking, including non-positive timeouts
- Process group execution with stdin=DEVNULL and start_new_session=True
- Strengthened Core Neutrality Gate (AST names, attributes, string constants, and imports verify zero provider terms)
"""

from __future__ import annotations

import ast
import glob
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.auth import load_or_create_token, verify_bearer_token
from core.concurrency import AdmissionController
from core.config import (
    DEFAULT_MAX_CONTENT_LENGTH,
    DEFAULT_MAX_HTTP_CONNECTIONS,
    DEFAULT_MAX_POST_CONNECTIONS,
    DEFAULT_PORT,
    DEFAULT_SOCKET_TIMEOUT,
    DEFAULT_TOKEN_FILE,
    GatewayConfig,
)
from core.process import run_process_group
from core.result import ExecutorResult, LEGAL_STATUSES, normalize_usage
from core.session_lock import SessionLockManager
from core.timeout import DeadlineTimer


class TestCoreResult(unittest.TestCase):
    """Tests for ExecutorResult schema validation, normalized usage, and serialization."""

    def test_01_legal_statuses(self):
        self.assertEqual(LEGAL_STATUSES, {"success", "partial_success", "error"})

        for status in ("success", "partial_success", "error"):
            res = ExecutorResult(status=status, executor="test_exec")
            self.assertEqual(res.status, status)
            self.assertEqual(res.executor, "test_exec")

    def test_02_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            ExecutorResult(status="invalid_status", executor="test_exec")

        with self.assertRaises(ValueError):
            ExecutorResult(status="", executor="test_exec")

    def test_03_invalid_attributes_raise(self):
        with self.assertRaises(ValueError):
            ExecutorResult(status="success", executor="")

        with self.assertRaises(ValueError):
            ExecutorResult(status="success", executor="test_exec", timing="invalid")  # type: ignore

        with self.assertRaises(ValueError):
            ExecutorResult(status="success", executor="test_exec", usage="invalid")  # type: ignore

        with self.assertRaises(ValueError):
            ExecutorResult(status="success", executor="test_exec", warnings="invalid")  # type: ignore

        with self.assertRaises(ValueError):
            ExecutorResult(status="success", executor="test_exec", raw="invalid")  # type: ignore

    def test_04_to_dict_matches_section_10_schema(self):
        res = ExecutorResult(
            status="success",
            executor="test_exec",
            session_id="session-1234",
            response="Execution complete",
            exit_code=0,
            timing={"duration_ms": 1500},
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": None},
            warnings=["warning 1"],
            error=None,
            raw={"raw_key": "raw_val"},
        )
        d = res.to_dict()

        self.assertEqual(d["status"], "success")
        self.assertEqual(d["executor"], "test_exec")
        self.assertEqual(d["session_id"], "session-1234")
        self.assertEqual(d["response"], "Execution complete")
        self.assertEqual(d["exit_code"], 0)
        self.assertEqual(d["timing"], {"duration_ms": 1500})
        self.assertEqual(
            d["usage"],
            {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": None},
        )
        self.assertEqual(d["warnings"], ["warning 1"])
        self.assertIsNone(d["error"])
        self.assertEqual(d["raw"], {"raw_key": "raw_val"})

    def test_05_from_dict_roundtrip(self):
        data = {
            "status": "partial_success",
            "executor": "test_exec",
            "session_id": "sess-xyz",
            "response": "Partial output",
            "exit_code": 1,
            "timing": {"duration_ms": 2000},
            "usage": {"input_tokens": 80, "output_tokens": 40, "total_tokens": 120, "cost_usd": None},
            "warnings": ["Upstream error occurred"],
            "error": "Non-zero exit",
            "raw": {"status": "ERROR"},
        }
        res = ExecutorResult.from_dict(data)
        self.assertEqual(res.status, "partial_success")
        self.assertEqual(res.executor, "test_exec")
        self.assertEqual(res.session_id, "sess-xyz")
        self.assertEqual(res.response, "Partial output")
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.timing["duration_ms"], 2000)
        self.assertEqual(res.warnings, ["Upstream error occurred"])
        self.assertEqual(res.error, "Non-zero exit")
        self.assertEqual(res.to_dict(), data)

    def test_06_usage_normalization_filters_provider_keys(self):
        provider_usage = {
            "input_tokens": 120,
            "output_tokens": 80,
            "total_tokens": 200,
            "cost_usd": 0.005,
            "thinking_tokens": 45,
            "cache_read_tokens": 150,
            "cache_write_tokens": 30,
        }
        normalized = normalize_usage(provider_usage)
        self.assertEqual(
            normalized,
            {
                "input_tokens": 120,
                "output_tokens": 80,
                "total_tokens": 200,
                "cost_usd": 0.005,
            },
        )
        self.assertNotIn("thinking_tokens", normalized)
        self.assertNotIn("cache_read_tokens", normalized)

        res = ExecutorResult(
            status="success",
            executor="test_exec",
            usage=provider_usage,
        )
        self.assertEqual(
            res.usage,
            {
                "input_tokens": 120,
                "output_tokens": 80,
                "total_tokens": 200,
                "cost_usd": 0.005,
            },
        )


class TestCoreAuth(unittest.TestCase):
    """Tests for token creation, file permissions, and verification."""

    def test_07_load_or_create_token_creates_file_with_0600(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, "nested", "acp_token")
            token = load_or_create_token(token_path)

            self.assertTrue(os.path.exists(token_path))
            self.assertEqual(len(token), 48)  # 24 bytes hex = 48 chars

            # Check file permissions (0600)
            mode = stat.S_IMODE(os.stat(token_path).st_mode)
            self.assertEqual(mode, 0o600)

            # Subsequent load returns identical token
            loaded_token = load_or_create_token(token_path)
            self.assertEqual(loaded_token, token)

    def test_08_verify_bearer_token(self):
        token = "secret_token_123456789"
        valid_header = f"Bearer {token}"
        valid_header_with_spaces = f"  Bearer {token}  "

        self.assertTrue(verify_bearer_token(valid_header, token))
        self.assertTrue(verify_bearer_token(valid_header_with_spaces, token))

        self.assertFalse(verify_bearer_token(f"Bearer wrong_token", token))
        self.assertFalse(verify_bearer_token(token, token))  # Missing "Bearer " prefix
        self.assertFalse(verify_bearer_token("Basic 12345", token))
        self.assertFalse(verify_bearer_token("", token))
        self.assertFalse(verify_bearer_token(None, token))
        self.assertFalse(verify_bearer_token(valid_header, ""))


class TestCoreConfig(unittest.TestCase):
    """Tests for GatewayConfig and transport defaults."""

    def test_09_gateway_config_defaults(self):
        cfg = GatewayConfig()
        self.assertEqual(cfg.port, DEFAULT_PORT)
        self.assertEqual(cfg.token_file, DEFAULT_TOKEN_FILE)
        self.assertEqual(cfg.max_content_length, DEFAULT_MAX_CONTENT_LENGTH)
        self.assertEqual(cfg.max_http_connections, DEFAULT_MAX_HTTP_CONNECTIONS)
        self.assertEqual(cfg.max_post_connections, DEFAULT_MAX_POST_CONNECTIONS)
        self.assertEqual(cfg.socket_timeout_sec, DEFAULT_SOCKET_TIMEOUT)

    def test_10_gateway_config_from_env(self):
        custom_env = {
            "ACP_PORT": "9000",
            "ACP_TOKEN_FILE": "/tmp/custom_token",
        }
        orig_env = os.environ.copy()
        try:
            os.environ.update(custom_env)
            cfg = GatewayConfig.from_env()
            self.assertEqual(cfg.port, 9000)
            self.assertEqual(cfg.token_file, "/tmp/custom_token")
        finally:
            os.environ.clear()
            os.environ.update(orig_env)


class TestSessionLockManager(unittest.TestCase):
    """Tests for multi-executor session lock isolation."""

    def test_11_multi_executor_session_lock_isolation(self):
        mgr = SessionLockManager()
        session_id = "shared-session-uuid-1111"

        # Executor 1 acquires session_id -> True
        self.assertTrue(mgr.acquire("test_a", session_id))
        self.assertTrue(mgr.is_locked("test_a", session_id))
        self.assertEqual(mgr.active_count(), 1)

        # Executor 2 acquires the same session_id -> True (independent namespace!)
        self.assertTrue(mgr.acquire("test_b", session_id))
        self.assertTrue(mgr.is_locked("test_b", session_id))
        self.assertEqual(mgr.active_count(), 2)

        # Executor 1 attempts to re-acquire the same session_id -> False (conflict!)
        self.assertFalse(mgr.acquire("test_a", session_id))

        # Executor 2 attempts to re-acquire the same session_id -> False (conflict!)
        self.assertFalse(mgr.acquire("test_b", session_id))

        # Release Executor 1
        mgr.release("test_a", session_id)
        self.assertFalse(mgr.is_locked("test_a", session_id))
        self.assertTrue(mgr.is_locked("test_b", session_id))
        self.assertEqual(mgr.active_count(), 1)

        # Now Executor 1 can acquire again
        self.assertTrue(mgr.acquire("test_a", session_id))
        mgr.release("test_a", session_id)
        mgr.release("test_b", session_id)
        self.assertEqual(mgr.active_count(), 0)

    def test_12_empty_session_id_never_locks(self):
        mgr = SessionLockManager()
        self.assertTrue(mgr.acquire("test_a", None))
        self.assertTrue(mgr.acquire("test_a", ""))
        self.assertTrue(mgr.acquire("test_a", "   "))
        self.assertFalse(mgr.is_locked("test_a", None))
        self.assertFalse(mgr.is_locked("test_a", ""))
        self.assertEqual(mgr.active_count(), 0)

        # Releasing None/empty does not raise
        mgr.release("test_a", None)
        mgr.release("test_a", "")


class TestAdmissionController(unittest.TestCase):
    """Tests for AdmissionController semaphores."""

    def test_13_admission_controller_permits(self):
        ctrl = AdmissionController(
            max_http_connections=2,
            max_post_connections=1,
            max_worker_concurrency=1,
        )

        # HTTP permits
        self.assertTrue(ctrl.acquire_http())
        self.assertTrue(ctrl.acquire_http())
        self.assertFalse(ctrl.acquire_http())  # Exceeded
        ctrl.release_http()
        self.assertTrue(ctrl.acquire_http())
        ctrl.release_http()
        ctrl.release_http()

        # POST permits
        self.assertTrue(ctrl.acquire_post())
        self.assertFalse(ctrl.acquire_post())  # Exceeded
        ctrl.release_post()

        # Worker permits
        self.assertTrue(ctrl.acquire_worker())
        self.assertFalse(ctrl.acquire_worker())  # Exceeded
        ctrl.release_worker()


class TestDeadlineTimer(unittest.TestCase):
    """Tests for DeadlineTimer monotonic tracking."""

    def test_14_deadline_timer_budget(self):
        timer = DeadlineTimer(0.1)
        self.assertFalse(timer.is_expired())
        self.assertGreater(timer.remaining(), 0.0)

        time.sleep(0.12)
        self.assertTrue(timer.is_expired())
        self.assertLessEqual(timer.remaining(), 0.0)
        self.assertGreaterEqual(timer.elapsed_ms(), 100)

        with self.assertRaises(subprocess.TimeoutExpired):
            timer.check_or_raise(["cmd"])

    def test_15_non_positive_timeout_expires_immediately(self):
        timer_zero = DeadlineTimer(0)
        self.assertTrue(timer_zero.is_expired())
        self.assertLessEqual(timer_zero.remaining(), 0.0)
        with self.assertRaises(subprocess.TimeoutExpired):
            timer_zero.check_or_raise(["cmd"])

        timer_neg = DeadlineTimer(-5.0)
        self.assertTrue(timer_neg.is_expired())
        self.assertLessEqual(timer_neg.remaining(), 0.0)
        with self.assertRaises(subprocess.TimeoutExpired):
            timer_neg.check_or_raise(["cmd"])


class TestProcessGroupExecution(unittest.TestCase):
    """Tests for run_process_group."""

    def test_16_run_process_group_success(self):
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write('hello stdout'); sys.stderr.write('hello stderr')"]
        res = run_process_group(cmd, timeout_sec=5.0)

        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "hello stdout")
        self.assertEqual(res.stderr, "hello stderr")

    def test_17_run_process_group_with_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cmd = [sys.executable, "-c", "import os; print(os.getcwd())"]
            res = run_process_group(cmd, timeout_sec=5.0, cwd=temp_dir)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(os.path.realpath(res.stdout.strip()), os.path.realpath(temp_dir))

    def test_18_run_process_group_timeout_raises(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process_group(cmd, timeout_sec=0.2)


class TestCoreNeutrality(unittest.TestCase):
    """
    Strengthened Architectural Gate: Ensure core/ modules remain strictly executor-neutral:
    - Core MUST NOT import adapters or acp_server
    - Core MUST NOT contain provider names, conversation_id, CLI flag terms, or AGY env vars
    - Allows generic executor and session_id concepts
    """

    def test_19_core_dependency_and_identifier_neutrality(self):
        core_dir = os.path.join(REPO_ROOT, "core")
        core_files = glob.glob(os.path.join(core_dir, "*.py"))
        self.assertGreater(len(core_files), 0, "Core directory must contain python files")

        forbidden_imports = {"adapters", "acp_server", "adapters.base", "adapters.antigravity"}
        forbidden_terms = {
            "agy",
            "antigravity",
            "conversation_id",
            "--conversation",
            "--output-format",
            "--dangerously-skip-permissions",
            "AGY_BIN",
            "AGY_MAX_CONCURRENCY",
            "ACP_AGENT_TIMEOUT_SEC",
            "ACP_AUTH_GRACE_SEC",
        }

        for file_path in core_files:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            tree = ast.parse(content, filename=file_path)

            # Check imports
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        base_mod = alias.name.split(".")[0]
                        self.assertNotIn(
                            base_mod,
                            forbidden_imports,
                            f"Neutrality violation: {file_path} imports '{alias.name}'",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        base_mod = node.module.split(".")[0]
                        self.assertNotIn(
                            base_mod,
                            forbidden_imports,
                            f"Neutrality violation: {file_path} imports from '{node.module}'",
                        )
                # Check Name, Attribute, and Constant nodes for forbidden provider terms
                elif isinstance(node, ast.Name):
                    val = node.id.lower()
                    for term in forbidden_terms:
                        self.assertNotIn(
                            term.lower(),
                            val,
                            f"Neutrality violation: {file_path} contains identifier '{node.id}' containing forbidden term '{term}'",
                        )
                elif isinstance(node, ast.Attribute):
                    val = node.attr.lower()
                    for term in forbidden_terms:
                        self.assertNotIn(
                            term.lower(),
                            val,
                            f"Neutrality violation: {file_path} contains attribute '{node.attr}' containing forbidden term '{term}'",
                        )
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    val = node.value.lower()
                    for term in forbidden_terms:
                        self.assertNotIn(
                            term.lower(),
                            val,
                            f"Neutrality violation: {file_path} contains string constant '{node.value}' containing forbidden term '{term}'",
                        )


if __name__ == "__main__":
    unittest.main()
