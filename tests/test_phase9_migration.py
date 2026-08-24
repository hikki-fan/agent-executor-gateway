#!/usr/bin/env python3
"""
Phase 9 — Migration Candidate Test Suite and Opt-in Live Smoke Harness.

Tests the candidate deployment infrastructure, verification matrices,
and rollback readiness per Goal Prompt Section 51.

Deterministic tests run by default.
Live tests require explicit opt-in via RUN_PHASE9_LIVE=1.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.config import GatewayConfig
from core.result import ExecutorResult
from core.session_lock import SessionLockManager
from orchestration.task import Task, validate_task_dict
from orchestration.router import route_task
from orchestration.escalation import evaluate_escalation, init_escalation_state
from orchestration.worktree import WorktreeManager
from orchestration.dag import TaskDAG, run_dag_parallel, STATE_DONE
from adapters.grok import GrokAdapter


def find_free_port() -> int:
    """Find a local unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_dummy_task(
    task_id: str,
    executor: str,
    complexity: str = "M",
    task_type: str = "feature",
    repo_path: str = "/workspace",
    base_commit: str = "abc1234567890",
) -> Task:
    task_dict = {
        "version": "1",
        "task_id": task_id,
        "parent_task_id": None,
        "goal": f"Phase 9 migration candidate task {task_id}",
        "repository": {
            "path": repo_path,
            "base_commit": base_commit,
        },
        "classification": {
            "complexity": complexity,
            "risk": "low" if complexity == "S" else "medium",
            "type": task_type,
        },
        "execution": {
            "executor": executor,
            "fallback_executor": "grok" if executor == "agy" else "agy",
            "max_same_executor_attempts": 2,
            "max_executor_switches": 1,
            "isolated_worktree": True,
        },
        "scope": {
            "allowed_paths": ["**"],
            "forbidden_paths": ["database/migrations/**"],
        },
        "acceptance": ["Candidate task verification passes"],
        "verification": {
            "commands": ["python3 -c \"print('verified')\""],
        },
        "depends_on": [],
    }
    t, errs = validate_task_dict(task_dict)
    if errs or t is None:
        raise ValueError(f"Task creation error: {errs}")
    return t


class TestPhase9CandidateConfiguration(unittest.TestCase):
    """Tests for candidate environment variables, defaults, and isolation."""

    def test_01_candidate_port_defaults_and_env(self):
        orig = os.environ.get("ACP_PORT")
        try:
            os.environ.pop("ACP_PORT", None)
            cfg = GatewayConfig.from_env()
            self.assertEqual(cfg.port, 8765)

            # Overridden port
            os.environ["ACP_PORT"] = "8766"
            cfg2 = GatewayConfig.from_env()
            self.assertEqual(cfg2.port, 8766)
        finally:
            if orig is not None:
                os.environ["ACP_PORT"] = orig
            else:
                os.environ.pop("ACP_PORT", None)

    def test_02_candidate_token_path_isolation(self):
        orig = os.environ.get("ACP_TOKEN_FILE")
        try:
            with tempfile.TemporaryDirectory(prefix="candidate_auth_") as tmp_dir:
                token_file = os.path.join(tmp_dir, "candidate.token")
                os.environ["ACP_TOKEN_FILE"] = token_file
                cfg = GatewayConfig.from_env()
                self.assertEqual(cfg.token_file, token_file)
        finally:
            if orig is not None:
                os.environ["ACP_TOKEN_FILE"] = orig
            else:
                os.environ.pop("ACP_TOKEN_FILE", None)


class TestPhase9CandidateScriptLifecycle(unittest.TestCase):
    """Tests for scripts/migration_candidate.sh lifecycle and safety."""

    def setUp(self):
        self.script_path = os.path.join(REPO_ROOT, "scripts", "migration_candidate.sh")
        self.test_run_dir = tempfile.mkdtemp(prefix="candidate_test_run_")
        self.test_port = str(find_free_port())

    def tearDown(self):
        # Ensure candidate on test_port is stopped
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port
        subprocess.run([self.script_path, "stop"], env=env, capture_output=True)
        shutil.rmtree(self.test_run_dir, ignore_errors=True)

    def test_03_script_status_when_stopped(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        res = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("STOPPED", res.stdout)

    def test_04_script_stop_when_stopped_is_noop(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        res = subprocess.run([self.script_path, "stop"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("already stopped", res.stdout)

    def test_05_script_start_status_stop_lifecycle(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port
        token_file = os.path.join(self.test_run_dir, "candidate.token")
        env["ACP_TOKEN_FILE"] = token_file

        # 1. Start candidate
        res_start = subprocess.run([self.script_path, "start"], env=env, capture_output=True, text=True)
        self.assertEqual(res_start.returncode, 0, f"Start failed: {res_start.stderr}")
        self.assertIn("started successfully", res_start.stdout)

        # Verify token file was created with 0600 permissions
        self.assertTrue(os.path.exists(token_file))
        mode = oct(os.stat(token_file).st_mode & 0o777)
        self.assertEqual(mode, "0o600")

        # 2. Status candidate
        res_status = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
        self.assertEqual(res_status.returncode, 0, f"Status failed: {res_status.stderr}")
        self.assertIn("ONLINE", res_status.stdout)

        # 3. Stop candidate
        res_stop = subprocess.run([self.script_path, "stop"], env=env, capture_output=True, text=True)
        self.assertEqual(res_stop.returncode, 0, f"Stop failed: {res_stop.stderr}")
        self.assertIn("Candidate stopped", res_stop.stdout)

        # 4. Status after stop
        res_status2 = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
        self.assertEqual(res_status2.returncode, 1)

    def test_05b_script_rejects_invalid_ports(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir

        for bad_port in ("-1", "0", "80", "70000", "invalid_port"):
            env["ACP_PORT"] = bad_port
            res = subprocess.run([self.script_path, "start"], env=env, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("must be an integer between 1024 and 65535", res.stderr)

    def test_05c_script_rejects_symlink_files(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        # 1. Symlink token file
        target_token = os.path.join(self.test_run_dir, "real_token.txt")
        with open(target_token, "w") as f:
            f.write("secret\n")
        symlink_token = os.path.join(self.test_run_dir, "candidate.token")
        os.symlink(target_token, symlink_token)
        env["ACP_TOKEN_FILE"] = symlink_token

        res = subprocess.run([self.script_path, "start"], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("is a symbolic link", res.stderr)

        os.unlink(symlink_token)

        # 2. Symlink PID file
        symlink_pid = os.path.join(self.test_run_dir, "candidate.pid")
        dummy_file = os.path.join(self.test_run_dir, "dummy_pid.txt")
        with open(dummy_file, "w") as f:
            f.write("12345\n")
        os.symlink(dummy_file, symlink_pid)

        res_status = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
        self.assertNotEqual(res_status.returncode, 0)
        self.assertIn("is a symbolic link", res_status.stderr)

    def test_05d_script_rejects_forged_pid_file(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        # Forge PID with a non-candidate process PID (e.g. current python process or PID 1)
        pid_file = os.path.join(self.test_run_dir, "candidate.pid")
        with open(pid_file, "w") as f:
            f.write(f"{os.getpid()}\n")

        # 1. status should refuse to treat current python process as candidate
        res_status = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
        self.assertNotEqual(res_status.returncode, 0)
        self.assertIn("STOPPED", res_status.stdout)

        # Re-write forged PID for stop test
        with open(pid_file, "w") as f:
            f.write(f"{os.getpid()}\n")

        # 2. stop should strictly refuse to send signal to unrelated process
        res_stop = subprocess.run([self.script_path, "stop"], env=env, capture_output=True, text=True)
        self.assertEqual(res_stop.returncode, 1)
        self.assertIn("Security Error", res_stop.stderr)

    def test_05e_script_flock_concurrency_lock(self):
        import fcntl
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        lock_file = os.path.join(self.test_run_dir, "candidate.lock")
        os.makedirs(self.test_run_dir, exist_ok=True)

        with open(lock_file, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                # Running script while lock is held must fail immediately
                res = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("Another migration candidate operation is currently in progress", res.stderr)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def test_05f_runtime_files_permissions(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port
        token_file = os.path.join(self.test_run_dir, "candidate.token")
        env["ACP_TOKEN_FILE"] = token_file

        res_start = subprocess.run([self.script_path, "start"], env=env, capture_output=True, text=True)
        self.assertEqual(res_start.returncode, 0)
        try:
            # Check directory permissions (0700)
            dir_mode = oct(os.stat(self.test_run_dir).st_mode & 0o777)
            self.assertEqual(dir_mode, "0o700")

            # Check candidate.token permissions (0600)
            token_mode = oct(os.stat(token_file).st_mode & 0o777)
            self.assertEqual(token_mode, "0o600")

            # Check candidate.log permissions (0600)
            log_file = os.path.join(self.test_run_dir, "candidate.log")
            if os.path.exists(log_file):
                log_mode = oct(os.stat(log_file).st_mode & 0o777)
                self.assertEqual(log_mode, "0o600")

            # Check candidate.lock permissions (0600)
            lock_file = os.path.join(self.test_run_dir, "candidate.lock")
            if os.path.exists(lock_file):
                lock_mode = oct(os.stat(lock_file).st_mode & 0o777)
                self.assertEqual(lock_mode, "0o600")
        finally:
            subprocess.run([self.script_path, "stop"], env=env, capture_output=True)

    def test_05g_script_rejects_foreign_directory_acp_server_process(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        foreign_dir = tempfile.mkdtemp(prefix="foreign_server_")
        foreign_script = os.path.join(foreign_dir, "acp_server.py")
        with open(foreign_script, "w") as f:
            f.write("import time; time.sleep(60)\n")

        proc = subprocess.Popen([sys.executable, foreign_script])
        try:
            pid_file = os.path.join(self.test_run_dir, "candidate.pid")
            with open(pid_file, "w") as f:
                f.write(f"{proc.pid}\n")

            # 1. status must reject foreign directory's acp_server.py
            res_status = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
            self.assertNotEqual(res_status.returncode, 0)
            self.assertIn("STOPPED", res_status.stdout)

            # Re-write PID for stop test
            with open(pid_file, "w") as f:
                f.write(f"{proc.pid}\n")

            # 2. stop must reject and NOT kill the foreign process
            res_stop = subprocess.run([self.script_path, "stop"], env=env, capture_output=True, text=True)
            self.assertEqual(res_stop.returncode, 1)
            self.assertIn("Security Error", res_stop.stderr)

            # Assert foreign process is STILL running and unharmed
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate()
            proc.wait()
            shutil.rmtree(foreign_dir, ignore_errors=True)

    def test_05h_script_handles_zombie_pid_status_and_stop(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        env["ACP_PORT"] = self.test_port

        pid_file = os.path.join(self.test_run_dir, "candidate.pid")

        # Fork a child that immediately exits to produce a real Linux zombie process
        child_pid = os.fork()
        if child_pid == 0:
            os._exit(0)

        try:
            # Wait briefly for child to exit and become a zombie in kernel
            time.sleep(0.1)

            # Write child PID into candidate.pid
            with open(pid_file, "w") as f:
                f.write(f"{child_pid}\n")

            # 1. status should recognize zombie as STOPPED and clean up PID file
            res_status = subprocess.run([self.script_path, "status"], env=env, capture_output=True, text=True)
            self.assertNotEqual(res_status.returncode, 0)
            self.assertIn("STOPPED", res_status.stdout)
            self.assertIn("zombie", res_status.stdout)
            self.assertFalse(os.path.exists(pid_file))

            # Re-write child PID for stop test
            with open(pid_file, "w") as f:
                f.write(f"{child_pid}\n")

            # 2. stop should recognize zombie as already exited, return 0, clean up PID file, no security errors
            res_stop = subprocess.run([self.script_path, "stop"], env=env, capture_output=True, text=True)
            self.assertEqual(res_stop.returncode, 0)
            self.assertIn("already exited", res_stop.stdout)
            self.assertNotIn("Security Error", res_stop.stderr)
            self.assertNotIn("changed identity", res_stop.stderr)
            self.assertFalse(os.path.exists(pid_file))
        finally:
            # Reap child process
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass

    def test_05i_script_launch_detaches_stdin(self):
        # Statically verify that migration_candidate.sh passes < /dev/null when launching background daemon
        with open(self.script_path, "r", encoding="utf-8") as f:
            script_text = f.read()

        self.assertIn("< /dev/null", script_text)
        self.assertIn("200>&-", script_text)

    def test_05j_script_stop_reaps_server_pid_without_zombie_and_releases_port(self):
        env = os.environ.copy()
        env["CANDIDATE_RUN_DIR"] = self.test_run_dir
        token_file = os.path.join(self.test_run_dir, "candidate.token")
        env["ACP_TOKEN_FILE"] = token_file

        # Choose a dynamic free port
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            test_port = str(s.getsockname()[1])
        env["ACP_PORT"] = test_port

        # 1. Start candidate
        res_start = subprocess.run([self.script_path, "start"], env=env, capture_output=True, text=True)
        self.assertEqual(res_start.returncode, 0, f"Start failed: {res_start.stderr}")

        pid_file = os.path.join(self.test_run_dir, "candidate.pid")
        self.assertTrue(os.path.exists(pid_file))
        with open(pid_file, "r") as f:
            server_pid = int(f.read().strip())

        # Verify server PID is running, not zombie, and its PPid is the persistent reaper process (NOT PID 1)
        self.assertTrue(os.path.exists(f"/proc/{server_pid}"))
        server_ppid = None
        if os.path.exists(f"/proc/{server_pid}/status"):
            with open(f"/proc/{server_pid}/status", "r") as f:
                for line in f:
                    if line.startswith("State:"):
                        self.assertNotIn("Z", line)
                    elif line.startswith("PPid:"):
                        server_ppid = int(line.split()[1])

        self.assertIsNotNone(server_ppid)
        self.assertNotEqual(server_ppid, 1, "Server process PPid must be the persistent reaper wrapper, not PID 1")
        self.assertTrue(os.path.exists(f"/proc/{server_ppid}"), f"Reaper parent process {server_ppid} must be running")

        # Verify port is listening
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            conn_res = s.connect_ex(("127.0.0.1", int(test_port)))
            self.assertEqual(conn_res, 0, "Port should be actively accepting connections")

        # 2. Stop candidate
        res_stop = subprocess.run([self.script_path, "stop"], env=env, capture_output=True, text=True)
        self.assertEqual(res_stop.returncode, 0, f"Stop failed: {res_stop.stderr}")

        # 3. Assert server_pid is completely reaped and no longer exists in /proc
        time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{server_pid}"), f"Server PID {server_pid} was not reaped and still exists in /proc")

        # 4. Assert port is released
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            conn_res2 = s.connect_ex(("127.0.0.1", int(test_port)))
            self.assertNotEqual(conn_res2, 0, "Port should be released after stop")


class TestPhase9WorkflowRoutingAndEscalationMatrix(unittest.TestCase):
    """Deterministic matrix testing of Phase 9 regression and routing workflows."""

    def test_06_routing_matrix(self):
        # 1. Small feature -> AGY
        t_s = make_dummy_task(task_id="T_S", executor="agy", complexity="S", task_type="feature")
        dec_s = route_task(t_s)
        self.assertEqual(dec_s.executor, "agy")

        # 2. Medium feature -> AGY
        t_mf = make_dummy_task(task_id="T_MF", executor="agy", complexity="M", task_type="feature")
        dec_mf = route_task(t_mf)
        self.assertEqual(dec_mf.executor, "agy")

        # 3. Medium debug -> Grok
        t_md = make_dummy_task(task_id="T_MD", executor="agy", complexity="M", task_type="debug")
        dec_md = route_task(t_md)
        self.assertEqual(dec_md.executor, "grok")

    def test_07_agy_fail_to_grok_escalation_flow(self):
        t_esc = make_dummy_task(task_id="T_ESC", executor="agy", complexity="M", task_type="feature")
        state = init_escalation_state(t_esc)

        # Attempt 1: agy fails -> retry same executor
        from orchestration.verifier import TaskVerificationResult, ScopeCheckResult
        dummy_verif = TaskVerificationResult(
            task_id=t_esc.task_id,
            status="failed",
            reason="test_failure",
            command_results=[],
            scope_result=ScopeCheckResult(passed=True, details="", violating_files=[]),
            summary="Tests failed",
        )

        dec1 = evaluate_escalation(t_esc, state, dummy_verif)
        self.assertEqual(dec1.action, "retry_same_executor")
        self.assertEqual(dec1.next_executor, "agy")

        # Attempt 2: agy fails -> switch to grok
        dec2 = evaluate_escalation(t_esc, state, dummy_verif)
        self.assertEqual(dec2.action, "switch_executor")
        self.assertEqual(dec2.next_executor, "grok")
        self.assertIsNotNone(dec2.context)
        self.assertIn("Executor 'agy' executed 2 attempt(s)", dec2.context.previous_executor_summary)

    def test_08_agy_and_grok_worktree_parallel_execution(self):
        with tempfile.TemporaryDirectory(prefix="p9_repo_") as test_repo, tempfile.TemporaryDirectory(prefix="p9_wt_root_") as wt_root:
            subprocess.run(["git", "init"], cwd=test_repo, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=test_repo, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=test_repo, capture_output=True, check=True)

            with open(os.path.join(test_repo, "app.py"), "w") as f:
                f.write("# baseline\n")
            subprocess.run(["git", "add", "app.py"], cwd=test_repo, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=test_repo, capture_output=True, check=True)

            head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=test_repo, capture_output=True, text=True, check=True).stdout.strip()

            t_agy = make_dummy_task(task_id="TASK-AGY", executor="agy", repo_path=test_repo, base_commit=head_commit)
            t_grok = make_dummy_task(task_id="TASK-GROK", executor="grok", repo_path=test_repo, base_commit=head_commit)

            dag = TaskDAG([t_agy, t_grok])

            executed: list[str] = []

            def worker_fn(task: Task, wt_info) -> dict:
                executed.append(f"{task.task_id}-{wt_info.executor}")
                time.sleep(0.05)
                return {"status": "success"}

            res = run_dag_parallel(
                dag=dag,
                worker_fn=worker_fn,
                repo_path=test_repo,
                worktree_root=wt_root,
                max_concurrency=2,
            )

            self.assertTrue(res["completed"])
            self.assertEqual(len(executed), 2)
            self.assertIn("TASK-AGY-agy", executed)
            self.assertIn("TASK-GROK-grok", executed)

    def test_09_multi_executor_concurrency_and_session_lock(self):
        lock_mgr = SessionLockManager()
        # Same executor, same session -> conflict
        self.assertTrue(lock_mgr.acquire("agy", "sess-1"))
        self.assertFalse(lock_mgr.acquire("agy", "sess-1"))

        # Different executor, same session ID -> isolated, no conflict
        self.assertTrue(lock_mgr.acquire("grok", "sess-1"))

        lock_mgr.release("agy", "sess-1")
        lock_mgr.release("grok", "sess-1")

    def test_10_grok_adapter_json_parsing_and_contract(self):
        from adapters.grok import parse_grok_json, extract_grok_usage
        raw_json_output = json.dumps({
            "text": "Hello from Grok candidate",
            "stopReason": "stop",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            "cost": 0.005,
        })

        parsed = parse_grok_json(raw_json_output)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("text"), "Hello from Grok candidate")
        self.assertEqual(parsed.get("sessionId"), "11111111-2222-3333-4444-555555555555")

        usage = extract_grok_usage(parsed)
        self.assertEqual(usage.get("input_tokens"), 10)
        self.assertEqual(usage.get("output_tokens"), 20)
        self.assertEqual(usage.get("total_tokens"), 30)

        # Mock runner test through GrokAdapter.invoke
        def mock_runner(cmd, timeout_sec, env=None, cwd=None):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=raw_json_output, stderr="")

        adapter = GrokAdapter(runner=mock_runner, bin_path="grok")
        res = adapter.invoke(prompt="hello")
        self.assertEqual(res.status, "success")
        self.assertEqual(res.response, "Hello from Grok candidate")
        self.assertEqual(res.session_id, "11111111-2222-3333-4444-555555555555")


class TestPhase9LiveSmokeHarness(unittest.TestCase):
    """
    Opt-in Live Smoke Test against running candidates.
    Activated only when RUN_PHASE9_LIVE=1 is set in the environment.
    """

    @unittest.skipUnless(os.environ.get("RUN_PHASE9_LIVE") == "1", "Opt-in live smoke test; set RUN_PHASE9_LIVE=1 to execute")
    def test_11_live_candidate_health_probe(self):
        """Probe live candidate on port 8766."""
        candidate_port = os.environ.get("ACP_PORT", "8766")
        token_file = os.environ.get("ACP_TOKEN_FILE", os.path.expanduser("~/.agent-executor-gateway/candidate.token"))

        self.assertTrue(os.path.exists(token_file), f"Candidate token file not found at {token_file}")
        with open(token_file, "r") as f:
            token = f.read().strip()

        # Probe health
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{candidate_port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertIn("status", data)

        # Probe executors endpoint with token
        req_exc = urllib.request.Request(
            f"http://127.0.0.1:{candidate_port}/v1/executors",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req_exc, timeout=5) as resp_exc:
            self.assertEqual(resp_exc.status, 200)
            exc_data = json.loads(resp_exc.read().decode())
            self.assertIn("executors", exc_data)

    @unittest.skipUnless(os.environ.get("RUN_PHASE9_LIVE") == "1", "Opt-in live smoke test; set RUN_PHASE9_LIVE=1 to execute")
    def test_12_live_candidate_agy_cwd_creation(self):
        """Verify that invoking live AGY with cwd creates files in the targeted cwd directory."""
        candidate_port = os.environ.get("ACP_PORT", "8766")
        token_file = os.environ.get("ACP_TOKEN_FILE", os.path.expanduser("~/.agent-executor-gateway/candidate.token"))

        self.assertTrue(os.path.exists(token_file), f"Candidate token file not found at {token_file}")
        with open(token_file, "r") as f:
            token = f.read().strip()

        import urllib.request
        with tempfile.TemporaryDirectory(prefix="phase9_live_agy_cwd_") as tmp_cwd:
            target_filename = "phase9_agy_smoke.txt"
            target_file_path = os.path.join(tmp_cwd, target_filename)

            payload = json.dumps({
                "prompt": f"Create a file named {target_filename} with content 'phase9_cwd_verified' in the current workspace directory.",
                "cwd": tmp_cwd,
            }).encode("utf-8")

            req = urllib.request.Request(
                f"http://127.0.0.1:{candidate_port}/v1/executors/agy/invoke",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data.get("status"), "success")

            # Assert file was created in tmp_cwd
            self.assertTrue(os.path.exists(target_file_path), f"File was not created in target cwd: {target_file_path}")
            with open(target_file_path, "r") as f:
                content = f.read()
            self.assertIn("phase9_cwd_verified", content)


if __name__ == "__main__":
    unittest.main()
