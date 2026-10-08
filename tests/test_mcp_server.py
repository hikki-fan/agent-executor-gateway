"""Unit tests for the local stdio MCP Gateway adapter."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mcp_server import (
    GatewayClient,
    GatewayError,
    dispatch,
    handle_tool_call,
    run_stdio,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def health(self):
        return {"status": "online"}

    def executor_health(self, executor: str):
        return {"executor": executor, "available": True}

    def invoke(self, executor: str, arguments):
        self.calls.append((executor, dict(arguments)))
        return {"status": "success", "executor": executor, "session_id": "sid-1"}


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class McpServerTests(unittest.TestCase):
    def test_initialize_negotiates_supported_protocol(self):
        response = dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            FakeClient(),
        )
        assert response is not None
        self.assertEqual(response["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list_exposes_gateway_and_fixed_executors(self):
        response = dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, FakeClient())
        assert response is not None
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {"gateway_health", "gateway_executor_health", "agy_invoke", "grok_invoke", "kimi_invoke"})

    def test_grok_tool_routes_only_to_grok(self):
        client = FakeClient()
        result = handle_tool_call("grok_invoke", {"prompt": "review this"}, client)
        self.assertFalse(result["isError"])
        self.assertEqual(client.calls, [("grok", {"prompt": "review this"})])

    def test_kimi_tool_routes_only_to_kimi(self):
        client = FakeClient()
        result = handle_tool_call("kimi_invoke", {"prompt": "implement this frontend"}, client)
        self.assertFalse(result["isError"])
        self.assertEqual(client.calls, [("kimi", {"prompt": "implement this frontend"})])

    def test_invoke_validation_rejects_unknown_and_invalid_fields(self):
        client = FakeClient()
        unknown = handle_tool_call("agy_invoke", {"prompt": "x", "token": "secret"}, client)
        self.assertTrue(unknown["isError"])
        bad_timeout = handle_tool_call("agy_invoke", {"prompt": "x", "timeout_sec": True}, client)
        self.assertTrue(bad_timeout["isError"])
        self.assertEqual(client.calls, [])

    def test_notifications_do_not_produce_responses(self):
        self.assertIsNone(dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, FakeClient()))

    def test_stdio_transport_is_newline_delimited(self):
        incoming = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )
        outgoing = io.StringIO()
        run_stdio(incoming, outgoing, FakeClient())
        lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([line["id"] for line in lines], [1, 2])

    def test_gateway_client_adds_token_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("test-secret-token\n", encoding="utf-8")
            os.chmod(token_file, stat.S_IRUSR | stat.S_IWUSR)
            seen = {}

            def opener(request, timeout):
                seen["authorization"] = request.headers.get("Authorization")
                seen["timeout"] = timeout
                return FakeResponse({"status": "online"})

            client = GatewayClient(
                base_url="http://127.0.0.1:8765",
                token_file=str(token_file),
                opener=opener,
            )
            self.assertEqual(client._request("POST", "/v1/executors/grok/invoke", {"prompt": "x"}), {"status": "online"})
            self.assertEqual(seen["authorization"], "Bearer test-secret-token")
            self.assertNotIn("test-secret-token", json.dumps({"status": "online"}))

    def test_gateway_client_rejects_broad_token_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("secret", encoding="utf-8")
            os.chmod(token_file, 0o644)
            client = GatewayClient(token_file=str(token_file), opener=lambda *args, **kwargs: None)
            with self.assertRaises(GatewayError):
                client._request("POST", "/v1/executors/grok/invoke", {"prompt": "x"})


if __name__ == "__main__":
    unittest.main()
