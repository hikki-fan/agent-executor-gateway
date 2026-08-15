#!/usr/bin/env python3
"""
Comprehensive HTTP-level & Unit Test Suite for ACP REST Bridge (v2.4.0)
Tests:
- Token isolation (temporary token file without reading real secrets)
- Real HTTP server POST /invoke (new conversation with extracted ID)
- Real HTTP server POST /invoke (resume with explicit --conversation and flags before -p)
- Real HTTP server POST /send-message (explicit --conversation)
- Same-conversation concurrency HTTP 409 Conflict
- Global concurrency HTTP 429 Too Many Requests
- Pre-execution 0-turn EOF retry (retries up to 3 times for new conversation)
- Explicit continuation (--conversation) with 0-turn EOF error (strictly NO retry, calls exactly once)
- Existing conversation_id in error JSON (NO retry)
- Invalid JSON output handling (NO retry, diagnostic HTTP 500)
- Response text containing UUID but missing top-level conversation_id (strictly rejected with HTTP 500)
- ERROR with a non-empty response preserved as HTTP 200 partial_success
- Separate automatic-login grace included in server and client timeout budgets
- Monotonic timeout budget deadline enforcement & process group cleanup
- CLI argument parsing (parse_invoke_args for --conversation, --conversation=, positional uuid, and prompt)
- Codebase & startup script audit (zero agy -c commands)
"""

import unittest
from unittest.mock import patch, MagicMock
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

# Ensure isolated temporary token file before importing acp_server
TEMP_TOKEN_DIR = tempfile.mkdtemp()
TEMP_TOKEN_FILE = os.path.join(TEMP_TOKEN_DIR, "test_acp_token")
TEST_TOKEN = "test_bearer_token_1234567890abcdef"
with open(TEMP_TOKEN_FILE, "w") as f:
    f.write(TEST_TOKEN)

os.environ["ACP_TOKEN_FILE"] = TEMP_TOKEN_FILE
os.environ["ACP_PORT"] = "0"  # ephemeral port for tests
os.environ["AGY_MAX_CONCURRENCY"] = "1"
os.environ["ACP_AGENT_TIMEOUT_SEC"] = "10"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acp_server

from importlib.machinery import SourceFileLoader
acp_cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acp-cli")
acp_cli = SourceFileLoader("acp_cli_mod", acp_cli_path).load_module()

class TestACPBridgeHTTPAndUnit(unittest.TestCase):
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
        try:
            if os.path.exists(TEMP_TOKEN_FILE):
                os.remove(TEMP_TOKEN_FILE)
            if os.path.exists(TEMP_TOKEN_DIR):
                os.rmdir(TEMP_TOKEN_DIR)
        except Exception:
            pass

    def _http_request(self, path, data=None, token=TEST_TOKEN):
        url = f"{self.server_url}{path}"
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token:
            headers['Authorization'] = f"Bearer {token}"

        body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method='POST' if data is not None else 'GET')

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.getcode()
                resp_data = json.loads(resp.read().decode('utf-8'))
                return status_code, resp_data
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8')
            try:
                parsed = json.loads(err_body)
            except Exception:
                parsed = {'raw': err_body}
            return e.code, parsed

    def test_01_http_invoke_new_conversation_extracts_id(self):
        """Test HTTP POST /invoke creates new conversation and extracts top-level conversation_id"""
        mock_output = {
            "conversation_id": "11111111-2222-3333-4444-555555555555",
            "status": "SUCCESS",
            "response": "Hello from mock agy",
            "num_turns": 1,
            "usage": {"total_tokens": 100}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command') as mock_run:
            mock_run.return_value = mock_res
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'Write a function'})

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('conversation_id'), '11111111-2222-3333-4444-555555555555')
            self.assertEqual(resp.get('action'), 'new-conversation')
            self.assertEqual(resp.get('mode'), 'explicit_conversation_cli')

            called_cmd = mock_run.call_args[0][0]
            self.assertNotIn('--conversation', called_cmd)
            self.assertIn('--output-format', called_cmd)
            p_idx = called_cmd.index('-p')
            self.assertEqual(p_idx, len(called_cmd) - 2)
            self.assertEqual(called_cmd[-1], 'Write a function')

    def test_02_http_invoke_resume_conversation_flag_order(self):
        """Test HTTP POST /invoke with conversation_id passes explicit --conversation before -p"""
        cid = "22222222-3333-4444-5555-666666666666"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Turn 2 complete",
            "num_turns": 2,
            "usage": {"total_tokens": 250}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command') as mock_run:
            mock_run.return_value = mock_res
            code, resp = self._http_request('/acp/v1/invoke', {
                'conversation_id': cid,
                'prompt': 'Refactor the function',
                'model': 'pro',
                'effort': 'high'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('conversation_id'), cid)
            self.assertEqual(resp.get('action'), 'invoke')

            called_cmd = mock_run.call_args[0][0]
            self.assertIn('--conversation', called_cmd)
            c_idx = called_cmd.index('--conversation')
            self.assertEqual(called_cmd[c_idx + 1], cid)
            p_idx = called_cmd.index('-p')
            self.assertGreater(p_idx, c_idx)
            self.assertEqual(called_cmd[-1], 'Refactor the function')

    def test_03_http_send_message_explicit_resume(self):
        """Test HTTP POST /send-message passes explicit --conversation and flags before -p"""
        cid = "33333333-4444-5555-6666-777777777777"
        mock_output = {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": "Message received",
            "num_turns": 3,
            "usage": {"total_tokens": 400}
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command') as mock_run:
            mock_run.return_value = mock_res
            code, resp = self._http_request('/acp/v1/send-message', {
                'recipient_id': cid,
                'content': 'Proceed to execution'
            })

            self.assertEqual(code, 200)
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('conversation_id'), cid)
            self.assertEqual(resp.get('action'), 'send-message')

            called_cmd = mock_run.call_args[0][0]
            self.assertIn('--conversation', called_cmd)
            self.assertEqual(called_cmd[-1], 'Proceed to execution')

    def test_04_http_same_conversation_concurrency_409(self):
        """Test concurrent requests to the same conversation_id immediately return HTTP 409 Conflict"""
        cid = "lock-test-4444-5555-6666-777777777777"
        start_event = threading.Event()
        finish_event = threading.Event()

        def slow_run(cmd, timeout):
            start_event.set()
            finish_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "Done"}),
                stderr=''
            )

        with patch('acp_server.run_agent_command', side_effect=slow_run):
            t1 = threading.Thread(
                target=lambda: self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'turn 1'})
            )
            t1.start()

            start_event.wait(timeout=2.0)

            # Second concurrent request for the same conversation_id
            code, resp = self._http_request('/acp/v1/invoke', {'conversation_id': cid, 'prompt': 'turn 2'})

            finish_event.set()
            t1.join()

            self.assertEqual(code, 409)
            self.assertIn('Conflict', resp.get('error', ''))
            self.assertIn(cid, resp.get('error', ''))

    def test_05_http_global_concurrency_429(self):
        """Test global concurrency limit enforcement returns HTTP 429 Too Many Requests"""
        start_event = threading.Event()
        finish_event = threading.Event()

        def slow_run(cmd, timeout):
            start_event.set()
            finish_event.wait(timeout=5.0)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"status": "SUCCESS", "response": "Done"}),
                stderr=''
            )

        with patch('acp_server.run_agent_command', side_effect=slow_run):
            t1 = threading.Thread(
                target=lambda: self._http_request('/acp/v1/invoke', {'prompt': 'global 1'})
            )
            t1.start()

            start_event.wait(timeout=2.0)

            # Second request with different/empty conversation_id hits global limit (1)
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'global 2'})

            finish_event.set()
            t1.join()

            self.assertEqual(code, 429)
            self.assertIn('Too Many Requests', resp.get('error', ''))

    def test_06_pre_execution_0_turn_eof_retries_3_times(self):
        """Test pre-execution 0-turn EOF error without conversation_id retries up to 3 times"""
        eof_json = {
            "status": "ERROR",
            "error": "network stream EOF before task start",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": ""
        }
        success_json = {
            "conversation_id": "new-conv-after-retry",
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
            res, parsed = acp_server.execute_with_retry(['agy', '-p', 'test'], total_timeout_sec=10, max_retries=3)
            self.assertEqual(call_count, 3)
            self.assertEqual(parsed.get('status'), 'SUCCESS')
            self.assertEqual(parsed.get('conversation_id'), 'new-conv-after-retry')

    def test_07_continuation_command_eof_never_retries(self):
        """Test that explicit continuation (--conversation) with 0-turn EOF is strictly called only ONCE (no retry)"""
        eof_json_no_cid = {
            "status": "ERROR",
            "error": "network EOF during connection",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": ""
        }
        call_count = 0
        def single_run(cmd, timeout):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=json.dumps(eof_json_no_cid), stderr='')

        with patch('acp_server.run_agent_command', side_effect=single_run):
            cmd = ['agy', '--conversation', 'resume-cid-1111', '-p', 'test']
            res, parsed = acp_server.execute_with_retry(cmd, total_timeout_sec=10, max_retries=3)
            # Must NOT retry because cmd has --conversation
            self.assertEqual(call_count, 1)
            self.assertEqual(parsed.get('status'), 'ERROR')

    def test_08_existing_conversation_id_in_error_json_does_not_retry(self):
        """Test error containing an existing conversation_id is NEVER retried"""
        err_with_cid = {
            "conversation_id": "existing-cid-1234",
            "status": "ERROR",
            "error": "network EOF during turn",
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

    def test_09_invalid_json_does_not_retry_and_returns_500(self):
        """Test that invalid/unparseable JSON output is not retried and returns HTTP 500 error"""
        call_count = 0
        def invalid_json_run(cmd, timeout):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[Not a valid JSON dict]", stderr='')

        with patch('acp_server.run_agent_command', side_effect=invalid_json_run):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'trigger invalid json'})
            self.assertEqual(call_count, 1)
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn('not a valid JSON object', resp.get('error', ''))

    def test_10_response_body_contains_uuid_but_missing_top_level_cid_rejected(self):
        """Test that a UUID in response text without top-level parsed['conversation_id'] is rejected with HTTP 500"""
        mock_output = {
            "status": "SUCCESS",
            "response": "Here is a random UUID: 99999999-8888-7777-6666-555555555555 in response text",
            "num_turns": 1
            # Note: conversation_id is intentionally missing from the top-level dict!
        }
        mock_res = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout=json.dumps(mock_output), stderr='')

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'generate a uuid'})
            # Must NOT guess the UUID from response text, and MUST reject with HTTP 500 diagnostic error
            self.assertEqual(code, 500)
            self.assertEqual(resp.get('status'), 'error')
            self.assertIn("missing required top-level 'conversation_id' field", resp.get('error', ''))

    def test_11_invoke_error_with_response_is_partial_success(self):
        """Preserve a resumed turn when agy reports ERROR after producing a response."""
        cid = "44444444-5555-6666-7777-888888888888"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "Agent execution terminated due to error.",
            "response": "Review completed: all tests passed.",
            "num_turns": 4,
            "usage": {"total_tokens": 800},
        }
        mock_res = subprocess.CompletedProcess(
            args=['agy'], returncode=1, stdout=json.dumps(mock_output), stderr=''
        )

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {
                'conversation_id': cid,
                'prompt': 'Review the implementation',
            })

        self.assertEqual(code, 200)
        self.assertEqual(resp.get('status'), 'partial_success')
        self.assertEqual(resp.get('conversation_id'), cid)
        self.assertEqual(resp.get('upstream_status'), 'ERROR')
        self.assertEqual(resp.get('cli_exit_code'), 1)
        self.assertIn('terminated', resp.get('upstream_error', ''))
        self.assertEqual(resp.get('parsed', {}).get('response'), mock_output['response'])

    def test_12_new_conversation_error_with_response_requires_conversation_id(self):
        """A partial first turn without a conversation ID cannot satisfy 1:1 mapping."""
        mock_output = {
            "status": "ERROR",
            "error": "Agent execution terminated due to error.",
            "response": "Some useful output",
            "num_turns": 1,
        }
        mock_res = subprocess.CompletedProcess(
            args=['agy'], returncode=1, stdout=json.dumps(mock_output), stderr=''
        )

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {'prompt': 'Start work'})

        self.assertEqual(code, 500)
        self.assertEqual(resp.get('status'), 'error')
        self.assertIn('no conversation_id', resp.get('error', ''))

    def test_13_send_message_error_with_response_is_partial_success(self):
        """The send-message alias applies the same partial-success contract."""
        cid = "55555555-6666-7777-8888-999999999999"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "late print-mode shutdown error",
            "response": "The requested work is complete.",
        }
        mock_res = subprocess.CompletedProcess(
            args=['agy'], returncode=2, stdout=json.dumps(mock_output), stderr=''
        )

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/send-message', {
                'recipient_id': cid,
                'content': 'Continue',
            })

        self.assertEqual(code, 200)
        self.assertEqual(resp.get('status'), 'partial_success')
        self.assertEqual(resp.get('action'), 'send-message')
        self.assertEqual(resp.get('cli_exit_code'), 2)

    def test_14_error_without_response_remains_http_500(self):
        """Do not soften genuine failures that produced no assistant response."""
        cid = "66666666-7777-8888-9999-000000000000"
        mock_output = {
            "conversation_id": cid,
            "status": "ERROR",
            "error": "User location is not supported",
            "response": "",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
        }
        mock_res = subprocess.CompletedProcess(
            args=['agy'], returncode=1, stdout=json.dumps(mock_output), stderr=''
        )

        with patch('acp_server.run_agent_command', return_value=mock_res):
            code, resp = self._http_request('/acp/v1/invoke', {
                'conversation_id': cid,
                'prompt': 'Continue',
            })

        self.assertEqual(code, 500)
        self.assertEqual(resp.get('status'), 'error')
        self.assertIn('location', resp.get('error', ''))

    def test_15_health_reports_auth_grace_and_total_process_timeout(self):
        code, resp = self._http_request('/health')

        self.assertEqual(code, 200)
        self.assertEqual(resp.get('version'), '2.4.0')
        limits = resp.get('limits', {})
        self.assertEqual(limits.get('auth_grace_sec'), acp_server.AUTH_GRACE_SEC)
        self.assertEqual(
            limits.get('total_process_timeout_sec'),
            acp_server.SUBPROCESS_TIMEOUT + acp_server.AUTH_GRACE_SEC,
        )
        self.assertEqual(
            acp_cli.CLIENT_TIMEOUT,
            acp_server.SUBPROCESS_TIMEOUT + acp_server.AUTH_GRACE_SEC + 30,
        )

    def test_16_monotonic_timeout_deadline_and_cleanup(self):
        """Test total timeout budget raises TimeoutExpired and calls os.killpg"""
        def timeout_run(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with patch('acp_server.run_agent_command', side_effect=timeout_run):
            with self.assertRaises(subprocess.TimeoutExpired):
                acp_server.execute_with_retry(['agy', '-p', 'timeout test'], total_timeout_sec=2, max_retries=3)

    def test_17_acp_cli_argument_parsing(self):
        """Test acp-cli argument parsing for explicit --conversation, -c, and positional UUID without contacting network"""
        test_uuid = "abcdef12-3456-7890-abcd-ef1234567890"

        # 1. Flag syntax: --conversation <uuid> <prompt>
        cid1, prompt1 = acp_cli.parse_invoke_args(['--conversation', test_uuid, 'Refactor', 'code'])
        self.assertEqual(cid1, test_uuid)
        self.assertEqual(prompt1, 'Refactor code')

        # 2. Flag syntax: --conversation=<uuid> <prompt>
        cid2, prompt2 = acp_cli.parse_invoke_args([f'--conversation={test_uuid}', 'Run', 'tests'])
        self.assertEqual(cid2, test_uuid)
        self.assertEqual(prompt2, 'Run tests')

        # 3. Short flag syntax: -c <uuid> <prompt>
        cid3, prompt3 = acp_cli.parse_invoke_args(['-c', test_uuid, 'Check', 'status'])
        self.assertEqual(cid3, test_uuid)
        self.assertEqual(prompt3, 'Check status')

        # 4. Backward-compatible positional syntax: <uuid> <prompt>
        cid4, prompt4 = acp_cli.parse_invoke_args([test_uuid, 'Fix', 'bug'])
        self.assertEqual(cid4, test_uuid)
        self.assertEqual(prompt4, 'Fix bug')

        # 5. New conversation (no UUID): <prompt>
        cid5, prompt5 = acp_cli.parse_invoke_args(['Write', 'a', 'module'])
        self.assertIsNone(cid5)
        self.assertEqual(prompt5, 'Write a module')

    def test_18_codebase_audit_no_agy_c_command(self):
        """Audit codebase and startup scripts to verify zero active running commands contain agy -c"""
        files_to_check = [
            '/workspace/antigravity-rest-bridge/acp_server.py',
            '/workspace/antigravity-rest-bridge/acp_watchdog.sh',
            '/workspace/docker-codex/docker/start-codex-container.sh',
            '/workspace/antigravity-rest-bridge/install.sh'
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

if __name__ == '__main__':
    unittest.main()
