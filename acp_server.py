#!/usr/bin/env python3
"""
Custom Antigravity REST Bridge Server for Codex Integration (v2.4.0)
Phase 1 Refactored Architecture:
- Core primitives extracted to neutral core modules (auth, config, concurrency, session_lock, process, timeout, result)
- Provider specifics extracted to AntigravityAdapter (command construction, parsing, retry, classification)
- Preserves full backward-compatible HTTP surface on :8765 (/health, /acp/v1/*)
- Retains internal compatibility patch point run_agent_command for mock testing
- 1:1 Codex Session to Antigravity Conversation Mapping
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import socketserver
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from adapters.antigravity import AntigravityAdapter, AntigravityConfig
from adapters.grok import GrokAdapter, GrokConfig
from api.executors import ExecutorRegistry, validate_invoke_request
from core.auth import load_or_create_token, verify_bearer_token
from core.concurrency import AdmissionController
from core.config import GatewayConfig
from core.process import run_process_group
from core.result import ExecutorResult, normalize_usage
from core.session_lock import SessionLockManager

# Instantiate transport-level gateway configuration and provider configurations
gateway_config = GatewayConfig.from_env()
agy_config = AntigravityConfig.from_env()
grok_config = GrokConfig.from_env()

PORT = gateway_config.port
TOKEN_FILE = gateway_config.token_file
MAX_CONTENT_LENGTH = gateway_config.max_content_length
MAX_HTTP_CONNECTIONS = gateway_config.max_http_connections
MAX_POST_CONNECTIONS = gateway_config.max_post_connections
SOCKET_TIMEOUT = gateway_config.socket_timeout_sec

AGY_BIN = agy_config.bin_path
AGY_MAX_CONCURRENCY = agy_config.max_concurrency
GROK_MAX_CONCURRENCY = grok_config.max_concurrency
GATEWAY_MAX_CONCURRENCY = gateway_config.max_gateway_concurrency
SUBPROCESS_TIMEOUT = agy_config.subprocess_timeout_sec
AUTH_GRACE_SEC = agy_config.auth_grace_sec
TOTAL_PROCESS_TIMEOUT = agy_config.total_process_timeout_sec

# Initialize token authentication
ACP_AUTH_TOKEN = load_or_create_token(TOKEN_FILE)
EXPECTED_BEARER_HEADER = f"Bearer {ACP_AUTH_TOKEN}"

# Admission controller for connection and multi-executor execution limits (Phase 5)
admission_controller = AdmissionController(
    max_http_connections=MAX_HTTP_CONNECTIONS,
    max_post_connections=MAX_POST_CONNECTIONS,
    max_worker_concurrency=GATEWAY_MAX_CONCURRENCY,
    executor_limits={
        "agy": AGY_MAX_CONCURRENCY,
        "grok": GROK_MAX_CONCURRENCY,
    },
)

# Exported semaphores for compatibility with tests & legacy inspection
gateway_semaphore = admission_controller.gateway_semaphore
agent_semaphore = gateway_semaphore
agy_semaphore = admission_controller._executor_semaphores["agy"]
grok_semaphore = admission_controller._executor_semaphores["grok"]
http_connection_semaphore = admission_controller.http_semaphore
post_connection_semaphore = admission_controller.post_semaphore

# Core session lock manager
session_lock_manager = SessionLockManager()


class ConversationLockCompatibility:
    """Compatibility adapter bridging legacy ConversationLockManager to SessionLockManager."""

    def __init__(self, manager: SessionLockManager | None = None, executor: str = "agy") -> None:
        self._manager = manager if manager is not None else SessionLockManager()
        self._executor = executor

    def acquire(self, conversation_id: str | None) -> bool:
        return self._manager.acquire(self._executor, conversation_id)

    def release(self, conversation_id: str | None) -> None:
        self._manager.release(self._executor, conversation_id)

    def is_locked(self, conversation_id: str | None) -> bool:
        return self._manager.is_locked(self._executor, conversation_id)


conv_lock_mgr = ConversationLockCompatibility(session_lock_manager, "agy")
ConversationLockManager = ConversationLockCompatibility


# Preservation of the Phase 0 internal compatibility patch point
def run_agent_command(
    cmd: list[str],
    timeout_sec: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """
    Execute process group command in isolated session with stdin=DEVNULL.
    Preserved as the primary mock patch point for legacy and compatibility tests.
    """
    return run_process_group(cmd, timeout_sec, env=env, cwd=cwd)


def _adapter_runner_dispatch(
    cmd: list[str],
    timeout_sec: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Deterministic dispatch to module-level run_agent_command without catching exceptions."""
    if cwd is not None:
        return run_agent_command(cmd, timeout_sec, env=env, cwd=cwd)
    if env is not None:
        return run_agent_command(cmd, timeout_sec, env=env)
    return run_agent_command(cmd, timeout_sec)


# Instantiate the AntigravityAdapter with injected runner and resolved provider config
agy_adapter = AntigravityAdapter(
    runner=_adapter_runner_dispatch,
    config=agy_config,
)

# Instantiate the GrokAdapter with injected runner and resolved provider config
grok_adapter = GrokAdapter(
    runner=_adapter_runner_dispatch,
    config=grok_config,
)

# Generic Executor Registry managing registered adapters
executor_registry = ExecutorRegistry()
executor_registry.register(agy_adapter)
executor_registry.register(grok_adapter)


# Thin compatibility wrappers delegating to AntigravityAdapter
def build_agy_command(
    prompt: str,
    conversation_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    cwd: str | None = None,
) -> list[str]:
    return agy_adapter.build_command(
        prompt=prompt,
        conversation_id=conversation_id,
        model=model,
        effort=effort,
        cwd=cwd,
    )


def execute_with_retry(
    cmd: list[str],
    total_timeout_sec: float,
    max_retries: int = 3,
    cwd: str | None = None,
) -> tuple[subprocess.CompletedProcess, dict[str, Any] | None]:
    return agy_adapter.execute_with_retry(
        cmd=cmd,
        total_timeout_sec=total_timeout_sec,
        max_retries=max_retries,
        cwd=cwd,
    )


def is_retryable_pre_execution_error(
    proc_result: subprocess.CompletedProcess | None,
    parsed_json: Any = None,
    cmd: list[str] | None = None,
) -> bool:
    return agy_adapter.is_retryable_pre_execution_error(
        proc_result=proc_result,
        parsed_json=parsed_json,
        cmd=cmd,
    )


def has_cli_response(parsed_json: Any) -> bool:
    return agy_adapter.has_cli_response(parsed_json)


def is_partial_success_result(parsed_json: Any) -> bool:
    return agy_adapter.is_partial_success_result(parsed_json)


def cli_error_detail(
    proc_result: subprocess.CompletedProcess | None,
    parsed_json: Any,
    stderr_text: str,
) -> str:
    return agy_adapter.cli_error_detail(proc_result, parsed_json, stderr_text)


# Global ThreadPoolExecutor for agent subprocesses
agent_executor = ThreadPoolExecutor(
    max_workers=max(GATEWAY_MAX_CONCURRENCY * 2, 8),
    thread_name_prefix="AgentExecutorWorker",
)


class ACPRequestHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _verify_strict_bearer_auth(self) -> bool:
        """Strictly enforce Authorization: Bearer <TOKEN> header."""
        auth_header = self.headers.get("Authorization", "").strip()
        return verify_bearer_token(auth_header, ACP_AUTH_TOKEN)

    def do_OPTIONS(self) -> None:
        self._send_json({"status": "ok"})

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        parts = [p for p in path.split("/") if p]

        if path in ("/health", "/acp/v1/status"):
            self._send_json({
                "status": "online",
                "service": "Antigravity REST Bridge Server",
                "version": "2.4.0",
                "auth_type": "Strict Bearer Token",
                "mode": "explicit_conversation_cli",
                "language_server": {
                    "status": "disabled",
                    "address": None,
                    "mode": "explicit_conversation_cli",
                },
                "limits": {
                    "max_payload_bytes": MAX_CONTENT_LENGTH,
                    "subprocess_timeout_sec": SUBPROCESS_TIMEOUT,
                    "auth_grace_sec": AUTH_GRACE_SEC,
                    "total_process_timeout_sec": TOTAL_PROCESS_TIMEOUT,
                    "max_worker_threads": AGY_MAX_CONCURRENCY,
                    "gateway_max_concurrency": GATEWAY_MAX_CONCURRENCY,
                    "agy_max_concurrency": AGY_MAX_CONCURRENCY,
                    "grok_max_concurrency": GROK_MAX_CONCURRENCY,
                    "max_http_connections": MAX_HTTP_CONNECTIONS,
                    "max_post_connections": MAX_POST_CONNECTIONS,
                    "reserved_health_slots": MAX_HTTP_CONNECTIONS - MAX_POST_CONNECTIONS,
                    "socket_timeout_sec": SOCKET_TIMEOUT,
                    "admission_control": f"HTTP 429 Unified Semaphore (gateway={GATEWAY_MAX_CONCURRENCY}, agy={AGY_MAX_CONCURRENCY}, grok={GROK_MAX_CONCURRENCY})",
                },
            })
        elif path == "/v1/executors":
            self._send_json({"executors": executor_registry.list_executors()})
        elif len(parts) == 4 and parts[0] == "v1" and parts[1] == "executors" and parts[3] == "health":
            executor_name = parts[2]
            adapter = executor_registry.get(executor_name)
            if adapter is None:
                self._send_json({"error": f"Executor '{executor_name}' not found"}, status=404)
            else:
                self._send_json(adapter.health(), status=200)
        else:
            self._send_json({"error": "Endpoint not found"}, status=404)

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        parts = [p for p in path.split("/") if p]

        # Enforce POST Connection Capacity Limit (45 max)
        acquired_post = post_connection_semaphore.acquire(blocking=False)
        if not acquired_post:
            return self._send_json({
                "error": "Service Busy: Maximum POST API connection capacity reached",
                "status_code": 503,
            }, status=503)

        try:
            # 1. Strict Bearer Token Verification
            if not self._verify_strict_bearer_auth():
                return self._send_json({
                    "error": "Unauthorized: Strict Bearer token required",
                    "required_header": "Authorization: Bearer <TOKEN>",
                }, status=401)

            # 2. Payload Size Limit Check
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_CONTENT_LENGTH:
                return self._send_json({
                    "error": f"Payload Too Large: Exceeds limit of {MAX_CONTENT_LENGTH} bytes",
                }, status=413)

            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"

            try:
                payload = json.loads(body)
            except Exception as e:
                return self._send_json({"error": f"Invalid JSON payload: {str(e)}"}, status=400)

            # 3. Route Dispatch
            # Generic Executor API: POST /v1/executors/{name}/invoke
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "executors" and parts[3] == "invoke":
                executor_name = parts[2]
                adapter = executor_registry.get(executor_name)
                if adapter is None:
                    return self._send_json({"error": f"Executor '{executor_name}' not found"}, status=404)

                valid_params, err_msg = validate_invoke_request(payload)
                if err_msg is not None:
                    return self._send_json({"error": err_msg}, status=400)

                prompt = valid_params["prompt"]
                cwd = valid_params["cwd"]
                session_id = valid_params["session_id"]
                model = valid_params["model"]
                effort = valid_params["effort"]
                timeout_sec = valid_params["timeout_sec"]

                # 4. Check per-session concurrency lock (HTTP 409)
                if session_id and not session_lock_manager.acquire(executor_name, session_id):
                    err_res = ExecutorResult(
                        status="error",
                        executor=executor_name,
                        session_id=session_id,
                        error=f"Conflict: Session {session_id} is currently executing another turn",
                    )
                    return self._send_json(err_res.to_dict(), status=409)

                # 5. Check global concurrency semaphore (HTTP 429)
                acquired_permits, saturated_scope = admission_controller.acquire_execution_permits(
                    executor_name, blocking=False
                )
                if not acquired_permits:
                    if session_id:
                        session_lock_manager.release(executor_name, session_id)
                    if saturated_scope == "gateway":
                        err_msg = f"Too Many Requests: Maximum {GATEWAY_MAX_CONCURRENCY} concurrent gateway tasks active"
                    else:
                        limit = admission_controller.get_executor_limit(executor_name) or 1
                        err_msg = f"Too Many Requests: Maximum {limit} concurrent {executor_name} tasks active"
                    err_res = ExecutorResult(
                        status="error",
                        executor=executor_name,
                        session_id=session_id,
                        error=err_msg,
                    )
                    return self._send_json(err_res.to_dict(), status=429)

                try:
                    start_time = time.monotonic()
                    transport_margin = 5.0
                    adapter_default_timeout = getattr(adapter, "total_process_timeout", getattr(adapter, "default_timeout_sec", 600))
                    if timeout_sec is not None:
                        invoke_timeout = int(timeout_sec) if isinstance(timeout_sec, int) else float(timeout_sec)
                        future_timeout = float(timeout_sec) + transport_margin
                    else:
                        invoke_timeout = None
                        future_timeout = float(adapter_default_timeout) + transport_margin

                    future = None
                    try:
                        future = agent_executor.submit(
                            adapter.invoke,
                            prompt=prompt,
                            cwd=cwd,
                            session_id=session_id,
                            model=model,
                            effort=effort,
                            timeout_sec=invoke_timeout,
                        )
                        result = future.result(timeout=future_timeout)

                        if result.status in ("success", "partial_success"):
                            self._send_json(result.to_dict(), status=200)
                        else:
                            self._send_json(result.to_dict(), status=500)
                    except (subprocess.TimeoutExpired, TimeoutError):
                        if future:
                            future.cancel()
                        duration_ms = int((time.monotonic() - start_time) * 1000)
                        effective_t = timeout_sec if timeout_sec is not None else adapter_default_timeout
                        err_res = ExecutorResult(
                            status="error",
                            executor=executor_name,
                            session_id=session_id,
                            response=None,
                            exit_code=None,
                            timing={"duration_ms": duration_ms},
                            usage=normalize_usage(None),
                            warnings=[],
                            error=f"Executor '{executor_name}' task execution timed out after {effective_t}s",
                            raw={},
                        )
                        self._send_json(err_res.to_dict(), status=504)
                    except Exception as e:
                        if future:
                            future.cancel()
                        duration_ms = int((time.monotonic() - start_time) * 1000)
                        err_res = ExecutorResult(
                            status="error",
                            executor=executor_name,
                            session_id=session_id,
                            response=None,
                            exit_code=None,
                            timing={"duration_ms": duration_ms},
                            usage=normalize_usage(None),
                            warnings=[],
                            error=f"Internal executor failure: {str(e)}",
                            raw={},
                        )
                        self._send_json(err_res.to_dict(), status=500)
                finally:
                    admission_controller.release_execution_permits(executor_name)
                    if session_id:
                        session_lock_manager.release(executor_name, session_id)

            elif path in ("/acp/v1/invoke", "/acp/v1/new-conversation"):
                prompt = payload.get("prompt", "")
                model = payload.get("model")
                effort = payload.get("effort")
                conversation_id = payload.get("conversation_id") or payload.get("recipient_id") or ""
                conversation_id = str(conversation_id).strip()

                if not prompt:
                    return self._send_json({"error": 'Parameter "prompt" is required'}, status=400)

                # 4. Check per-conversation concurrency lock (HTTP 409)
                if conversation_id and not conv_lock_mgr.acquire(conversation_id):
                    return self._send_json({
                        "status": "error",
                        "error": f"Conflict: Conversation {conversation_id} is currently executing another turn",
                        "status_code": 409,
                    }, status=409)

                # 5. Check global concurrency semaphore (HTTP 429)
                acquired_permits, saturated_scope = admission_controller.acquire_execution_permits(
                    "agy", blocking=False
                )
                if not acquired_permits:
                    if conversation_id:
                        conv_lock_mgr.release(conversation_id)
                    if saturated_scope == "gateway":
                        err_msg = f"Too Many Requests: Maximum {GATEWAY_MAX_CONCURRENCY} concurrent gateway tasks active"
                    else:
                        err_msg = f"Too Many Requests: Maximum {AGY_MAX_CONCURRENCY} concurrent agent tasks active"
                    return self._send_json({
                        "status": "error",
                        "error": err_msg,
                        "status_code": 429,
                    }, status=429)

                try:
                    action_name = "invoke" if conversation_id else "new-conversation"
                    future = None
                    try:
                        future = agent_executor.submit(
                            agy_adapter.invoke,
                            prompt=prompt,
                            session_id=conversation_id if conversation_id else None,
                            model=model,
                            effort=effort,
                        )
                        result = future.result(timeout=TOTAL_PROCESS_TIMEOUT + 5)

                        raw_stdout = result.raw.get("stdout", "")
                        raw_parsed = result.raw.get("parsed")

                        if result.status == "success":
                            self._send_json({
                                "status": "success",
                                "action": action_name,
                                "conversation_id": result.session_id,
                                "mode": "explicit_conversation_cli",
                                "output": raw_stdout,
                                "parsed": raw_parsed,
                            })
                        elif result.status == "partial_success":
                            self._send_json({
                                "status": "partial_success",
                                "action": action_name,
                                "conversation_id": result.session_id,
                                "mode": "explicit_conversation_cli",
                                "warning": "agy reported ERROR after producing a non-empty response; review the response before relying on it",
                                "upstream_status": result.raw.get("upstream_status", "ERROR"),
                                "upstream_error": result.raw.get("upstream_error", result.error),
                                "cli_exit_code": result.raw.get("cli_exit_code", result.exit_code),
                                "output": raw_stdout,
                                "parsed": raw_parsed,
                            })
                        else:
                            self._send_json({
                                "status": "error",
                                "action": action_name,
                                "conversation_id": conversation_id if conversation_id else None,
                                "mode": "explicit_conversation_cli",
                                "error": result.error,
                                "output": raw_stdout,
                                "parsed": raw_parsed,
                            }, status=500)
                    except subprocess.TimeoutExpired:
                        if future:
                            future.cancel()
                        self._send_json({
                            "status": "error",
                            "error": f"Agent Execution Timed Out after {SUBPROCESS_TIMEOUT}s task budget plus {AUTH_GRACE_SEC}s auth grace",
                            "conversation_id": conversation_id if conversation_id else None,
                            "status_code": 504,
                        }, status=504)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({
                            "status": "error",
                            "error": f"Agent Execution Failed: {str(e)}",
                            "conversation_id": conversation_id if conversation_id else None,
                        }, status=504)
                finally:
                    admission_controller.release_execution_permits("agy")
                    if conversation_id:
                        conv_lock_mgr.release(conversation_id)

            elif path == "/acp/v1/send-message":
                recipient_id = payload.get("recipient_id") or payload.get("conversation_id") or ""
                recipient_id = str(recipient_id).strip()
                content = payload.get("content") or payload.get("prompt") or ""
                model = payload.get("model")
                effort = payload.get("effort")

                if not recipient_id or not content:
                    return self._send_json({
                        "error": 'Parameters "recipient_id" and "content" are required',
                    }, status=400)

                # Check per-conversation concurrency lock (HTTP 409)
                if not conv_lock_mgr.acquire(recipient_id):
                    return self._send_json({
                        "status": "error",
                        "error": f"Conflict: Conversation {recipient_id} is currently executing another turn",
                        "status_code": 409,
                    }, status=409)

                # Check global concurrency semaphore (HTTP 429)
                acquired_permits, saturated_scope = admission_controller.acquire_execution_permits(
                    "agy", blocking=False
                )
                if not acquired_permits:
                    conv_lock_mgr.release(recipient_id)
                    if saturated_scope == "gateway":
                        err_msg = f"Too Many Requests: Maximum {GATEWAY_MAX_CONCURRENCY} concurrent gateway tasks active"
                    else:
                        err_msg = f"Too Many Requests: Maximum {AGY_MAX_CONCURRENCY} concurrent agent tasks active"
                    return self._send_json({
                        "status": "error",
                        "error": err_msg,
                        "status_code": 429,
                    }, status=429)

                try:
                    future = None
                    try:
                        future = agent_executor.submit(
                            agy_adapter.invoke,
                            prompt=content,
                            session_id=recipient_id,
                            model=model,
                            effort=effort,
                        )
                        result = future.result(timeout=TOTAL_PROCESS_TIMEOUT + 5)

                        raw_stdout = result.raw.get("stdout", "")
                        raw_parsed = result.raw.get("parsed")

                        if result.status == "success":
                            self._send_json({
                                "status": "success",
                                "action": "send-message",
                                "conversation_id": recipient_id,
                                "mode": "explicit_conversation_cli",
                                "output": raw_stdout,
                                "parsed": raw_parsed,
                            })
                        elif result.status == "partial_success":
                            self._send_json({
                                "status": "partial_success",
                                "action": "send-message",
                                "conversation_id": recipient_id,
                                "mode": "explicit_conversation_cli",
                                "warning": "agy reported ERROR after producing a non-empty response; review the response before relying on it",
                                "upstream_status": result.raw.get("upstream_status", "ERROR"),
                                "upstream_error": result.raw.get("upstream_error", result.error),
                                "cli_exit_code": result.raw.get("cli_exit_code", result.exit_code),
                                "output": raw_stdout,
                                "parsed": raw_parsed,
                            })
                        else:
                            self._send_json({
                                "status": "error",
                                "action": "send-message",
                                "conversation_id": recipient_id,
                                "mode": "explicit_conversation_cli",
                                "error": result.error,
                                "output": raw_stdout,
                                "parsed": raw_parsed,
                            }, status=500)
                    except subprocess.TimeoutExpired:
                        if future:
                            future.cancel()
                        self._send_json({
                            "status": "error",
                            "error": f"Agent Message Timed Out after {SUBPROCESS_TIMEOUT}s task budget plus {AUTH_GRACE_SEC}s auth grace",
                            "conversation_id": recipient_id,
                            "status_code": 504,
                        }, status=504)
                    except Exception as e:
                        if future:
                            future.cancel()
                        self._send_json({
                            "status": "error",
                            "error": f"Agent Message Failed: {str(e)}",
                            "conversation_id": recipient_id,
                        }, status=504)
                finally:
                    admission_controller.release_execution_permits("agy")
                    conv_lock_mgr.release(recipient_id)

            elif path == "/acp/v1/metadata":
                self._send_json({
                    "status": "error",
                    "error": "Metadata endpoint is not supported in explicit conversation CLI mode without Language Server",
                    "status_code": 501,
                }, status=501)

            else:
                self._send_json({"error": f"Unknown path: {path}"}, status=404)
        finally:
            post_connection_semaphore.release()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """
    Threaded HTTP Server with Socket Read/Write Timeouts (10s),
    Exception-Safe Connection Semaphore (50 max), and Reserved Connection Capacity.
    """
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request: Any, client_address: Any) -> None:
        """Enforce maximum total HTTP connection limit of 50 sockets with exception safety."""
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

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            http_connection_semaphore.release()


server_instance: ThreadedHTTPServer | None = None


def sigterm_handler(signum: int, frame: Any) -> None:
    """
    Non-deadlocking Signal Handler:
    BaseServer.shutdown() MUST be called from a separate thread while serve_forever() runs.
    """
    print(f"\n[*] Received signal {signum}, triggering async server shutdown...")
    if server_instance:
        threading.Thread(target=server_instance.shutdown, daemon=True).start()


def run_server() -> None:
    global server_instance
    server_instance = ThreadedHTTPServer(("127.0.0.1", PORT), ACPRequestHandler)

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    print(f"[*] Custom Antigravity REST Bridge Server listening on http://127.0.0.1:{PORT}")
    print(
        f"[*] Mode=explicit_conversation_cli, AGY_MAX_CONCURRENCY={AGY_MAX_CONCURRENCY}, "
        f"TaskBudget={SUBPROCESS_TIMEOUT}s, AuthGrace={AUTH_GRACE_SEC}s"
    )
    print(f"[*] Max HTTP Connections={MAX_HTTP_CONNECTIONS}, Socket Timeout={SOCKET_TIMEOUT}s")
    print(f"[*] Token auth: Strict Bearer from {TOKEN_FILE}")

    try:
        server_instance.serve_forever()
    finally:
        print("[*] Exited serve_forever(). Cancelling unstarted futures & shutting down ThreadPool...")
        server_instance.server_close()
        agent_executor.shutdown(wait=True, cancel_futures=True)
        print("[*] Graceful shutdown completed cleanly.")


if __name__ == "__main__":
    run_server()
