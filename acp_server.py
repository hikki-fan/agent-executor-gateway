#!/usr/bin/env python3
"""
Custom Antigravity REST Bridge Server for Codex Integration (v2.3.0)
- Explicit 1:1 Codex Session to Antigravity Conversation Mapping
- Stateless Gateway (No session pseudo-mapping, client holds conversation_id)
- Decoupled from global agy -c loop & Language Server (Zero session preemption/hijacking)
- Per-Conversation Concurrency Locking (HTTP 409 Conflict Protection)
- Global Concurrency Semaphore (AGY_MAX_CONCURRENCY, default 1, HTTP 429)
- Configurable Total Agent Timeout Budget (ACP_AGENT_TIMEOUT_SEC, default 300s)
- Monotonic Deadline Enforcement across subprocesses & retry attempts
- Reliable CLI Flag Ordering (all options precede -p <prompt>)
- Strict JSON Object & Status Verification for CLI output
- Output JSON Parsing with Pre-execution Error Retry (EOF/network <= 3 times on new 0-turn dict only)
- In-flight Error Preservation (No retry once turns/tokens/response/conversation_id started or if resuming)
- Explicit Pipe Closure (stdin=DEVNULL) & Process Group Tree Cleanup (os.setsid + os.killpg)
- Reserved Health Capacity (45 POST / 5 Reserved Health) & Socket Timeout (10s)
"""

import http.server
import socketserver
import json
import subprocess
import os
import secrets
import sys
import signal
import socket
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PORT = int(os.environ.get("ACP_PORT", 8765))
AGY_BIN = os.environ.get("AGY_BIN") or shutil.which("agy") or "/home/codex/.local/bin/agy"
TOKEN_FILE = os.environ.get("ACP_TOKEN_FILE") or "/home/codex/.codex/acp_token"

MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # 2MB Limit
SUBPROCESS_TIMEOUT = int(os.environ.get("ACP_AGENT_TIMEOUT_SEC", 300))  # Default 300s total timeout budget
AGY_MAX_CONCURRENCY = int(os.environ.get("AGY_MAX_CONCURRENCY", 1))  # Default 1 concurrent task
MAX_HTTP_CONNECTIONS = 50  # Maximum 50 total HTTP connections
MAX_POST_CONNECTIONS = 45  # Reserved 5 connection slots strictly for /health
SOCKET_TIMEOUT = 10.0  # 10s socket read/write timeout

# Ensure token file exists
if not os.path.exists(TOKEN_FILE):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    token_val = secrets.token_hex(24)
    with open(TOKEN_FILE, "w") as f:
        f.write(token_val)
    os.chmod(TOKEN_FILE, 0o600)
else:
    with open(TOKEN_FILE, "r") as f:
        token_val = f.read().strip()

ACP_AUTH_TOKEN = token_val
EXPECTED_BEARER_HEADER = f"Bearer {ACP_AUTH_TOKEN}"

# Bounded admission control semaphore: Global max concurrent agent tasks
agent_semaphore = threading.BoundedSemaphore(AGY_MAX_CONCURRENCY)

# Total HTTP connection limit: Max 50 HTTP sockets
http_connection_semaphore = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)

# Reserved Heavy POST connection limit: Max 45 sockets (Guarantees 5 slots for /health)
post_connection_semaphore = threading.BoundedSemaphore(MAX_POST_CONNECTIONS)

# Global ThreadPoolExecutor for agent subprocesses
agent_executor = ThreadPoolExecutor(max_workers=max(AGY_MAX_CONCURRENCY, 4), thread_name_prefix="ACP_AgentWorker")

class ConversationLockManager:
    """Thread-safe lock manager ensuring that only one turn per conversation_id runs at a time."""
    def __init__(self):
        self._lock = threading.Lock()
        self._active_conversations = set()

    def acquire(self, conversation_id):
        if not conversation_id:
            return True
        with self._lock:
            if conversation_id in self._active_conversations:
                return False
            self._active_conversations.add(conversation_id)
            return True

    def release(self, conversation_id):
        if not conversation_id:
            return
        with self._lock:
            self._active_conversations.discard(conversation_id)

    def is_locked(self, conversation_id):
        if not conversation_id:
            return False
        with self._lock:
            return conversation_id in self._active_conversations

conv_lock_mgr = ConversationLockManager()

def build_agy_command(prompt, conversation_id=None, model=None, effort=None):
    """
    Construct agy CLI command with strict flag ordering:
    All configuration options precede `-p <prompt>`.
    """
    cmd = [AGY_BIN]
    if conversation_id:
        cmd.extend(['--conversation', str(conversation_id)])
    cmd.extend(['--output-format', 'json', '--dangerously-skip-permissions'])
    if model:
        cmd.extend(['--model', str(model)])
    if effort:
        cmd.extend(['--effort', str(effort)])
    cmd.extend(['-p', str(prompt)])
    return cmd

def run_agent_command(cmd, timeout_sec, env=None):
    """
    Execute agy command in an isolated Process Group with standard input closed.
    Ensures full process group cleanup (SIGKILL) on timeout.
    """
    run_env = env if env is not None else os.environ.copy()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=run_env,
        start_new_session=True  # Spawns isolated process group; proc.pid is PGID
    )
    pgid = proc.pid
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        raise

RETRYABLE_ERROR_PATTERNS = re.compile(
    r'\b(eof|broken pipe|connection reset|connection refused|network|temporary failure|resource temporarily unavailable|timeout before start)\b',
    re.IGNORECASE
)

def is_retryable_pre_execution_error(proc_result, parsed_json=None, cmd=None):
    """
    Evaluate if an error is a pre-execution transient error eligible for retry.
    Strict Requirements:
    - If cmd specifies an explicit continuation (--conversation), NEVER retry.
    - parsed_json MUST be a valid dict (if JSON missing, corrupted, or non-dict -> DO NOT retry).
    - parsed_json.get("status") == "ERROR"
    - parsed_json.get("num_turns", 0) == 0
    - parsed_json.get("usage", {}).get("total_tokens", 0) == 0
    - response must be empty / None / whitespace only
    - conversation_id must NOT exist or be empty (if conversation_id exists -> DO NOT retry).
    - error message must match retryable patterns (EOF, connection reset, etc.).
    """
    if cmd and '--conversation' in cmd:
        return False

    if not isinstance(parsed_json, dict):
        return False

    if parsed_json.get("status") != "ERROR":
        return False

    # Never retry if conversation_id was already allocated or present
    if parsed_json.get("conversation_id"):
        return False

    # num_turns must be strictly 0
    num_turns = parsed_json.get("num_turns", 0)
    if num_turns != 0:
        return False

    # usage.total_tokens must be strictly 0
    usage = parsed_json.get("usage")
    if isinstance(usage, dict):
        if usage.get("total_tokens", 0) != 0:
            return False
    elif usage is not None:
        return False

    # response must be empty
    response_content = parsed_json.get("response", "")
    if response_content and str(response_content).strip():
        return False

    # Error message must contain retryable indications
    err_msg = str(parsed_json.get("error", "")) + " " + str(parsed_json.get("message", ""))
    if RETRYABLE_ERROR_PATTERNS.search(err_msg):
        return True

    return False

def execute_with_retry(cmd, total_timeout_sec, max_retries=3):
    """
    Execute agy command with monotonic total timeout budget across retry attempts.
    Continuation commands (containing --conversation) are strictly single-attempt.
    """
    is_continuation = '--conversation' in cmd
    effective_max_retries = 1 if is_continuation else max_retries

    deadline = time.monotonic() + total_timeout_sec
    attempts = 0
    last_res = None
    last_parsed = None

    while attempts < effective_max_retries:
        attempts += 1
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=total_timeout_sec)

        last_res = run_agent_command(cmd, remaining)

        stdout_text = last_res.stdout.strip() if last_res.stdout else ""
        try:
            raw_parsed = json.loads(stdout_text) if stdout_text else None
            last_parsed = raw_parsed if isinstance(raw_parsed, dict) else None
        except Exception:
            last_parsed = None

        # Check if success: returncode == 0, valid JSON dict, status == "SUCCESS"
        if last_res.returncode == 0 and isinstance(last_parsed, dict) and last_parsed.get("status") == "SUCCESS":
            return last_res, last_parsed

        # Check if eligible for pre-execution retry
        if attempts < effective_max_retries and is_retryable_pre_execution_error(last_res, last_parsed, cmd=cmd):
            backoff = 0.3 * attempts
            if (deadline - time.monotonic()) > backoff:
                time.sleep(backoff)
                continue
            else:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=total_timeout_sec)

        # Non-retryable failure or retries exhausted
        break

    return last_res, last_parsed

class ACPRequestHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _verify_strict_bearer_auth(self):
        """Strictly enforce Authorization: Bearer <TOKEN> header"""
        auth_header = self.headers.get('Authorization', '').strip()
        if auth_header == EXPECTED_BEARER_HEADER:
            return True
        return False

    def do_OPTIONS(self):
        self._send_json({'status': 'ok'})

    def do_GET(self):
        path = self.path.rstrip('/')
        if path == '/health' or path == '/acp/v1/status':
            self._send_json({
                'status': 'online',
                'service': 'Antigravity REST Bridge Server',
                'version': '2.3.0',
                'auth_type': 'Strict Bearer Token',
                'mode': 'explicit_conversation_cli',
                'language_server': {
                    'status': 'disabled',
                    'address': None,
                    'mode': 'explicit_conversation_cli'
                },
                'limits': {
                    'max_payload_bytes': MAX_CONTENT_LENGTH,
                    'subprocess_timeout_sec': SUBPROCESS_TIMEOUT,
                    'max_worker_threads': AGY_MAX_CONCURRENCY,
                    'max_http_connections': MAX_HTTP_CONNECTIONS,
                    'max_post_connections': MAX_POST_CONNECTIONS,
                    'reserved_health_slots': MAX_HTTP_CONNECTIONS - MAX_POST_CONNECTIONS,
                    'socket_timeout_sec': SOCKET_TIMEOUT,
                    'admission_control': f'HTTP 429 Bounded Semaphore ({AGY_MAX_CONCURRENCY})'
                }
            })
        else:
            self._send_json({'error': 'Endpoint not found'}, status=404)

    def do_POST(self):
        path = self.path.rstrip('/')

        # Enforce POST Connection Capacity Limit (45 max)
        acquired_post = post_connection_semaphore.acquire(blocking=False)
        if not acquired_post:
            return self._send_json({
                'error': 'Service Busy: Maximum POST API connection capacity reached',
                'status_code': 503
            }, status=503)

        try:
            # 1. Strict Bearer Token Verification
            if not self._verify_strict_bearer_auth():
                return self._send_json({
                    'error': 'Unauthorized: Strict Bearer token required',
                    'required_header': 'Authorization: Bearer <TOKEN>'
                }, status=401)

            # 2. Payload Size Limit Check
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > MAX_CONTENT_LENGTH:
                return self._send_json({
                    'error': f'Payload Too Large: Exceeds limit of {MAX_CONTENT_LENGTH} bytes'
                }, status=413)

            body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else '{}'

            try:
                payload = json.loads(body)
            except Exception as e:
                return self._send_json({'error': f'Invalid JSON payload: {str(e)}'}, status=400)

            # 3. Handle Endpoints
            if path in ['/acp/v1/invoke', '/acp/v1/new-conversation']:
                prompt = payload.get('prompt', '')
                model = payload.get('model')
                effort = payload.get('effort')
                # Extract explicit conversation_id (or recipient_id for backward compat)
                conversation_id = payload.get('conversation_id') or payload.get('recipient_id') or ''
                conversation_id = str(conversation_id).strip()

                if not prompt:
                    return self._send_json({'error': 'Parameter "prompt" is required'}, status=400)

                # 4. Check per-conversation concurrency lock (HTTP 409)
                if conversation_id and not conv_lock_mgr.acquire(conversation_id):
                    return self._send_json({
                        'status': 'error',
                        'error': f'Conflict: Conversation {conversation_id} is currently executing another turn',
                        'status_code': 409
                    }, status=409)

                # 5. Check global concurrency semaphore (HTTP 429)
                acquired_agent = agent_semaphore.acquire(blocking=False)
                if not acquired_agent:
                    if conversation_id:
                        conv_lock_mgr.release(conversation_id)
                    return self._send_json({
                        'status': 'error',
                        'error': f'Too Many Requests: Maximum {AGY_MAX_CONCURRENCY} concurrent agent tasks active',
                        'status_code': 429
                    }, status=429)

                try:
                    cmd = build_agy_command(prompt, conversation_id=conversation_id if conversation_id else None, model=model, effort=effort)
                    future = None
                    try:
                        future = agent_executor.submit(execute_with_retry, cmd, SUBPROCESS_TIMEOUT, 3)
                        res, parsed = future.result(timeout=SUBPROCESS_TIMEOUT + 5)

                        out_text = res.stdout.strip() if res.stdout else ""
                        err_text = res.stderr.strip() if res.stderr else ""

                        # Strict validation:
                        # 1. returncode == 0
                        # 2. parsed must be a valid dict
                        # 3. parsed.get("status") == "SUCCESS"
                        # 4. For new conversation: parsed must have top-level conversation_id
                        is_valid_success = (
                            res.returncode == 0 and
                            isinstance(parsed, dict) and
                            parsed.get("status") == "SUCCESS"
                        )

                        if is_valid_success:
                            if conversation_id:
                                out_cid = conversation_id
                            else:
                                top_cid = parsed.get("conversation_id")
                                if top_cid and str(top_cid).strip():
                                    out_cid = str(top_cid).strip()
                                else:
                                    out_cid = None
                                    is_valid_success = False

                        if is_valid_success:
                            self._send_json({
                                'status': 'success',
                                'action': 'invoke' if conversation_id else 'new-conversation',
                                'conversation_id': str(out_cid),
                                'mode': 'explicit_conversation_cli',
                                'output': out_text,
                                'parsed': parsed
                            })
                        else:
                            error_detail = ""
                            if isinstance(parsed, dict) and parsed.get("status") == "SUCCESS" and not conversation_id and not parsed.get("conversation_id"):
                                error_detail = "CLI returned status: SUCCESS but missing required top-level 'conversation_id' field in JSON"
                            elif isinstance(parsed, dict) and parsed.get("error"):
                                error_detail = str(parsed.get("error"))
                            elif err_text:
                                error_detail = err_text
                            elif not isinstance(parsed, dict):
                                error_detail = "CLI output is not a valid JSON object"
                            elif parsed.get("status") != "SUCCESS":
                                error_detail = f"CLI returned status: {parsed.get('status')}"
                            else:
                                error_detail = f"CLI process exited with code {res.returncode}"

                            self._send_json({
                                'status': 'error',
                                'action': 'invoke' if conversation_id else 'new-conversation',
                                'conversation_id': conversation_id if conversation_id else None,
                                'mode': 'explicit_conversation_cli',
                                'error': error_detail,
                                'output': out_text,
                                'parsed': parsed
                            }, status=500)
                    except subprocess.TimeoutExpired:
                        if future:
                            future.cancel()
                        self._send_json({
                            'status': 'error',
                            'error': f'Agent Execution Timed Out after total budget of {SUBPROCESS_TIMEOUT}s',
                            'conversation_id': conversation_id if conversation_id else None,
                            'status_code': 504
                        }, status=504)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({
                            'status': 'error',
                            'error': f'Agent Execution Failed: {str(e)}',
                            'conversation_id': conversation_id if conversation_id else None
                        }, status=504)
                finally:
                    agent_semaphore.release()
                    if conversation_id:
                        conv_lock_mgr.release(conversation_id)

            elif path == '/acp/v1/send-message':
                recipient_id = payload.get('recipient_id') or payload.get('conversation_id') or ''
                recipient_id = str(recipient_id).strip()
                content = payload.get('content') or payload.get('prompt') or ''
                model = payload.get('model')
                effort = payload.get('effort')

                if not recipient_id or not content:
                    return self._send_json({'error': 'Parameters "recipient_id" and "content" are required'}, status=400)

                # Check per-conversation concurrency lock (HTTP 409)
                if not conv_lock_mgr.acquire(recipient_id):
                    return self._send_json({
                        'status': 'error',
                        'error': f'Conflict: Conversation {recipient_id} is currently executing another turn',
                        'status_code': 409
                    }, status=409)

                # Check global concurrency semaphore (HTTP 429)
                acquired_agent = agent_semaphore.acquire(blocking=False)
                if not acquired_agent:
                    conv_lock_mgr.release(recipient_id)
                    return self._send_json({
                        'status': 'error',
                        'error': f'Too Many Requests: Maximum {AGY_MAX_CONCURRENCY} concurrent agent tasks active',
                        'status_code': 429
                    }, status=429)

                try:
                    cmd = build_agy_command(content, conversation_id=recipient_id, model=model, effort=effort)
                    future = None
                    try:
                        future = agent_executor.submit(execute_with_retry, cmd, SUBPROCESS_TIMEOUT, 3)
                        res, parsed = future.result(timeout=SUBPROCESS_TIMEOUT + 5)

                        out_text = res.stdout.strip() if res.stdout else ""
                        err_text = res.stderr.strip() if res.stderr else ""

                        is_valid_success = (
                            res.returncode == 0 and
                            isinstance(parsed, dict) and
                            parsed.get("status") == "SUCCESS"
                        )

                        if is_valid_success:
                            self._send_json({
                                'status': 'success',
                                'action': 'send-message',
                                'conversation_id': recipient_id,
                                'mode': 'explicit_conversation_cli',
                                'output': out_text,
                                'parsed': parsed
                            })
                        else:
                            error_detail = ""
                            if isinstance(parsed, dict) and parsed.get("error"):
                                error_detail = str(parsed.get("error"))
                            elif err_text:
                                error_detail = err_text
                            elif not isinstance(parsed, dict):
                                error_detail = "CLI output is not a valid JSON object"
                            elif parsed.get("status") != "SUCCESS":
                                error_detail = f"CLI returned status: {parsed.get('status')}"
                            else:
                                error_detail = f"CLI process exited with code {res.returncode}"

                            self._send_json({
                                'status': 'error',
                                'action': 'send-message',
                                'conversation_id': recipient_id,
                                'mode': 'explicit_conversation_cli',
                                'error': error_detail,
                                'output': out_text,
                                'parsed': parsed
                            }, status=500)
                    except subprocess.TimeoutExpired:
                        if future:
                            future.cancel()
                        self._send_json({
                            'status': 'error',
                            'error': f'Agent Message Timed Out after total budget of {SUBPROCESS_TIMEOUT}s',
                            'conversation_id': recipient_id,
                            'status_code': 504
                        }, status=504)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({
                            'status': 'error',
                            'error': f'Agent Message Failed: {str(e)}',
                            'conversation_id': recipient_id
                        }, status=504)
                finally:
                    agent_semaphore.release()
                    conv_lock_mgr.release(recipient_id)

            elif path == '/acp/v1/metadata':
                # Metadata endpoint requires Language Server IPC which is disabled in explicit conversation CLI mode.
                self._send_json({
                    'status': 'error',
                    'error': 'Metadata endpoint is not supported in explicit conversation CLI mode without Language Server',
                    'status_code': 501
                }, status=501)

            else:
                self._send_json({'error': f'Unknown path: {path}'}, status=404)
        finally:
            post_connection_semaphore.release()

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """
    Threaded HTTP Server with Socket Read/Write Timeouts (10s),
    Exception-Safe Connection Semaphore (50 max), and Reserved /health capacity.
    """
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        """Enforce maximum total HTTP connection limit of 50 sockets with exception safety"""
        if not http_connection_semaphore.acquire(blocking=False):
            try:
                request.close()
            except Exception:
                pass
            return

        try:
            request.settimeout(SOCKET_TIMEOUT)
            super().process_request(request, client_address)
        except Exception:
            http_connection_semaphore.release()
            try:
                request.close()
            except Exception:
                pass
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            http_connection_semaphore.release()

server_instance = None

def sigterm_handler(signum, frame):
    """
    Non-deadlocking Signal Handler:
    BaseServer.shutdown() MUST be called from a separate thread while serve_forever() runs.
    """
    print(f"\n[*] Received signal {signum}, triggering async server shutdown...")
    if server_instance:
        threading.Thread(target=server_instance.shutdown, daemon=True).start()

def run_server():
    global server_instance
    server_instance = ThreadedHTTPServer(('127.0.0.1', PORT), ACPRequestHandler)

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    print(f"[*] Custom Antigravity REST Bridge Server listening on http://127.0.0.1:{PORT}")
    print(f"[*] Mode=explicit_conversation_cli, AGY_MAX_CONCURRENCY={AGY_MAX_CONCURRENCY}, TimeoutBudget={SUBPROCESS_TIMEOUT}s")
    print(f"[*] Max HTTP Connections={MAX_HTTP_CONNECTIONS}, Socket Timeout={SOCKET_TIMEOUT}s")
    print(f"[*] Token auth: Strict Bearer from {TOKEN_FILE}")

    try:
        server_instance.serve_forever()
    finally:
        print("[*] Exited serve_forever(). Cancelling unstarted futures & shutting down ThreadPool...")
        server_instance.server_close()
        agent_executor.shutdown(wait=True, cancel_futures=True)
        print("[*] Graceful shutdown completed cleanly.")

if __name__ == '__main__':
    run_server()
