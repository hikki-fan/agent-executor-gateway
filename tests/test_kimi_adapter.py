"""Focused contract tests for Kimi Code CLI integration."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from adapters.kimi import KimiAdapter, KimiConfig


class KimiAdapterTests(unittest.TestCase):
    def test_command_supports_new_and_resumed_sessions(self):
        adapter = KimiAdapter(config=KimiConfig(bin_path="/opt/kimi", code_home="/tmp/kimi"))
        command = adapter.build_command(prompt="fix the button", model="kimi-code", session_id="session-123")
        self.assertEqual(
            command,
            [
                "/opt/kimi",
                "--session", "session-123",
                "--model", "kimi-code",
                "--prompt", "fix the button",
                "--output-format", "stream-json",
            ],
        )

    def test_stream_parser_returns_final_message_and_resume_hint(self):
        stream = "\n".join(
            (
                json.dumps({"role": "assistant", "content": "Checking files", "tool_calls": [{"id": "tc1"}]}),
                json.dumps({"role": "tool", "content": "private tool output"}),
                json.dumps({"role": "assistant", "content": "Frontend change is complete."}),
                json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": "session-456"}),
            )
        )
        response, session_id, parsed_lines = adapter_parser(stream)
        self.assertEqual(response, "Frontend change is complete.")
        self.assertEqual(session_id, "session-456")
        self.assertEqual(parsed_lines, 4)

    def test_invoke_uses_isolated_runner_and_does_not_return_tool_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured: dict[str, object] = {}
            stdout = "\n".join(
                (
                    json.dumps({"role": "tool", "content": "private repository output"}),
                    json.dumps({"role": "assistant", "content": "Done."}),
                )
            )

            def runner(command, timeout, env=None, cwd=None):
                captured.update(command=command, timeout=timeout, env=env, cwd=cwd)
                return subprocess.CompletedProcess(command, 0, stdout, "")

            config = KimiConfig(
                bin_path="/opt/kimi",
                code_home="/home/codex/.kimi-code",
                default_timeout_sec=90,
            )
            adapter = KimiAdapter(runner=runner, config=config)
            result = adapter.invoke(prompt="update component", cwd=tmp, effort="high")

            self.assertEqual(result.status, "success")
            self.assertEqual(result.response, "Done.")
            self.assertNotIn("private repository output", json.dumps(result.to_dict()))
            self.assertIn("does not expose a reasoning-effort flag", result.warnings[0])
            self.assertEqual(captured["timeout"], 90.0)
            self.assertEqual(captured["cwd"], tmp)
            self.assertEqual(captured["env"]["KIMI_CODE_HOME"], "/home/codex/.kimi-code")

    def test_health_requires_binary_and_auth_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "kimi"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o700)
            home = Path(tmp) / "home"
            credentials = home / "credentials" / "kimi-code.json"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("{}", encoding="utf-8")
            credentials.chmod(0o600)
            adapter = KimiAdapter(config=KimiConfig(bin_path=str(binary), code_home=str(home)))

            self.assertTrue(adapter.health()["available"])
            credentials.unlink()
            self.assertFalse(adapter.health()["available"])


def adapter_parser(stream: str):
    return KimiAdapter._parse_stream(stream)


if __name__ == "__main__":
    unittest.main()
