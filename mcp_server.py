#!/usr/bin/env python3
"""
Local stdio MCP adapter for Agent Executor Gateway.

The Gateway remains the only production execution boundary.  This process is
deliberately a thin MCP transport adapter: it accepts MCP JSON-RPC messages on
stdin, calls the Gateway's authenticated REST API, and writes MCP responses to
stdout.  It never invokes ``agy`` or ``grok`` directly.

The adapter uses only Python's standard library so it can be started by Codex
without installing a second runtime dependency.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from typing import Any, Callable, IO, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


SERVER_NAME = "agent-executor-gateway"
SERVER_VERSION = "0.2.0"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
DEFAULT_TOKEN_FILE = "/home/codex/.codex/acp_token"
DEFAULT_TOOL_TIMEOUT_SEC = 930.0
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

SERVER_INSTRUCTIONS = (
    "Use this MCP server for all production AGY/Grok/Kimi work; do not invoke provider "
    "CLIs directly. Use kimi_invoke for frontend implementation; Codex scopes and "
    "reviews Kimi changes and runs final regression checks. Use agy_invoke for "
    "backend implementation, feature, bugfix, and refactor work. Use grok_invoke "
    "for explicit Grok requests, independent "
    "reviews, and deep debug/investigation. The Gateway enforces authentication, "
    "session locking, timeouts, process cleanup, and bounded concurrency. Never "
    "place tokens, credentials, or private session data in prompts."
)

# The Gateway is intentionally loopback-only.  Do not let inherited HTTP(S)
# proxy variables route an Authorization header or task prompt outside the
# container.
_LOCAL_OPENER = build_opener(ProxyHandler({}))


def _local_urlopen(request: Request, timeout: float):
    return _LOCAL_OPENER.open(request, timeout=timeout)


class GatewayError(RuntimeError):
    """Safe, user-facing error returned by the Gateway adapter."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _safe_text(value: Any, limit: int = 1200) -> str:
    """Convert provider/Gateway diagnostics to bounded, credential-safe text."""
    text = str(value)
    # Never echo an Authorization value or the known local token path into the
    # model-visible MCP response.
    import re

    text = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", text)
    text = text.replace(DEFAULT_TOKEN_FILE, "<token-file>")
    return text[:limit]


def _json_body(raw: bytes) -> Any:
    """Decode a Gateway response without failing the MCP transport."""
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return {"raw": raw.decode("utf-8", errors="replace")[:1200]}


@dataclass
class GatewayClient:
    """Small REST client for the already-running local Gateway."""

    base_url: str = ""
    token_file: str = ""
    opener: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or os.environ.get("AGENT_EXECUTOR_GATEWAY_URL", DEFAULT_GATEWAY_URL)).rstrip("/")
        self.token_file = self.token_file or os.environ.get("ACP_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        self.opener = self.opener or _local_urlopen

    def _read_token(self) -> str:
        """Read the Gateway token without ever logging or returning its path/value."""
        try:
            file_stat = os.lstat(self.token_file)
        except OSError as exc:
            raise GatewayError("Gateway authentication token is unavailable") from exc
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise GatewayError("Gateway authentication token is not a regular file")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise GatewayError("Gateway authentication token permissions are too broad")
        try:
            with open(self.token_file, "r", encoding="utf-8") as token_handle:
                token = token_handle.read().strip()
        except OSError as exc:
            raise GatewayError("Gateway authentication token cannot be read") from exc
        if not token:
            raise GatewayError("Gateway authentication token is empty")
        return token

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        timeout_sec: float = 10.0,
    ) -> Any:
        url = f"{self.base_url}{path}"
        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Authorization"] = f"Bearer {self._read_token()}"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=timeout_sec) as response:
                return _json_body(response.read())
        except HTTPError as exc:
            parsed = _json_body(exc.read())
            detail = None
            if isinstance(parsed, dict):
                detail = parsed.get("error") or parsed.get("message")
            message = _safe_text(detail or f"Gateway HTTP {exc.code}")
            raise GatewayError(message, status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GatewayError("Gateway connection failed or timed out") from exc

    def health(self) -> Any:
        return self._request("GET", "/health", timeout_sec=5.0)

    def executor_health(self, executor: str) -> Any:
        return self._request("GET", f"/v1/executors/{executor}/health", timeout_sec=5.0)

    def invoke(self, executor: str, arguments: Mapping[str, Any]) -> Any:
        payload: dict[str, Any] = {"prompt": arguments["prompt"]}
        for key in ("cwd", "session_id", "model", "effort", "timeout_sec"):
            value = arguments.get(key)
            if value is not None:
                payload[key] = value

        requested_timeout = payload.get("timeout_sec")
        if isinstance(requested_timeout, int) and not isinstance(requested_timeout, bool):
            http_timeout = float(requested_timeout) + 15.0
        else:
            http_timeout = DEFAULT_TOOL_TIMEOUT_SEC
        return self._request(
            "POST",
            f"/v1/executors/{executor}/invoke",
            payload=payload,
            timeout_sec=http_timeout,
        )


def _common_invoke_properties() -> dict[str, Any]:
    return {
        "prompt": {
            "type": "string",
            "description": "Concrete task instruction. Do not include credentials, tokens, or private account data.",
        },
        "cwd": {
            "type": "string",
            "description": "Optional repository working directory.",
        },
        "session_id": {
            "type": "string",
            "description": "Optional session ID returned by a previous call, for a serialized continuation.",
        },
        "model": {
            "type": "string",
            "description": "Optional provider model override.",
        },
        "effort": {
            "type": "string",
            "description": "Optional provider reasoning-effort override.",
        },
        "timeout_sec": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional bounded executor timeout in seconds.",
        },
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "gateway_health",
        "description": "Check production Gateway health without starting an Agent task.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "gateway_executor_health",
        "description": "Check whether a registered AGY, Grok, or Kimi executor is available through the Gateway.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "executor": {"type": "string", "enum": ["agy", "grok", "kimi"]},
            },
            "required": ["executor"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agy_invoke",
        "description": "Invoke AGY through the production Gateway. Use for implementation, feature, bugfix, refactor, and ordinary coding work. Never invoke the agy CLI directly.",
        "inputSchema": {
            "type": "object",
            "properties": _common_invoke_properties(),
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grok_invoke",
        "description": "Invoke Grok Build through the production Gateway. Use for an explicit Grok request, independent review, deep debug/investigation, or an escalation after AGY verification failure. Never invoke the grok CLI directly.",
        "inputSchema": {
            "type": "object",
            "properties": _common_invoke_properties(),
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "kimi_invoke",
        "description": "Invoke Kimi Code through the production Gateway. Use for frontend implementation (pages, components, styling, client-side behavior, and UI tests). Give a concrete scope and acceptance criteria; Codex independently reviews and verifies all changes. Never invoke the kimi CLI directly.",
        "inputSchema": {
            "type": "object",
            "properties": _common_invoke_properties(),
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
]

_TOOL_NAMES = {tool["name"] for tool in TOOLS}
_INVOKE_TO_EXECUTOR = {"agy_invoke": "agy", "grok_invoke": "grok", "kimi_invoke": "kimi"}


def _validate_invoke_arguments(arguments: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(arguments, dict):
        return None, "Tool arguments must be an object"
    allowed = set(_common_invoke_properties())
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        return None, f"Unknown argument(s): {', '.join(unknown)}"

    prompt = arguments.get("prompt")
    if isinstance(prompt, bool) or not isinstance(prompt, str) or not prompt.strip():
        return None, "prompt must be a non-empty string"

    normalized: dict[str, Any] = {"prompt": prompt}
    for key in ("cwd", "session_id", "model", "effort"):
        value = arguments.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, str):
                return None, f"{key} must be a string when provided"
            if key == "session_id" and not value.strip():
                return None, "session_id must be non-empty when provided"
            normalized[key] = value

    timeout_sec = arguments.get("timeout_sec")
    if timeout_sec is not None:
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int) or timeout_sec <= 0:
            return None, "timeout_sec must be a positive integer when provided"
        normalized["timeout_sec"] = timeout_sec
    return normalized, None


def _result_content(value: Any, is_error: bool = False) -> dict[str, Any]:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def handle_tool_call(name: Any, arguments: Any, client: GatewayClient) -> dict[str, Any]:
    """Dispatch one MCP tools/call request to the corresponding Gateway API."""
    if name not in _TOOL_NAMES:
        return _result_content({"error": f"Unknown tool: {name}"}, is_error=True)

    if name == "gateway_health":
        if arguments not in (None, {}):
            return _result_content({"error": "gateway_health takes no arguments"}, is_error=True)
        try:
            return _result_content(client.health())
        except GatewayError as exc:
            return _result_content({"error": exc.message, "status_code": exc.status_code}, is_error=True)

    if name == "gateway_executor_health":
        if not isinstance(arguments, dict) or arguments.get("executor") not in ("agy", "grok", "kimi"):
            return _result_content({"error": "executor must be one of: agy, grok, kimi"}, is_error=True)
        try:
            return _result_content(client.executor_health(arguments["executor"]))
        except GatewayError as exc:
            return _result_content({"error": exc.message, "status_code": exc.status_code}, is_error=True)

    normalized, error = _validate_invoke_arguments(arguments)
    if error or normalized is None:
        return _result_content({"error": error or "Invalid arguments"}, is_error=True)
    executor = _INVOKE_TO_EXECUTOR[name]
    try:
        return _result_content(client.invoke(executor, normalized))
    except GatewayError as exc:
        response: dict[str, Any] = {"error": exc.message}
        if exc.status_code is not None:
            response["status_code"] = exc.status_code
        return _result_content(response, is_error=True)


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def dispatch(message: Any, client: GatewayClient) -> dict[str, Any] | None:
    """Process one JSON-RPC request or notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error_response(None, -32600, "Invalid JSON-RPC request")

    method = message.get("method")
    request_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params")
    if params is not None and not isinstance(params, dict):
        return None if is_notification else _error_response(request_id, -32602, "params must be an object")

    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "ping":
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "initialize":
        requested_version = (params or {}).get("protocolVersion")
        protocol_version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        result = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        }
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "tools/list":
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        call_params = params or {}
        result = handle_tool_call(call_params.get("name"), call_params.get("arguments"), client)
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": result}

    return None if is_notification else _error_response(request_id, -32601, f"Method not found: {method}")


def run_stdio(
    input_stream: IO[str] | None = None,
    output_stream: IO[str] | None = None,
    client: GatewayClient | None = None,
) -> None:
    """Run the newline-delimited MCP stdio transport."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    client = client or GatewayClient()

    for raw_line in input_stream:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
            response = dispatch(message, client)
        except json.JSONDecodeError:
            response = _error_response(None, -32700, "Parse error")
        except Exception:
            # Keep unexpected implementation details out of the model-visible
            # response and preserve the JSON-RPC stream.
            response = _error_response(None, -32603, "Internal MCP adapter error")
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()


if __name__ == "__main__":
    run_stdio()
