#!/usr/bin/env python3
"""
Unit and Integration Test Suite for Rule-Based Router, Multi-Executor Escalation,
Escalation Context Redaction, and agentctl CLI (Phase 7).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from orchestration.task import Task, validate_task_dict
from orchestration.router import route_task, RouteDecision
from orchestration.escalation import (
    AttemptRecord,
    EscalationContext,
    EscalationDecision,
    EscalationState,
    build_escalation_context,
    evaluate_escalation,
    get_git_diff_text,
    init_escalation_state,
)
from orchestration.verifier import (
    CommandVerificationResult,
    ScopeCheckResult,
    TaskVerificationResult,
    redact_sensitive_text,
)


def create_task(
    complexity: str = "M",
    risk: str = "medium",
    task_type: str = "feature",
    executor: str = "agy",
    fallback_executor: str | None = "grok",
    max_same_attempts: int = 2,
    max_switches: int = 1,
    repo_path: str = "/workspace/project",
    base_commit: str = "abc1234567890",
) -> Task:
    """Helper to construct Task instances for routing & escalation tests."""
    data = {
        "version": "1",
        "task_id": "TASK-ROUTING-001",
        "parent_task_id": None,
        "goal": "Implement routing and escalation pipeline",
        "repository": {
            "path": repo_path,
            "base_commit": base_commit,
        },
        "classification": {
            "complexity": complexity,
            "risk": risk,
            "type": task_type,
        },
        "execution": {
            "executor": executor,
            "fallback_executor": fallback_executor,
            "max_same_executor_attempts": max_same_attempts,
            "max_executor_switches": max_switches,
            "isolated_worktree": False,
        },
        "scope": {
            "allowed_paths": ["orchestration/**"],
            "forbidden_paths": [],
        },
        "acceptance": [
            "Routing adheres to Section 22 rules",
            "Escalation bounds switches and prevents loops",
        ],
        "verification": {
            "commands": ["pytest"],
        },
    }
    task, errors = validate_task_dict(data)
    if errors or task is None:
        raise ValueError(f"Invalid test task setup: {errors}")
    return task


class TestRuleBasedRouter(unittest.TestCase):
    """Unit tests for Goal Prompt Section 22 & 49 Rule-Based Router."""

    def test_01_small_task_routes_to_agy(self):
        # S Low -> AGY
        task_s_low = create_task(complexity="S", risk="low", task_type="feature")
        decision = route_task(task_s_low)
        self.assertEqual(decision.status, "routed")
        self.assertEqual(decision.executor, "agy")
        self.assertFalse(decision.requires_human_review)

        # S High -> AGY (with review recommendation)
        task_s_high = create_task(complexity="S", risk="high", task_type="feature")
        decision_high = route_task(task_s_high)
        self.assertEqual(decision_high.status, "routed")
        self.assertEqual(decision_high.executor, "agy")
        self.assertTrue(decision_high.requires_human_review)

    def test_02_medium_feature_and_bugfix_routes_to_agy(self):
        for t_type in ("feature", "bugfix", "refactor", "test", "config"):
            task = create_task(complexity="M", risk="medium", task_type=t_type)
            decision = route_task(task)
            self.assertEqual(decision.status, "routed", f"Failed for {t_type}")
            self.assertEqual(decision.executor, "agy", f"Failed for {t_type}")

    def test_03_medium_debug_and_investigation_routes_to_grok(self):
        for t_type in ("debug", "investigation"):
            task = create_task(complexity="M", risk="medium", task_type=t_type)
            decision = route_task(task)
            self.assertEqual(decision.status, "routed", f"Failed for {t_type}")
            self.assertEqual(decision.executor, "grok", f"Failed for {t_type}")
            self.assertEqual(decision.rule, "medium_debug_investigation_rule")

    def test_04_large_and_xlarge_tasks_require_override(self):
        for comp in ("L", "XL"):
            task = create_task(complexity=comp, risk="medium", task_type="feature")
            decision = route_task(task)
            self.assertEqual(decision.status, "override_required", f"Failed for {comp}")
            self.assertIsNone(decision.executor)
            self.assertTrue(decision.requires_human_review)
            self.assertIn("manual decomposition", decision.reason)

    def test_05_explicit_override_takes_precedence(self):
        # Override S task to Grok
        task_s = create_task(complexity="S", risk="low", task_type="feature")
        decision_s = route_task(task_s, executor_override="grok")
        self.assertEqual(decision_s.status, "routed")
        self.assertEqual(decision_s.executor, "grok")
        self.assertEqual(decision_s.rule, "explicit_codex_override")

        # Override L task to AGY
        task_l = create_task(complexity="L", risk="high", task_type="feature")
        decision_l = route_task(task_l, executor_override="agy")
        self.assertEqual(decision_l.status, "routed")
        self.assertEqual(decision_l.executor, "agy")
        self.assertEqual(decision_l.rule, "explicit_codex_override")

    def test_06_invalid_executor_override_rejected(self):
        task = create_task(complexity="M", risk="low", task_type="feature")
        decision = route_task(task, executor_override="unknown_bot")
        self.assertEqual(decision.status, "invalid_override")
        self.assertIsNone(decision.executor)
        self.assertIn("Invalid executor override", decision.reason)


class TestEscalationStateMachine(unittest.TestCase):
    """Unit tests for Multi-Attempt Escalation Engine and Loop Prevention."""

    def _make_dummy_verif_result(self, passed: bool, summary: str = "Test run") -> TaskVerificationResult:
        cmd_res = CommandVerificationResult(
            command="pytest",
            exit_code=0 if passed else 1,
            status="passed" if passed else "failed",
            summary="pytest passed" if passed else "pytest failed: 1 test error",
            relevant_tail="AssertionError: expected 1 got 0" if not passed else "OK",
            log_path="/tmp/test.log",
            duration_ms=100,
        )
        scope_res = ScopeCheckResult(passed=True, changed_files=["app.py"])
        return TaskVerificationResult(
            task_id="TASK-ROUTING-001",
            status="passed" if passed else "failed",
            scope_result=scope_res,
            command_results=[cmd_res],
            reason=None if passed else "command_failure",
            summary=summary,
        )

    def test_07_immediate_success_completes_task(self):
        task = create_task()
        state = init_escalation_state(task)
        verif = self._make_dummy_verif_result(passed=True)

        decision = evaluate_escalation(task, state, verif)
        self.assertEqual(decision.action, "completed")
        self.assertIsNone(decision.next_executor)
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.total_attempts, 1)

    def test_08_standard_feature_escalation_lifecycle(self):
        """
        Tests the complete escalation chain:
        1. AGY Attempt 1 fails -> AGY Attempt 2 (Self-Repair)
        2. AGY Attempt 2 fails -> Grok Attempt 1 (Escalation + Context)
        3. Grok Attempt 1 fails -> Grok Attempt 2 (Self-Repair)
        4. Grok Attempt 2 fails -> REPLAN_REQUIRED (Codex Intervention)
        """
        task = create_task(executor="agy", fallback_executor="grok", max_same_attempts=2, max_switches=1)
        state = init_escalation_state(task)
        verif_fail = self._make_dummy_verif_result(passed=False)

        # 1. Turn 1 (AGY) fails
        dec1 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec1.action, "retry_same_executor")
        self.assertEqual(dec1.next_executor, "agy")
        self.assertEqual(state.current_attempt, 2)
        self.assertEqual(state.switches_used, 0)

        # 2. Turn 2 (AGY Self-Repair) fails -> Escalate to Grok
        dec2 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec2.action, "switch_executor")
        self.assertEqual(dec2.next_executor, "grok")
        self.assertEqual(state.current_executor, "grok")
        self.assertEqual(state.current_attempt, 1)
        self.assertEqual(state.switches_used, 1)
        self.assertIsNotNone(dec2.context)
        self.assertEqual(dec2.context.original_goal, task.goal)

        # Verify previous executor summary correctly attributes failed executor 'agy' (not incoming 'grok')
        self.assertIn("Executor 'agy' executed 2 attempt(s)", dec2.context.previous_executor_summary)
        self.assertEqual(len(dec2.context.previous_attempts), 2)
        self.assertEqual(dec2.context.previous_attempts[0]["executor"], "agy")
        self.assertEqual(dec2.context.previous_attempts[1]["executor"], "agy")

        # Verify rendered handover prompt contains previous attempts history and correct previous executor
        prompt_grok = dec2.context.to_prompt(target_executor="grok")
        self.assertIn("## Task Escalation Handover (GROK)", prompt_grok)
        self.assertIn("### Previous Attempts History", prompt_grok)
        self.assertIn("- Attempt #1 (agy):", prompt_grok)
        self.assertIn("- Attempt #2 (agy):", prompt_grok)
        self.assertIn("Executor 'agy' executed 2 attempt(s)", prompt_grok)

        # 3. Turn 3 (Grok Attempt 1) fails -> Grok Self-Repair
        dec3 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec3.action, "retry_same_executor")
        self.assertEqual(dec3.next_executor, "grok")
        self.assertEqual(state.current_attempt, 2)
        self.assertEqual(state.switches_used, 1)

        # 4. Turn 4 (Grok Self-Repair) fails -> REPLAN_REQUIRED (Switches exhausted)
        dec4 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec4.action, "replan_required")
        self.assertIsNone(dec4.next_executor)
        self.assertEqual(state.status, "replan_required")
        self.assertEqual(state.replans_used, 1)
        self.assertIsNotNone(dec4.context)
        self.assertIn("Executor 'grok' executed 2 attempt(s)", dec4.context.previous_executor_summary)
        self.assertEqual(len(dec4.context.previous_attempts), 4)

        # 5. Subsequent failure after replan exhausted -> FAILED
        dec5 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec5.action, "failed")
        self.assertIsNone(dec5.next_executor)
        self.assertEqual(state.status, "failed")

    def test_09_infinite_switch_loop_prevented(self):
        """Verify strict enforcement of max_executor_switches and max_replans prevents AGY<->Grok loops."""
        task = create_task(executor="agy", fallback_executor="grok", max_same_attempts=1, max_switches=1)
        state = init_escalation_state(task)
        verif_fail = self._make_dummy_verif_result(passed=False)

        # Turn 1 (AGY) fails -> switches to Grok
        dec1 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec1.action, "switch_executor")
        self.assertEqual(dec1.next_executor, "grok")

        # Turn 2 (Grok) fails -> cannot switch back to AGY because max_switches=1 -> triggers REPLAN_REQUIRED
        dec2 = evaluate_escalation(task, state, verif_fail)
        self.assertEqual(dec2.action, "replan_required")
        self.assertNotEqual(dec2.action, "switch_executor")

    def test_10_escalation_state_serialization_roundtrip(self):
        task = create_task()
        state = init_escalation_state(task)
        verif_fail = self._make_dummy_verif_result(passed=False, summary="SyntaxError")
        evaluate_escalation(task, state, verif_fail)

        serialized = state.to_dict()
        self.assertEqual(serialized["task_id"], task.task_id)
        self.assertEqual(len(serialized["history"]), 1)

        rebuilt = EscalationState.from_dict(serialized)
        self.assertEqual(rebuilt.task_id, state.task_id)
        self.assertEqual(rebuilt.current_attempt, state.current_attempt)
        self.assertEqual(len(rebuilt.history), 1)
        self.assertEqual(rebuilt.history[0].summary, "SyntaxError")


class TestEscalationContextAndRedaction(unittest.TestCase):
    """Tests for EscalationContext structure, prompt rendering, and sensitive token redaction."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="esc_ctx_test_")
        subprocess.run(["git", "init"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@agent.local"], cwd=self.test_dir, capture_output=True, check=True)

        with open(os.path.join(self.test_dir, "auth.py"), "w") as f:
            f.write("# Auth initial\n")
        subprocess.run(["git", "add", "auth.py"], cwd=self.test_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init commit"], cwd=self.test_dir, capture_output=True, check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True, check=True)
        self.base_commit = res.stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_11_escalation_context_redaction(self):
        # Modify auth.py with sensitive bearer tokens
        secret_token = "secret_bearer_token_" + ("x" * 32)
        with open(os.path.join(self.test_dir, "auth.py"), "w") as f:
            f.write(f'TOKEN = "Bearer {secret_token}"\n')

        task = create_task(repo_path=self.test_dir, base_commit=self.base_commit)
        state = init_escalation_state(task)

        cmd_res = CommandVerificationResult(
            command="pytest",
            exit_code=1,
            status="failed",
            summary=f"Failed with Bearer {secret_token}",
            relevant_tail=f"Error: Bearer {secret_token} invalid",
            log_path="",
            duration_ms=50,
        )
        verif = TaskVerificationResult(
            task_id=task.task_id,
            status="failed",
            scope_result=ScopeCheckResult(passed=True),
            command_results=[cmd_res],
            reason="command_failure",
            summary="Verification failed",
        )

        ctx = build_escalation_context(task, state, last_verification=verif, repo_path=self.test_dir)

        # Verify all tokens are redacted in diff and failure output
        self.assertNotIn(secret_token, ctx.current_git_diff)
        self.assertIn("[REDACTED]", ctx.current_git_diff)

        self.assertNotIn(secret_token, ctx.failure_output)
        self.assertIn("[REDACTED]", ctx.failure_output)

        # Check prompt rendering
        prompt = ctx.to_prompt(target_executor="grok")
        self.assertIn("## Task Escalation Handover (GROK)", prompt)
        self.assertIn("Take over the existing implementation", prompt)
        self.assertNotIn(secret_token, prompt)


class TestAgentctlRoutingCLI(unittest.TestCase):
    """Tests for agentctl task route and agentctl task plan CLI commands."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentctl_route_test_")
        self.agentctl_bin = os.path.join(REPO_ROOT, "agentctl")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_task_file(self, data: dict, filename: str = "task.json") -> str:
        path = os.path.join(self.test_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_12_agentctl_task_route_feature_and_debug(self):
        # 1. M Feature -> agy (exits 0)
        t_feat = create_task(complexity="M", task_type="feature", repo_path=self.test_dir)
        p_feat = self._write_task_file(t_feat.to_dict(), "feat.json")
        res_feat = subprocess.run([self.agentctl_bin, "task", "route", p_feat], capture_output=True, text=True)
        self.assertEqual(res_feat.returncode, 0)
        self.assertIn("Executor:     agy", res_feat.stdout)

        # 2. M Debug -> grok (exits 0)
        t_dbg = create_task(complexity="M", task_type="debug", repo_path=self.test_dir)
        p_dbg = self._write_task_file(t_dbg.to_dict(), "dbg.json")
        res_dbg = subprocess.run([self.agentctl_bin, "task", "route", p_dbg], capture_output=True, text=True)
        self.assertEqual(res_dbg.returncode, 0)
        self.assertIn("Executor:     grok", res_dbg.stdout)

    def test_13_agentctl_task_route_large_task_override_requirement(self):
        # L task without override -> exits 1
        t_large = create_task(complexity="L", task_type="feature", repo_path=self.test_dir)
        p_large = self._write_task_file(t_large.to_dict(), "large.json")
        res_l = subprocess.run([self.agentctl_bin, "task", "route", p_large], capture_output=True, text=True)
        self.assertEqual(res_l.returncode, 1)
        self.assertIn("OVERRIDE_REQUIRED", res_l.stdout)

        # L task with --override agy -> exits 0
        res_l_ov = subprocess.run([self.agentctl_bin, "task", "route", p_large, "--override", "agy"], capture_output=True, text=True)
        self.assertEqual(res_l_ov.returncode, 0)
        self.assertIn("Executor:     agy", res_l_ov.stdout)
        self.assertIn("explicit_codex_override", res_l_ov.stdout)

    def test_14_agentctl_task_plan(self):
        t = create_task(complexity="M", task_type="feature", repo_path=self.test_dir)
        p = self._write_task_file(t.to_dict(), "plan_task.json")
        res = subprocess.run([self.agentctl_bin, "task", "plan", p], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("=== Task Execution Plan", res.stdout)
        self.assertIn("Initial Executor:     agy", res.stdout)
        self.assertIn("Fallback Executor:    grok", res.stdout)
        self.assertIn("Escalation Chain:", res.stdout)


if __name__ == "__main__":
    unittest.main()
