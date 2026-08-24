#!/usr/bin/env python3
"""
Unit and Integration Test Suite for Isolated Worktree Execution and Task DAG (Phase 8).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from orchestration.task import Task, validate_task_dict
from orchestration.worktree import (
    WorktreeInfo,
    WorktreeManager,
    get_default_worktree_root,
    is_path_inside_root,
    sanitize_task_id,
)
from orchestration.dag import (
    STATE_BLOCKED,
    STATE_CANCELLED,
    STATE_CREATED,
    STATE_DONE,
    STATE_FAILED,
    STATE_READY,
    STATE_RUNNING,
    TaskDAG,
    TaskNode,
    run_dag_parallel,
)


def create_sample_task(
    task_id: str = "TASK-001",
    executor: str = "agy",
    repo_path: str = "/workspace/project",
    base_commit: str = "abc1234567890",
    depends_on: list[str] | None = None,
    allowed_paths: list[str] | None = None,
) -> Task:
    """Helper to construct a valid Task instance."""
    data = {
        "version": "1",
        "task_id": task_id,
        "parent_task_id": None,
        "goal": f"Execute worktree task {task_id}",
        "repository": {
            "path": repo_path,
            "base_commit": base_commit,
        },
        "classification": {
            "complexity": "M",
            "risk": "medium",
            "type": "feature",
        },
        "execution": {
            "executor": executor,
            "fallback_executor": "grok" if executor == "agy" else "agy",
            "max_same_executor_attempts": 2,
            "max_executor_switches": 1,
            "isolated_worktree": True,
        },
        "scope": {
            "allowed_paths": allowed_paths or ["**"],
            "forbidden_paths": ["database/migrations/**"],
        },
        "acceptance": ["Worktree execution passes", "Scope boundaries respected"],
        "verification": {
            "commands": ["python3 -c \"print('test ok')\""],
        },
        "depends_on": depends_on or [],
    }
    task, errors = validate_task_dict(data)
    if errors or task is None:
        raise ValueError(f"Task creation failed: {errors}")
    return task


class TestTaskDependsOnSchema(unittest.TestCase):
    """Tests for depends_on field in Task JSON schema."""

    def test_01_depends_on_default_and_explicit(self):
        # 1. Explicit depends_on
        t1 = create_sample_task(task_id="TASK-01", depends_on=["TASK-00"])
        self.assertEqual(t1.depends_on, ["TASK-00"])
        self.assertIn("depends_on", t1.to_dict())
        self.assertEqual(t1.to_dict()["depends_on"], ["TASK-00"])

        # 2. Omitted depends_on defaults to empty list
        raw_data = t1.to_dict()
        del raw_data["depends_on"]
        t2, errs = validate_task_dict(raw_data)
        self.assertEqual(errs, [])
        self.assertIsNotNone(t2)
        self.assertEqual(t2.depends_on, [])

    def test_02_invalid_depends_on_rejected(self):
        base_dict = create_sample_task().to_dict()

        # Non-list
        base_dict["depends_on"] = "TASK-00"
        _, errs = validate_task_dict(base_dict)
        self.assertTrue(any("depends_on" in e for e in errs))

        # Non-string item
        base_dict["depends_on"] = [123]
        _, errs = validate_task_dict(base_dict)
        self.assertTrue(any("depends_on" in e for e in errs))

        # Empty string item
        base_dict["depends_on"] = ["   "]
        _, errs = validate_task_dict(base_dict)
        self.assertTrue(any("depends_on" in e for e in errs))


class TestWorktreeManager(unittest.TestCase):
    """Tests for Git worktree creation, isolation, safety, and removal."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="worktree_main_repo_")
        self.wt_root = tempfile.mkdtemp(prefix="worktree_root_")

        # Initialize real git repository
        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        with open(os.path.join(self.test_dir, "app.py"), "w") as f:
            f.write("# Main app initial\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        shutil.rmtree(self.wt_root, ignore_errors=True)

    def test_03_create_worktree_at_base_commit_and_isolation(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        task = create_sample_task(task_id="TASK-101", executor="agy", repo_path=self.test_dir, base_commit=self.base_commit)

        info = mgr.create_worktree(task)
        self.assertEqual(info.task_id, "TASK-101")
        self.assertEqual(info.executor, "agy")
        self.assertEqual(info.branch, "agent/TASK-101-agy")
        self.assertTrue(os.path.exists(info.path))
        self.assertTrue(os.path.exists(os.path.join(info.path, "app.py")))

        # Modify file inside worktree
        with open(os.path.join(info.path, "feature.py"), "w") as f:
            f.write("# Isolated feature\n")

        # Main repo working directory must remain completely clean
        status_res = subprocess.run(["git", "status", "--porcelain"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.assertEqual(status_res.stdout.strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "feature.py")))

    def test_04_duplicate_worktree_rejection(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        task = create_sample_task(task_id="TASK-102", executor="grok", repo_path=self.test_dir, base_commit=self.base_commit)

        mgr.create_worktree(task)
        # Attempting to create duplicate worktree raises FileExistsError
        with self.assertRaises(FileExistsError):
            mgr.create_worktree(task)

    def test_04b_branch_conflict_rejected_without_destructive_delete(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        task = create_sample_task(task_id="TASK-BRANCH-CONFLICT", executor="agy", repo_path=self.test_dir, base_commit=self.base_commit)

        # Pre-create the branch directly in git
        branch_name = "agent/TASK-BRANCH-CONFLICT-agy"
        subprocess.run(["git", "branch", branch_name, self.base_commit], cwd=self.test_dir, capture_output=True, check=True)

        # Attempt to create worktree -> must raise FileExistsError without deleting branch
        with self.assertRaises(FileExistsError):
            mgr.create_worktree(task)

        # Verify branch still exists and was not deleted
        res = subprocess.run(["git", "rev-parse", "--verify", f"refs/heads/{branch_name}"], cwd=self.test_dir, capture_output=True)
        self.assertEqual(res.returncode, 0)

    def test_04c_repo_path_mismatch_rejected(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        other_repo = tempfile.mkdtemp(prefix="other_repo_")
        try:
            task = create_sample_task(task_id="TASK-MISMATCH", executor="agy", repo_path=other_repo, base_commit=self.base_commit)
            with self.assertRaises(ValueError) as ctx:
                mgr.create_worktree(task)
            self.assertIn("does not match WorktreeManager repository", str(ctx.exception))
        finally:
            shutil.rmtree(other_repo, ignore_errors=True)

    def test_04d_root_inside_repo_rejected(self):
        # Worktree root cannot be the repo itself or inside the repo
        with self.assertRaises(ValueError):
            WorktreeManager(repo_path=self.test_dir, root_dir=self.test_dir)

        inside_repo_root = os.path.join(self.test_dir, ".agent-worktrees")
        with self.assertRaises(ValueError):
            WorktreeManager(repo_path=self.test_dir, root_dir=inside_repo_root)

    def test_05_list_worktrees(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        task_a = create_sample_task(task_id="TASK-A", executor="agy", repo_path=self.test_dir, base_commit=self.base_commit)
        task_b = create_sample_task(task_id="TASK-B", executor="grok", repo_path=self.test_dir, base_commit=self.base_commit)

        mgr.create_worktree(task_a)
        mgr.create_worktree(task_b)

        worktrees = mgr.list_worktrees()
        self.assertEqual(len(worktrees), 2)
        task_ids = {wt.task_id for wt in worktrees}
        self.assertIn("TASK-A", task_ids)
        self.assertIn("TASK-B", task_ids)

    def test_06_security_path_traversal_prevention(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)

        # Invalid task IDs with path traversal tokens
        for bad_id in ("../escaped", "/etc/passwd", "sub/dir", "   "):
            with self.assertRaises(ValueError):
                sanitize_task_id(bad_id)

    def test_07_cleanup_worktree_safety(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        task = create_sample_task(task_id="TASK-CLEAN", executor="agy", repo_path=self.test_dir, base_commit=self.base_commit)
        info = mgr.create_worktree(task)

        self.assertTrue(os.path.exists(info.path))
        cleaned = mgr.cleanup_worktree(info.task_id, force=True)
        self.assertTrue(cleaned)
        self.assertFalse(os.path.exists(info.path))

        # Refuse to clean arbitrary path outside root_dir
        with self.assertRaises(ValueError):
            mgr.cleanup_worktree("/tmp")

    def test_07b_fake_directory_in_root_not_deleted(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        fake_dir = os.path.join(self.wt_root, "fake_dir")
        os.makedirs(fake_dir, exist_ok=True)
        fake_file = os.path.join(fake_dir, "data.txt")
        with open(fake_file, "w") as f:
            f.write("Important data\n")

        # Cleanup should refuse to delete unregistered directory
        cleaned = mgr.cleanup_worktree("fake_dir")
        self.assertFalse(cleaned)
        self.assertTrue(os.path.exists(fake_dir))
        self.assertTrue(os.path.exists(fake_file))

    def test_07c_non_agent_branch_worktree_not_deleted(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        non_agent_path = os.path.join(self.wt_root, "other_wt")

        # Add worktree on feature/custom-branch
        subprocess.run(
            ["git", "worktree", "add", "-b", "feature/custom-branch", non_agent_path, self.base_commit],
            cwd=self.test_dir,
            capture_output=True,
            check=True,
        )

        # list_worktrees must ignore non-agent branch worktrees
        active = mgr.list_worktrees()
        self.assertNotIn("other_wt", [w.task_id for w in active])

        # cleanup must refuse to delete non-agent branch worktrees
        cleaned = mgr.cleanup_worktree("other_wt")
        self.assertFalse(cleaned)
        self.assertTrue(os.path.exists(non_agent_path))

        # Cleanup manually
        subprocess.run(["git", "worktree", "remove", "--force", non_agent_path], cwd=self.test_dir, capture_output=True)

    def test_08_worktree_scope_checking(self):
        mgr = WorktreeManager(repo_path=self.test_dir, root_dir=self.wt_root)
        task = create_sample_task(
            task_id="TASK-SCOPE",
            executor="agy",
            repo_path=self.test_dir,
            base_commit=self.base_commit,
            allowed_paths=["backend/**"],
        )
        info = mgr.create_worktree(task)

        # 1. Allowed modification inside worktree
        os.makedirs(os.path.join(info.path, "backend"), exist_ok=True)
        with open(os.path.join(info.path, "backend", "auth.py"), "w") as f:
            f.write("# auth code\n")

        scope_res = mgr.check_worktree_scope(task, info)
        self.assertTrue(scope_res.passed)

        # 2. Forbidden modification inside worktree
        os.makedirs(os.path.join(info.path, "database", "migrations"), exist_ok=True)
        with open(os.path.join(info.path, "database", "migrations", "001.sql"), "w") as f:
            f.write("DROP TABLE users;\n")

        scope_res_fail = mgr.check_worktree_scope(task, info)
        self.assertFalse(scope_res_fail.passed)
        self.assertEqual(scope_res_fail.reason, "scope_violation")


class TestTaskDAG(unittest.TestCase):
    """Tests for DAG topology, cycle detection, and readiness state transitions."""

    def test_09_dag_independent_tasks_ready(self):
        t1 = create_sample_task(task_id="T1", depends_on=[])
        t2 = create_sample_task(task_id="T2", depends_on=[])
        dag = TaskDAG([t1, t2])

        valid, errors = dag.validate_dag()
        self.assertTrue(valid)
        self.assertEqual(errors, [])

        ready = dag.get_ready_tasks()
        self.assertEqual(len(ready), 2)
        ready_ids = {n.task.task_id for n in ready}
        self.assertEqual(ready_ids, {"T1", "T2"})

    def test_10_dag_dependency_chain_and_blocked(self):
        # T1 -> T2 -> T3
        t1 = create_sample_task(task_id="T1", depends_on=[])
        t2 = create_sample_task(task_id="T2", depends_on=["T1"])
        t3 = create_sample_task(task_id="T3", depends_on=["T2"])
        dag = TaskDAG([t1, t2, t3])

        valid, errors = dag.validate_dag()
        self.assertTrue(valid)

        # Initial: only T1 is READY
        ready = dag.get_ready_tasks()
        self.assertEqual([n.task.task_id for n in ready], ["T1"])
        self.assertEqual(dag.nodes["T2"].status, STATE_CREATED)
        self.assertEqual(dag.nodes["T3"].status, STATE_CREATED)

        # T1 finishes successfully -> T2 becomes READY
        dag.update_status("T1", STATE_DONE)
        ready = dag.get_ready_tasks()
        self.assertEqual([n.task.task_id for n in ready], ["T2"])

        # T2 fails -> T3 becomes BLOCKED
        dag.update_status("T2", STATE_FAILED)
        dag.evaluate_readiness()
        self.assertEqual(dag.nodes["T3"].status, STATE_BLOCKED)
        self.assertTrue(dag.is_completed())

    def test_11_dag_cycle_and_missing_dep_detection(self):
        # 1. Missing dependency
        t1 = create_sample_task(task_id="T1", depends_on=["UNKNOWN_TASK"])
        dag1 = TaskDAG([t1])
        valid1, errs1 = dag1.validate_dag()
        self.assertFalse(valid1)
        self.assertTrue(any("unknown task" in e for e in errs1))

        # 2. Circular dependency (T1 -> T2 -> T1)
        t_a = create_sample_task(task_id="TA", depends_on=["TB"])
        t_b = create_sample_task(task_id="TB", depends_on=["TA"])
        dag2 = TaskDAG([t_a, t_b])
        valid2, errs2 = dag2.validate_dag()
        self.assertFalse(valid2)
        self.assertTrue(any("Circular dependency" in e for e in errs2))

    def test_11b_missing_dep_without_validate_dag_blocks_task(self):
        """Verify that evaluate_readiness / get_ready_tasks marks task as BLOCKED if depends_on is missing even without calling validate_dag()."""
        t_missing = create_sample_task(task_id="T_MISSING", depends_on=["NON_EXISTENT_TASK"])
        dag = TaskDAG([t_missing])

        # Without calling validate_dag, get_ready_tasks must NOT treat t_missing as READY
        ready = dag.get_ready_tasks()
        self.assertEqual(ready, [])
        self.assertEqual(dag.nodes["T_MISSING"].status, STATE_BLOCKED)


class TestDAGParallelExecution(unittest.TestCase):
    """Integration tests for multi-task parallel dispatch in isolated worktrees."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="dag_main_repo_")
        self.wt_root = tempfile.mkdtemp(prefix="dag_wt_root_")

        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        with open(os.path.join(self.test_dir, "main.py"), "w") as f:
            f.write("# Main service\n")
        subprocess.run(["git", "add", "main.py"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init commit"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        shutil.rmtree(self.wt_root, ignore_errors=True)

    def test_12_concurrent_worktree_execution_no_auto_merge(self):
        """
        Runs AGY Task-A and Grok Task-B concurrently in separate worktrees.
        Verifies:
        - Concurrent execution across 2 worktrees.
        - Zero automatic merging into main branch.
        - Main working directory remains clean.
        """
        task_a = create_sample_task(task_id="TASK-A", executor="agy", repo_path=self.test_dir, base_commit=self.base_commit)
        task_b = create_sample_task(task_id="TASK-B", executor="grok", repo_path=self.test_dir, base_commit=self.base_commit)

        dag = TaskDAG([task_a, task_b])

        worker_paths: dict[str, str] = {}
        execution_order: list[str] = []
        lock = time.time()

        def fake_worker(task: Task, worktree: WorktreeInfo) -> dict[str, Any]:
            worker_paths[task.task_id] = worktree.path
            # Simulate work in isolated worktree
            time.sleep(0.1)
            filename = f"{task.task_id.lower()}_output.py"
            with open(os.path.join(worktree.path, filename), "w") as f:
                f.write(f"# Output from {task.execution.executor}\n")
            execution_order.append(task.task_id)
            return {"status": "success", "file": filename}

        result = run_dag_parallel(
            dag=dag,
            worker_fn=fake_worker,
            repo_path=self.test_dir,
            worktree_root=self.wt_root,
            max_concurrency=2,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["tasks"]["TASK-A"]["status"], STATE_DONE)
        self.assertEqual(result["tasks"]["TASK-B"]["status"], STATE_DONE)

        # Confirm both worktrees were distinct and isolated
        self.assertIn("TASK-A-agy", worker_paths["TASK-A"])
        self.assertIn("TASK-B-grok", worker_paths["TASK-B"])
        self.assertNotEqual(worker_paths["TASK-A"], worker_paths["TASK-B"])

        # Main repo must NOT have the worktree files (no auto-merge)
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "task-a_output.py")))
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "task-b_output.py")))

        # Main repo git status must remain clean
        main_status = subprocess.run(["git", "status", "--porcelain"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.assertEqual(main_status.stdout.strip(), "")


class TestAgentctlWorktreeAndDAG(unittest.TestCase):
    """Tests for agentctl worktree and agentctl task ready/graph CLI commands."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentctl_wt_test_")
        self.wt_root = tempfile.mkdtemp(prefix="agentctl_wt_root_")
        self.agentctl_bin = os.path.join(REPO_ROOT, "agentctl")

        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        with open(os.path.join(self.test_dir, "app.py"), "w") as f:
            f.write("# Base app\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        shutil.rmtree(self.wt_root, ignore_errors=True)

    def _write_task_file(self, task: Task, name: str) -> str:
        path = os.path.join(self.test_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f)
        return path

    def test_13_agentctl_worktree_create_list_cleanup(self):
        task = create_sample_task(task_id="CLI-TASK", executor="agy", repo_path=self.test_dir, base_commit=self.base_commit)
        task_file = self._write_task_file(task, "task.json")

        # 1. Create worktree
        res_create = subprocess.run(
            [self.agentctl_bin, "worktree", "create", task_file, "--root", self.wt_root],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_create.returncode, 0)
        self.assertIn("Worktree CREATED", res_create.stdout)

        # 2. List worktrees
        res_list = subprocess.run(
            [self.agentctl_bin, "worktree", "list", "--repo", self.test_dir, "--root", self.wt_root],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_list.returncode, 0)
        self.assertIn("CLI-TASK", res_list.stdout)

        # 3. Cleanup worktree
        res_clean = subprocess.run(
            [self.agentctl_bin, "worktree", "cleanup", "CLI-TASK", "--repo", self.test_dir, "--root", self.wt_root],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_clean.returncode, 0)
        self.assertIn("safely removed", res_clean.stdout)

    def test_14_agentctl_task_ready_and_graph(self):
        t1 = create_sample_task(task_id="T1", repo_path=self.test_dir, base_commit=self.base_commit, depends_on=[])
        t2 = create_sample_task(task_id="T2", repo_path=self.test_dir, base_commit=self.base_commit, depends_on=["T1"])

        f1 = self._write_task_file(t1, "t1.json")
        f2 = self._write_task_file(t2, "t2.json")

        # Ready command: T1 ready, T2 not ready
        res_ready = subprocess.run([self.agentctl_bin, "task", "ready", f1, f2], capture_output=True, text=True)
        self.assertEqual(res_ready.returncode, 0)
        self.assertIn("T1", res_ready.stdout)
        self.assertNotIn("T2", res_ready.stdout)

        # Graph command: outputs dependency relation
        res_graph = subprocess.run([self.agentctl_bin, "task", "graph", f1, f2], capture_output=True, text=True)
        self.assertEqual(res_graph.returncode, 0)
        self.assertIn("T1", res_graph.stdout)
        self.assertIn("T2", res_graph.stdout)
        self.assertIn("depends on [T1]", res_graph.stdout)


if __name__ == "__main__":
    unittest.main()
