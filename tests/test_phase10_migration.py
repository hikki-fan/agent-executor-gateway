# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fcntl
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest


class TestPhase10MigrationSafetyGates(unittest.TestCase):
    """Deterministic verification of Phase 10 safety confirmation gates and preflight inspection."""

    def setUp(self):
        self.script_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "migrate_production.sh")
        )
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.test_run_dir = tempfile.mkdtemp(prefix="phase10_run_")
        self.base_env = os.environ.copy()
        self.base_env["MIGRATION_RUN_DIR"] = self.test_run_dir
        self.base_env["ACP_TOKEN_FILE"] = os.path.join(self.test_run_dir, "production.token")

    def tearDown(self):
        shutil.rmtree(self.test_run_dir, ignore_errors=True)

    def test_01_default_command_is_preflight(self):
        # Running without arguments must execute read-only preflight
        res = subprocess.run([self.script_path], env=self.base_env, capture_output=True, text=True)
        self.assertIn("Phase 10 Migration Preflight", res.stdout)
        self.assertIn("Strictly Read-Only", res.stdout)

    def test_02_preflight_zero_filesystem_mutations(self):
        # Point to a non-existent directory
        non_existent_dir = os.path.join(self.test_run_dir, "must_not_be_created")
        env = self.base_env.copy()
        env["MIGRATION_RUN_DIR"] = non_existent_dir

        self.assertFalse(os.path.exists(non_existent_dir))
        res = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
        self.assertIn("Phase 10 Migration Preflight", res.stdout)

        # Assert directory was NOT created and no lock file was generated
        self.assertFalse(os.path.exists(non_existent_dir), "Preflight must never create run directory")

    def test_02b_preflight_zero_pycache_mutations(self):
        # Take snapshot of all __pycache__ directories in the repository before preflight
        pycache_snapshot = {}
        for root, dirs, files in os.walk(self.repo_root):
            if "__pycache__" in root:
                for f in files:
                    full_p = os.path.join(root, f)
                    pycache_snapshot[full_p] = os.path.getmtime(full_p)

        res = subprocess.run([self.script_path, "preflight"], env=self.base_env, capture_output=True, text=True)
        self.assertIn("Phase 10 Migration Preflight", res.stdout)

        # Verify no new .pyc files were written during preflight
        current_pycache = {}
        for root, dirs, files in os.walk(self.repo_root):
            if "__pycache__" in root:
                for f in files:
                    full_p = os.path.join(root, f)
                    current_pycache[full_p] = os.path.getmtime(full_p)

        self.assertEqual(pycache_snapshot, current_pycache, "Preflight must not write any bytecode or .pyc files")

    def test_02c_preflight_fails_closed_when_process_not_resolved(self):
        # Point OLD_BRIDGE_SERVER_SCRIPT to a non-existent script so find_process_on_port fails
        env = self.base_env.copy()
        env["OLD_BRIDGE_SERVER_SCRIPT"] = "/workspace/non_existent_legacy_script.py"
        res = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("could not be strictly resolved and verified", res.stdout)

    def test_03_cutover_rejected_without_cli_flag(self):
        env = self.base_env.copy()
        env["CONFIRM_PRODUCTION_CUTOVER"] = "1"
        res = subprocess.run([self.script_path, "cutover"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("SAFETY REFUSAL", res.stderr)
        self.assertIn("--confirm-cutover", res.stderr)

    def test_04_cutover_rejected_without_env_var(self):
        env = self.base_env.copy()
        env.pop("CONFIRM_PRODUCTION_CUTOVER", None)
        res = subprocess.run([self.script_path, "cutover", "--confirm-cutover"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("SAFETY REFUSAL", res.stderr)
        self.assertIn("CONFIRM_PRODUCTION_CUTOVER=1", res.stderr)

    def test_05_rollback_rejected_without_cli_flag(self):
        env = self.base_env.copy()
        env["CONFIRM_PRODUCTION_ROLLBACK"] = "1"
        res = subprocess.run([self.script_path, "rollback"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("SAFETY REFUSAL", res.stderr)
        self.assertIn("--confirm-rollback", res.stderr)

    def test_06_rollback_rejected_without_env_var(self):
        env = self.base_env.copy()
        env.pop("CONFIRM_PRODUCTION_ROLLBACK", None)
        res = subprocess.run([self.script_path, "rollback", "--confirm-rollback"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("SAFETY REFUSAL", res.stderr)
        self.assertIn("CONFIRM_PRODUCTION_ROLLBACK=1", res.stderr)

    def test_07_dirty_legacy_repo_fails_preflight(self):
        # Create a mock git repository with uncommitted dirty changes
        mock_repo_dir = tempfile.mkdtemp(prefix="mock_dirty_repo_")
        try:
            subprocess.run(["git", "init"], cwd=mock_repo_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=mock_repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=mock_repo_dir, check=True)
            with open(os.path.join(mock_repo_dir, "file.txt"), "w") as f:
                f.write("committed\n")
            subprocess.run(["git", "add", "file.txt"], cwd=mock_repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=mock_repo_dir, check=True)

            # Make it dirty
            with open(os.path.join(mock_repo_dir, "dirty.txt"), "w") as f:
                f.write("untracked dirty change\n")

            env = self.base_env.copy()
            env["OLD_BRIDGE_DIR"] = mock_repo_dir
            res = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("DIRTY", res.stdout)
        finally:
            shutil.rmtree(mock_repo_dir, ignore_errors=True)

    def test_08_invalid_port_rejected(self):
        env = self.base_env.copy()
        for bad_port in ("-1", "0", "80", "70000", "invalid"):
            env["PROD_PORT"] = bad_port
            res = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("must be an integer between 1024 and 65535", res.stderr)

    def test_09_symlink_lock_or_run_dir_rejected(self):
        env = self.base_env.copy()
        target_lock = os.path.join(self.test_run_dir, "real_lock.txt")
        with open(target_lock, "w") as f:
            f.write("")
        symlink_lock = os.path.join(self.test_run_dir, "production.lock")
        os.symlink(target_lock, symlink_lock)
        env["PROD_LOCK_FILE"] = symlink_lock

        # Run mutating command to trigger lock inspection
        res = subprocess.run([self.script_path, "cutover", "--confirm-cutover"], env={**env, "CONFIRM_PRODUCTION_CUTOVER": "1"}, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("is a symbolic link", res.stderr)

    def test_09b_preflight_inspects_custom_configured_artifact_paths(self):
        # Create a custom directory with a custom token file having insecure mode 0644
        custom_dir = tempfile.mkdtemp(prefix="custom_run_")
        try:
            custom_token = os.path.join(custom_dir, "custom.token")
            with open(custom_token, "w") as f:
                f.write("secret")
            os.chmod(custom_token, 0o644)
            os.chmod(custom_dir, 0o700)

            env = self.base_env.copy()
            env["MIGRATION_RUN_DIR"] = custom_dir
            env["ACP_TOKEN_FILE"] = custom_token
            res = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("Security Warning: Configured runtime file", res.stdout)
            self.assertIn("custom.token", res.stdout)
        finally:
            shutil.rmtree(custom_dir, ignore_errors=True)

    def test_10_flock_concurrency_blocks_simultaneous_migration(self):
        env = self.base_env.copy()
        lock_file = os.path.join(self.test_run_dir, "production.lock")
        os.makedirs(self.test_run_dir, exist_ok=True)

        with open(lock_file, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                res = subprocess.run([self.script_path, "cutover", "--confirm-cutover"], env={**env, "CONFIRM_PRODUCTION_CUTOVER": "1"}, capture_output=True, text=True)
                self.assertEqual(res.returncode, 1)
                self.assertIn("Another production migration operation is currently in progress", res.stderr)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def test_11_status_command_formats_state_record(self):
        env = self.base_env.copy()
        state_file = os.path.join(self.test_run_dir, "migration_state.json")
        os.makedirs(self.test_run_dir, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({
                "state": "CUTOVER_COMPLETE",
                "timestamp": "2026-08-24T12:00:00Z",
                "prod_port": 8765,
                "gateway_pid": "12345",
            }, f, indent=2)

        res = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("CUTOVER_COMPLETE", res.stdout)
        self.assertIn("12345", res.stdout)

    def test_12_watchdog_detection_blocks_preflight_and_cutover_without_override(self):
        mock_watchdog_script = os.path.join(self.test_run_dir, "acp_watchdog.sh")
        with open(mock_watchdog_script, "w") as f:
            f.write("#!/usr/bin/env bash\nwhile true; do sleep 1; done\n")
        os.chmod(mock_watchdog_script, 0o755)

        proc = subprocess.Popen(["bash", mock_watchdog_script])
        try:
            time.sleep(0.2)
            env = self.base_env.copy()
            env["WATCHDOG_SCRIPT"] = mock_watchdog_script
            res_preflight = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
            self.assertEqual(res_preflight.returncode, 1)
            self.assertIn("Active Watchdog detected", res_preflight.stdout)

            res_cutover = subprocess.run([self.script_path, "cutover", "--confirm-cutover"], env={**env, "CONFIRM_PRODUCTION_CUTOVER": "1"}, capture_output=True, text=True)
            self.assertEqual(res_cutover.returncode, 1)
            self.assertIn("SAFETY REFUSAL", res_cutover.stderr)
            self.assertIn("Active Watchdog Supervisor Detected", res_cutover.stderr)
        finally:
            proc.terminate()
            proc.wait()

    def test_12b_foreign_watchdog_same_name_rejected(self):
        foreign_watchdog_dir = tempfile.mkdtemp(prefix="foreign_watchdog_")
        try:
            foreign_script = os.path.join(foreign_watchdog_dir, "acp_watchdog.sh")
            with open(foreign_script, "w") as f:
                f.write("#!/usr/bin/env bash\nwhile true; do sleep 1; done\n")
            os.chmod(foreign_script, 0o755)

            proc = subprocess.Popen(["bash", foreign_script])
            try:
                time.sleep(0.2)
                check_cmd = f"""
source "{self.script_path}"
find_watchdog_process "/workspace/scripts/acp_watchdog.sh"
"""
                res = subprocess.run(["bash", "-c", check_cmd], capture_output=True, text=True)
                self.assertNotIn(str(proc.pid), res.stdout.strip())
            finally:
                proc.terminate()
                proc.wait()
        finally:
            shutil.rmtree(foreign_watchdog_dir, ignore_errors=True)

    def test_12c_bash_c_command_with_watchdog_arg_rejected(self):
        # Launch a bash -c command passing expected watchdog path in a subsequent argv slot
        target_script = "/workspace/scripts/acp_watchdog.sh"
        proc = subprocess.Popen(["bash", "-c", "while true; do sleep 1; done", target_script])
        try:
            time.sleep(0.2)
            check_cmd = f"""
source "{self.script_path}"
is_watchdog_process "{proc.pid}" "{target_script}"
"""
            res = subprocess.run(["bash", "-c", check_cmd], capture_output=True)
            self.assertNotEqual(res.returncode, 0, "bash -c command containing target watchdog path in argv must be strictly rejected (argv[1] is -c)")
        finally:
            proc.terminate()
            proc.wait()

    def test_13_fuzzy_foreign_pid_rejected_by_is_acp_server_process(self):
        foreign_dir = tempfile.mkdtemp(prefix="foreign_server_")
        try:
            foreign_script = os.path.join(foreign_dir, "acp_server.py")
            with open(foreign_script, "w") as f:
                f.write("import time; time.sleep(10)\n")

            proc = subprocess.Popen([sys.executable, foreign_script])
            try:
                time.sleep(0.2)
                check_cmd = f"""
source "{self.script_path}"
is_acp_server_process "{proc.pid}" "{os.path.join(os.path.dirname(self.script_path), '..', 'acp_server.py')}"
"""
                res = subprocess.run(["bash", "-c", check_cmd], capture_output=True)
                self.assertNotEqual(res.returncode, 0, "Foreign acp_server.py script must be strictly rejected")
            finally:
                proc.terminate()
                proc.wait()
        finally:
            shutil.rmtree(foreign_dir, ignore_errors=True)

    def test_13b_python_c_command_with_script_arg_rejected(self):
        # Spawn python3 -c "import time, sys; time.sleep(10)" /workspace/agent-executor-gateway/acp_server.py
        target_script = os.path.abspath(os.path.join(self.repo_root, "acp_server.py"))
        proc = subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(10)", target_script])
        try:
            time.sleep(0.2)
            check_cmd = f"""
source "{self.script_path}"
is_acp_server_process "{proc.pid}" "{target_script}"
"""
            res = subprocess.run(["bash", "-c", check_cmd], capture_output=True)
            self.assertNotEqual(res.returncode, 0, "python -c command containing target script in argv must be strictly rejected (argv[1] is -c)")
        finally:
            proc.terminate()
            proc.wait()

    def test_14_rollback_refusal_when_running_process_not_new_gateway(self):
        dummy_dir = tempfile.mkdtemp(prefix="dummy_proc_")
        try:
            dummy_script = os.path.join(dummy_dir, "other_server.py")
            with open(dummy_script, "w") as f:
                f.write("import time; time.sleep(10)\n")

            proc = subprocess.Popen([sys.executable, dummy_script])
            try:
                time.sleep(0.2)
                pid_file = os.path.join(self.test_run_dir, "production.pid")
                with open(pid_file, "w") as f:
                    f.write(f"{proc.pid}\n")

                env = self.base_env.copy()
                env["CONFIRM_PRODUCTION_ROLLBACK"] = "1"
                res_rollback = subprocess.run([self.script_path, "rollback", "--confirm-rollback"], env=env, capture_output=True, text=True)
                self.assertEqual(res_rollback.returncode, 1)
                self.assertIn("Security Error", res_rollback.stderr)
                self.assertIn("Refusing to terminate unverified process", res_rollback.stderr)

                self.assertTrue(os.path.exists(f"/proc/{proc.pid}"), "Foreign process must not be killed on rollback refusal")
            finally:
                proc.terminate()
                proc.wait()
        finally:
            shutil.rmtree(dummy_dir, ignore_errors=True)

    def test_14b_rollback_refusal_on_occupied_unknown_port(self):
        # Choose a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            test_port = str(s.getsockname()[1])

        # Start an unknown HTTP server on this port
        dummy_dir = tempfile.mkdtemp(prefix="dummy_http_")
        try:
            dummy_script = os.path.join(dummy_dir, "http_server.py")
            with open(dummy_script, "w") as f:
                f.write(f"""
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(('127.0.0.1', {test_port}), H).serve_forever()
""")
            proc = subprocess.Popen([sys.executable, dummy_script])
            try:
                time.sleep(0.3)
                env = self.base_env.copy()
                env["PROD_PORT"] = test_port
                env["CONFIRM_PRODUCTION_ROLLBACK"] = "1"
                res_rollback = subprocess.run([self.script_path, "rollback", "--confirm-rollback"], env=env, capture_output=True, text=True)
                self.assertEqual(res_rollback.returncode, 1)
                self.assertIn("occupied by an unverified/unknown service", res_rollback.stderr)
            finally:
                proc.terminate()
                proc.wait()
        finally:
            shutil.rmtree(dummy_dir, ignore_errors=True)

    def test_16_signal_process_safely_distinguishes_pgid_leader_and_non_leader(self):
        # Test signal_process_safely helper behavior for leader vs non-leader
        test_script = f"""
source "{self.script_path}"
# Non-leader process
sleep 10 &
p_non_leader=$!
pgid_non_leader=$(get_process_pgid "$p_non_leader")
if [ "$pgid_non_leader" -eq "$p_non_leader" ]; then
    echo "ERROR: Child was group leader"
    exit 1
fi
signal_process_safely "$p_non_leader" "TERM"
wait "$p_non_leader" 2>/dev/null || true

# Leader process via setsid
setsid sleep 10 &
p_leader=$!
sleep 0.1
pgid_leader=$(get_process_pgid "$p_leader")
if [ "$pgid_leader" -ne "$p_leader" ]; then
    echo "ERROR: setsid process was not group leader"
    exit 1
fi
signal_process_safely "$p_leader" "TERM"
wait "$p_leader" 2>/dev/null || true
echo "ALL_SAFE"
"""
        res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Signal helper failed: {res.stdout}\n{res.stderr}")
        self.assertIn("ALL_SAFE", res.stdout)

    def test_17_token_alignment_reuses_valid_legacy_token(self):
        # When ACP_TOKEN_FILE is unset and LEGACY_DEFAULT_TOKEN_FILE is regular 0600, ensure_token reuses it
        temp_dir = tempfile.mkdtemp(prefix="token_test_")
        try:
            legacy_token = os.path.join(temp_dir, "legacy.token")
            run_dir = os.path.join(temp_dir, "run")
            with open(legacy_token, "w") as f:
                f.write("legacy-secret-token-12345\n")
            os.chmod(legacy_token, 0o600)

            test_script = f"""
export MIGRATION_RUN_DIR="{run_dir}"
export LEGACY_DEFAULT_TOKEN_FILE="{legacy_token}"
unset ACP_TOKEN_FILE
source "{self.script_path}"
ensure_token
echo "FINAL_TOKEN_FILE=$PROD_TOKEN_FILE"
"""
            res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn(f"FINAL_TOKEN_FILE={legacy_token}", res.stdout)
            self.assertIn("Token Alignment: Reusing verified legacy token", res.stdout)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_18_token_alignment_creates_migration_token_when_legacy_insecure_or_symlink(self):
        # When legacy token is insecure (e.g. 0644 or symlink), ensure_token creates controlled migration token
        temp_dir = tempfile.mkdtemp(prefix="token_test_insecure_")
        try:
            legacy_insecure = os.path.join(temp_dir, "insecure.token")
            run_dir = os.path.join(temp_dir, "run")
            with open(legacy_insecure, "w") as f:
                f.write("insecure-token\n")
            os.chmod(legacy_insecure, 0o644)

            test_script = f"""
export MIGRATION_RUN_DIR="{run_dir}"
export LEGACY_DEFAULT_TOKEN_FILE="{legacy_insecure}"
unset ACP_TOKEN_FILE
source "{self.script_path}"
ensure_token
echo "FINAL_TOKEN_FILE=$PROD_TOKEN_FILE"
"""
            res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            expected_migration_token = os.path.join(run_dir, "production.token")
            self.assertIn(f"FINAL_TOKEN_FILE={expected_migration_token}", res.stdout)
            self.assertIn("Token Alignment Notice:", res.stdout)
            self.assertTrue(os.path.exists(expected_migration_token))
            self.assertEqual(oct(stat.S_IMODE(os.stat(expected_migration_token).st_mode)), "0o600")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_19_token_alignment_respects_custom_acp_token_file_override(self):
        temp_dir = tempfile.mkdtemp(prefix="token_test_custom_")
        try:
            custom_token = os.path.join(temp_dir, "custom.token")
            run_dir = os.path.join(temp_dir, "run")

            test_script = f"""
export MIGRATION_RUN_DIR="{run_dir}"
export ACP_TOKEN_FILE="{custom_token}"
source "{self.script_path}"
ensure_token
echo "FINAL_TOKEN_FILE=$PROD_TOKEN_FILE"
"""
            res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn(f"FINAL_TOKEN_FILE={custom_token}", res.stdout)
            self.assertTrue(os.path.exists(custom_token))
            self.assertEqual(oct(stat.S_IMODE(os.stat(custom_token).st_mode)), "0o600")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_20_preflight_blocks_legacy_persistent_startup_hooks(self):
        temp_dir = tempfile.mkdtemp(prefix="startup_handoff_block_")
        try:
            entrypoint = os.path.join(temp_dir, "start-codex-container")
            with open(entrypoint, "w") as f:
                f.write("#!/bin/sh\n# old hook\n/workspace/scripts/acp_watchdog.sh\n")
            os.chmod(entrypoint, 0o755)

            env = self.base_env.copy()
            env["STARTUP_ENTRYPOINT_FILE"] = entrypoint
            env["STARTUP_PROFILE_FILE"] = ""
            env["GATEWAY_WATCHDOG_SCRIPT"] = os.path.join(self.repo_root, "scripts", "gateway_watchdog.sh")
            res = subprocess.run([self.script_path, "preflight"], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("Legacy startup hook still present", res.stdout)
            self.assertIn("Cutover remains blocked", res.stdout)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_21_default_legacy_graceful_timeout_covers_live_budget(self):
        command = f'source "{self.script_path}"; validate_legacy_graceful_timeout; printf "%s" "$LEGACY_GRACEFUL_TIMEOUT_SEC"'
        res = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.endswith("335"), res.stdout)

    def test_22_gateway_watchdog_candidate_has_isolation_guards(self):
        watchdog_path = os.path.join(self.repo_root, "scripts", "gateway_watchdog.sh")
        with open(watchdog_path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF", source)
        self.assertIn("flock -n 201", source)
        self.assertIn("201>&-", source)
        self.assertIn("close_fds=True", source)
        self.assertIn("start_new_session=True", source)
        self.assertIn("curl -fsS --max-time", source)
        self.assertIn("port_has_listener", source)
        self.assertNotIn("acp_watchdog.sh", source)
        self.assertNotIn("ensure_acp_bridge.sh", source)


class TestPhase10SandboxCutoverRollbackLifecycle(unittest.TestCase):
    """
    Deterministic verification of end-to-end cutover and rollback execution
    in an isolated test sandbox on dynamic ports, with ZERO impact on active production.
    """

    def setUp(self):
        self.script_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "migrate_production.sh")
        )
        self.test_dir = tempfile.mkdtemp(prefix="phase10_sandbox_")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        os.chmod(self.run_dir, 0o700)

        # Allocate dynamic free port for sandbox production simulation
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self.sandbox_prod_port = str(s.getsockname()[1])

        # Allocate dynamic free port for candidate
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self.sandbox_cand_port = str(s.getsockname()[1])

        self.mock_old_bridge_dir = os.path.join(self.test_dir, "old_bridge")
        os.makedirs(self.mock_old_bridge_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=self.mock_old_bridge_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.mock_old_bridge_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.mock_old_bridge_dir, check=True)

        # Create mock old bridge script
        self.mock_old_script = os.path.join(self.mock_old_bridge_dir, "acp_server.py")
        with open(self.mock_old_script, "w") as f:
            f.write(f"""
import http.server, json, os, sys

class MockHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({{
                'status': 'online',
                'service': 'Antigravity REST Bridge Server',
                'version': '1.0.0-legacy',
                'admission_control': 'HTTP 429 Bounded Semaphore (1)'
            }}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

port = int(os.environ.get('ACP_PORT', '{self.sandbox_prod_port}'))
server = http.server.ThreadingHTTPServer(('127.0.0.1', port), MockHandler)
server.serve_forever()
""")
        subprocess.run(["git", "add", "acp_server.py"], cwd=self.mock_old_bridge_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init mock bridge"], cwd=self.mock_old_bridge_dir, check=True)

        self.mock_watchdog_script = os.path.join(self.test_dir, "mock_watchdog.sh")
        with open(self.mock_watchdog_script, "w") as f:
            f.write("#!/usr/bin/env bash\nwhile true; do sleep 1; done\n")
        os.chmod(self.mock_watchdog_script, 0o755)

        # The sandbox models an already-installed, explicit gateway startup handoff.
        self.startup_entrypoint = os.path.join(self.test_dir, "start-codex-container")
        self.startup_profile = os.path.join(self.test_dir, "bashrc")
        gateway_watchdog = os.path.join(os.path.dirname(self.script_path), "gateway_watchdog.sh")
        handoff = f"# AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF\nsetsid {gateway_watchdog} </dev/null >/dev/null 2>&1 &\n"
        for startup_file in (self.startup_entrypoint, self.startup_profile):
            with open(startup_file, "w") as f:
                f.write(handoff)
            os.chmod(startup_file, 0o644)

        self.env = os.environ.copy()
        self.env["PROD_PORT"] = self.sandbox_prod_port
        self.env["CANDIDATE_PORT"] = self.sandbox_cand_port
        self.env["OLD_BRIDGE_DIR"] = self.mock_old_bridge_dir
        self.env["OLD_BRIDGE_SERVER_SCRIPT"] = self.mock_old_script
        self.env["WATCHDOG_SCRIPT"] = self.mock_watchdog_script
        self.env["MIGRATION_RUN_DIR"] = self.run_dir
        self.env["ACP_TOKEN_FILE"] = os.path.join(self.run_dir, "production.token")
        self.env["STARTUP_ENTRYPOINT_FILE"] = self.startup_entrypoint
        self.env["STARTUP_PROFILE_FILE"] = self.startup_profile
        self.env["GATEWAY_WATCHDOG_SCRIPT"] = os.path.join(os.path.dirname(self.script_path), "gateway_watchdog.sh")

        self.old_proc = None

    def tearDown(self):
        if self.old_proc and self.old_proc.poll() is None:
            self.old_proc.terminate()
            self.old_proc.wait()
        try:
            subprocess.run([self.script_path, "rollback", "--confirm-rollback"], env={**self.env, "CONFIRM_PRODUCTION_ROLLBACK": "1"}, capture_output=True)
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_15_mock_cutover_and_rollback_lifecycle_in_isolated_sandbox(self):
        # 1. Start mock legacy bridge on sandbox production port
        old_env = os.environ.copy()
        old_env["ACP_PORT"] = self.sandbox_prod_port
        self.old_proc = subprocess.Popen([sys.executable, self.mock_old_script], env=old_env)

        # Wait for mock old bridge to be online
        import urllib.request
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.sandbox_prod_port}/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.2)

        # Preflight check on sandbox environment must succeed
        res_preflight = subprocess.run([self.script_path, "preflight", "--handle-watchdog"], env={**self.env, "CONFIRM_WATCHDOG_OVERRIDE": "1"}, capture_output=True, text=True)
        self.assertEqual(res_preflight.returncode, 0, f"Sandbox preflight failed: {res_preflight.stdout}\n{res_preflight.stderr}")
        self.assertIn("Preflight Result: PASSED", res_preflight.stdout)

        # 2. Execute Cutover
        cutover_env = self.env.copy()
        cutover_env["CONFIRM_PRODUCTION_CUTOVER"] = "1"
        cutover_env["CONFIRM_WATCHDOG_OVERRIDE"] = "1"
        res_cutover = subprocess.run([self.script_path, "cutover", "--confirm-cutover", "--handle-watchdog"], env=cutover_env, capture_output=True, text=True)
        self.assertEqual(res_cutover.returncode, 0, f"Cutover failed: {res_cutover.stdout}\n{res_cutover.stderr}")
        self.assertIn("Cutover SUCCESS", res_cutover.stdout)

        # Verify new gateway is running on sandbox port and reports unified semaphore
        with urllib.request.urlopen(f"http://127.0.0.1:{self.sandbox_prod_port}/health", timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("status"), "online")
            self.assertIn("limits", data)
            self.assertIn("gateway_max_concurrency", data["limits"])

        # Check migration state file
        state_file = os.path.join(self.run_dir, "migration_state.json")
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            state_data = json.load(f)
        self.assertEqual(state_data.get("state"), "CUTOVER_COMPLETE")

        # 3. Execute Rollback
        rollback_env = self.env.copy()
        rollback_env["CONFIRM_PRODUCTION_ROLLBACK"] = "1"
        res_rollback = subprocess.run([self.script_path, "rollback", "--confirm-rollback"], env=rollback_env, capture_output=True, text=True)
        self.assertEqual(res_rollback.returncode, 0, f"Rollback failed: {res_rollback.stdout}\n{res_rollback.stderr}")
        self.assertIn("Rollback SUCCESS", res_rollback.stdout)

        # Verify legacy bridge is restored and online on sandbox port
        with urllib.request.urlopen(f"http://127.0.0.1:{self.sandbox_prod_port}/health", timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            restored_data = json.loads(resp.read().decode())
            self.assertEqual(restored_data.get("version"), "1.0.0-legacy")

        with open(state_file) as f:
            state_data2 = json.load(f)
        self.assertEqual(state_data2.get("state"), "ROLLED_BACK")


if __name__ == "__main__":
    unittest.main()
