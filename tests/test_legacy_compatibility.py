#!/usr/bin/env python3
"""
Comprehensive Legacy Compatibility Test Suite for Agent Executor Gateway (Phase 0 Baseline)

This suite guarantees strict backward compatibility with the legacy antigravity-rest-bridge v2.4.0:
1. Health & Status Endpoints (/health, /acp/v1/status, OPTIONS, 404)
2. Authentication Enforcement (Strict Bearer Token, 401 Unauthorized, Token Isolation)
3. New Conversation Contract (Explicit flag ordering, Top-level conversation_id extraction, 500 on missing ID)
4. Continue Conversation Contract (Explicit --conversation flag placement, /invoke & /send-message)
5. Timeout & Process Group Lifecycle (start_new_session=True, os.killpg SIGKILL cleanup, HTTP 504)
6. Same-Conversation Concurrency Locking (HTTP 409 Conflict protection, exception-safe lock release)
7. Global Concurrency Admission Control (HTTP 429 Too Many Requests, semaphore release)
8. Pre-execution Retry Mechanics (0-turn EOF retry <=3, strict single-attempt continuation, in-flight preservation)
9. Partial Success Preservation (ERROR status + non-empty response -> HTTP 200 partial_success, diagnostic payload)
10. Input Validation & Connection Limits (400 Bad Request, 413 Payload Too Large, 501 Not Implemented, 503 Busy)
11. CLI Client Compatibility (acp-cli flag parsing, positional UUID handling, non-zero exits)
12. Codebase Cleanliness Audit (Zero active agy -c loops across scripts and servers)
13. Real Process Tree Cleanup Integration (Linux-only deterministic verification of run_agent_command killpg)

All HTTP contract tests run against an ephemeral port (ACP_PORT=0) with isolated temporary tokens, mocked subprocess
execution, and zero dependencies on live network services or production daemons.
"""

import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import signal
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import atexit
from importlib.machinery import SourceFileLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PROD_TOKEN_PATH = "/home/codex/.codex/acp_token"

TEMP_TOKEN_DIR = None
TEMP_TOKEN_FILE = None

if "acp_server" in sys.modules:
    acp_server_mod = sys.modules["acp_server"]
    resolved_token_file = os.path.realpath(getattr(acp_server_mod, "TOKEN_FILE", ""))
    if resolved_token_file == os.path.realpath(PROD_TOKEN_PATH):
        raise RuntimeError(
            "Security violation: acp_server was preloaded with the production token file path. "
            "Refusing to mutate preloaded module state."
        )
    TEST_TOKEN = acp_server_mod.ACP_AUTH_TOKEN
    import acp_server
else:
    TEMP_TOKEN_DIR = tempfile.mkdtemp(prefix="legacy_compat_token_")
    TEMP_TOKEN_FILE = os.path.join(TEMP_TOKEN_DIR, "test_acp_token")
    TEST_ISOLATED_TOKEN = "test_legacy_bearer_token_9876543210abcdef"

    with open(TEMP_TOKEN_FILE, "w") as f:
        f.write(TEST_ISOLATED_TOKEN)
    os.chmod(TEMP_TOKEN_FILE, 0o600)

    os.environ["ACP_TOKEN_FILE"] = TEMP_TOKEN_FILE
    os.environ["ACP_PORT"] = "0"  # Ephemeral port assignment
    os.environ["AGY_MAX_CONCURRENCY"] = "1"
    os.environ["ACP_AGENT_TIMEOUT_SEC"] = "10"

    import acp_server

    if os.path.realpath(acp_server.TOKEN_FILE) == os.path.realpath(PROD_TOKEN_PATH):
        raise RuntimeError("Security violation: acp_server imported with production token path.")

    TEST_TOKEN = acp_server.ACP_AUTH_TOKEN

assert os.path.realpath(acp_server.TOKEN_FILE) != os.path.realpath(PROD_TOKEN_PATH), "Security violation: acp_server must not use production token file!"
assert acp_server.ACP_AUTH_TOKEN, "Security violation: ACP_AUTH_TOKEN must be non-empty"

# Load acp-cli dynamically from repository root
acp_cli_path = os.path.join(REPO_ROOT, "acp-cli")
acp_cli = SourceFileLoader("acp_cli_mod", acp_cli_path).load_module()


def _cleanup_test_tokens():
    try:
        if TEMP_TOKEN_FILE and os.path.exists(TEMP_TOKEN_FILE):
            os.remove(TEMP_TOKEN_FILE)
        if TEMP_TOKEN_DIR and os.path.exists(TEMP_TOKEN_DIR):
            os.rmdir(TEMP_TOKEN_DIR)
    except Exception:
        pass

atexit.register(_cleanup_test_tokens)


class TestLegacyCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start a test instance of ThreadedHTTPServer on an ephemeral port
        cls.server = acp_server.ThreadedHTTPServer(('127.0.0.1', 0), acp_server.ACPRequestHandler)
        cls.server_port = cls.server.server_address[1]
        cls.server_url = f"http://127.0.0.1:{cls.server_port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        _cleanup_test_tokens()

    def _http_request(self, path, data=None, token=TEST_TOKEN, custom_headers=None, raw_body=None, method=None):
        """Helper to send HTTP requests to the isolated test server."""
        url = f"{self.server_url}{path}"
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token is not None:
            headers['Authorization'] = f"Bearer {token}"
        if custom_headers:
            headers.update(custom_headers)

        if raw_body is not None:
            body = raw_body
        elif data is not None:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        else:
            body = None

        http_method = method if method else ('POST' if (data is not None or raw_body is not None) else 'GET')
        req = urllib.request.Request(url, data=body, headers=headers, method=http_method)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.getcode()
                resp_bytes = resp.read()
                try:
                    resp_data = json.loads(resp_bytes.decode('utf-8'))
                except Exception:
                    resp_data = {'raw': resp_bytes.decode('utf-8')}
                return status_code, resp_data
        except urllib.error.HTTPError as e:
            err_bytes = e.read()
            try:
                parsed = json.loads(err_bytes.decode('utf-8'))
            except Exception:
                parsed = {'raw': err_bytes.decode('utf-8')}
            return e.code, parsed

    # =========================================================================
    # 1. Health & Status Endpoints
    # =========================================================================

    def test_01_health_endpoint_contract(self):
        """GET /health returns 200, online status, version 2.4.0, and exact limit structures."""
        code, resp = self._http_request('/health')
        self.assertEqual(code, 200)
        self.assertEqual(resp.get('status'), 'online')
        self.assertEqual(resp.get('service'), 'Antigravity REST Bridge Server')
        self.assertEqual(resp.get('version'), '2.4.0')
        self.assertEqual(resp.get('auth_type'), 'Strict Bearer Token')
        self.assertEqual(resp.get('mode'), 'explicit_conversation_cli')

        limits = resp.get('limits', {})
        self.assertEqual(limits.get('max_payload_bytes'), acp_server.MAX_CONTENT_LENGTH)
        self.assertEqual(limits.get('subprocess_timeout_sec'), acp_server.SUBPROCESS_TIMEOUT)
        self.assertEqual(limits.get('auth_grace_sec'), acp_server.AUTH_GRACE_SEC)
        self.assertEqual(limits.get('total_process_timeout_sec'), acp_server.TOTAL_PROCESS_TIMEOUT)
        self.assertEqual(limits.get('max_worker_threads'), acp_server.AGY_MAX_CONCURRENCY)
        self.assertEqual(limits.get('max_http_connections'), acp_server.MAX_HTTP_CONNECTIONS)
        self.assertEqual(limits.get('max_post_connections'), acp_server.MAX_POST_CONNECTIONS)
        self.assertEqual(limits.get('reserved_health_slots'), 5)

    def test_02_status_endpoint_alias(self):
        """GET /acp/v1/status provides equivalent response to /health without requiring authentication."""
        code, resp = self._http_request('/acp/v1/status', token=None)
        self.assertEqual(code, 200)
        self.assertEqual(resp.get('status'), 'online')
        self.assertEqual(resp.get('version'), '2.4.0')

    def test_03_cors_options_handling(self):
        """OPTIONS requests to endpoints return 200 with standard CORS allow headers."""
        code, resp = self._http_request('/acp/v1/invoke', method='OPTIONS')
        self.assertEqual(code, 200)
        self.assertEqual(resp.get('status'), 'ok')

    def test_04_unknown_endpoint_returns_404(self):
        """Requests to unknown endpoints return HTTP 404 with standard error body."""
        code, resp = self._http_request('/nonexistent/route')
        self.assertEqual(code, 404)
        self.assertIn('not found', resp.get('error', '').lower())

    # =========================================================================
    # 2. Authentication Enforcement & Token Isolation
    # =========================================================================

    def test_05_post_unauthorized_without_token(self):
        """POST to protected endpoints without Authorization header returns HTTP 401 Unauthorized."""
        code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'hello'}, token=None)
        self.assertEqual(code, 401)
        self.assertIn('Unauthorized', resp.get('error', ''))

    def test_06_post_unauthorized_with_wrong_token(self):
        """POST to protected endpoints with incorrect Bearer token returns HTTP 401 Unauthorized."""
        code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'hello'}, token="wrong_token_value_xyz")
        self.assertEqual(code, 401)
        self.assertIn('Unauthorized', resp.get('error', ''))

    def test_07_token_isolation_from_production(self):
        """Verify the test suite runs with an isolated temporary token and does not access production token."""
        self.assertNotEqual(os.path.realpath(acp_server.TOKEN_FILE), os.path.realpath(PROD_TOKEN_PATH))
        self.assertEqual(acp_server.ACP_AUTH_TOKEN, TEST_TOKEN)

    # =========================================================================
    # 3. New Conversation Contract
    # =========================================================================

    def test_08_invoke_new_conversation_creates_and_extracts_id(self):
        """POST /acp/v1/invoke without conversation_id starts a new conversation and extracts top-level conversation_id."""
        cid = "aaaa1111-2222-3333-4444-555555555555"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "New conversation initialized",
            "num_turns": 1,
            "usage": {"total_tokens": 120}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command') as mock_run:
            mock_run.return_value = mock_res
            code, resp = self._http_request('/acp/v1/invoke', {
                'prompt': 'Write a binary search function',
                'model': 'flash',
                'effort': 'medium'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('action'), 'new-conversation')
            self.assertEqual(resp.get('conversation_id'), cid)
            self.assertEqual(resp.get('mode'), 'explicit_conversation_cli')

            # Verify CLI flag ordering
            cmd = mock_run.call_args[0][0]
            self.assertNotIn('--conversation', cmd)
            self.assertIn('--output-format', cmd)
            self.assertIn('--dangerously-skip-permissions', cmd)
            self.assertIn('--model', cmd)
            self.assertEqual(cmd[cmd.index('--model') + 1], 'flash')
            self.assertIn('--effort', cmd)
            self.assertEqual(cmd[cmd.index('--effort') + 1], 'medium')

            # Ensure prompt is strictly the last argument after -p
            p_idx = cmd.index('-p')
            self.assertEqual(p_idx, len(cmd) - 2)
            self.assertEqual(cmd[-1], 'Write a binary search function')

    def test_09_new_conversation_endpoint_alias(self):
        """POST /acp/v1/new-conversation behaves identically to invoke without conversation_id."""
        cid = "bbbb2222-3333-4444-5555-666666666666"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Alias conversation created",
            "num_turns": 1,
            "usage": {"total_tokens": 150}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/new-conversation', {'prompt': 'Create new project'})
            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('action'), 'new-conversation')
            self.assertEqual(resp.get('conversation_id'), cid)

    def test_10_new_conversation_missing_top_level_cid_rejected_with_500(self):
        """If AGY CLI returns SUCCESS but lacks top-level 'conversation_id', request is rejected with HTTP 500."""
        mock_output = {
            "status": "SUCCESS",
            "response": "Done without conversation ID field",
            "num_turns": 1
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'Test missing ID'})
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn("missing required top-level 'conversation_id'", resp.get('error', ''))

    def test_11_response_body_uuid_not_extracted_as_top_level_cid(self):
        """UUID inside response text is never guessed/extracted; missing top-level field strictly returns HTTP 500."""
        mock_output = {
            "status": "SUCCESS",
            "response": "Generated session ID: 12345678-1234-5678-1234-567812345678 in text body",
            "num_turns": 1
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'Generate uuid in text'})
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn("missing required top-level 'conversation_id'", resp.get('error', ''))

    # =========================================================================
    # 4. Continue Conversation Contract
    # =========================================================================

    def test_12_invoke_continue_conversation_passes_explicit_flag(self):
        """POST /acp/v1/invoke with conversation_id passes explicit --conversation <id> before -p."""
        cid = "cccc3333-4444-5555-6666-777777777777"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Turn 2 executed",
            "num_turns": 2,
            "usage": {"total_tokens": 300}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command') as mock_run:
            mock_run.return_value = mock_res
            code, resp = self._http_request('/acp/v1/invoke', {
                'conversation_id': cid,
                'prompt': 'Refactor the previous function',
                'model': 'pro'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('action'), 'invoke')
            self.assertEqual(resp.get('conversation_id'), cid)

            cmd = mock_run.call_args[0][0]
            self.assertIn('--conversation', cmd)
            c_idx = cmd.index('--conversation')
            self.assertEqual(cmd[c_idx + 1], cid)
            p_idx = cmd.index('-p')
            self.assertGreater(p_idx, c_idx)
            self.assertEqual(cmd[-1], 'Refactor the previous function')

    def test_13_send_message_endpoint_passes_explicit_conversation(self):
        """POST /acp/v1/send-message with recipient_id and content passes explicit --conversation before -p."""
        cid = "dddd4444-5555-6666-7777-888888888888"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Message turn processed",
            "num_turns": 3,
            "usage": {"total_tokens": 450}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command') as mock_run:
            mock_run.return_value = mock_res
            code, resp = self._http_request('/acp/v1/send-message', {
                'recipient_id': cid,
                'content': 'Run unit tests now'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('action'), 'send-message')
            self.assertEqual(resp.get('conversation_id'), cid)

            cmd = mock_run.call_args[0][0]
            self.assertIn('--conversation', cmd)
            self.assertEqual(cmd[cmd.index('--conversation') + 1], cid)
            self.assertEqual(cmd[-1], 'Run unit tests now')

    # =========================================================================
    # 5. Timeout & Process Group Lifecycle (Mock-based contract tests)
    # =========================================================================

    def test_14_process_group_isolation_setsid(self):
        """run_agent_command executes subprocess with start_new_session=True for isolated process group."""
        with patch('subprocess.Popen') as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_proc.communicate.return_value = ('{"status":"SUCCESS"}', '')
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            acp_server.run_agent_command(['agy', '-p', 'test'], timeout_sec=5)
            self.assertTrue(mock_popen.called)
            kwargs = mock_popen.call_args[1]
            self.assertTrue(kwargs.get('start_new_session'))
            self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)

    def test_15_timeout_process_group_killpg_cleanup(self):
        """Subprocess TimeoutExpired triggers os.killpg(pgid, signal.SIGKILL) to terminate entire process tree."""
        with patch('subprocess.Popen') as mock_popen, patch('os.killpg') as mock_killpg:
            mock_proc = MagicMock()
            mock_proc.pid = 88888
            mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=['agy'], timeout=2.0)
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            with self.assertRaises(subprocess.TimeoutExpired):
                acp_server.run_agent_command(['agy', '-p', 'test'], timeout_sec=2)

            mock_killpg.assert_called_once_with(88888, signal.SIGKILL)
            mock_proc.stdout.close.assert_called()
            mock_proc.stderr.close.assert_called()

    def test_16_http_invoke_timeout_returns_504(self):
        """HTTP POST /invoke timing out returns HTTP 504 Gateway Timeout with structured error message."""
        def timeout_run(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with patch('acp_server.run_agent_command', side_effect=timeout_run):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'Trigger server timeout'})
            self.assertEqual(code, 504)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn('Timed Out', resp.get('error', ''))
            self.assertEqual(resp.get('status_code'), 504)

    def test_17_monotonic_timeout_budget_enforcement(self):
        """execute_with_retry tracks monotonic deadline and raises TimeoutExpired when deadline is exceeded."""
        def sleeping_run(cmd, timeout):
            time.sleep(0.1)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with patch('acp_server.run_agent_command', side_effect=sleeping_run):
            with self.assertRaises(subprocess.TimeoutExpired):
                acp_server.execute_with_retry(['agy', '-p', 'budget test'], total_timeout_sec=0.05, max_retries=3)

    # =========================================================================
    # 6. Concurrency & Locking (409 Conflict & 429 Too Many Requests)
    # =========================================================================

    def test_18_same_conversation_concurrency_409(self):
        """Concurrent turns on the exact same conversation_id return HTTP 409 Conflict immediately."""
        cid = "lock-cid-5555-6666-7777-888888888888"
        start_event = threading.Event()
        finish_event = threading.Event()

        def slow_turn(cmd, timeout):
            start_event.set()
            finish_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "Turn 1 done"}),
                stderr=''
            )

        with patch('acp_server.run_agent_command', side_effect=slow_turn):
            t1 = threading.Thread(
                target=lambda: self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'turn 1'})
            )
            t1.start()
            start_event.wait(timeout=2.0)

            # Concurrent second turn on same conversation_id
            code, resp = self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'turn 2'})

            finish_event.set()
            t1.join()

            self.assertEqual(code, 409)
            self.assertEqual(resp.get('status'), 'error')
            self.assertEqual(resp.get('status_code'), 409)
            self.assertIn('Conflict', resp.get('error', ''))
            self.assertIn(cid, resp.get('error', ''))

    def test_19_conversation_lock_released_on_completion(self):
        """Conversation lock is cleanly released after turn completes, allowing subsequent turns."""
        cid = "lock-cid-release-test-1111"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Success",
            "num_turns": 1
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code1, resp1 = self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'first turn'})
            self.assertEqual(code1, 200)
            self.assertFalse(acp_server.conv_lock_mgr.is_locked(cid))

            code2, resp2 = self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'second turn'})
            self.assertEqual(code2, 200)
            self.assertFalse(acp_server.conv_lock_mgr.is_locked(cid))

    def test_20_conversation_lock_released_on_exception(self):
        """Conversation lock is safely released in finally block even if execution fails or errors out."""
        cid = "lock-cid-error-release-2222"

        with patch('acp_server.run_agent_command', side_effect=RuntimeError("Subprocess failed")):
            code, resp = self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'turn error'})
            self.assertIn(code, (500, 504))
            self.assertFalse(acp_server.conv_lock_mgr.is_locked(cid))

    def test_21_global_concurrency_limit_429(self):
        """Global concurrency saturation (AGY_MAX_CONCURRENCY=1) returns HTTP 429 Too Many Requests."""
        start_event = threading.Event()
        finish_event = threading.Event()

        def slow_global(cmd, timeout):
            start_event.set()
            finish_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"status": "SUCCESS", "response": "Global 1 done"}),
                stderr=''
            )

        with patch('acp_server.run_agent_command', side_effect=slow_global):
            t1 = threading.Thread(
                target=lambda: self._http_request('/acp/v1/invoke', {'prompt': 'global task 1'})
            )
            t1.start()
            start_event.wait(timeout=2.0)

            # Second request on different conversation hits global concurrency limit
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'global task 2'})

            finish_event.set()
            t1.join()

            self.assertEqual(code, 429)
            self.assertEqual(resp.get('status'), 'error')
            self.assertEqual(resp.get('status_code'), 429)
            self.assertIn('Too Many Requests', resp.get('error', ''))

    # =========================================================================
    # 7. Pre-execution Retry Mechanics
    # =========================================================================

    def test_22_pre_execution_0_turn_eof_retries_and_succeeds(self):
        """Pre-execution transient 0-turn EOF error on new conversation retries up to 3 times."""
        eof_json = {
            "status": "ERROR",
            "error": "network stream EOF before task start",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": ""
        }
        success_json = {
            "conversation_id": "retry-succeeded-cid",
            "status": "SUCCESS",
            "response": "Success on attempt 3"
        }

        call_count = 0
        def retry_run(cmd, timeout):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=json.dumps(eof_json), stderr='')
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=json.dumps(success_json), stderr='')

        with patch('acp_server.run_agent_command', side_effect=retry_run):
            res, parsed = acp_server.execute_with_retry(['agy', '-p', 'retry test'], total_timeout_sec=10, max_retries=3)
            self.assertEqual(call_count, 3)
            self.assertEqual(parsed.get('status'), 'SUCCESS')
            self.assertEqual(parsed.get('conversation_id'), 'retry-succeeded-cid')

    def test_23_continuation_command_eof_never_retries(self):
        """Continuation command with --conversation is strictly single-attempt and NEVER retries on EOF."""
        eof_json = {
            "status": "ERROR",
            "error": "connection reset during turn",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": ""
        }
        call_count = 0
        def single_run(cmd, timeout):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=json.dumps(eof_json), stderr='')

        with patch('acp_server.run_agent_command', side_effect=single_run):
            cmd = ['agy', '--conversation', 'resume-cid-999', '-p', 'test continuation']
            res, parsed = acp_server.execute_with_retry(cmd, total_timeout_sec=10, max_retries=3)
            self.assertEqual(call_count, 1)
            self.assertEqual(parsed.get('status'), 'ERROR')

    def test_24_error_with_existing_cid_never_retries(self):
        """Error payload that already contains a conversation_id is NEVER retried."""
        err_with_cid = {
            "conversation_id": "existing-cid-777",
            "status": "ERROR",
            "error": "network failure after turn init",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": ""
        }
        call_count = 0
        def single_run(cmd, timeout):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=json.dumps(err_with_cid), stderr='')

        with patch('acp_server.run_agent_command', side_effect=single_run):
            res, parsed = acp_server.execute_with_retry(['agy', '-p', 'test'], total_timeout_sec=10, max_retries=3)
            self.assertEqual(call_count, 1)
            self.assertEqual(parsed.get('status'), 'ERROR')

    def test_25_invalid_json_never_retries_and_returns_500(self):
        """Malformed/non-JSON output from CLI is never retried and returns HTTP 500 diagnostic error."""
        call_count = 0
        def non_json_run(cmd, timeout):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="FATAL: Non JSON crash output", stderr='')

        with patch('acp_server.run_agent_command', side_effect=non_json_run):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'trigger non-json'})
            self.assertEqual(call_count, 1)
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn('not a valid JSON object', resp.get('error', ''))

    # =========================================================================
    # 8. Partial Success Preservation
    # =========================================================================

    def test_26_invoke_error_with_response_is_partial_success(self):
        """Resumed conversation turn where AGY reports ERROR after emitting response returns HTTP 200 partial_success."""
        cid = "partial-cid-1111-2222-3333-444444444444"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "Agent execution terminated due to error after completion.",
            "response": "Here is the refactored code and summary.",
            "num_turns": 4,
            "usage": {"total_tokens": 750}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=1, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {
                'conversation_id': cid,
                'prompt': 'Complete refactoring'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'partial_success')
            self.assertEqual(resp.get('action'), 'invoke')
            self.assertEqual(resp.get('conversation_id'), cid)
            self.assertEqual(resp.get('upstream_status'), 'ERROR')
            self.assertEqual(resp.get('cli_exit_code'), 1)
            self.assertIn('terminated', resp.get('upstream_error', ''))
            self.assertIn('warning', resp)
            self.assertEqual(resp.get('parsed', {}).get('response'), mock_output['response'])

    def test_27_send_message_error_with_response_is_partial_success(self):
        """POST /send-message applies identical partial-success preservation contract."""
        cid = "partial-cid-send-5555-6666-7777-888888888888"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "Late shutdown exception in print-mode",
            "response": "Refactoring completed successfully."
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=2, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/send-message', {
                'recipient_id': cid,
                'content': 'Continue next step'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'partial_success')
            self.assertEqual(resp.get('action'), 'send-message')
            self.assertEqual(resp.get('conversation_id'), cid)
            self.assertEqual(resp.get('cli_exit_code'), 2)

    def test_28_new_conversation_error_with_response_without_cid_returns_500(self):
        """Partial first turn without a conversation_id cannot establish 1:1 mapping and returns HTTP 500."""
        mock_output = {
            "status": "ERROR",
            "error": "Init failure with partial text",
            "response": "Partial first turn text without conversation_id",
            "num_turns": 1
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=1, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'First turn partial without cid'})
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn('no conversation_id', resp.get('error', ''))

    def test_29_error_without_response_remains_http_500(self):
        """Genuine failures that produced no assistant response remain HTTP 500 errors."""
        cid = "fail-cid-0000-1111-2222-333333333333"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "Authentication token expired",
            "response": "",
            "num_turns": 0,
            "usage": {"total_tokens": 0}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=1, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {
                'conversation_id': cid,
                'prompt': 'Test genuine failure'
            })
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn('Authentication token expired', resp.get('error', ''))

    # =========================================================================
    # 9. Input Validation, Limits & Edge Cases
    # =========================================================================

    def test_30_missing_prompt_returns_400(self):
        """POST /invoke missing 'prompt' parameter returns HTTP 400 Bad Request."""
        code, resp = self._http_request('/acp/v1/invoke', {})
        self.assertEqual(code, 400)
        self.assertIn('prompt', resp.get('error', ''))

    def test_31_missing_send_message_params_returns_400(self):
        """POST /send-message missing recipient_id or content returns HTTP 400 Bad Request."""
        code1, resp1 = self._http_request('/acp/v1/send-message', {'recipient_id': '123'})
        self.assertEqual(code1, 400)

        code2, resp2 = self._http_request('/acp/v1/send-message', {'content': 'hello'})
        self.assertEqual(code2, 400)

    def test_32_invalid_json_body_returns_400(self):
        """Sending invalid JSON payload returns HTTP 400 Bad Request."""
        code, resp = self._http_request('/acp/v1/invoke', raw_body=b"{invalid json payload]")
        self.assertEqual(code, 400)
        self.assertIn('Invalid JSON', resp.get('error', ''))

    def test_33_payload_too_large_returns_413(self):
        """Sending request with Content-Length exceeding 2MB MAX_CONTENT_LENGTH returns HTTP 413 Payload Too Large."""
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server_port, timeout=5)
        try:
            conn.putrequest('POST', '/acp/v1/invoke')
            conn.putheader('Authorization', f'Bearer {TEST_TOKEN}')
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Content-Length', str(acp_server.MAX_CONTENT_LENGTH + 100))
            conn.endheaders()
            resp = conn.getresponse()
            code = resp.status
            data = json.loads(resp.read().decode('utf-8'))
            self.assertEqual(code, 413)
            self.assertIn('Payload Too Large', data.get('error', ''))
        finally:
            conn.close()

    def test_34_metadata_endpoint_returns_501(self):
        """POST /acp/v1/metadata returns HTTP 501 Not Implemented in explicit conversation CLI mode."""
        code, resp = self._http_request('/acp/v1/metadata', {'conversation_id': '123'})
        self.assertEqual(code, 501)
        self.assertEqual(resp.get('status'), 'error')
        self.assertEqual(resp.get('status_code'), 501)

    def test_35_post_connection_limit_exhaustion_returns_503(self):
        """Exhausting MAX_POST_CONNECTIONS returns HTTP 503 while preserving /health availability."""
        acquired_count = 0
        try:
            # Drain all 45 post connection semaphore permits safely
            for _ in range(acp_server.MAX_POST_CONNECTIONS):
                if acp_server.post_connection_semaphore.acquire(blocking=False):
                    acquired_count += 1

            self.assertEqual(acquired_count, acp_server.MAX_POST_CONNECTIONS)

            # 46th POST should return 503 Service Busy
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'busy test'})
            self.assertEqual(code, 503)
            self.assertIn('Service Busy', resp.get('error', ''))

            # /health must still succeed (fast GET slot unaffected)
            h_code, h_resp = self._http_request('/health')
            self.assertEqual(h_code, 200)
            self.assertEqual(h_resp.get('status'), 'online')
        finally:
            # Strictly release only the permits that were successfully acquired
            for _ in range(acquired_count):
                acp_server.post_connection_semaphore.release()

    # =========================================================================
    # 10. CLI Client & Codebase Cleanliness Audit
    # =========================================================================

    def test_36_acp_cli_argument_parsing(self):
        """Test acp-cli argument parsing for explicit flags, short flags, and positional UUIDs."""
        uuid_str = "abcdef12-3456-7890-abcd-ef1234567890"

        # 1. Flag: --conversation <uuid> <prompt>
        cid1, p1 = acp_cli.parse_invoke_args(['--conversation', uuid_str, 'Run', 'analysis'])
        self.assertEqual(cid1, uuid_str)
        self.assertEqual(p1, 'Run analysis')

        # 2. Flag: --conversation=<uuid> <prompt>
        cid2, p2 = acp_cli.parse_invoke_args([f'--conversation={uuid_str}', 'Fix', 'bug'])
        self.assertEqual(cid2, uuid_str)
        self.assertEqual(p2, 'Fix bug')

        # 3. Short flag: -c <uuid> <prompt>
        cid3, p3 = acp_cli.parse_invoke_args(['-c', uuid_str, 'Build', 'artifact'])
        self.assertEqual(cid3, uuid_str)
        self.assertEqual(p3, 'Build artifact')

        # 4. Positional UUID backward compat: <uuid> <prompt>
        cid4, p4 = acp_cli.parse_invoke_args([uuid_str, 'Review', 'diff'])
        self.assertEqual(cid4, uuid_str)
        self.assertEqual(p4, 'Review diff')

        # 5. New conversation without UUID: <prompt>
        cid5, p5 = acp_cli.parse_invoke_args(['Create', 'new', 'service'])
        self.assertIsNone(cid5)
        self.assertEqual(p5, 'Create new service')

    def test_37_codebase_clean_audit_no_agy_c(self):
        """Audit repository files and startup scripts to verify zero active running commands contain agy -c."""
        files_to_check = [
            os.path.join(REPO_ROOT, 'acp_server.py'),
            os.path.join(REPO_ROOT, 'acp_watchdog.sh'),
            os.path.join(REPO_ROOT, 'ensure_acp_bridge.sh'),
            os.path.join(REPO_ROOT, 'install.sh'),
            os.path.join(REPO_ROOT, 'acp-cli'),
        ]

        for file_path in files_to_check:
            if not os.path.exists(file_path):
                continue
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            lines = content.splitlines()
            in_docstring = False
            for idx, line in enumerate(lines, 1):
                clean_line = line.strip()
                if '"""' in clean_line or "'''" in clean_line:
                    if clean_line.count('"""') % 2 != 0 or clean_line.count("'''") % 2 != 0:
                        in_docstring = not in_docstring
                    continue
                if in_docstring or clean_line.startswith('#') or clean_line.startswith('//') or clean_line.startswith('*'):
                    continue
                self.assertNotIn('agy -c', clean_line, f"Found active 'agy -c' on line {idx} of {file_path}: {line}")
                self.assertNotIn("while true; do agy", clean_line, f"Found active agy loop on line {idx} of {file_path}: {line}")

    # =========================================================================
    # 11. Real Linux Process Group Cleanup Integration Test
    # =========================================================================

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only process group integration test")
    def test_38_real_process_tree_cleanup_integration_linux(self):
        """Real Linux integration test: verify run_agent_command terminates entire process group on timeout without mocks."""
        temp_dir = tempfile.mkdtemp(prefix="pg_integration_test_")
        marker_file = os.path.join(temp_dir, "pids.json")
        script = f"""
import subprocess, sys, time, json, os
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(r"{marker_file}", "w") as f:
    json.dump({{"parent_pid": os.getpid(), "child_pid": child.pid}}, f)
    f.flush()
time.sleep(60)
"""
        cmd = [sys.executable, "-c", script]

        def is_pid_active(pid):
            try:
                with open(f"/proc/{pid}/status", "r") as f:
                    for line in f:
                        if line.startswith("State:"):
                            state = line.split()[1]
                            return state not in ("Z", "X")
                return False
            except (FileNotFoundError, ProcessLookupError):
                return False

        parent_pid = None
        child_pid = None
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                acp_server.run_agent_command(cmd, timeout_sec=0.5)

            self.assertTrue(os.path.exists(marker_file), "Process should have written marker file before timeout")
            with open(marker_file, "r") as f:
                data = json.load(f)

            parent_pid = data.get("parent_pid")
            child_pid = data.get("child_pid")

            self.assertIsNotNone(parent_pid)
            self.assertIsNotNone(child_pid)

            # Give a brief window (up to 1.0s) for kernel SIGKILL delivery & reaping
            for _ in range(20):
                if not is_pid_active(parent_pid) and not is_pid_active(child_pid):
                    break
                time.sleep(0.05)

            self.assertFalse(is_pid_active(parent_pid), f"Parent process {parent_pid} must not be active after killpg")
            self.assertFalse(is_pid_active(child_pid), f"Child process {child_pid} must not be active after killpg")
        finally:
            # Defensive cleanup in case of assertion failure
            for pid in (parent_pid, child_pid):
                if pid and is_pid_active(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
            try:
                if os.path.exists(marker_file):
                    os.remove(marker_file)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
            except Exception:
                pass


if __name__ == '__main__':
    unittest.main()
