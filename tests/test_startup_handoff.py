import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path


class TestStartupHandoffInstaller(unittest.TestCase):
    """The installer must be safe by default and transactional when authorized."""

    def setUp(self):
        self.repo = Path(__file__).resolve().parents[1]
        self.script = self.repo / "scripts" / "install_startup_handoff.py"
        self.temp_dir = Path(tempfile.mkdtemp(prefix="startup_handoff_"))
        self.entrypoint = self.temp_dir / "start-codex-container"
        self.profile = self.temp_dir / "bashrc"
        self.backup_root = self.temp_dir / "backups"
        self.token = self.temp_dir / "acp_token"
        self.runtime = self.temp_dir / "runtime"
        self.token.write_text("test-token\n", encoding="utf-8")
        os.chmod(self.token, 0o600)
        self.entrypoint.write_text(
            """#!/usr/bin/env bash
set -e

# Keep existing startup work.

# Restore the ACP CLI after a container rebuild. Prefer the canonical
# persistent install, and fall back to the checked-out bridge repository.
if [ -x /workspace/scripts/acp-cli ]; then
  ln -sf /workspace/scripts/acp-cli /usr/local/bin/acp-cli
elif [ -x /workspace/antigravity-rest-bridge/acp-cli ]; then
  ln -sf /workspace/antigravity-rest-bridge/acp-cli /usr/local/bin/acp-cli
fi

# Start the health watchdog when the bridge has been installed in the
# persistent workspace. Its singleton lock makes repeated starts harmless.
if [ -f /workspace/scripts/acp_watchdog.sh ]; then
  setsid bash /workspace/scripts/acp_watchdog.sh </dev/null >/dev/null 2>&1 &
fi

tmux has-session -t codex 2>/dev/null ||
  tmux new-session -d -s codex 'cd /workspace && exec bash'

exec tail -f /dev/null
""",
            encoding="utf-8",
        )
        self.profile.write_text(
            "alias agy='agy --dangerously-skip-permissions'\n"
            "/workspace/scripts/ensure_acp_bridge.sh >/dev/null 2>&1\n"
            "export PATH=\"$HOME/.grok/bin:$PATH\"\n",
            encoding="utf-8",
        )
        os.chmod(self.entrypoint, 0o755)
        os.chmod(self.profile, 0o666)
        self.original_entrypoint = self.entrypoint.read_bytes()
        self.original_profile = self.profile.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_installer(self, *args, confirm=False):
        env = os.environ.copy()
        if confirm:
            env["CONFIRM_STARTUP_HANDOFF"] = "1"
        command = [
            "python3",
            str(self.script),
            "--entrypoint",
            str(self.entrypoint),
            "--profile",
            str(self.profile),
            "--token-file",
            str(self.token),
            "--runtime-dir",
            str(self.runtime),
            "--backup-root",
            str(self.backup_root),
            *args,
        ]
        return subprocess.run(command, env=env, capture_output=True, text=True)

    def test_check_is_read_only(self):
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Startup handoff inspection: PASS", result.stdout)
        self.assertIn("Dry run only", result.stdout)
        self.assertEqual(self.entrypoint.read_bytes(), self.original_entrypoint)
        self.assertEqual(self.profile.read_bytes(), self.original_profile)
        self.assertEqual(stat.S_IMODE(self.entrypoint.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o666)
        self.assertFalse(self.backup_root.exists())

    def test_apply_requires_both_confirmations(self):
        result = self.run_installer("--apply", "--confirm-startup-handoff")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONFIRM_STARTUP_HANDOFF=1", result.stderr)
        self.assertEqual(self.entrypoint.read_bytes(), self.original_entrypoint)
        self.assertEqual(self.profile.read_bytes(), self.original_profile)

    def test_apply_rewrites_files_and_keeps_private_backup(self):
        result = self.run_installer("--apply", "--confirm-startup-handoff", confirm=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        entrypoint = self.entrypoint.read_text(encoding="utf-8")
        profile = self.profile.read_text(encoding="utf-8")
        for text in (entrypoint, profile):
            self.assertIn("AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF", text)
            self.assertIn("gateway_watchdog.sh", text)
            self.assertNotIn("acp_watchdog.sh", text)
            self.assertNotIn("ensure_acp_bridge.sh", text)
        self.assertIn("/usr/local/bin/acp-cli", entrypoint)
        self.assertNotIn("/workspace/scripts/acp-cli", entrypoint)
        self.assertEqual(stat.S_IMODE(self.entrypoint.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o644)

        backup_dirs = [path for path in self.backup_root.iterdir() if path.is_dir()]
        self.assertEqual(len(backup_dirs), 1)
        backup_dir = backup_dirs[0]
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual((backup_dir / self.entrypoint.name).read_bytes(), self.original_entrypoint)
        self.assertEqual((backup_dir / self.profile.name).read_bytes(), self.original_profile)
        self.assertEqual(stat.S_IMODE((backup_dir / "manifest.json").stat().st_mode), 0o600)

    def test_check_is_idempotent_after_apply(self):
        first = self.run_installer("--apply", "--confirm-startup-handoff", confirm=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        entrypoint_after = self.entrypoint.read_bytes()
        profile_after = self.profile.read_bytes()
        second = self.run_installer("--check")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already installed", second.stdout)
        self.assertEqual(self.entrypoint.read_bytes(), entrypoint_after)
        self.assertEqual(self.profile.read_bytes(), profile_after)

    def test_unknown_entrypoint_layout_fails_closed(self):
        self.entrypoint.write_text("#!/bin/sh\n# custom startup\n", encoding="utf-8")
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("known legacy ACP handoff", result.stderr)
        self.assertEqual(self.profile.read_bytes(), self.original_profile)

    def test_symlink_target_is_rejected(self):
        real_entrypoint = self.temp_dir / "real-entrypoint"
        real_entrypoint.write_bytes(self.original_entrypoint)
        os.chmod(real_entrypoint, 0o755)
        self.entrypoint.unlink()
        self.entrypoint.symlink_to(real_entrypoint)
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symbolic link", result.stderr)

    def test_symlink_gateway_artifact_is_rejected_before_resolve(self):
        cli_link = self.temp_dir / "gateway-cli-link"
        watchdog_link = self.temp_dir / "gateway-watchdog-link"
        cli_link.symlink_to(self.repo / "acp-cli")
        watchdog_link.symlink_to(self.repo / "scripts" / "gateway_watchdog.sh")

        result_cli = self.run_installer("--check", "--gateway-cli", str(cli_link))
        self.assertEqual(result_cli.returncode, 1)
        self.assertIn("new gateway client is a symbolic link", result_cli.stderr)

        result_watchdog = self.run_installer("--check", "--gateway-watchdog", str(watchdog_link))
        self.assertEqual(result_watchdog.returncode, 1)
        self.assertIn("new gateway watchdog is a symbolic link", result_watchdog.stderr)

    def test_profile_without_legacy_hook_can_be_extended(self):
        self.profile.write_text("export PATH=\"$HOME/bin:$PATH\"\n", encoding="utf-8")
        os.chmod(self.profile, 0o644)
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("changed", result.stdout)
        self.assertNotIn("AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF", self.profile.read_text(encoding="utf-8"))

    def test_second_write_failure_restores_first_file(self):
        spec = importlib.util.spec_from_file_location("startup_handoff", self.script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        original_atomic_write = module.atomic_write

        def fail_profile(path, content, info):
            if Path(path) == self.profile:
                raise OSError("simulated profile replacement failure")
            return original_atomic_write(path, content, info)

        module.atomic_write = fail_profile
        old_argv = sys.argv
        old_confirm = os.environ.get("CONFIRM_STARTUP_HANDOFF")
        try:
            os.environ["CONFIRM_STARTUP_HANDOFF"] = "1"
            sys.argv = [
                str(self.script),
                "--apply",
                "--confirm-startup-handoff",
                "--entrypoint",
                str(self.entrypoint),
                "--profile",
                str(self.profile),
                "--token-file",
                str(self.token),
                "--runtime-dir",
                str(self.runtime),
                "--backup-root",
                str(self.backup_root),
            ]
            with self.assertRaises(OSError):
                module.main()
        finally:
            sys.argv = old_argv
            if old_confirm is None:
                os.environ.pop("CONFIRM_STARTUP_HANDOFF", None)
            else:
                os.environ["CONFIRM_STARTUP_HANDOFF"] = old_confirm

        self.assertEqual(self.entrypoint.read_bytes(), self.original_entrypoint)
        self.assertEqual(self.profile.read_bytes(), self.original_profile)


if __name__ == "__main__":
    unittest.main()
