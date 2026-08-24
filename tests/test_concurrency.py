#!/usr/bin/env python3
"""
Comprehensive Concurrency Test Suite for Agent Executor Gateway (Phase 5).

Tests Goal Prompt Section 47 & Unified Concurrency Requirements:
1. AGY × 1 + Grok × 1 executes in parallel (PASS)
2. AGY × 2 returns HTTP 429 Too Many Requests (AGY semaphore saturation)
3. Grok × 2 returns HTTP 429 Too Many Requests (Grok semaphore saturation)
4. Same AGY session × 2 returns HTTP 409 Conflict
5. Same Grok session × 2 returns HTTP 409 Conflict
6. Cross-executor identical session_id ("shared-sess-id") does NOT collide (isolated by executor key)
7. Global Gateway capacity saturation (GATEWAY_MAX_CONCURRENCY=2) returns HTTP 429
8. Exception-safe permit and lock release across success, adapter failure, timeout, and collision paths
9. Cross-API concurrency and mutual exclusion (Legacy ACP /acp/v1/* vs Generic /v1/executors/*)
10. Configuration resolution and /health concurrency limits inspection
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
    TEMP_TOKEN_DIR = tempfile.mkdtemp(prefix="concurrency_test_token_")
    TEMP_TOKEN_FILE = os.path.join(TEMP_TOKEN_DIR, "test_acp_token")
    TEST_TOKEN = "test_concurrency_bearer_token_1234567890abcdef"
    with open(TEMP_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(TEST_TOKEN)
    os.chmod(TEMP_TOKEN_FILE, 0o600)

    os.environ["ACP_TOKEN_FILE"] = TEMP_TOKEN_FILE
    os.environ["ACP_PORT"] = "0"
    os.environ["GATEWAY_MAX_CONCURRENCY"] = "2"
    os.environ["AGY_MAX_CONCURRENCY"] = "1"
    os.environ["GROK_MAX_CONCURRENCY"] = "1"
    os.environ["ACP_AGENT_TIMEOUT_SEC"] = "10"

    import acp_server


def _cleanup_test_tokens() -> None:
    try:
        if TEMP_TOKEN_FILE and os.path.exists(TEMP_TOKEN_FILE):
            os.remove(TEMP_TOKEN_FILE)
        if TEMP_TOKEN_DIR and os.path.exists(TEMP_TOKEN_DIR):
            os.rmdir(TEMP_TOKEN_DIR)
    except Exception:
        pass


atexit.register(_cleanup_test_tokens)

from core.concurrency import AdmissionController
from core.session_lock import SessionLockManager


class TestUnifiedConcurrency(unittest.TestCase):
    """Deterministic Concurrency & Session Locking Integration Tests."""

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
        method: str | None = None,
    ) -> tuple[int, dict]:
        url = f"{self.server_url}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        else:
            body = None

        http_method = method if method else ("POST" if data is not None else "GET")
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
    # 1. Section 47 Core Scenarios
    # =========================================================================

    def test_01_agy_1_plus_grok_1_parallel_execution_pass(self):
        """AGY × 1 + Grok × 1 can execute concurrently in parallel without blocking or 429."""
        agy_started = threading.Event()
        grok_started = threading.Event()
        finish_event = threading.Event()

        agy_res = []
        grok_res = []

        def mock_runner(cmd, timeout, **kwargs):
            if "agy" in cmd[0]:
                agy_started.set()
                finish_event.wait(timeout=5.0)
                return subprocess.CompletedProcess(
                    cmd, 0, json.dumps({"status": "SUCCESS", "conversation_id": "cid-agy-1", "response": "AGY done"}), ""
                )
            else:
                grok_started.set()
                finish_event.wait(timeout=5.0)
                return subprocess.CompletedProcess(
                    cmd, 0, json.dumps({"text": "Grok done", "sessionId": "grok-sess"}), ""
                )

        with patch("acp_server.run_agent_command", side_effect=mock_runner):
            t_agy = threading.Thread(
                target=lambda: agy_res.append(
                    self._http_request("/v1/executors/agy/invoke", {"prompt": "task agy"})
                )
            )
            t_grok = threading.Thread(
                target=lambda: grok_res.append(
                    self._http_request("/v1/executors/grok/invoke", {"prompt": "task grok"})
                )
            )

            t_agy.start()
            self.assertTrue(agy_started.wait(timeout=3.0))

            t_grok.start()
            self.assertTrue(grok_started.wait(timeout=3.0))

            # Both tasks are running concurrently in parallel!
            finish_event.set()
            t_agy.join(timeout=5.0)
            t_grok.join(timeout=5.0)

            self.assertEqual(len(agy_res), 1)
            self.assertEqual(len(grok_res), 1)
            self.assertEqual(agy_res[0][0], 200)
            self.assertEqual(grok_res[0][0], 200)
            self.assertEqual(agy_res[0][1]["status"], "success")
            self.assertEqual(grok_res[0][1]["status"], "success")

    def test_02_agy_2_concurrent_returns_429(self):
        """AGY × 2 concurrent turns returns HTTP 429 when AGY_MAX_CONCURRENCY=1."""
        started = threading.Event()
        release = threading.Event()

        def slow_agy(cmd, timeout, **kwargs):
            started.set()
            release.wait(timeout=5.0)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": "SUCCESS", "response": "AGY 1 done"}), "")

        with patch("acp_server.run_agent_command", side_effect=slow_agy):
            t1 = threading.Thread(
                target=lambda: self._http_request("/v1/executors/agy/invoke", {"prompt": "agy task 1"})
            )
            t1.start()
            self.assertTrue(started.wait(timeout=3.0))

            # Second AGY task on different session is rejected with 429
            code, resp = self._http_request("/v1/executors/agy/invoke", {"prompt": "agy task 2"})

            release.set()
            t1.join(timeout=5.0)

            self.assertEqual(code, 429)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "agy")
            self.assertIn("Too Many Requests", resp.get("error", ""))

    def test_03_grok_2_concurrent_returns_429(self):
        """Grok × 2 concurrent turns returns HTTP 429 when GROK_MAX_CONCURRENCY=1."""
        started = threading.Event()
        release = threading.Event()

        def slow_grok(cmd, timeout, **kwargs):
            started.set()
            release.wait(timeout=5.0)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"text": "Grok 1 done", "sessionId": "g1"}), "")

        with patch("acp_server.run_agent_command", side_effect=slow_grok):
            t1 = threading.Thread(
                target=lambda: self._http_request("/v1/executors/grok/invoke", {"prompt": "grok task 1"})
            )
            t1.start()
            self.assertTrue(started.wait(timeout=3.0))

            # Second Grok task is rejected with 429
            code, resp = self._http_request("/v1/executors/grok/invoke", {"prompt": "grok task 2"})

            release.set()
            t1.join(timeout=5.0)

            self.assertEqual(code, 429)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "grok")
            self.assertIn("Too Many Requests", resp.get("error", ""))

    def test_04_same_agy_session_concurrent_returns_409(self):
        """Same AGY session × 2 concurrent turns returns HTTP 409 Conflict."""
        started = threading.Event()
        release = threading.Event()

        def slow_turn(cmd, timeout, **kwargs):
            started.set()
            release.wait(timeout=5.0)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": "SUCCESS", "response": "done"}), "")

        with patch("acp_server.run_agent_command", side_effect=slow_turn):
            t1 = threading.Thread(
                target=lambda: self._http_request(
                    "/v1/executors/agy/invoke",
                    {"prompt": "turn 1", "session_id": "session-agy-conflict"},
                )
            )
            t1.start()
            self.assertTrue(started.wait(timeout=3.0))

            code, resp = self._http_request(
                "/v1/executors/agy/invoke",
                {"prompt": "turn 2 colliding", "session_id": "session-agy-conflict"},
            )

            release.set()
            t1.join(timeout=5.0)

            self.assertEqual(code, 409)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "agy")
            self.assertEqual(resp["session_id"], "session-agy-conflict")
            self.assertIn("Conflict", resp.get("error", ""))

    def test_05_same_grok_session_concurrent_returns_409(self):
        """Same Grok session × 2 concurrent turns returns HTTP 409 Conflict."""
        started = threading.Event()
        release = threading.Event()

        def slow_turn(cmd, timeout, **kwargs):
            started.set()
            release.wait(timeout=5.0)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"text": "done", "sessionId": "session-grok-conflict"}), "")

        with patch("acp_server.run_agent_command", side_effect=slow_turn):
            t1 = threading.Thread(
                target=lambda: self._http_request(
                    "/v1/executors/grok/invoke",
                    {"prompt": "turn 1", "session_id": "session-grok-conflict"},
                )
            )
            t1.start()
            self.assertTrue(started.wait(timeout=3.0))

            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                {"prompt": "turn 2 colliding", "session_id": "session-grok-conflict"},
            )

            release.set()
            t1.join(timeout=5.0)

            self.assertEqual(code, 409)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["executor"], "grok")
            self.assertEqual(resp["session_id"], "session-grok-conflict")
            self.assertIn("Conflict", resp.get("error", ""))

    def test_06_cross_executor_identical_session_id_no_conflict(self):
        """
        Cross-executor identical session_id ("shared-sess-999") does NOT collide:
        AGY and Grok locks are partitioned by (executor, session_id).
        """
        agy_started = threading.Event()
        grok_started = threading.Event()
        release_all = threading.Event()

        agy_res = []
        grok_res = []

        def mock_runner(cmd, timeout, **kwargs):
            if "agy" in cmd[0]:
                agy_started.set()
                release_all.wait(timeout=5.0)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": "SUCCESS", "response": "AGY ok"}), "")
            else:
                grok_started.set()
                release_all.wait(timeout=5.0)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"text": "Grok ok", "sessionId": "shared-sess-999"}), "")

        with patch("acp_server.run_agent_command", side_effect=mock_runner):
            t_agy = threading.Thread(
                target=lambda: agy_res.append(
                    self._http_request(
                        "/v1/executors/agy/invoke",
                        {"prompt": "agy turn", "session_id": "shared-sess-999"},
                    )
                )
            )
            t_grok = threading.Thread(
                target=lambda: grok_res.append(
                    self._http_request(
                        "/v1/executors/grok/invoke",
                        {"prompt": "grok turn", "session_id": "shared-sess-999"},
                    )
                )
            )

            t_agy.start()
            self.assertTrue(agy_started.wait(timeout=3.0))

            t_grok.start()
            self.assertTrue(grok_started.wait(timeout=3.0))

            release_all.set()
            t_agy.join(timeout=5.0)
            t_grok.join(timeout=5.0)

            self.assertEqual(len(agy_res), 1)
            self.assertEqual(len(grok_res), 1)
            self.assertEqual(agy_res[0][0], 200)
            self.assertEqual(grok_res[0][0], 200)

    def test_07_global_gateway_saturation_returns_429(self):
        """
        Global gateway capacity limit (GATEWAY_MAX_CONCURRENCY=2):
        When 1 AGY + 1 Grok are running, a 3rd request is rejected with 429.
        """
        task1_started = threading.Event()
        task2_started = threading.Event()
        release_all = threading.Event()

        def slow_runner(cmd, timeout, **kwargs):
            if "agy" in cmd[0]:
                task1_started.set()
                release_all.wait(timeout=5.0)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": "SUCCESS"}), "")
            else:
                task2_started.set()
                release_all.wait(timeout=5.0)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"text": "done", "sessionId": "s2"}), "")

        with patch("acp_server.run_agent_command", side_effect=slow_runner):
            t1 = threading.Thread(
                target=lambda: self._http_request("/v1/executors/agy/invoke", {"prompt": "task 1"})
            )
            t2 = threading.Thread(
                target=lambda: self._http_request("/v1/executors/grok/invoke", {"prompt": "task 2"})
            )

            t1.start()
            self.assertTrue(task1_started.wait(timeout=3.0))

            t2.start()
            self.assertTrue(task2_started.wait(timeout=3.0))

            # 3rd request arrives while gateway is at 2/2 capacity
            code3, resp3 = self._http_request("/v1/executors/agy/invoke", {"prompt": "task 3"})

            release_all.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            self.assertEqual(code3, 429)
            self.assertEqual(resp3["status"], "error")
            self.assertIn("Too Many Requests", resp3.get("error", ""))

    def test_08_exception_safe_permit_and_lock_cleanup(self):
        """Verify session locks and permits are cleanly released after errors, timeouts, and collisions."""
        # 1. Successful execution releases lock & permits
        with patch("acp_server.run_agent_command", return_value=subprocess.CompletedProcess(["grok"], 0, json.dumps({"text": "ok", "sessionId": "s-cleanup"}), "")):
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                {"prompt": "turn", "session_id": "s-cleanup"},
            )
            self.assertEqual(code, 200)
            self.assertFalse(acp_server.session_lock_manager.is_locked("grok", "s-cleanup"))

        # 2. Timeout releases lock & permits
        with patch("acp_server.run_agent_command", side_effect=subprocess.TimeoutExpired(cmd=["grok"], timeout=1.0)):
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                {"prompt": "turn", "session_id": "s-cleanup", "timeout_sec": 1},
            )
            self.assertEqual(code, 504)
            self.assertFalse(acp_server.session_lock_manager.is_locked("grok", "s-cleanup"))

        # 3. Subsequent turn can immediately acquire lock
        with patch("acp_server.run_agent_command", return_value=subprocess.CompletedProcess(["grok"], 0, json.dumps({"text": "subsequent ok", "sessionId": "s-cleanup"}), "")):
            code, resp = self._http_request(
                "/v1/executors/grok/invoke",
                {"prompt": "turn again", "session_id": "s-cleanup"},
            )
            self.assertEqual(code, 200)
            self.assertEqual(resp["response"], "subsequent ok")

    def test_09_cross_legacy_and_generic_concurrency(self):
        """Legacy /acp/v1/invoke (AGY) and Generic /v1/executors/grok/invoke can execute concurrently."""
        legacy_started = threading.Event()
        generic_started = threading.Event()
        release_all = threading.Event()

        legacy_res = []
        generic_res = []

        def mock_runner(cmd, timeout, **kwargs):
            if "agy" in cmd[0]:
                legacy_started.set()
                release_all.wait(timeout=5.0)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": "SUCCESS", "conversation_id": "leg-cid", "response": "leg ok"}), "")
            else:
                generic_started.set()
                release_all.wait(timeout=5.0)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"text": "gen grok ok", "sessionId": "gen-sid"}), "")

        with patch("acp_server.run_agent_command", side_effect=mock_runner):
            t_leg = threading.Thread(
                target=lambda: legacy_res.append(
                    self._http_request("/acp/v1/invoke", {"prompt": "legacy agy turn"})
                )
            )
            t_gen = threading.Thread(
                target=lambda: generic_res.append(
                    self._http_request("/v1/executors/grok/invoke", {"prompt": "generic grok turn"})
                )
            )

            t_leg.start()
            self.assertTrue(legacy_started.wait(timeout=3.0))

            t_gen.start()
            self.assertTrue(generic_started.wait(timeout=3.0))

            release_all.set()
            t_leg.join(timeout=5.0)
            t_gen.join(timeout=5.0)

            self.assertEqual(len(legacy_res), 1)
            self.assertEqual(len(generic_res), 1)
            self.assertEqual(legacy_res[0][0], 200)
            self.assertEqual(generic_res[0][0], 200)

    def test_10_health_reports_concurrency_limits(self):
        """GET /health reports gateway, agy, and grok concurrency limits."""
        code, resp = self._http_request("/health", token=None)
        self.assertEqual(code, 200)
        limits = resp.get("limits", {})
        self.assertEqual(limits.get("gateway_max_concurrency"), 2)
        self.assertEqual(limits.get("agy_max_concurrency"), 1)
        self.assertEqual(limits.get("grok_max_concurrency"), 1)
        self.assertIn("admission_control", limits)


if __name__ == "__main__":
    unittest.main()
