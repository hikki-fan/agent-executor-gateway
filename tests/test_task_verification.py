#!/usr/bin/env python3
"""
Unit and Integration Test Suite for Task Schema, Verification Pipeline, Scope Control,
Completion Report, Metrics, and agentctl CLI (Phase 6).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from orchestration.task import (
    Task,
    load_and_validate_task,
    validate_task_dict,
)
from orchestration.scope import (
    check_scope,
    get_git_changed_and_untracked_files,
    match_glob_pattern,
)
from orchestration.verifier import (
    extract_relevant_tail,
    redact_sensitive_text,
    run_command_safe,
    verify_task,
)
from orchestration.report import (
    generate_completion_report,
    get_git_diff_stats,
    record_task_metrics,
)


def get_sample_valid_task_dict(repo_dir: str = "/workspace/project") -> dict:
    """Return a valid Section 18 Task dictionary."""
    return {
        "version": "1",
        "task_id": "TASK-001",
        "parent_task_id": None,
        "goal": "增加 Telegram 下载任务取消功能",
        "repository": {
            "path": repo_dir,
            "base_commit": "abc1234567890",
        },
        "classification": {
            "complexity": "M",
            "risk": "medium",
            "type": "feature",
        },
        "execution": {
            "executor": "agy",
            "fallback_executor": "grok",
            "max_same_executor_attempts": 2,
            "max_executor_switches": 1,
            "isolated_worktree": False,
        },
        "scope": {
            "allowed_paths": [
                "backend/tasks/**",
                "backend/api/**",
            ],
            "forbidden_paths": [
                "database/migrations/**",
            ],
        },
        "acceptance": [
            "运行中的任务可以取消",
            "取消后状态变为 cancelled",
            "不得删除已经完成的文件",
            "现有测试继续通过",
        ],
        "verification": {
            "commands": [
                "python3 -c \"print('verification test passed')\"",
            ],
        },
    }


class TestTaskValidation(unittest.TestCase):
    """Tests for Task JSON schema and validation rules."""

    def test_01_valid_task_dict_success(self):
        data = get_sample_valid_task_dict()
        task, errors = validate_task_dict(data)
        self.assertEqual(errors, [])
        self.assertIsNotNone(task)
        self.assertEqual(task.task_id, "TASK-001")
        self.assertEqual(task.classification.complexity, "M")
        self.assertEqual(task.execution.executor, "agy")
        self.assertEqual(task.execution.fallback_executor, "grok")
        self.assertEqual(task.execution.max_same_executor_attempts, 2)
        self.assertEqual(task.execution.max_executor_switches, 1)
        self.assertFalse(task.execution.isolated_worktree)
        self.assertEqual(len(task.scope.allowed_paths), 2)
        self.assertEqual(len(task.acceptance), 4)
        self.assertEqual(len(task.verification.commands), 1)

    def test_02_task_serialization_roundtrip(self):
        data = get_sample_valid_task_dict()
        task, errors = validate_task_dict(data)
        self.assertEqual(errors, [])
        serialized = task.to_dict()
        self.assertEqual(serialized["task_id"], "TASK-001")
        self.assertEqual(serialized["repository"]["path"], "/workspace/project")

        # Roundtrip from dict
        task_rebuilt = Task.from_dict(serialized)
        self.assertEqual(task_rebuilt.task_id, task.task_id)
        self.assertEqual(task_rebuilt.goal, task.goal)

    def test_03_missing_top_level_keys_rejected(self):
        required_keys = [
            "version",
            "task_id",
            "goal",
            "repository",
            "classification",
            "execution",
            "scope",
            "acceptance",
            "verification",
        ]
        for key in required_keys:
            data = get_sample_valid_task_dict()
            del data[key]
            task, errors = validate_task_dict(data)
            self.assertIsNone(task, f"Should reject missing key '{key}'")
            self.assertTrue(any(key in err for err in errors), f"Error should mention '{key}'")

    def test_04_invalid_complexity_enum_rejected(self):
        data = get_sample_valid_task_dict()
        data["classification"]["complexity"] = "XXL"
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("complexity" in err for err in errors))

    def test_05_invalid_risk_enum_rejected(self):
        data = get_sample_valid_task_dict()
        data["classification"]["risk"] = "fatal"
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("risk" in err for err in errors))

    def test_06_invalid_executor_enum_rejected(self):
        data = get_sample_valid_task_dict()
        data["execution"]["executor"] = "unknown_bot"
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("execution.executor" in err for err in errors))

    def test_07_invalid_fallback_executor_enum_rejected(self):
        data = get_sample_valid_task_dict()
        data["execution"]["fallback_executor"] = "copilot"
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("execution.fallback_executor" in err for err in errors))

    def test_08_non_positive_attempts_or_switches_rejected(self):
        # max_same_executor_attempts <= 0
        data = get_sample_valid_task_dict()
        data["execution"]["max_same_executor_attempts"] = 0
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("max_same_executor_attempts" in err for err in errors))

        # max_executor_switches < 0
        data = get_sample_valid_task_dict()
        data["execution"]["max_executor_switches"] = -1
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("max_executor_switches" in err for err in errors))

        # boolean instead of int
        data = get_sample_valid_task_dict()
        data["execution"]["max_same_executor_attempts"] = True
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("max_same_executor_attempts" in err for err in errors))

    def test_09_invalid_path_fields_rejected(self):
        # Empty repository.path
        data = get_sample_valid_task_dict()
        data["repository"]["path"] = "   "
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("repository.path" in err for err in errors))

        # Non-string scope items
        data = get_sample_valid_task_dict()
        data["scope"]["allowed_paths"] = [123, "backend/**"]
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("scope.allowed_paths" in err for err in errors))

    def test_10_empty_acceptance_or_verification_commands_rejected(self):
        data = get_sample_valid_task_dict()
        data["acceptance"] = []
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("acceptance" in err for err in errors))

        data = get_sample_valid_task_dict()
        data["verification"]["commands"] = []
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("verification.commands" in err for err in errors))

    def test_10b_version_type_parent_and_task_type_are_strict(self):
        data = get_sample_valid_task_dict()
        data["version"] = "2"
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("version" in err for err in errors))

        data = get_sample_valid_task_dict()
        data["parent_task_id"] = "   "
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("parent_task_id" in err for err in errors))

        data = get_sample_valid_task_dict()
        data["classification"]["type"] = "bug"
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("classification.type" in err for err in errors))

        data = get_sample_valid_task_dict()
        data["scope"]["allowed_paths"] = ["   "]
        task, errors = validate_task_dict(data)
        self.assertIsNone(task)
        self.assertTrue(any("allowed_paths" in err for err in errors))


class TestVerifierAndScope(unittest.TestCase):
    """Tests for machine verification, process execution, and scope control."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="task_verif_test_")
        # Initialize a temporary git repository
        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        # Initial commit
        readme = os.path.join(self.test_dir, "README.md")
        with open(readme, "w") as f:
            f.write("# Test Project\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_11_match_glob_pattern(self):
        self.assertTrue(match_glob_pattern("backend/tasks/worker.py", "backend/**"))
        self.assertTrue(match_glob_pattern("backend/api/views.py", "backend/api/**"))
        self.assertTrue(match_glob_pattern("database/migrations/0001_init.sql", "database/migrations/**"))
        self.assertTrue(match_glob_pattern("test.py", "*.py"))
        self.assertTrue(match_glob_pattern("sub/test.py", "**/*.py"))

        self.assertFalse(match_glob_pattern("frontend/app.js", "backend/**"))
        self.assertFalse(match_glob_pattern("backend/tasks/sub/worker.py", "backend/tasks/*.py"))

    def test_12_scope_control_allowed_and_forbidden(self):
        # Create allowed file
        os.makedirs(os.path.join(self.test_dir, "backend", "tasks"), exist_ok=True)
        with open(os.path.join(self.test_dir, "backend", "tasks", "task.py"), "w") as f:
            f.write("# Task code\n")

        # 1. Allowed paths pass
        res_pass = check_scope(
            repo_path=self.test_dir,
            allowed_paths=["backend/**"],
            forbidden_paths=["database/**"],
            base_commit=self.base_commit,
        )
        self.assertTrue(res_pass.passed)
        self.assertEqual(res_pass.violating_files, [])

        # 2. Forbidden path violation
        os.makedirs(os.path.join(self.test_dir, "database", "migrations"), exist_ok=True)
        with open(os.path.join(self.test_dir, "database", "migrations", "001.sql"), "w") as f:
            f.write("-- migration\n")

        res_forbid = check_scope(
            repo_path=self.test_dir,
            allowed_paths=["backend/**", "database/**"],
            forbidden_paths=["database/migrations/**"],
            base_commit=self.base_commit,
        )
        self.assertFalse(res_forbid.passed)
        self.assertEqual(res_forbid.reason, "scope_violation")
        self.assertIn("database/migrations/001.sql", res_forbid.violating_files)

        # 3. Not in allowed paths violation
        os.makedirs(os.path.join(self.test_dir, "frontend"), exist_ok=True)
        with open(os.path.join(self.test_dir, "frontend", "index.html"), "w") as f:
            f.write("<html></html>\n")

        res_outside = check_scope(
            repo_path=self.test_dir,
            allowed_paths=["backend/**"],
            forbidden_paths=[],
            base_commit=self.base_commit,
        )
        self.assertFalse(res_outside.passed)
        self.assertEqual(res_outside.reason, "scope_violation")
        self.assertIn("frontend/index.html", res_outside.violating_files)

    def test_13_untracked_files_detected_in_scope(self):
        # Untracked file in forbidden directory
        os.makedirs(os.path.join(self.test_dir, "secret"), exist_ok=True)
        with open(os.path.join(self.test_dir, "secret", "keys.txt"), "w") as f:
            f.write("secret data\n")

        res = check_scope(
            repo_path=self.test_dir,
            allowed_paths=["backend/**"],
            forbidden_paths=[],
            base_commit=self.base_commit,
        )
        self.assertFalse(res.passed)
        self.assertIn("secret/keys.txt", res.violating_files)

    def test_14_run_command_safe_success_and_cwd(self):
        # Create a marker file in test repo
        with open(os.path.join(self.test_dir, "marker.txt"), "w") as f:
            f.write("MARKER_123")

        cmd = "python3 -c \"import os; assert os.path.exists('marker.txt'); print('CWD verified')\""
        res = run_command_safe(cmd, cwd=self.test_dir, timeout_sec=5.0)

        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.status, "passed")
        self.assertIn("CWD verified", res.relevant_tail)
        self.assertTrue(os.path.exists(res.log_path))

    def test_15_run_command_safe_failure(self):
        cmd = "python3 -c \"import sys; sys.exit(7)\""
        res = run_command_safe(cmd, cwd=self.test_dir, timeout_sec=5.0)
        self.assertEqual(res.exit_code, 7)
        self.assertEqual(res.status, "failed")

    def test_16_run_command_safe_timeout(self):
        cmd = "python3 -c \"import time; time.sleep(10)\""
        res = run_command_safe(cmd, cwd=self.test_dir, timeout_sec=0.2)
        self.assertEqual(res.status, "timed_out")
        self.assertEqual(res.exit_code, 124)

    def test_17_shell_metacharacters_not_executed(self):
        # Attempt shell injection: python3 command followed by ; or && creating /tmp/injected.txt
        injected_file = os.path.join(self.test_dir, "should_not_exist.txt")
        cmd = f"python3 -c \"print('safe')\" ; touch {injected_file}"
        res = run_command_safe(cmd, cwd=self.test_dir, timeout_sec=5.0)

        # Because shell=False and shlex.split(), the ';' and 'touch' are passed as extra arguments to python3
        self.assertFalse(os.path.exists(injected_file), "Shell metacharacters must not execute separate commands")

    def test_18_log_redaction_and_tail(self):
        bearer_fixture = "secret_" + "bearer_" + "token_" + "1234567890"
        cmd = f"python3 -c \"print('Authorization: Bearer {bearer_fixture}'); print('Done')\""
        res = run_command_safe(cmd, cwd=self.test_dir, timeout_sec=5.0)

        self.assertEqual(res.exit_code, 0)
        with open(res.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()

        self.assertNotIn(bearer_fixture, log_content)
        self.assertIn("[REDACTED]", log_content)
        self.assertNotIn(bearer_fixture, res.relevant_tail)
        self.assertEqual(stat.S_IMODE(os.stat(res.log_path).st_mode), 0o600)

        github_fixture = "ghp_" + ("a" * 24)
        openai_fixture = "sk-" + ("b" * 24)
        standalone_secrets = redact_sensitive_text(
            f"{github_fixture} {openai_fixture}"
        )
        self.assertNotIn(github_fixture, standalone_secrets)
        self.assertNotIn(openai_fixture, standalone_secrets)
        self.assertEqual(standalone_secrets.count("[REDACTED]"), 2)

    def test_19_full_task_verification_pipeline(self):
        # 1. Successful verification
        task_data = get_sample_valid_task_dict(self.test_dir)
        task_data["repository"]["base_commit"] = self.base_commit
        task_data["scope"]["allowed_paths"] = ["backend/**", "README.md"]
        task_data["scope"]["forbidden_paths"] = []
        task_data["verification"]["commands"] = [
            "python3 -c \"print('test 1 passed')\"",
            "python3 -c \"print('test 2 passed')\"",
        ]

        task = Task.from_dict(task_data)
        res = verify_task(task, repo_path=self.test_dir)
        self.assertEqual(res.status, "passed")
        self.assertIsNone(res.reason)
        self.assertEqual(len(res.command_results), 2)

        # 2. Scope violation causes overall verification failure even if commands exit 0
        with open(os.path.join(self.test_dir, "unauthorized.txt"), "w") as f:
            f.write("violating file\n")

        res_scope_fail = verify_task(task, repo_path=self.test_dir)
        self.assertEqual(res_scope_fail.status, "failed")
        self.assertEqual(res_scope_fail.reason, "scope_violation")

    def test_19b_scope_is_rechecked_after_verification_commands(self):
        task_data = get_sample_valid_task_dict(self.test_dir)
        task_data["repository"]["base_commit"] = self.base_commit
        task_data["scope"]["allowed_paths"] = ["allowed/**"]
        task_data["scope"]["forbidden_paths"] = []
        task_data["verification"]["commands"] = [
            "python3 -c \"open('unauthorized-after-check.txt', 'w').write('x')\""
        ]
        task = Task.from_dict(task_data)

        result = verify_task(task, repo_path=self.test_dir)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "scope_violation")
        self.assertIn("unauthorized-after-check.txt", result.scope_result.violating_files)

    def test_19c_invalid_base_commit_fails_scope_check(self):
        task_data = get_sample_valid_task_dict(self.test_dir)
        task_data["repository"]["base_commit"] = "does-not-exist"
        task = Task.from_dict(task_data)

        result = verify_task(task, repo_path=self.test_dir)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "invalid_base_commit")


class TestCompletionReportAndMetrics(unittest.TestCase):
    """Tests for completion report structure and metrics appending."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="report_test_")
        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        with open(os.path.join(self.test_dir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        subprocess.run(["git", "add", "main.py"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_20_completion_report_structure(self):
        task_data = get_sample_valid_task_dict(self.test_dir)
        task_data["repository"]["base_commit"] = self.base_commit
        task = Task.from_dict(task_data)

        verif_res = verify_task(task, repo_path=self.test_dir)
        report = generate_completion_report(
            task=task,
            verification=verif_res,
            executor="agy",
            session_id="session-test-123",
            summary="Task completed cleanly",
            known_issues=["None"],
            repo_path=self.test_dir,
        )

        self.assertEqual(report["task_id"], "TASK-001")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["executor"], "agy")
        self.assertEqual(report["session_id"], "session-test-123")
        self.assertEqual(report["git"]["base_commit"], self.base_commit)
        self.assertIn("head_commit", report["git"])
        self.assertIn("files", report["changes"])
        self.assertIn("insertions", report["changes"])
        self.assertIn("deletions", report["changes"])
        self.assertEqual(report["verification"]["status"], "passed")
        self.assertEqual(len(report["verification"]["commands"]), 1)
        self.assertEqual(report["known_issues"], ["None"])

    def test_21_metrics_jsonl_append(self):
        task_data = get_sample_valid_task_dict(self.test_dir)
        task_data["repository"]["base_commit"] = self.base_commit
        task = Task.from_dict(task_data)

        verif_res = verify_task(task, repo_path=self.test_dir)
        metrics_file = record_task_metrics(
            task=task,
            verification=verif_res,
            duration_ms=1500,
            attempts=1,
            fallback_used=False,
            repo_path=self.test_dir,
        )

        self.assertTrue(os.path.exists(metrics_file))
        with open(metrics_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["task_id"], "TASK-001")
        self.assertEqual(entry["complexity"], "M")
        self.assertEqual(entry["risk"], "medium")
        self.assertEqual(entry["executor"], "agy")
        self.assertEqual(entry["duration_ms"], 1500)
        self.assertTrue(entry["verification_pass"])
        self.assertTrue(entry["first_pass_success"])


class TestAgentctlCLI(unittest.TestCase):
    """Tests for agentctl command line interface."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentctl_test_")
        self.agentctl_bin = os.path.join(REPO_ROOT, "agentctl")

        # Initialize git repo in test_dir
        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        with open(os.path.join(self.test_dir, "app.py"), "w") as f:
            f.write("print('app')\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_22_agentctl_task_validate_valid_and_invalid(self):
        # 1. Valid task file
        valid_task = get_sample_valid_task_dict(self.test_dir)
        valid_path = os.path.join(self.test_dir, "valid_task.json")
        with open(valid_path, "w") as f:
            json.dump(valid_task, f)

        res_val = subprocess.run([self.agentctl_bin, "task", "validate", valid_path], capture_output=True, text=True)
        self.assertEqual(res_val.returncode, 0)
        self.assertIn("Task validation PASSED", res_val.stdout)

        # 2. Invalid task file
        invalid_task = get_sample_valid_task_dict(self.test_dir)
        invalid_task["classification"]["complexity"] = "INVALID"
        invalid_path = os.path.join(self.test_dir, "invalid_task.json")
        with open(invalid_path, "w") as f:
            json.dump(invalid_task, f)

        res_inval = subprocess.run([self.agentctl_bin, "task", "validate", invalid_path], capture_output=True, text=True)
        self.assertEqual(res_inval.returncode, 1)
        self.assertIn("FAILED", res_inval.stderr)

    def test_23_agentctl_task_verify_passing_and_failing(self):
        # 1. Passing verification
        task_data = get_sample_valid_task_dict(self.test_dir)
        task_data["repository"]["base_commit"] = self.base_commit
        task_data["scope"]["allowed_paths"] = ["**"]
        task_data["scope"]["forbidden_paths"] = []
        task_data["verification"]["commands"] = ["python3 -c \"print('agentctl ok')\""]
        task_path = os.path.join(self.test_dir, "task.json")
        with open(task_path, "w") as f:
            json.dump(task_data, f)

        res = subprocess.run([self.agentctl_bin, "task", "verify", task_path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Status:       PASSED", res.stdout)

        # 2. Failing verification command
        task_data["verification"]["commands"] = ["python3 -c \"import sys; sys.exit(9)\""]
        with open(task_path, "w") as f:
            json.dump(task_data, f)

        res_fail = subprocess.run([self.agentctl_bin, "task", "verify", task_path], capture_output=True, text=True)
        self.assertEqual(res_fail.returncode, 1)
        self.assertIn("Status:       FAILED", res_fail.stdout)

    def test_24_agentctl_task_run_rejected_in_phase_6(self):
        task_path = os.path.join(self.test_dir, "task.json")
        with open(task_path, "w") as f:
            json.dump(get_sample_valid_task_dict(self.test_dir), f)

        res = subprocess.run([self.agentctl_bin, "task", "run", task_path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("not implemented in Phase 6", res.stderr)

    def test_25_agentctl_executors_and_health(self):
        res_exc = subprocess.run([self.agentctl_bin, "executors", "--json"], capture_output=True, text=True)
        self.assertEqual(res_exc.returncode, 0)
        data = json.loads(res_exc.stdout)
        self.assertIn("executors", data)

        res_hlt = subprocess.run([self.agentctl_bin, "health"], capture_output=True, text=True)
        self.assertEqual(res_hlt.returncode, 0)
        hlt_data = json.loads(res_hlt.stdout)
        self.assertEqual(hlt_data.get("status"), "online")


if __name__ == "__main__":
    unittest.main()
