#!/usr/bin/env python3
"""
Custom Antigravity REST Bridge Server for Codex Integration (v2.2.1)
- Antigravity Language Server (LS) Auto-Discovery & Dynamic Injection (ANTIGRAVITY_LS_ADDRESS)
- Deep Health Check (Language Server Connectivity Verification)
- Standalone CLI Fallback (`agy -p`) when LS is offline
- Reserved Health Capacity (45 POST / 5 Reserved Health)
- Explicit Pipe Closure & Process Group Tree Cleanup (os.setsid + os.killpg)
- Socket Timeout (10s) & Bounded Semaphore Admission Control (10 Agent tasks & HTTP 429)
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
import glob
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PORT = int(os.environ.get("ACP_PORT", 8765))
AGY_BIN = os.environ.get("AGY_BIN") or shutil.which("agy") or "/home/codex/.local/bin/agy"
TOKEN_FILE = os.environ.get("ACP_TOKEN_FILE") or "/home/codex/.codex/acp_token"
AGY_LOG_DIR = os.environ.get("AGY_LOG_DIR") or "/home/codex/.gemini/antigravity-cli/log"
ACP_PROJECT_ID = os.environ.get("ACP_PROJECT_ID") or "default-cli-project"

MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # 2MB Limit
SUBPROCESS_TIMEOUT = 60  # 60s timeout for agent subprocesses
MAX_WORKERS = 10  # Maximum 10 concurrent agent tasks
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

# Bounded admission control semaphore: Max 10 agent tasks
agent_semaphore = threading.BoundedSemaphore(MAX_WORKERS)

# Total HTTP connection limit: Max 50 HTTP sockets
http_connection_semaphore = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)

# Reserved Heavy POST connection limit: Max 45 sockets (Guarantees 5 slots for /health)
post_connection_semaphore = threading.BoundedSemaphore(MAX_POST_CONNECTIONS)

# Global ThreadPoolExecutor for heavy agent subprocesses
agent_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ACP_AgentWorker")
ls_discovery_lock = threading.Lock()
ls_discovery_cache = {"address": None, "expires_at": 0.0}

def test_ls_connect(addr):
    """Test TCP socket connection to Language Server address"""
    if not addr:
        return False
    try:
        host, port = addr.split(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False

def agy_owns_listening_port(port):
    """Verify that a local TCP listening socket belongs to the managed `agy -c` process."""
    target_inodes = set()
    port_hex = f"{int(port):04X}"
    for table_path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table_path, "r", encoding="ascii") as table:
                next(table, None)
                for line in table:
                    fields = line.split()
                    if len(fields) > 9 and fields[1].rsplit(":", 1)[-1].upper() == port_hex and fields[3] == "0A":
                        target_inodes.add(fields[9])
        except OSError:
            pass
    if not target_inodes:
        return False

    socket_links = {f"socket:[{inode}]" for inode in target_inodes}
    for proc_dir in glob.glob("/proc/[0-9]*"):
        try:
            exe_name = os.path.basename(os.readlink(os.path.join(proc_dir, "exe")))
            if exe_name != "agy":
                continue
            with open(os.path.join(proc_dir, "cmdline"), "rb") as cmdline_file:
                args = [arg.decode("utf-8", errors="ignore") for arg in cmdline_file.read().split(b"\0") if arg]
            if len(args) < 2 or args[1] != "-c":
                continue
            for fd_path in glob.glob(os.path.join(proc_dir, "fd", "*")):
                try:
                    if os.readlink(fd_path) in socket_links:
                        return True
                except OSError:
                    pass
        except OSError:
            pass
    return False

def valid_ls_candidate(addr):
    """Accept only a reachable local listener owned by the managed agy process."""
    try:
        host, port = addr.rsplit(":", 1)
        return host in ("localhost", "127.0.0.1") and test_ls_connect(addr) and agy_owns_listening_port(int(port))
    except (AttributeError, TypeError, ValueError):
        return False

def read_log_tail(path, max_bytes=65536):
    """Read only the tail of an agy log to bound health-check disk I/O."""
    with open(path, "rb") as log_file:
        log_file.seek(0, os.SEEK_END)
        size = log_file.tell()
        log_file.seek(max(0, size - max_bytes), os.SEEK_SET)
        return log_file.read().decode("utf-8", errors="ignore")

def discover_ls_address():
    """
    Auto-discover live Antigravity Language Server address.
    Checks environment variables/process environments first, then parses recent agy logs.
    agy 1.1.13 starts the HTTP Language Server on a random local port but does not
    export ANTIGRAVITY_LS_ADDRESS in its own process environment.
    """
    with ls_discovery_lock:
        cached_addr = ls_discovery_cache["address"]
        if time.monotonic() < ls_discovery_cache["expires_at"] and valid_ls_candidate(cached_addr):
            return cached_addr

        env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
        if env_addr and valid_ls_candidate(env_addr):
            ls_discovery_cache.update(address=env_addr, expires_at=time.monotonic() + 3.0)
            return env_addr

        for p in glob.glob("/proc/[0-9]*/environ"):
            try:
                with open(p, "rb") as f:
                    content = f.read().decode("utf-8", errors="ignore")
                    if "ANTIGRAVITY_LS_ADDRESS=" in content:
                        for item in content.split("\0"):
                            if item.startswith("ANTIGRAVITY_LS_ADDRESS="):
                                candidate = item.split("=", 1)[1].strip()
                                if candidate and valid_ls_candidate(candidate):
                                    ls_discovery_cache.update(address=candidate, expires_at=time.monotonic() + 3.0)
                                    return candidate
            except Exception:
                pass

        # Prefer the newest logs and require the listener to belong to `agy -c`.
        log_paths = glob.glob(os.path.join(AGY_LOG_DIR, "cli-*.log"))
        log_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        port_pattern = re.compile(r"Language server listening on random port at (\d+) for HTTP\b")
        for path in log_paths[:20]:
            try:
                for line in reversed(read_log_tail(path).splitlines()):
                    match = port_pattern.search(line)
                    if match:
                        port = int(match.group(1))
                        candidate = f"localhost:{port}"
                        if valid_ls_candidate(candidate):
                            ls_discovery_cache.update(address=candidate, expires_at=time.monotonic() + 3.0)
                            return candidate
            except (OSError, ValueError):
                pass
        ls_discovery_cache.update(address=None, expires_at=time.monotonic() + 1.0)
        return None

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
            ls_addr = discover_ls_address()
            ls_connected = test_ls_connect(ls_addr)
            self._send_json({
                'status': 'online',
                'service': 'Antigravity REST Bridge Server',
                'version': '2.2.1',
                'auth_type': 'Strict Bearer Token',
                'language_server': {
                    'status': 'connected' if ls_connected else 'offline',
                    'address': ls_addr if ls_connected else None,
                    'mode': 'agentapi_ipc' if ls_connected else 'standalone_fallback'
                },
                'limits': {
                    'max_payload_bytes': MAX_CONTENT_LENGTH,
                    'subprocess_timeout_sec': SUBPROCESS_TIMEOUT,
                    'max_worker_threads': MAX_WORKERS,
                    'max_http_connections': MAX_HTTP_CONNECTIONS,
                    'max_post_connections': MAX_POST_CONNECTIONS,
                    'reserved_health_slots': MAX_HTTP_CONNECTIONS - MAX_POST_CONNECTIONS,
                    'socket_timeout_sec': SOCKET_TIMEOUT,
                    'admission_control': 'HTTP 429 Bounded Semaphore (10)'
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

            # Dynamic LS Address discovery & environment injection
            ls_address = discover_ls_address()
            env = os.environ.copy()
            if ls_address:
                env["ANTIGRAVITY_LS_ADDRESS"] = ls_address
                # Required by agy 1.1.13 when agentapi includes workspace environment data.
                env.setdefault("ANTIGRAVITY_PROJECT_ID", ACP_PROJECT_ID)

            # Helper for process-group execution with LS environment injection
            def run_agent_command(cmd, timeout_sec):
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
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
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass
                    raise

            # 3. Admission Control: Try acquiring non-blocking semaphore
            acquired_agent = agent_semaphore.acquire(blocking=False)
            if not acquired_agent:
                return self._send_json({
                    'error': 'Too Many Requests: Maximum 10 concurrent agent tasks active',
                    'status_code': 429
                }, status=429)

            try:
                if path in ['/acp/v1/invoke', '/acp/v1/new-conversation']:
                    prompt = payload.get('prompt', '')
                    model = payload.get('model', '')
                    title = payload.get('title', '')

                    if not prompt:
                        return self._send_json({'error': 'Parameter "prompt" is required'}, status=400)

                    # Choose execution strategy: agentapi IPC (if LS online) or standalone agy -p
                    if ls_address:
                        cmd = [AGY_BIN, 'agentapi', 'new-conversation']
                        if model:
                            cmd.append(f'--model={model}')
                        if title:
                            cmd.append(f'--title={title}')
                        cmd.append(prompt)
                    else:
                        # Fallback: standalone print mode
                        cmd = [AGY_BIN, '-p', prompt, '--dangerously-skip-permissions', '--output-format', 'json']
                        if model:
                            cmd.extend(['--model', model])

                    future = None
                    try:
                        future = agent_executor.submit(run_agent_command, cmd, SUBPROCESS_TIMEOUT)
                        res = future.result(timeout=SUBPROCESS_TIMEOUT + 2)
                        if res.returncode == 0:
                            self._send_json({
                                'status': 'success',
                                'action': 'new-conversation',
                                'mode': 'agentapi_ipc' if ls_address else 'standalone_cli',
                                'output': res.stdout.strip()
                            })
                        else:
                            self._send_json({
                                'status': 'error',
                                'error': res.stderr.strip() or res.stdout.strip()
                            }, status=500)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({
                            'status': 'error',
                            'error': f'Agent Execution Timed Out or Failed: {str(e)}'
                        }, status=504)

                elif path == '/acp/v1/send-message':
                    recipient_id = payload.get('recipient_id', '')
                    content = payload.get('content', '')
                    title = payload.get('title', '')

                    if not recipient_id or not content:
                        return self._send_json({'error': 'Parameters "recipient_id" and "content" are required'}, status=400)

                    if not ls_address:
                        return self._send_json({'error': 'Cannot send message: Antigravity Language Server is offline'}, status=503)

                    cmd = [AGY_BIN, 'agentapi', 'send-message']
                    if title:
                        cmd.append(f'--title={title}')
                    cmd.extend([recipient_id, content])

                    future = None
                    try:
                        future = agent_executor.submit(run_agent_command, cmd, SUBPROCESS_TIMEOUT)
                        res = future.result(timeout=SUBPROCESS_TIMEOUT + 2)
                        if res.returncode == 0:
                            self._send_json({
                                'status': 'success',
                                'action': 'send-message',
                                'output': res.stdout.strip()
                            })
                        else:
                            self._send_json({
                                'status': 'error',
                                'error': res.stderr.strip()
                            }, status=500)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({
                            'status': 'error',
                            'error': f'Agent Message Timed Out or Failed: {str(e)}'
                        }, status=504)

                elif path == '/acp/v1/metadata':
                    conversation_id = payload.get('conversation_id', '')
                    if not conversation_id:
                        return self._send_json({'error': 'Parameter "conversation_id" is required'}, status=400)

                    if not ls_address:
                        return self._send_json({'error': 'Cannot fetch metadata: Antigravity Language Server is offline'}, status=503)

                    cmd = [AGY_BIN, 'agentapi', 'get-conversation-metadata', conversation_id]
                    future = None
                    try:
                        future = agent_executor.submit(run_agent_command, cmd, 15)
                        res = future.result(timeout=18)
                        if res.returncode == 0:
                            self._send_json({
                                'status': 'success',
                                'action': 'get-metadata',
                                'output': res.stdout.strip()
                            })
                        else:
                            self._send_json({
                                'status': 'error',
                                'error': res.stderr.strip()
                            }, status=500)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({'status': 'error', 'error': f'Metadata fetch failed: {str(e)}'}, status=504)
                else:
                    self._send_json({'error': f'Unknown path: {path}'}, status=404)
            finally:
                agent_semaphore.release()
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
    
    # Register SIGTERM and SIGINT signal handlers
    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)
    
    print(f"[*] Custom Antigravity REST Bridge Server listening on http://127.0.0.1:{PORT}")
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
